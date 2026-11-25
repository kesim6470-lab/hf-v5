"""
================================================================================
                    TELEGRAM ADVANCED REFERRAL BOT SYSTEM
   [ v4: Softer fraud rules (fewer false-positive bans), stale verify-widget
     cleanup on every /start, and a resumable "stop bot" admin control. ]
     [ v4.1: Performance indexes + faster referral metrics for large user bases ]
         [ One person = One account. Shared IP alone is NOT a ban reason.    ]
================================================================================
"""

import os
import sys
import json
import hmac
import base64
import random
import secrets
import asyncio
import re
import hashlib
import logging
import urllib.parse
from datetime import datetime, timedelta
from collections import defaultdict
import time

import httpx
import uvicorn
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, FSInputFile, InlineQuery, InlineQueryResultCachedPhoto,
    InlineQueryResultArticle, InputTextMessageContent,
    BufferedInputFile, InputMediaPhoto
)

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field
from typing import Optional, List

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ReferralBotSystem")
logger.info("════════════════════════════════════════════════")
logger.info("  BOT CODE VERSION: v4.2-debug-2026-06-30")
logger.info("  If you don't see this line after deploy/restart,")
logger.info("  the OLD code is still running on Railway.")
logger.info("════════════════════════════════════════════════")

BOT_TOKEN            = os.getenv("BOT_TOKEN", "")
ADMIN_IDS            = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip()]
PAYMENT_LOG_CHANNEL  = os.getenv("PAYMENT_LOG_CHANNEL", "").strip()
# Private channel (only you, the admin, should be a member of it) that
# receives a copy of the SQLite database file on a schedule — a safety
# net in case the Railway Volume is ever lost/corrupted. Leave empty to
# disable backups entirely.
DB_BACKUP_CHANNEL_ID   = os.getenv("DB_BACKUP_CHANNEL_ID", "").strip()
DB_BACKUP_INTERVAL_HOURS = int(os.getenv("DB_BACKUP_INTERVAL_HOURS", "24"))
# WEBAPP_URL = this backend's OWN public URL (Railway). Used for API calls
# and to build the /verify page link. Kept for backward compatibility.
WEBAPP_URL           = os.getenv("WEBAPP_URL", "http://localhost:8000").rstrip("/")
# FRONTEND_URL = the Mini App static site's public URL (Vercel). This is
# what Telegram "Open Mini App" buttons must point to. Falls back to
# WEBAPP_URL if not set, so old single-domain deployments still work.
FRONTEND_URL         = os.getenv("FRONTEND_URL", "").rstrip("/") or WEBAPP_URL
PROXYCHECK_API_KEY   = os.getenv("PROXYCHECK_API_KEY", "")
ALLOWED_ORIGIN       = os.getenv("ALLOWED_ORIGIN", "").strip()
# Only honour CF-Connecting-IP / X-Real-IP / X-Forwarded-For in
# extract_real_ip() when the backend genuinely sits behind a trusted
# reverse proxy (e.g. Cloudflare). Default OFF: on Railway the backend is
# directly exposed, so those headers are client-controlled and worthless
# (BUG-REPORT #2 — rotating one bypassed every IP-based fraud check).
TRUSTED_PROXY        = os.getenv("TRUSTED_PROXY", "0").strip() == "1"
DB_PATH               = os.getenv("DB_PATH", "referral_bot.db")

# Broadcast throttling — Telegram flood-limits bots that blast messages too
# fast. 0.05s (20 msg/sec) was too aggressive and triggered RetryAfter a lot.
# Slower pacing + a periodic longer pause keeps delivery smooth and reliable.
BROADCAST_DELAY        = float(os.getenv("BROADCAST_DELAY", "0.35"))   # per-message delay (~3 msg/sec)
BROADCAST_BATCH_SIZE   = int(os.getenv("BROADCAST_BATCH_SIZE", "25"))  # extra pause every N messages
BROADCAST_BATCH_PAUSE  = float(os.getenv("BROADCAST_BATCH_PAUSE", "2.0"))

# Guards against a broadcast being triggered twice at once (e.g. admin
# double-taps "Send Now", or the API endpoint gets called twice in a row)
# which would otherwise fire two overlapping loops and double-send to
# every single user. Only one broadcast may run at a time.
_broadcast_lock = asyncio.Lock()
_broadcast_running = False
TASK_JOIN_WAIT_SECONDS = int(os.getenv("TASK_JOIN_WAIT_SECONDS", "5"))

# How old a Telegram WebApp initData payload is allowed to be before we
# reject it. initData is only re-issued when the Mini App is (re)opened
# inside Telegram, so this bounds how long a copied/leaked initData string
# (e.g. pasted into a normal browser) stays usable. Telegram itself
# recommends checking auth_date for exactly this reason.
MAX_INIT_DATA_AGE_SECONDS = int(os.getenv("MAX_INIT_DATA_AGE_SECONDS", str(6 * 3600)))

TELEBIRR_PROOF_IMAGE = "AgACAgQAAxkBAAIBImpM6RKFmzDV8HLYo0XHukkMK-RaAAI-D2sbGJdwUazZrBH4OObkAQADAgADeAADPAQ"

if not WEBAPP_URL.startswith(("http://", "https://")):
    WEBAPP_URL = f"https://{WEBAPP_URL}"

BOT_RULES_CAPTION = (
    "🛡 <b>Anon Earn — Fair Play Rules</b>\n\n"
    "• One person, one account. No self-invites, no clone accounts.\n"
    "• No VPN, proxy or emulator while you verify or earn.\n"
    "• Your rewards unlock after the quick in-app security check.\n"
    "🚫 <i>Breaking the rules means a permanent ban.</i>"
)

# ─────────────────────────────────────────────────────────────────────────────
# RATE LIMITER
# ─────────────────────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, max_calls: int = 5, window_seconds: int = 60):
        self.max_calls     = max_calls
        self.window        = window_seconds
        self._calls: dict  = defaultdict(list)
        self._lock         = asyncio.Lock()

    async def is_allowed(self, key: str) -> bool:
        async with self._lock:
            now    = time.monotonic()
            bucket = self._calls[key]
            bucket[:] = [t for t in bucket if now - t < self.window]
            if len(bucket) >= self.max_calls:
                return False
            bucket.append(now)
            return True

verify_limiter = RateLimiter(max_calls=6, window_seconds=60)

# ─────────────────────────────────────────────────────────────────────────────
# IP COOLDOWN — አንድ IP verify ካደረገ በኋላ 3 ደቂቃ ሌላ NEW verify እንዳይሞክር
# (fingerprint ግን ለዘላለም DB ውስጥ ይቆያል — ይህ ለ rapid multi-account creation ብቻ ነው)
# ─────────────────────────────────────────────────────────────────────────────
IP_VERIFY_COOLDOWN_SECONDS = 180   # 3 ደቂቃ

class IPCooldownTracker:
    def __init__(self):
        self._last_verify: dict = {}   # ip -> timestamp
        self._lock = asyncio.Lock()

    async def is_on_cooldown(self, ip: str) -> tuple[bool, int]:
        """Returns (is_blocked, seconds_remaining)."""
        if not ip or ip in ("127.0.0.1", "::1", "unknown"):
            return False, 0
        async with self._lock:
            last = self._last_verify.get(ip)
            if last is None:
                return False, 0
            elapsed = time.monotonic() - last
            remaining = IP_VERIFY_COOLDOWN_SECONDS - elapsed
            if remaining > 0:
                return True, int(remaining)
            return False, 0

    async def mark_verified(self, ip: str):
        if not ip or ip in ("127.0.0.1", "::1", "unknown"):
            return
        async with self._lock:
            self._last_verify[ip] = time.monotonic()

ip_cooldown = IPCooldownTracker()

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE SCHEMA
# ─────────────────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
    user_id             INTEGER PRIMARY KEY,
    username            TEXT,
    full_name           TEXT,
    referred_by         INTEGER,
    balance             REAL    DEFAULT 0,
    is_banned           INTEGER DEFAULT 0,
    last_verify_msg_id  INTEGER DEFAULT 0,
    last_seen           TEXT    DEFAULT '',
    joined_at           TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS verifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER UNIQUE,
    ip_address      TEXT,
    user_agent      TEXT,
    fingerprint     TEXT,
    referrer_ip     TEXT    DEFAULT '',
    tg_platform     TEXT    DEFAULT '',
    tg_version      TEXT    DEFAULT '',
    tg_app_version  TEXT    DEFAULT '',
    canvas_hash     TEXT    DEFAULT '',
    webgl_hash      TEXT    DEFAULT '',
    screen_sig      TEXT    DEFAULT '',
    verified_at     TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,
    amount          REAL,
    full_name       TEXT,
    phone           TEXT,
    status          TEXT    DEFAULT 'pending',
    channel_post_id INTEGER DEFAULT 0,
    reason          TEXT    DEFAULT '',
    created_at      TEXT    DEFAULT (datetime('now')),
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS force_channels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id   TEXT UNIQUE,
    channel_name TEXT,
    invite_link  TEXT,
    bot_added    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS fake_join_seen (
    user_id INTEGER PRIMARY KEY,
    seen_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS banned_ips (
    ip_address  TEXT PRIMARY KEY,
    reason      TEXT,
    banned_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fraud_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    reason      TEXT,
    ip_address  TEXT,
    details     TEXT    DEFAULT '',
    logged_at   TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_checkins (
    user_id         INTEGER PRIMARY KEY,
    last_claim_date TEXT,
    streak          INTEGER DEFAULT 0
);

-- Durable (DB-backed) stand-in for the old FSMContext-only
-- "stashed_referrer_id". A person can sit on the "join these channels
-- first" screen for a long time before tapping Joined, and the bot's
-- FSM storage is in-memory (see MemoryStorage()) — a restart/redeploy
-- in that window used to silently drop their referrer with no trace.
-- This survives that: /start writes here immediately, and it's read
-- back at verification time regardless of whether the in-memory FSM
-- state is still around.
CREATE TABLE IF NOT EXISTS pending_referrals (
    user_id      INTEGER PRIMARY KEY,
    referrer_id  INTEGER NOT NULL,
    created_at   TEXT    DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────
-- MINI APP TASK SYSTEM
-- task_type: 'force' (bot must be admin in channel_id → real membership
--            check via getChatMember before reward is paid)
--          | 'fake'  (no real check — just a Join-link + wait-timer, for
--            channels the bot cannot/should not be admin in)
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    channel_id   TEXT    DEFAULT '',
    invite_link  TEXT    DEFAULT '',
    task_type    TEXT    DEFAULT 'fake',
    reward       REAL    DEFAULT 0,
    position     INTEGER DEFAULT 0,
    is_active    INTEGER DEFAULT 1,
    created_at   TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_progress (
    user_id      INTEGER,
    task_id      INTEGER,
    status       TEXT DEFAULT 'joined',
    joined_at    TEXT,
    completed_at TEXT,
    PRIMARY KEY (user_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_task_progress_user ON task_progress(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_position     ON tasks(position);

-- ─────────────────────────────────────────────────────────────────────────
-- ADSGRAM — REWARDED VIDEO + DIRECT LINK
-- kind: 'video'       (AdsGram SDK rewarded interstitial, completion
--                       confirmed client-side by the SDK promise, then
--                       claimed straight away — no wait timer needed)
--     | 'direct_link' (AdsGram Direct Link/Smart Link — no SDK callback
--                       exists for this format, so we use the same
--                       open → wait → claim pattern as 'fake' tasks)
-- status: 'pending' (direct_link opened, waiting out the timer)
--       | 'completed'
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ad_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER,
    kind         TEXT    NOT NULL,
    status       TEXT    DEFAULT 'pending',
    reward       REAL    DEFAULT 0,
    started_at   TEXT    DEFAULT (datetime('now')),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ad_events_user_kind ON ad_events(user_id, kind, completed_at);

-- Tracks the daily "ads reset" notifications so each user gets at most
-- one of each message per calendar day, even though the sender loop
-- wakes up and re-checks multiple times a day.
CREATE TABLE IF NOT EXISTS daily_notify_log (
    user_id     INTEGER,
    notify_type TEXT,
    notify_date TEXT,
    PRIMARY KEY (user_id, notify_type, notify_date)
);

-- ─────────────────────────────────────────────────────────────────────────
-- PERFORMANCE INDEXES
-- ብዙ users ሲኖሩ balance/referrals/withdraw ቁልፎች የሚጠቀሙባቸውን query ዎች
-- (referred_by lookups, fingerprint/ip correlation, withdrawal status)
-- ፈጣን ለማድረግ። IF NOT EXISTS ስለሆነ deploy በተደጋጋመ ቁጥር ምንም ችግር አይፈጥርም።
-- ─────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_users_referred_by   ON users(referred_by);
CREATE INDEX IF NOT EXISTS idx_users_is_banned     ON users(is_banned);
CREATE INDEX IF NOT EXISTS idx_verif_fingerprint   ON verifications(fingerprint);
CREATE INDEX IF NOT EXISTS idx_verif_ip            ON verifications(ip_address);
CREATE INDEX IF NOT EXISTS idx_verif_canvas_webgl  ON verifications(canvas_hash, webgl_hash);
CREATE INDEX IF NOT EXISTS idx_verif_tg_device     ON verifications(tg_platform, tg_version);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status  ON withdrawals(status);
CREATE INDEX IF NOT EXISTS idx_withdrawals_user    ON withdrawals(user_id);
CREATE INDEX IF NOT EXISTS idx_fraud_log_logged_at ON fraud_log(logged_at);
CREATE INDEX IF NOT EXISTS idx_fraud_log_user      ON fraud_log(user_id);
CREATE INDEX IF NOT EXISTS idx_banned_ips_ip        ON banned_ips(ip_address);
CREATE INDEX IF NOT EXISTS idx_users_last_seen      ON users(last_seen);

INSERT OR IGNORE INTO settings (key, value) VALUES ('reward_per_referral', '10');
INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdrawal', '50');
INSERT OR IGNORE INTO settings (key, value) VALUES ('min_ads_for_withdrawal', '30');
INSERT OR IGNORE INTO settings (key, value) VALUES ('usdt_rate', '155');

-- AdsGram — rewarded video
INSERT OR IGNORE INTO settings (key, value) VALUES ('ads_enabled', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('adsgram_block_id', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('monetag_zone_id', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('monetag_sdk_url', '');
-- Tads — second/gating rewarded ad shown right after AdsGram's video ad
-- finishes. When enabled with a widget ID configured, the AdsGram S2S
-- reward postback is withheld until Tads also reports its outcome: pay
-- if Tads actually showed and the user completed it, OR if Tads had no
-- ad to show (no-fill) — but never if Tads had an ad and it wasn't
-- completed.
INSERT OR IGNORE INTO settings (key, value) VALUES ('tads_enabled', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tads_widget_id', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('referral_skip_enabled', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('referral_skip_batch_size', '6');
INSERT OR IGNORE INTO settings (key, value) VALUES ('referral_skip_min', '1');
INSERT OR IGNORE INTO settings (key, value) VALUES ('referral_skip_max', '3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('user_task_creation_enabled', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('user_task_min_reward', '1');
INSERT OR IGNORE INTO settings (key, value) VALUES ('user_task_max_reward', '20');
INSERT OR IGNORE INTO settings (key, value) VALUES ('user_task_min_slots', '5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('user_task_max_slots', '500');
INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_reward_amount', '0.5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_daily_limit', '10');
INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_cooldown_seconds', '30');
INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_batch_size', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_batch_cooldown_seconds', '3600');
INSERT OR IGNORE INTO settings (key, value) VALUES ('ads_reset_broadcast_enabled', '1');

-- AdsGram — require a real "left the app for the banner" signal
-- (blur/visibilitychange) before the S2S reward postback is honored.
INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_require_banner_click', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_banner_click_window_seconds', '120');

-- CTR control: once today's verified-click reward count hits this cap,
-- the click requirement is quietly relaxed for the rest of the day
-- (pay every ad view) so AdsGram doesn't see an unnaturally high
-- confirmed-click ratio.
INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_banner_click_cap_enabled', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_banner_click_daily_cap', '124');

-- AdsGram — Direct Link
INSERT OR IGNORE INTO settings (key, value) VALUES ('adsgram_direct_link', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('direct_link_reward_amount', '0.3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('direct_link_daily_limit', '10');
INSERT OR IGNORE INTO settings (key, value) VALUES ('direct_link_wait_seconds', '15');
INSERT OR IGNORE INTO settings (key, value) VALUES ('direct_link_cooldown_seconds', '30');
INSERT OR IGNORE INTO settings (key, value) VALUES ('multi_account_detect_enabled', '1');
INSERT OR IGNORE INTO settings (key, value) VALUES ('proof_post_enabled', '1');
INSERT OR IGNORE INTO settings (key, value) VALUES ('withdrawal_process_value', '24-48');
INSERT OR IGNORE INTO settings (key, value) VALUES ('withdrawal_process_unit', 'hours');
INSERT OR IGNORE INTO settings (key, value) VALUES ('withdrawal_notify_mode', 'both');

INSERT OR IGNORE INTO fake_join_seen (user_id)
SELECT user_id FROM verifications;
"""

# NOTE: All ALTER TABLE statements are wrapped in try/except at apply-time
# (see DataEngine.init_database) so they're safe to keep here permanently —
# running them again on a DB that already has the columns is a harmless no-op.
MIGRATION_STATEMENTS = [
    "ALTER TABLE verifications ADD COLUMN referrer_ip    TEXT DEFAULT ''",
    "ALTER TABLE verifications ADD COLUMN tg_platform    TEXT DEFAULT ''",
    "ALTER TABLE verifications ADD COLUMN tg_version     TEXT DEFAULT ''",
    "ALTER TABLE verifications ADD COLUMN tg_app_version TEXT DEFAULT ''",
    "ALTER TABLE verifications ADD COLUMN canvas_hash    TEXT DEFAULT ''",
    "ALTER TABLE verifications ADD COLUMN webgl_hash     TEXT DEFAULT ''",
    "ALTER TABLE verifications ADD COLUMN screen_sig     TEXT DEFAULT ''",
    "ALTER TABLE users         ADD COLUMN last_verify_msg_id INTEGER DEFAULT 0",
    "ALTER TABLE users         ADD COLUMN last_seen TEXT DEFAULT ''",
    # 1 = counts toward the referrer's own "Direct" stat (paid, or the
    # skip feature is off / this row predates it). Set to 0 only when
    # referral_skip silently withholds this referral's payout — so a
    # skipped join never shows up in the referrer's own numbers at all,
    # not just missing a payout notification.
    "ALTER TABLE users         ADD COLUMN referral_paid INTEGER DEFAULT 1",
    "ALTER TABLE tasks         ADD COLUMN created_by    INTEGER DEFAULT 0",
    "ALTER TABLE tasks         ADD COLUMN budget_slots  INTEGER DEFAULT 0",
    "ALTER TABLE tasks         ADD COLUMN slots_used    INTEGER DEFAULT 0",
    "ALTER TABLE tasks         ADD COLUMN review_status TEXT DEFAULT 'approved'",
    # Binance Pay payout option — existing Telebirr withdrawals default to
    # method='telebirr' so old rows/behaviour are unaffected.
    "ALTER TABLE withdrawals   ADD COLUMN method       TEXT DEFAULT 'telebirr'",
    "ALTER TABLE withdrawals   ADD COLUMN binance_uid  TEXT DEFAULT ''",
    # Manual/screenshot-review tasks: 'instructions' is shown to the user
    # on the upload page, 'screenshot_count' (1 or 2) is how many photos
    # they must attach before /api/tasks/manual/submit will accept it.
    "ALTER TABLE tasks         ADD COLUMN instructions     TEXT DEFAULT ''",
    "ALTER TABLE tasks         ADD COLUMN screenshot_count INTEGER DEFAULT 1",
    # One row per attempt (not per task) so a rejected user can resubmit —
    # the latest row per (user_id, task_id) is what /api/tasks reports and
    # what the admin queue reviews. screenshots is a JSON array of
    # Telegram file_ids (a few dozen bytes each) — the actual photo bytes
    # are relayed straight to the admin's Telegram DM and never written
    # here, so this table can't grow into a storage problem.
    """CREATE TABLE IF NOT EXISTS manual_submissions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id      INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        screenshots  TEXT    NOT NULL,
        status       TEXT    DEFAULT 'pending',
        submitted_at TEXT    DEFAULT (datetime('now')),
        reviewed_at  TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_manual_submissions_status    ON manual_submissions(status)",
    "CREATE INDEX IF NOT EXISTS idx_manual_submissions_user_task ON manual_submissions(user_id, task_id)",
    """CREATE TABLE IF NOT EXISTS fraud_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, reason TEXT, ip_address TEXT,
        details TEXT DEFAULT '', logged_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS daily_checkins (
        user_id INTEGER PRIMARY KEY,
        last_claim_date TEXT,
        streak INTEGER DEFAULT 0
    )""",
    # Real click-away detection for the AdsGram block_id (video/banner)
    # flow: the frontend fires this the moment Telegram's WebApp is
    # actually backgrounded (blur / visibilitychange) while an ad is
    # showing — i.e. real evidence the user left the app for the ad's
    # banner/link, not just that the SDK promise resolved. One row per
    # user, overwritten on every new click signal.
    """CREATE TABLE IF NOT EXISTS banner_click_signals (
        user_id    INTEGER PRIMARY KEY,
        clicked_at TEXT DEFAULT (datetime('now'))
    )""",
    # One row per reward that was actually gated on a verified click (see
    # ad_require_banner_click). Used purely to count today's "genuinely
    # click-verified" rewards against ad_banner_click_daily_cap — once
    # that count is reached, the CTR-control feature below quietly stops
    # requiring the click for the rest of the day (still pays on
    # impression), so the fraction of "confirmed click" traffic AdsGram
    # sees doesn't sit unnaturally high and risk a fraud flag.
    """CREATE TABLE IF NOT EXISTS banner_click_reward_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER,
        credited_at TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_banner_click_reward_log_date ON banner_click_reward_log(credited_at)",
    # Marks that this user's very next AdsGram video-ad completion
    # (i.e. the next /api/ads/adsgram-reward postback) was shown by the
    # Daily Check-in flow, not the regular Watch Ad button — both reuse
    # the same AdsGram block_id, so AdsGram's S2S postback can't tell
    # them apart on its own. Consumed (checked + deleted) the moment the
    # postback lands, so claim_video_ad_atomic can still record the
    # completed ad_events row (daily check-in's has_recent_completed_ad
    # gate depends on that) while skipping the separate ad_reward_amount
    # payout — the daily bonus itself is paid once, separately, by
    # claim_daily_checkin_atomic.
    """CREATE TABLE IF NOT EXISTS daily_checkin_ad_pending (
        user_id   INTEGER PRIMARY KEY,
        marked_at TEXT DEFAULT (datetime('now'))
    )""",
    # Reconciles AdsGram's S2S postback with Tads's client-reported
    # outcome, whichever of the two arrives first — see
    # gate_adsgram_confirmation / record_tads_result. One row per user;
    # overwritten on every new attempt (see clear_ad_reward_gate).
    """CREATE TABLE IF NOT EXISTS ad_reward_gate (
        user_id              INTEGER PRIMARY KEY,
        adsgram_confirmed_at TEXT,
        adsgram_payload      TEXT,
        tads_status          TEXT,
        tads_at              TEXT
    )""",
    # AdsGram S2S postback idempotency (BUG-REPORT #8): one row per
    # delivery key AdsGram sends (its transaction/event id, when the
    # reward URL is configured to pass one). A redelivered postback for
    # the same ad view is acknowledged but never credited twice.
    """CREATE TABLE IF NOT EXISTS adsgram_postbacks (
        event_key  TEXT PRIMARY KEY,
        user_id    INTEGER,
        seen_at    TEXT DEFAULT (datetime('now'))
    )""",
]

# ─────────────────────────────────────────────────────────────────────────────
# HTML SANITIZER
# ─────────────────────────────────────────────────────────────────────────────
def sanitize_html(text: str) -> str:
    if not text:
        return ""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )

# ─────────────────────────────────────────────────────────────────────────────
# DATA ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class DataEngine:
    @staticmethod
    async def init_database():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(SCHEMA)
            await db.commit()
            for stmt in MIGRATION_STATEMENTS:
                try:
                    await db.execute(stmt)
                    await db.commit()
                except Exception:
                    pass
            # Run ANALYZE once at startup so SQLite's query planner picks
            # good index strategies once the table has data.
            try:
                await db.execute("ANALYZE")
                await db.commit()
            except Exception:
                pass

    @staticmethod
    async def get_user(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            return await cur.fetchone()

    @staticmethod
    async def create_user(user_id: int, username: str, full_name: str, referred_by: int = None):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, username, full_name, referred_by) VALUES (?,?,?,?)",
                (user_id, username, full_name, referred_by),
            )
            await db.commit()

    @staticmethod
    async def add_balance(user_id: int, amount: float):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET balance = ROUND(balance + ?, 2) WHERE user_id = ?", (amount, user_id)
            )
            await db.commit()

    @staticmethod
    async def is_referral_skipped(referrer_id: int, position: int) -> bool:
        """
        Decides whether the `position`-th (1-indexed) direct referral a
        given referrer has ever brought in should be paid or silently
        skipped. Controlled by the referral_skip_* settings.

        Works in fixed-size batches (default 6): within each batch, a
        random 1-3 (configurable) of the slots are picked to be unpaid.
        The choice is deterministic per (referrer_id, batch_number) — no
        extra table needed, and re-computing it always gives the same
        answer for the same referral, which matters if this is ever
        called twice for the same person.
        """
        enabled = (await DataEngine.get_setting("referral_skip_enabled", "0")) == "1"
        if not enabled:
            return False
        batch_size = max(2, int(await DataEngine.get_setting("referral_skip_batch_size", "6")))
        skip_min = max(0, int(await DataEngine.get_setting("referral_skip_min", "1")))
        skip_max = max(skip_min, int(await DataEngine.get_setting("referral_skip_max", "3")))
        skip_max = min(skip_max, batch_size - 1)  # never skip an entire batch

        batch_number = (position - 1) // batch_size
        position_in_batch = (position - 1) % batch_size + 1  # 1..batch_size

        rng = random.Random(f"{referrer_id}:{batch_number}:{batch_size}:{skip_min}:{skip_max}")
        skip_count = rng.randint(skip_min, skip_max)
        if skip_count <= 0:
            return False
        skip_positions = set(rng.sample(range(1, batch_size + 1), skip_count))
        return position_in_batch in skip_positions

    @staticmethod
    async def stash_pending_referral(user_id: int, referrer_id: int):
        """Called from /start the moment we see a ?ref= param — before we
        know if the person will pass the membership gate or when they'll
        tap 'Joined'. Written to disk immediately so it isn't lost if the
        bot restarts while they're still off joining channels."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO pending_referrals (user_id, referrer_id, created_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(user_id) DO UPDATE SET referrer_id=excluded.referrer_id, "
                "created_at=excluded.created_at",
                (user_id, referrer_id),
            )
            await db.commit()

    @staticmethod
    async def get_pending_referral(user_id: int):
        """Returns the stashed referrer_id if one exists and is recent
        (7 days — long enough to cover any realistic gap between /start
        and finishing verification, short enough that a genuinely stale
        one from months ago isn't wrongly resurrected)."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT referrer_id FROM pending_referrals "
                "WHERE user_id=? AND created_at >= datetime('now','-7 days')",
                (user_id,),
            )
            row = await cur.fetchone()
            return row["referrer_id"] if row else None

    @staticmethod
    async def clear_pending_referral(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM pending_referrals WHERE user_id=?", (user_id,))
            await db.commit()

    @staticmethod
    async def mark_referral_unpaid(user_id: int):
        """Called when referral_skip decides this user's join shouldn't be
        paid out — also hides them from the referrer's own 'Direct' count
        (see get_paid_referral_metrics), not just from the payout message."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET referral_paid = 0 WHERE user_id = ?", (user_id,))
            await db.commit()

    @staticmethod
    async def get_paid_referral_metrics(user_id: int):
        """Same shape as get_referral_metrics, but only counts referrals
        that were actually paid — this is what the referrer themselves
        should see (bot chat, Mini App Invite tab). Admin-facing views
        should keep using get_referral_metrics (the true, unfiltered
        count) since referral_skip's whole point is that only the
        referrer stays unaware of it."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur1 = await db.execute(
                "SELECT COUNT(*) FROM users WHERE referred_by = ? AND referral_paid = 1", (user_id,)
            )
            direct_count = (await cur1.fetchone())[0] or 0
            cur2 = await db.execute(
                "SELECT COUNT(*) FROM users u2 "
                "JOIN users u1 ON u2.referred_by = u1.user_id "
                "WHERE u1.referred_by = ? AND u1.referral_paid = 1",
                (user_id,)
            )
            tier2_count = (await cur2.fetchone())[0] or 0
            return direct_count, tier2_count

    @staticmethod
    async def get_referral_metrics(user_id: int):
        """
        Direct + tier-2 referral counts.
        Tier-2 now uses an explicit JOIN instead of a correlated subquery —
        with the idx_users_referred_by index this lets SQLite do two fast
        index lookups instead of a nested scan, which matters once the
        users table is large.
        """
        async with aiosqlite.connect(DB_PATH) as db:
            cur1 = await db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,))
            direct_count = (await cur1.fetchone())[0] or 0
            cur2 = await db.execute(
                "SELECT COUNT(*) FROM users u2 "
                "JOIN users u1 ON u2.referred_by = u1.user_id "
                "WHERE u1.referred_by = ?",
                (user_id,)
            )
            tier2_count = (await cur2.fetchone())[0] or 0
            return direct_count, tier2_count

    @staticmethod
    async def get_all_invited_users(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT user_id, username, full_name, joined_at FROM users WHERE referred_by = ?", (user_id,)
            )
            return await cur.fetchall()

    @staticmethod
    async def touch_last_seen(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET last_seen = datetime('now') WHERE user_id = ?", (user_id,)
            )
            await db.commit()

    @staticmethod
    async def get_user_activity_stats(online_window_minutes: int = 5):
        async with aiosqlite.connect(DB_PATH) as db:
            cur1 = await db.execute("SELECT COUNT(*) FROM users")
            total = (await cur1.fetchone())[0] or 0
            cur2 = await db.execute(
                "SELECT COUNT(*) FROM users WHERE last_seen != '' "
                f"AND last_seen >= datetime('now', '-{int(online_window_minutes)} minutes')"
            )
            online = (await cur2.fetchone())[0] or 0
            return total, online

    @staticmethod
    async def ban_user(user_id: int, status: int = 1):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (status, user_id))
            await db.commit()

    @staticmethod
    async def full_clear_verification(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM verifications WHERE user_id = ?", (user_id,))
            await db.commit()

    @staticmethod
    async def inject_fake_verification(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO verifications "
                "(user_id, ip_address, user_agent, fingerprint, referrer_ip, tg_platform, tg_version, tg_app_version) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (user_id, "BYPASS_ADMIN", "BYPASS_ADMIN", f"BYPASS_{user_id}", "", "", "", ""),
            )
            await db.commit()

    @staticmethod
    async def is_verified(user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id FROM verifications WHERE user_id = ?", (user_id,))
            return (await cur.fetchone()) is not None

    @staticmethod
    async def save_verification(
        user_id: int,
        ip: str,
        ua: str,
        fingerprint: str,
        referrer_ip: str = "",
        tg_platform: str = "",
        tg_version: str = "",
        tg_app_version: str = "",
        canvas_hash: str = "",
        webgl_hash: str = "",
        screen_sig: str = "",
    ) -> bool:
        """Atomically claims the verification slot for this user_id.
        verifications.user_id is UNIQUE, and this is a plain INSERT (not
        OR REPLACE), so if two /api/verify requests for the same uid ever
        race each other (double-tap, network retry, two tabs...), only
        ONE of them can succeed here — SQLite serializes the write and
        rejects the second with an IntegrityError. Returns True only for
        the call that actually won that race; the caller must treat False
        as "already verified" and skip account creation / referral payout
        entirely, so a race can never pay a referral reward twice."""
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute(
                    "INSERT INTO verifications "
                    "(user_id, ip_address, user_agent, fingerprint, referrer_ip, "
                    "tg_platform, tg_version, tg_app_version, "
                    "canvas_hash, webgl_hash, screen_sig) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, ip, ua, fingerprint, referrer_ip,
                     tg_platform, tg_version, tg_app_version,
                     canvas_hash, webgl_hash, screen_sig),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    @staticmethod
    async def get_verification(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM verifications WHERE user_id = ?", (user_id,))
            return await cur.fetchone()

    @staticmethod
    async def is_ip_banned(ip: str) -> bool:
        if not ip or ip in ("127.0.0.1", "::1", "unknown", "BYPASS_ADMIN"):
            return False
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT ip_address FROM banned_ips WHERE ip_address = ?", (ip,))
            return (await cur.fetchone()) is not None

    @staticmethod
    async def ban_ip(ip: str, reason: str):
        if not ip or ip in ("127.0.0.1", "::1", "unknown", "BYPASS_ADMIN"):
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO banned_ips (ip_address, reason) VALUES (?,?)", (ip, reason)
            )
            await db.commit()

    @staticmethod
    async def log_fraud_attempt(user_id: int, reason: str, ip: str, details: str = ""):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO fraud_log (user_id, reason, ip_address, details) VALUES (?,?,?,?)",
                (user_id, reason, ip, details),
            )
            await db.commit()

    @staticmethod
    async def count_ip_users(ip: str) -> int:
        if not ip or ip in ("127.0.0.1", "::1", "unknown", "BYPASS_ADMIN"):
            return 0
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM verifications WHERE ip_address = ?", (ip,)
            )
            return (await cur.fetchone())[0] or 0

    @staticmethod
    async def count_ip_distinct_fingerprints(ip: str) -> int:
        """
        How many DIFFERENT device fingerprints have verified on this IP.
        Used to tell a legitimate shared network (many distinct phones,
        e.g. CGNAT / office WiFi / mobile carrier NAT) apart from a real
        bot farm (few/duplicate fingerprints reused across many accounts).
        """
        if not ip or ip in ("127.0.0.1", "::1", "unknown", "BYPASS_ADMIN"):
            return 0
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT COUNT(DISTINCT fingerprint) FROM verifications "
                "WHERE ip_address = ? AND fingerprint != '' "
                "AND fingerprint NOT LIKE 'BYPASS_%'",
                (ip,)
            )
            return (await cur.fetchone())[0] or 0

    @staticmethod
    async def set_last_verify_msg(user_id: int, msg_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET last_verify_msg_id = ? WHERE user_id = ?", (msg_id, user_id)
            )
            await db.commit()

    @staticmethod
    async def create_withdrawal_atomic(
        user_id: int, amount: float, full_name: str, phone: str,
        method: str = "telebirr", binance_uid: str = ""
    ) -> tuple[int, bool]:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN EXCLUSIVE")
            try:
                cur = await db.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (user_id,)
                )
                row = await cur.fetchone()
                if not row or round(float(row[0]), 2) < round(amount, 2):
                    await db.execute("ROLLBACK")
                    return 0, False

                cur2 = await db.execute(
                    "INSERT INTO withdrawals (user_id, amount, full_name, phone, method, binance_uid) VALUES (?,?,?,?,?,?)",
                    (user_id, amount, full_name, phone, method, binance_uid),
                )
                tid = cur2.lastrowid
                await db.execute(
                    "UPDATE users SET balance = ROUND(balance - ?, 2) WHERE user_id = ?",
                    (amount, user_id),
                )
                await db.execute("COMMIT")
                return tid, True
            except Exception as e:
                await db.execute("ROLLBACK")
                raise e

    @staticmethod
    async def deduct_balance_atomic(user_id: int, amount: float) -> bool:
        """Atomically checks sufficient balance and deducts it in one
        BEGIN EXCLUSIVE transaction — same pattern as
        create_withdrawal_atomic. Returns False (deducting nothing) if
        the balance is insufficient. Without this, a separate
        SELECT-balance-then-UPDATE check (as user task creation used to
        do) can be raced by two concurrent requests that both read the
        same starting balance and both pass the check, overdrawing the
        account into the negative."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN EXCLUSIVE")
            try:
                cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
                row = await cur.fetchone()
                if not row or round(float(row[0]), 2) < round(amount, 2):
                    await db.execute("ROLLBACK")
                    return False
                await db.execute(
                    "UPDATE users SET balance = ROUND(balance - ?, 2) WHERE user_id = ?",
                    (amount, user_id),
                )
                await db.execute("COMMIT")
                return True
            except Exception:
                await db.execute("ROLLBACK")
                raise

    @staticmethod
    async def get_withdrawal(wid: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (wid,))
            return await cur.fetchone()

    @staticmethod
    async def update_withdrawal_status(wid: int, status: str, post_id: int = 0, reason: str = ""):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE withdrawals SET status=?, channel_post_id=?, reason=?, resolved_at=datetime('now') WHERE id=?",
                (status, post_id, reason, wid),
            )
            await db.commit()

    @staticmethod
    async def claim_withdrawal_approval(wid: int) -> bool:
        """Atomically flips a withdrawal pending -> approved and returns
        True only for the ONE caller that wins the flip. This is the
        exactly-once guard for approval (BUG-REPORT #4): the old
        read-status-then-update flow in approve_withdrawal_core let a
        concurrent rejection (which refunds the balance) land in between
        and then be overwritten back to 'approved' — the user kept the
        refund AND the payout, and double-approval duplicated the proof
        post/DM. The guarded WHERE makes the flip itself the race arbiter,
        the same pattern as execute_withdrawal_rejection's refund."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE withdrawals SET status='approved', resolved_at=datetime('now') "
                "WHERE id=? AND status='pending'",
                (wid,),
            )
            await db.commit()
            return cur.rowcount > 0

    @staticmethod
    async def mark_adsgram_postback_seen(event_key: str, user_id: int) -> bool:
        """Records an AdsGram S2S delivery key. Returns True the first
        time the key is seen; False means this exact delivery was already
        processed (a redelivery of the same ad view) and must not credit
        again (BUG-REPORT #8)."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO adsgram_postbacks (event_key, user_id) VALUES (?, ?)",
                (event_key, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    @staticmethod
    async def get_pending_withdrawals():
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY created_at")
            return await cur.fetchall()

    @staticmethod
    async def add_force_channel(channel_id: str, channel_name: str, invite_link: str, bot_added: int = 0):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO force_channels (channel_id, channel_name, invite_link, bot_added) VALUES (?,?,?,?)",
                (channel_id, channel_name, invite_link, bot_added),
            )
            await db.commit()

    @staticmethod
    async def get_force_channels():
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM force_channels")
            return await cur.fetchall()

    @staticmethod
    async def has_seen_fake_join(user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT user_id FROM fake_join_seen WHERE user_id = ?", (user_id,))
            return (await cur.fetchone()) is not None

    @staticmethod
    async def mark_fake_join_seen(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO fake_join_seen (user_id) VALUES (?)", (user_id,))
            await db.commit()

    _settings_cache: dict = {}
    _settings_lock = asyncio.Lock()

    @staticmethod
    async def get_setting(key: str, default=None):
        async with DataEngine._settings_lock:
            if key in DataEngine._settings_cache:
                return DataEngine._settings_cache[key]
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
                row = await cur.fetchone()
                val = row["value"] if row else default
                DataEngine._settings_cache[key] = val
                return val

    @staticmethod
    async def set_setting(key: str, value: str):
        async with DataEngine._settings_lock:
            DataEngine._settings_cache[key] = value
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
            await db.commit()

    # ── AdsGram: rewarded video + direct link ──────────────────────────
    @staticmethod
    async def count_ad_events_today(user_id: int, kind: str) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM ad_events WHERE user_id=? AND kind=? AND status='completed' "
                "AND date(completed_at) = date('now')",
                (user_id, kind),
            )
            row = await cur.fetchone()
            return row[0] if row else 0

    @staticmethod
    async def will_next_video_attempt_pay(user_id: int) -> bool:
        """Read-only prediction of the SAME odd/even rule claim_video_ad_atomic
        applies (attempt_num % 2 != 0 with skip_enabled means 'warning',
        no pay) — used purely so the frontend can skip its reward-confirm
        polling loop entirely for an attempt that was never going to pay
        out, instead of waiting out the full timeout for nothing every
        single time. No writes, no gating — claim_video_ad_atomic remains
        the sole source of truth for what actually happens."""
        skip_enabled = (await DataEngine.get_setting("ad_click_reminder_enabled", "1")) == "1"
        if not skip_enabled:
            return True
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM ad_events WHERE user_id=? AND kind='video' "
                "AND status IN ('completed', 'warning') AND date(started_at)=date('now')",
                (user_id,),
            )
            completed_or_warned = (await cur.fetchone())[0]
        attempt_num = completed_or_warned + 1
        return attempt_num % 2 == 0

    @staticmethod
    async def count_total_completed_ads(user_id: int) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM ad_events WHERE user_id=? AND status IN ('completed', 'completed_checkin')",
                (user_id,),
            )
            row = await cur.fetchone()
            return row[0] if row else 0

    @staticmethod
    async def get_last_ad_event(user_id: int, kind: str):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT completed_at FROM ad_events WHERE user_id=? AND kind=? AND status='completed' "
                "ORDER BY completed_at DESC LIMIT 1",
                (user_id, kind),
            )
            row = await cur.fetchone()
            return row["completed_at"] if row else None

    @staticmethod
    async def record_ad_event(user_id: int, kind: str, reward: float):
        """Straight-to-completed event — used for rewarded video, whose
        completion is already confirmed client-side by the AdsGram SDK
        promise before this is ever called."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO ad_events (user_id, kind, status, reward, completed_at) "
                "VALUES (?, ?, 'completed', ?, datetime('now'))",
                (user_id, kind, reward),
            )
            await db.commit()

    @staticmethod
    async def claim_video_ad_atomic(
        user_id: int, reward: float, daily_limit: int, cooldown_seconds: int,
        skip_enabled: bool = True, batch_size: int = 0, batch_cooldown_seconds: int = 0,
        skip_balance_credit: bool = False,
    ) -> tuple[bool, str]:
        """Re-checks the daily cap and cooldown AND records+pays the
        reward, all inside one BEGIN EXCLUSIVE transaction.
        Rule: Alternating ad attempts based on completed/warning count today.
        - 1st, 3rd, 5th, 7th... (Odd attempts): No pay, record warning.
        - 2nd, 4th, 6th, 8th... (Even attempts): Pay out reward.
        Pass skip_enabled=False (admin setting) to disable the odd/even
        withholding entirely — every attempt then pays out normally.

        batch_size/batch_cooldown_seconds implement ad "batches": e.g.
        batch_size=5 groups today's ads into runs of 5, and the moment a
        run is finished (watched_today is an exact multiple of
        batch_size), the NEXT ad waits out batch_cooldown_seconds
        instead of the normal per-ad cooldown_seconds — a longer pause
        between batches, short pauses within one. batch_size=0 disables
        this entirely (falls back to the flat cooldown everywhere).

        Pass skip_balance_credit=True when this completed view belongs
        to the Daily Check-in flow (see daily_checkin_ad_pending) — the
        'completed' ad_events row still gets recorded, so Daily Check-in's
        own has_recent_completed_ad gate keeps working, but the separate
        ad_reward_amount is NOT added to balance, since that view is
        already being paid for by claim_daily_checkin_atomic."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN EXCLUSIVE")
            try:
                # Daily Check-in views are a separate, fixed bonus system —
                # they only need proof an ad was actually watched, not the
                # odd/even reward-withholding rule, daily_limit, or cooldown
                # that exist purely to gate the regular per-view earn flow.
                # Applying those here meant a Daily Check-in ad that landed
                # on an odd-numbered slot got recorded as 'warning' instead
                # of 'completed', which silently failed the whole check-in
                # (has_recent_completed_ad found nothing) even though the
                # user genuinely watched the ad. So: record 'completed'
                # unconditionally, with reward 0, and stop there.
                if skip_balance_credit:
                    await db.execute(
                        "INSERT INTO ad_events (user_id, kind, status, reward, completed_at) "
                        "VALUES (?, 'video', 'completed_checkin', 0, datetime('now'))",
                        (user_id,),
                    )
                    await db.execute("COMMIT")
                    return True, ""

                # Count finished/processed ad attempts today (status='completed' or 'warning')
                cur_total = await db.execute(
                    "SELECT COUNT(*) FROM ad_events WHERE user_id=? AND kind='video' "
                    "AND status IN ('completed', 'warning') AND date(started_at)=date('now')",
                    (user_id,),
                )
                completed_or_warned = (await cur_total.fetchone())[0]
                attempt_num = completed_or_warned + 1  # 1st ad = 1, 2nd ad = 2, 3rd ad = 3...

                # Odd numbered attempts (1, 3, 5, 7...) trigger the click reminder & withhold reward
                if skip_enabled and attempt_num % 2 != 0:
                    await db.execute(
                        "INSERT INTO ad_events (user_id, kind, status, reward) "
                        "VALUES (?, 'video', 'warning', 0)",
                        (user_id,),
                    )
                    await db.execute("COMMIT")
                    return False, "ad_click_reminder"

                cur = await db.execute(
                    "SELECT COUNT(*) FROM ad_events WHERE user_id=? AND kind='video' "
                    "AND status='completed' AND date(completed_at)=date('now')",
                    (user_id,),
                )
                watched_today = (await cur.fetchone())[0]
                if watched_today >= daily_limit:
                    await db.execute("ROLLBACK")
                    asyncio.create_task(notify_user_limit_reached(user_id))
                    return False, "daily_limit_reached"

                cur2 = await db.execute(
                    "SELECT completed_at FROM ad_events WHERE user_id=? AND kind='video' "
                    "AND status='completed' ORDER BY completed_at DESC LIMIT 1",
                    (user_id,),
                )
                last = await cur2.fetchone()
                if last and last[0]:
                    try:
                        elapsed = (datetime.utcnow() - datetime.strptime(last[0], "%Y-%m-%d %H:%M:%S")).total_seconds()
                        starting_new_batch = (
                            batch_size > 0 and watched_today > 0 and watched_today % batch_size == 0
                        )
                        effective_cooldown = batch_cooldown_seconds if starting_new_batch else cooldown_seconds
                        if elapsed < effective_cooldown:
                            await db.execute("ROLLBACK")
                            return False, "batch_cooldown_active" if starting_new_batch else "cooldown_active"
                    except Exception:
                        pass

                await db.execute(
                    "INSERT INTO ad_events (user_id, kind, status, reward, completed_at) "
                    "VALUES (?, 'video', 'completed', ?, datetime('now'))",
                    (user_id, reward),
                )
                await db.execute(
                    "UPDATE users SET balance = ROUND(balance + ?, 2) WHERE user_id = ?",
                    (reward, user_id),
                )
                await db.execute("COMMIT")
                return True, ""
            except Exception:
                await db.execute("ROLLBACK")
                raise

    @staticmethod
    async def get_daily_checkin_status(user_id: int) -> dict:
        """Returns whether today's check-in is already claimed, and the
        current streak, without touching anything."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT last_claim_date, streak FROM daily_checkins WHERE user_id=?",
                (user_id,),
            )
            row = await cur.fetchone()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if not row:
            return {"claimed_today": False, "streak": 0}
        return {"claimed_today": row["last_claim_date"] == today, "streak": row["streak"] or 0}

    @staticmethod
    async def has_recent_completed_ad(user_id: int, within_seconds: int = 180) -> bool:
        """Daily check-in requires actually watching an ad first — this
        checks for a genuinely S2S-confirmed ad view (kind='video',
        status='completed') in the last `within_seconds`, the same trust
        model already used for the regular Watch Ad reward, so check-in
        can't be claimed without really watching something."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT completed_at FROM ad_events WHERE user_id=? AND kind='video' "
                "AND status IN ('completed', 'completed_checkin') ORDER BY completed_at DESC LIMIT 1",
                (user_id,),
            )
            row = await cur.fetchone()
        if not row or not row[0]:
            return False
        try:
            elapsed = (datetime.utcnow() - datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")).total_seconds()
            return 0 <= elapsed <= within_seconds
        except Exception:
            return False

    @staticmethod
    async def has_recent_checkin_ad(user_id: int, within_seconds: int = 600) -> bool:
        """True only if a Daily Check-in-armed ad view landed recently —
        i.e. an ad_events row with status='completed_checkin', which is
        recorded exclusively when the AdsGram postback consumed this
        user's daily_checkin_ad_pending mark (see /api/dailycheck/arm).
        Regular Watch & Earn views (status='completed') deliberately do
        NOT count: they already paid their own per-view reward, and
        letting one also unlock the daily bonus was the double-dip in
        BUG-REPORT #7."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT completed_at FROM ad_events WHERE user_id=? AND kind='video' "
                "AND status='completed_checkin' ORDER BY completed_at DESC LIMIT 1",
                (user_id,),
            )
            row = await cur.fetchone()
        if not row or not row[0]:
            return False
        try:
            elapsed = (datetime.utcnow() - datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")).total_seconds()
            return 0 <= elapsed <= within_seconds
        except Exception:
            return False

    @staticmethod
    async def claim_daily_checkin_atomic(user_id: int, reward: float) -> tuple[bool, str, int]:
        """Re-checks 'not already claimed today' AND pays the reward, all
        inside one BEGIN EXCLUSIVE transaction — same double-claim
        protection pattern as withdrawals. Streak continues if the last
        claim was yesterday, otherwise resets to 1.
        Returns (ok, reason, new_streak)."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN EXCLUSIVE")
            try:
                cur = await db.execute(
                    "SELECT last_claim_date, streak FROM daily_checkins WHERE user_id=?",
                    (user_id,),
                )
                row = await cur.fetchone()
                if row and row[0] == today:
                    await db.execute("ROLLBACK")
                    return False, "already_claimed", row[1] or 0

                # BUG-REPORT #7: require (and consume) a check-in-armed
                # ad view inside this same transaction. The old gate —
                # has_recent_completed_ad — accepted ANY recent completed
                # video view, including a regular Watch & Earn view that
                # had already paid its own reward, so one ad paid twice.
                # Only a 'completed_checkin' row (recorded when the S2S
                # postback consumed the /api/dailycheck/arm mark) counts,
                # and flipping it to 'checkin_claimed' here means one
                # armed view can only ever unlock one claim.
                cur_ad = await db.execute(
                    "SELECT id, completed_at FROM ad_events WHERE user_id=? AND kind='video' "
                    "AND status='completed_checkin' ORDER BY completed_at DESC LIMIT 1",
                    (user_id,),
                )
                ad_row = await cur_ad.fetchone()
                ad_ok = False
                if ad_row and ad_row[1]:
                    try:
                        ad_elapsed = (datetime.utcnow() - datetime.strptime(ad_row[1], "%Y-%m-%d %H:%M:%S")).total_seconds()
                        ad_ok = 0 <= ad_elapsed <= 600
                    except Exception:
                        ad_ok = False
                if not ad_ok:
                    await db.execute("ROLLBACK")
                    return False, "watch_ad_first", (row[1] or 0) if row else 0
                await db.execute(
                    "UPDATE ad_events SET status='checkin_claimed' WHERE id=?", (ad_row[0],))

                new_streak = ((row[1] or 0) + 1) if (row and row[0] == yesterday) else 1

                await db.execute(
                    "INSERT INTO daily_checkins (user_id, last_claim_date, streak) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET last_claim_date=excluded.last_claim_date, streak=excluded.streak",
                    (user_id, today, new_streak),
                )
                await db.execute(
                    "UPDATE users SET balance = ROUND(balance + ?, 2) WHERE user_id = ?",
                    (reward, user_id),
                )
                await db.execute("COMMIT")
                return True, "", new_streak
            except Exception:
                await db.execute("ROLLBACK")
                raise

    @staticmethod
    async def mark_notified_today(user_id: int, notify_type: str) -> bool:
        """Records that user_id received notify_type today. Returns True the
        first time (caller should send), False if already sent today."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO daily_notify_log (user_id, notify_type, notify_date) "
                "VALUES (?, ?, date('now'))",
                (user_id, notify_type),
            )
            await db.commit()
            return cur.rowcount > 0

    @staticmethod
    async def mark_banner_clicked(user_id: int):
        """Records that the Mini App was just backgrounded (blur /
        visibilitychange) while an AdsGram ad was showing — real
        evidence the user actually left the app for the banner/link,
        rather than just the SDK promise resolving on its own."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO banner_click_signals (user_id, clicked_at) VALUES (?, datetime('now')) "
                "ON CONFLICT(user_id) DO UPDATE SET clicked_at=excluded.clicked_at",
                (user_id,),
            )
            await db.commit()

    @staticmethod
    async def consume_recent_banner_click(user_id: int, within_seconds: int = 120) -> bool:
        """Returns True and clears the signal only if a genuine click-away
        was recorded within the last `within_seconds` — consuming it so
        one real click can't be reused to cover multiple ad rewards."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT clicked_at FROM banner_click_signals WHERE user_id=?", (user_id,)
            )
            row = await cur.fetchone()
            if not row or not row[0]:
                return False
            try:
                elapsed = (datetime.utcnow() - datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")).total_seconds()
            except Exception:
                return False
            if not (0 <= elapsed <= within_seconds):
                return False
            await db.execute("DELETE FROM banner_click_signals WHERE user_id=?", (user_id,))
            await db.commit()
            return True

    @staticmethod
    async def clear_banner_click_signal(user_id: int):
        """Wipes any leftover click signal for this user. Called the
        moment a brand-new ad attempt is granted (/api/ads/claim), so a
        stale signal left over from an earlier, already-abandoned attempt
        can never bleed into a later, unrelated attempt's reward check —
        only a click that happens during THIS specific armed session can
        leave a signal a later postback will see."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM banner_click_signals WHERE user_id=?", (user_id,))
            await db.commit()

    @staticmethod
    async def mark_daily_checkin_ad_pending(user_id: int):
        """Records that the next AdsGram video-ad completion for this
        user was shown by the Daily Check-in flow, so the postback
        handler can skip the separate ad_reward_amount payout for it
        (the daily bonus itself is credited by claim_daily_checkin_atomic
        — without this, both rewards land on the same ad view)."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO daily_checkin_ad_pending (user_id, marked_at) VALUES (?, datetime('now')) "
                "ON CONFLICT(user_id) DO UPDATE SET marked_at=excluded.marked_at",
                (user_id,),
            )
            await db.commit()

    @staticmethod
    async def consume_daily_checkin_ad_pending(user_id: int, within_seconds: int = 300) -> bool:
        """Returns True and clears the mark only if it was set within the
        last `within_seconds` — consuming it so one Daily Check-in ad
        view can only ever suppress the ad_reward_amount payout for the
        one postback it belongs to, never a later, unrelated one."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT marked_at FROM daily_checkin_ad_pending WHERE user_id=?", (user_id,)
            )
            row = await cur.fetchone()
            if not row or not row[0]:
                return False
            try:
                elapsed = (datetime.utcnow() - datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")).total_seconds()
            except Exception:
                return False
            await db.execute("DELETE FROM daily_checkin_ad_pending WHERE user_id=?", (user_id,))
            await db.commit()
            if not (0 <= elapsed <= within_seconds):
                return False
            return True

    @staticmethod
    async def count_banner_click_rewards_today() -> int:
        """How many rewards today were actually gated on (and passed) a
        verified click — used against ad_banner_click_daily_cap."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM banner_click_reward_log WHERE date(credited_at)=date('now')"
            )
            row = await cur.fetchone()
            return row[0] if row else 0

    @staticmethod
    async def log_banner_click_reward(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO banner_click_reward_log (user_id, credited_at) VALUES (?, datetime('now'))",
                (user_id,),
            )
            await db.commit()

    # ── Tads gate — reconciles AdsGram's S2S postback with the Tads ad
    # shown right after it, whichever side reports in first ─────────────
    @staticmethod
    async def clear_ad_reward_gate(user_id: int):
        """Wipes any leftover AdsGram/Tads gate state for this user.
        Called the moment a brand-new Watch Ad attempt is granted, so a
        stale signal from an earlier, already-abandoned attempt can never
        be mistaken for evidence belonging to a later, unrelated one."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM ad_reward_gate WHERE user_id=?", (user_id,))
            await db.commit()

    @staticmethod
    async def gate_adsgram_confirmation(user_id: int, payload: dict, within_seconds: int = 300) -> tuple[bool, str]:
        """Called from the AdsGram S2S reward postback when a Tads gate is
        required (tads_enabled + tads_widget_id configured). If the user
        already reported a fresh Tads outcome (completed or no_fill),
        pays out immediately. Otherwise stores AdsGram's confirmation +
        the params claim_video_ad_atomic needs, and waits — the payout
        happens the moment record_tads_result sees this fresh entry,
        whichever call lands second."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT tads_status, tads_at FROM ad_reward_gate WHERE user_id=?", (user_id,)
            )
            row = await cur.fetchone()
            tads_status, tads_at = (row[0], row[1]) if row else (None, None)
            ready = False
            if tads_status and tads_at:
                try:
                    elapsed = (datetime.utcnow() - datetime.strptime(tads_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
                    ready = 0 <= elapsed <= within_seconds
                except Exception:
                    ready = False
            if ready:
                await db.execute("DELETE FROM ad_reward_gate WHERE user_id=?", (user_id,))
            else:
                await db.execute(
                    "INSERT INTO ad_reward_gate (user_id, adsgram_confirmed_at, adsgram_payload, tads_status, tads_at) "
                    "VALUES (?, datetime('now'), ?, NULL, NULL) "
                    "ON CONFLICT(user_id) DO UPDATE SET adsgram_confirmed_at=excluded.adsgram_confirmed_at, "
                    "adsgram_payload=excluded.adsgram_payload, tads_status=NULL, tads_at=NULL",
                    (user_id, json.dumps(payload)),
                )
            await db.commit()
        if ready:
            return await DataEngine.claim_video_ad_atomic(user_id, **payload)
        return False, "waiting_tads"

    @staticmethod
    async def record_tads_result(user_id: int, status: str, within_seconds: int = 300) -> bool:
        """Records the client-reported Tads outcome ('completed' or
        'no_fill'). If AdsGram's own S2S confirmation already arrived and
        is still fresh, finishes the payout right now (the counterpart to
        gate_adsgram_confirmation). Returns True if this call itself
        triggered the payout."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT adsgram_confirmed_at, adsgram_payload FROM ad_reward_gate WHERE user_id=?", (user_id,)
            )
            row = await cur.fetchone()
            confirmed_at, payload_json = (row[0], row[1]) if row else (None, None)
            ready = False
            payload = None
            if confirmed_at and payload_json:
                try:
                    elapsed = (datetime.utcnow() - datetime.strptime(confirmed_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
                    ready = 0 <= elapsed <= within_seconds
                    payload = json.loads(payload_json)
                except Exception:
                    ready = False
            if ready:
                await db.execute("DELETE FROM ad_reward_gate WHERE user_id=?", (user_id,))
            else:
                await db.execute(
                    "INSERT INTO ad_reward_gate (user_id, adsgram_confirmed_at, adsgram_payload, tads_status, tads_at) "
                    "VALUES (?, NULL, NULL, ?, datetime('now')) "
                    "ON CONFLICT(user_id) DO UPDATE SET tads_status=excluded.tads_status, tads_at=excluded.tads_at",
                    (user_id, status),
                )
            await db.commit()
        if ready and payload:
            ok, _ = await DataEngine.claim_video_ad_atomic(user_id, **payload)
            return ok
        return False

    @staticmethod
    async def log_ad_click(user_id: int, kind: str):
        """Lightweight analytics-only row — logged the moment a user taps
        'Watch Ad' / opens the ad, BEFORE we know whether they'll finish
        it. Never touched by the reward/daily-limit logic (that only
        ever counts status='completed'), so this is purely for the
        admin 'clicks vs completions' dashboard."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO ad_events (user_id, kind, status) VALUES (?, ?, 'clicked')",
                (user_id, kind),
            )
            await db.commit()

    @staticmethod
    async def get_ads_admin_analytics(video_daily_limit: int, direct_link_daily_limit: int) -> dict:
        """Admin-facing ad analytics: how many people clicked vs finished
        today, plus who has hit today's daily limit (with username)."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row

            async def scalar(query, params=()):
                cur = await db.execute(query, params)
                row = await cur.fetchone()
                return row[0] if row else 0

            video_clicks = await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM ad_events "
                "WHERE kind='video' AND status='clicked' AND date(started_at)=date('now')"
            )
            video_completed = await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM ad_events "
                "WHERE kind='video' AND status='completed' AND date(completed_at)=date('now')"
            )
            dl_opened = await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM ad_events "
                "WHERE kind='direct_link' AND date(started_at)=date('now')"
            )
            dl_completed = await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM ad_events "
                "WHERE kind='direct_link' AND status='completed' AND date(completed_at)=date('now')"
            )
            checkin_ad_views_today = await scalar(
                "SELECT COUNT(*) FROM ad_events "
                "WHERE kind='video' AND status='completed_checkin' AND date(completed_at)=date('now')"
            )
            checkin_ad_users_today = await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM ad_events "
                "WHERE kind='video' AND status='completed_checkin' AND date(completed_at)=date('now')"
            )

            async def limit_reached(kind: str, limit: int):
                cur = await db.execute(
                    "SELECT u.user_id, u.username, u.full_name, COUNT(*) AS watched "
                    "FROM ad_events e JOIN users u ON u.user_id = e.user_id "
                    "WHERE e.kind=? AND e.status='completed' AND date(e.completed_at)=date('now') "
                    "GROUP BY e.user_id HAVING watched >= ? ORDER BY watched DESC",
                    (kind, limit),
                )
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

            async def full_breakdown(kind: str, limit: int, cap: int = 200):
                """Every user with at least one completed ad today, not
                just the ones who hit the full daily limit — the count
                card up top counts everyone, so the list below it should
                be able to account for the same people, not just the
                subset that maxed out."""
                cur = await db.execute(
                    "SELECT u.user_id, u.username, u.full_name, COUNT(*) AS watched "
                    "FROM ad_events e JOIN users u ON u.user_id = e.user_id "
                    "WHERE e.kind=? AND e.status='completed' AND date(e.completed_at)=date('now') "
                    "GROUP BY e.user_id ORDER BY watched DESC LIMIT ?",
                    (kind, cap),
                )
                rows = await cur.fetchall()
                return [dict(r) | {"limit": limit, "reached_limit": r["watched"] >= limit} for r in rows]

            video_limit_users = await limit_reached("video", video_daily_limit)
            dl_limit_users    = await limit_reached("direct_link", direct_link_daily_limit)
            video_breakdown   = await full_breakdown("video", video_daily_limit)
            dl_breakdown      = await full_breakdown("direct_link", direct_link_daily_limit)

            return {
                "video_clicks_today":              video_clicks,
                "video_completed_today":            video_completed,
                "direct_link_opened_today":          dl_opened,
                "direct_link_completed_today":       dl_completed,
                "checkin_ad_views_today":            checkin_ad_views_today,
                "checkin_ad_users_today":            checkin_ad_users_today,
                "video_limit_reached_users":         video_limit_users,
                "direct_link_limit_reached_users":   dl_limit_users,
                "video_breakdown":                   video_breakdown,
                "direct_link_breakdown":             dl_breakdown,
            }

    @staticmethod
    async def start_ad_event(user_id: int, kind: str) -> int:
        """Opens a 'pending' event — used for Direct Link, which has no
        SDK callback, so we gate the reward behind a server-side wait
        timer instead (same trust model as 'fake' tasks)."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO ad_events (user_id, kind, status) VALUES (?, ?, 'pending')",
                (user_id, kind),
            )
            await db.commit()
            return cur.lastrowid

    @staticmethod
    async def get_ad_event(event_id: int, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM ad_events WHERE id=? AND user_id=?", (event_id, user_id)
            )
            return await cur.fetchone()

    @staticmethod
    async def complete_ad_event(event_id: int, reward: float) -> bool:
        """Atomically transitions an ad_events row to 'completed'. Returns
        True only for the call that actually performs that transition —
        the WHERE guard means a concurrent/duplicate claim for the same
        event_id can never both succeed, closing a double-reward race."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE ad_events SET status='completed', reward=?, completed_at=datetime('now') "
                "WHERE id=? AND status != 'completed'",
                (reward, event_id),
            )
            await db.commit()
            return cur.rowcount > 0

    @staticmethod
    async def claim_direct_link_atomic(
        event_id: int, user_id: int, reward: float, daily_limit: int
    ) -> tuple[bool, str]:
        """Direct Link claim, but daily_limit-safe: 'open' can only check
        against already-COMPLETED events (a pending event isn't proof of
        anything yet), so if the limit isn't re-checked again right here —
        atomically, in the same transaction as the completion write — a
        user can open several Direct Link slots before any of them are
        claimed (each 'open' call sees the same low completed-count) and
        then claim all of them afterwards with nothing left to stop them,
        sailing straight past daily_limit. This closes that gap: the
        count and the completing UPDATE happen inside one BEGIN EXCLUSIVE,
        so no concurrent/queued claim can slip through once the cap is
        hit, mirroring what claim_video_ad_atomic already does for video."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN EXCLUSIVE")
            try:
                cur = await db.execute(
                    "SELECT status FROM ad_events WHERE id=? AND user_id=? AND kind='direct_link'",
                    (event_id, user_id),
                )
                row = await cur.fetchone()
                if not row or row[0] == "completed":
                    await db.execute("ROLLBACK")
                    return False, "not_claimable"

                cur2 = await db.execute(
                    "SELECT COUNT(*) FROM ad_events WHERE user_id=? AND kind='direct_link' "
                    "AND status='completed' AND date(completed_at)=date('now')",
                    (user_id,),
                )
                watched_today = (await cur2.fetchone())[0]
                if watched_today >= daily_limit:
                    await db.execute("ROLLBACK")
                    return False, "daily_limit_reached"

                await db.execute(
                    "UPDATE ad_events SET status='completed', reward=?, completed_at=datetime('now') "
                    "WHERE id=? AND status != 'completed'",
                    (reward, event_id),
                )
                await db.commit()
                return True, ""
            except Exception:
                await db.execute("ROLLBACK")
                raise

    @staticmethod
    async def remove_force_channel(row_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM force_channels WHERE id = ?", (row_id,))
            await db.commit()

    @staticmethod
    async def get_user_withdrawals(user_id: int, limit: int = 30):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
            return await cur.fetchall()

    # ── TASK SYSTEM ─────────────────────────────────────────────────────────
    @staticmethod
    async def create_task(title: str, channel_id: str, invite_link: str, task_type: str, reward: float,
                           created_by: int = 0, budget_slots: int = 0, review_status: str = "approved",
                           instructions: str = "", screenshot_count: int = 1) -> int:
        # Admin-created tasks (created_by=0) publish immediately (is_active=1).
        # User-created tasks start pending review (is_active=0) until an
        # admin approves them — this stops a scam/spam channel going live
        # while an admin's balance is already sitting in escrow for it.
        is_active = 1 if review_status == "approved" else 0
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM tasks")
            pos = (await cur.fetchone())[0]
            cur2 = await db.execute(
                "INSERT INTO tasks (title, channel_id, invite_link, task_type, reward, position, "
                "created_by, budget_slots, slots_used, review_status, is_active, instructions, screenshot_count) "
                "VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?)",
                (title, channel_id, invite_link, task_type, reward, pos,
                 created_by, budget_slots, review_status, is_active, instructions, screenshot_count),
            )
            await db.commit()
            return cur2.lastrowid

    @staticmethod
    async def get_user_tasks(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tasks WHERE created_by = ? ORDER BY id DESC", (user_id,)
            )
            return await cur.fetchall()

    @staticmethod
    async def get_pending_review_tasks():
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tasks WHERE review_status = 'pending' ORDER BY id ASC"
            )
            return await cur.fetchall()

    @staticmethod
    async def increment_task_slot(task_id: int) -> bool:
        """
        Records that one more person completed a user-created task's slot.
        Auto-deactivates the task once its paid-for budget is fully used.
        Returns True if this call sold out the last slot.
        """
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            # Capped increment (BUG-REPORT #3): the UPDATE only lands
            # while a paid slot actually remains, so slots_used can never
            # run past budget_slots even under concurrent claims.
            await db.execute(
                "UPDATE tasks SET slots_used = slots_used + 1 WHERE id = ? "
                "AND (budget_slots <= 0 OR slots_used < budget_slots)", (task_id,))
            cur = await db.execute("SELECT slots_used, budget_slots FROM tasks WHERE id = ?", (task_id,))
            row = await cur.fetchone()
            sold_out = bool(row and row["budget_slots"] > 0 and row["slots_used"] >= row["budget_slots"])
            if sold_out:
                await db.execute("UPDATE tasks SET is_active = 0 WHERE id = ?", (task_id,))
            await db.commit()
            return sold_out

    @staticmethod
    async def try_take_task_slot(task_id: int) -> tuple[bool, bool]:
        """Atomically consumes one escrow slot of a user-created task.
        Returns (taken, sold_out): taken=False means every paid slot was
        already gone — the caller must NOT pay a reward, because the
        creator's escrow only ever covered budget_slots completions and
        the cancel-refund math assumes slots_used never exceeds it
        (BUG-REPORT #3)."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "UPDATE tasks SET slots_used = slots_used + 1 WHERE id = ? "
                "AND budget_slots > 0 AND slots_used < budget_slots", (task_id,))
            if cur.rowcount == 0:
                await db.commit()
                return False, True
            row = await (await db.execute(
                "SELECT slots_used, budget_slots FROM tasks WHERE id = ?", (task_id,))).fetchone()
            sold_out = bool(row and row["budget_slots"] > 0 and row["slots_used"] >= row["budget_slots"])
            if sold_out:
                await db.execute("UPDATE tasks SET is_active = 0 WHERE id = ?", (task_id,))
            await db.commit()
            return True, sold_out

    @staticmethod
    async def release_task_slot(task_id: int):
        """Gives back a slot taken by try_take_task_slot when the claim
        that took it ultimately fails (e.g. the progress transition lost
        a race) and no reward was paid for it."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tasks SET slots_used = MAX(0, slots_used - 1) WHERE id = ?", (task_id,))
            await db.commit()

    @staticmethod
    async def get_tasks(active_only: bool = True):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            q = "SELECT * FROM tasks"
            if active_only:
                q += " WHERE is_active = 1"
            q += " ORDER BY position ASC, id ASC"
            cur = await db.execute(q)
            return await cur.fetchall()

    @staticmethod
    async def get_task(task_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            return await cur.fetchone()

    @staticmethod
    async def update_task(task_id: int, **fields):
        if not fields:
            return
        allowed = {"title", "channel_id", "invite_link", "task_type", "reward", "is_active", "position",
                   "budget_slots", "slots_used", "review_status", "instructions", "screenshot_count"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [task_id]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", vals)
            await db.commit()

    @staticmethod
    async def cancel_user_task_atomic(task_id: int, created_by: int) -> tuple[bool, float]:
        """Atomically flips a task to 'cancelled' and pays its refund, all
        in one BEGIN EXCLUSIVE transaction. Without this, two concurrent
        /api/tasks/cancel calls for the same task could both read
        review_status='approved' before either commits, and both pay out
        a refund for the same unused slots."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN EXCLUSIVE")
            try:
                cur = await db.execute(
                    "SELECT created_by, review_status, budget_slots, slots_used, reward "
                    "FROM tasks WHERE id=?", (task_id,)
                )
                row = await cur.fetchone()
                if not row or row[0] != created_by or row[1] not in ("pending", "approved"):
                    await db.execute("ROLLBACK")
                    return False, 0.0
                budget_slots, slots_used, reward = row[2], row[3], row[4]
                unused_slots = max(0, int(budget_slots) - int(slots_used))
                refund = round(unused_slots * float(reward), 2)
                await db.execute(
                    "UPDATE tasks SET is_active=0, review_status='cancelled' WHERE id=?",
                    (task_id,),
                )
                if refund > 0:
                    await db.execute(
                        "UPDATE users SET balance = ROUND(balance + ?, 2) WHERE user_id = ?",
                        (refund, created_by),
                    )
                await db.execute("COMMIT")
                return True, refund
            except Exception:
                await db.execute("ROLLBACK")
                raise

    @staticmethod
    async def review_user_task_atomic(task_id: int, approve: bool) -> tuple[bool, dict | None]:
        """Atomically transitions a user-created task out of 'pending'
        review. Exactly one caller wins: the status flip AND (on reject)
        the full escrow refund happen in one BEGIN EXCLUSIVE transaction
        (BUG-REPORT #10 — the old read -> refund -> update sequence in
        /api/admin/tasks/review let concurrent reject+reject refund the
        escrow twice, and approve+reject both apply). Returns
        (won, {"created_by":…, "refund":…})."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN EXCLUSIVE")
            try:
                cur = await db.execute(
                    "SELECT created_by, budget_slots, reward FROM tasks "
                    "WHERE id=? AND review_status='pending'",
                    (task_id,),
                )
                row = await cur.fetchone()
                if not row:
                    await db.execute("ROLLBACK")
                    return False, None
                created_by, budget_slots, reward = row[0], row[1], float(row[2])
                if approve:
                    await db.execute(
                        "UPDATE tasks SET review_status='approved', is_active=1 WHERE id=?",
                        (task_id,))
                    refund = 0.0
                else:
                    refund = round(int(budget_slots) * reward, 2)
                    await db.execute(
                        "UPDATE tasks SET review_status='rejected', is_active=0 WHERE id=?",
                        (task_id,))
                    if refund > 0 and created_by:
                        await db.execute(
                            "UPDATE users SET balance = ROUND(balance + ?, 2) WHERE user_id = ?",
                            (refund, created_by),
                        )
                await db.execute("COMMIT")
                return True, {"created_by": created_by, "refund": refund}
            except Exception:
                await db.execute("ROLLBACK")
                raise

    @staticmethod
    async def delete_task(task_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            await db.execute("DELETE FROM task_progress WHERE task_id = ?", (task_id,))
            await db.commit()

    @staticmethod
    async def reorder_tasks(order: list):
        async with aiosqlite.connect(DB_PATH) as db:
            for idx, tid in enumerate(order):
                await db.execute("UPDATE tasks SET position = ? WHERE id = ?", (idx, int(tid)))
            await db.commit()

    @staticmethod
    async def get_task_progress(user_id: int, task_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM task_progress WHERE user_id = ? AND task_id = ?", (user_id, task_id)
            )
            return await cur.fetchone()

    @staticmethod
    async def get_all_task_progress_for_user(user_id: int) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM task_progress WHERE user_id = ?", (user_id,))
            rows = await cur.fetchall()
            return {r["task_id"]: r for r in rows}

    @staticmethod
    async def mark_task_joined(user_id: int, task_id: int):
        # INSERT OR IGNORE: the wait-timer only starts once. Re-tapping
        # "Join" must NOT reset the countdown (that would let a user farm
        # infinite retries to dodge the wait).
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO task_progress (user_id, task_id, status, joined_at) "
                "VALUES (?, ?, 'joined', datetime('now'))",
                (user_id, task_id),
            )
            await db.commit()

    @staticmethod
    async def mark_task_checked(user_id: int, task_id: int):
        # Only a user who has passed a *real* verification (membership
        # confirmed, or wait-timer elapsed for a fake task) is allowed to
        # move from 'joined' -> 'checked'. /api/tasks/claim requires this
        # status before it will pay out, so the reward can never be
        # farmed by calling /api/tasks/claim directly and skipping /check.
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE task_progress SET status='checked' WHERE user_id = ? AND task_id = ? AND status = 'joined'",
                (user_id, task_id),
            )
            await db.commit()

    @staticmethod
    async def mark_task_completed(user_id: int, task_id: int) -> bool:
        """Atomically transitions task_progress 'checked' -> 'completed'.
        Returns True only for the call that actually performs that
        transition. If two /api/tasks/claim requests for the same task
        race each other (double-tap, retry), the guarded WHERE means only
        one UPDATE can ever match a row — the other gets rowcount 0 and
        must not pay out, closing a double-reward race."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE task_progress SET status='completed', completed_at=datetime('now') "
                "WHERE user_id = ? AND task_id = ? AND status='checked'",
                (user_id, task_id),
            )
            await db.commit()
            return cur.rowcount > 0

    # ── Manual/screenshot-review tasks ──────────────────────────────────
    # One row per submission attempt (not per task), so a rejected user
    # can resubmit without losing the earlier attempt's history.
    @staticmethod
    async def create_manual_submission(user_id: int, task_id: int, screenshots: list) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO manual_submissions (task_id, user_id, screenshots, status) "
                "VALUES (?, ?, ?, 'pending')",
                (task_id, user_id, json.dumps(screenshots)),
            )
            await db.commit()
            return cur.lastrowid

    @staticmethod
    async def set_manual_submission_screenshots(submission_id: int, file_ids: list):
        """Stores Telegram file_ids (a few dozen bytes each) once the
        photos have been relayed to admins — never the image bytes
        themselves, so this table stays tiny no matter how many
        screenshots come in."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE manual_submissions SET screenshots = ? WHERE id = ?",
                (json.dumps(file_ids), submission_id),
            )
            await db.commit()

    @staticmethod
    async def get_latest_manual_submission(user_id: int, task_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM manual_submissions WHERE user_id = ? AND task_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, task_id),
            )
            return await cur.fetchone()

    @staticmethod
    async def get_latest_manual_submissions_for_user(user_id: int) -> dict:
        """One row per task_id — the most recent submission attempt only.
        Used to compute each manual task's status in /api/tasks without a
        query per task."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM manual_submissions WHERE user_id = ? AND id IN "
                "(SELECT MAX(id) FROM manual_submissions WHERE user_id = ? GROUP BY task_id)",
                (user_id, user_id),
            )
            rows = await cur.fetchall()
            return {r["task_id"]: r for r in rows}

    @staticmethod
    async def get_manual_submission(submission_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM manual_submissions WHERE id = ?", (submission_id,))
            return await cur.fetchone()

    @staticmethod
    async def get_pending_manual_submissions():
        """Joined with tasks/users so the admin queue has everything it
        needs (title, username) without extra round trips."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT ms.id, ms.task_id, ms.user_id, ms.screenshots, ms.submitted_at, "
                "t.title AS task_title, u.username AS username "
                "FROM manual_submissions ms "
                "JOIN tasks t ON t.id = ms.task_id "
                "LEFT JOIN users u ON u.user_id = ms.user_id "
                "WHERE ms.status = 'pending' ORDER BY ms.id ASC"
            )
            rows = await cur.fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["screenshots"] = json.loads(d["screenshots"])
                except Exception:
                    d["screenshots"] = []
                out.append(d)
            return out

    @staticmethod
    async def approve_manual_submission(submission_id: int) -> bool:
        # Guarded WHERE status='pending' — same double-approval race
        # protection as mark_task_completed above.
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE manual_submissions SET status='approved', reviewed_at=datetime('now') "
                "WHERE id = ? AND status='pending'",
                (submission_id,),
            )
            await db.commit()
            return cur.rowcount > 0

    @staticmethod
    async def reject_manual_submission(submission_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE manual_submissions SET status='rejected', reviewed_at=datetime('now') "
                "WHERE id = ? AND status='pending'",
                (submission_id,),
            )
            await db.commit()
            return cur.rowcount > 0

# ─────────────────────────────────────────────────────────────────────────────
# FRAUD DETECTION ENGINE  (v5 — correlation-based)
#
# ዓላማ: አንድ ሰው = አንድ አካውንት — ግን ንፁ ተጠቃሚን ላለማጥፋት "ብቻውን ምንም signal ban
# አያደርግም" የሚል መርህ ይከተላል። ይልቁንስ የ NEW ምዝገባ ምልክቶች ካለፈ ምዝገባ ጋር
# **በስንት ገለልተኛ ምድቦች ላይ እንደሚገጣጠሙ** (correlation) እየቆጠረ ነው ውሳኔ
# የሚሰጠው። እያንዳንዱ ምድብ ብቻውን በአጋጣሚ ሊገጣጠም ይችላል (ተመሳሳይ ስልክ ሞዴል፣
# ተመሳሳይ WiFi፣ cache clear...) — ግን ብዙ ምድቦች ላይ በተመሳሳይ ጊዜ ከአንድ ሰው ጋር
# ቢገጣጠሙ ብቻ እውነተኛ clone/multi-account ነው ብሎ በእርግጠኝነት መደምደም ይቻላል።
#
# MATCH CATEGORIES (እያንዳንዱ ነጥብ 1 ነው)፦
#   • fingerprint ሙሉ ለሙሉ ይገጣጠማል
#   • canvas_hash + webgl_hash ሁለቱም ይገጣጠማሉ (የ"ሃርድዌር" ምድብ)
#   • ip_address ይገጣጠማል
#   • tg_platform + tg_version ሁለቱም ይገጣጠማሉ
#
# DECISION (ከ ANY ነባር user ጋር ሲነጻጸር)፦
#   • Self-referral (referrer_id == new_user_id)        → BAN (ሁልጊዜ ግልጽ ነው)
#   • Weighted score ≥ 4/6 possible points, correlating to the SAME other
#     user                                                    → BAN
#     (fingerprint=2, hardware=1, ip=1, tg_device=1 — the combined
#     `fingerprint` hash is far more unique than any single sub-signal, since
#     it also folds in the full User-Agent string, timezone, cpu core count,
#     and language; `hardware` alone is just canvas+webgl and, on its own,
#     commonly collides across totally unrelated people who own the same
#     inexpensive phone model; `ip` and `tg_device` collide even more easily
#     — shared WiFi/CGNAT and "everyone's on the same Telegram Android
#     build" are both completely normal. Requiring the strong fingerprint
#     signal (or hardware) to be part of the match, not just any 3 weak
#     signals, keeps two unrelated people who happen to share a phone model
#     + network + app version from being auto-banned.)
#   • Score 1-3                                          → LOG ብቻ (admin review)
#   • referrer ጋር ብቻ: fingerprint=2, ip=1, hw=1 — ድምር ≥ 3 ሲደርስ         → BAN
#     ("ራሱ ራሱን ጋበዘ")
#   • referrer ጋር ድምር < 3                                  → LOG ብቻ
#   • IP farm: ብዙ users በአንድ IP ላይ *እና* ጥቂት የተለያዩ
#     fingerprints (ስለዚህ duplicate ስክሪፕት farm ይመስላል)      → BAN
#     ብዙ users በአንድ IP ላይ ግን እያንዳንዱ የተለየ fingerprint
#     አለው (ለምሳሌ CGNAT/mobile-data shared IP)               → LOG ብቻ፣ ban የለም
#
# ይሄ ከ v4 የበለጠ ጥንቃቄ ያለው ነው፦ ምንም single signal (fingerprint ብቻ፣ IP ብቻ፣
# ስልክ ሞዴል ብቻ) ብቻውን ሰውን አያስታግድም።
# ─────────────────────────────────────────────────────────────────────────────

CORRELATION_BAN_THRESHOLD = 4   # unrelated-account match at/above this = strong review flag (never an auto-ban; only the inviter↔invitee pair check blocks)
MAX_USERS_PER_IP          = 10  # ከዚህ በላይ → ጠቅሰ ብቻ (review)
MAX_USERS_PER_IP_BAN      = 40  # ከዚህ በላይ *እና* ዝቅተኛ fingerprint diversity → BAN
IP_FARM_MIN_FP_RATIO      = 0.5 # distinct_fp / user_count ከዚህ በታች ከሆነ "duplicate ስክሪፕት" ይመስላል

# Per-category weights — fingerprint (the combined hash) is far more
# unique than any single sub-signal and therefore worth more toward the
# ban threshold. See the comment block above for why.
WEIGHT_FINGERPRINT = 2
WEIGHT_HARDWARE    = 1
WEIGHT_IP          = 1
WEIGHT_TG_DEVICE   = 1


def _category_match_score(
    fingerprint: str, fp_ok: bool,
    canvas_hash: str, webgl_hash: str, hw_ok: bool,
    client_ip: str, ip_ok: bool,
    tg_platform: str, tg_version: str, tg_ok: bool,
    row: dict,
) -> tuple[int, list]:
    """Weighted sum of how many independent signal categories match a
    single candidate row — see WEIGHT_* constants above."""
    score = 0
    matched = []
    if fp_ok and row.get("fingerprint") and row["fingerprint"] == fingerprint:
        score += WEIGHT_FINGERPRINT
        matched.append("fingerprint")
    if hw_ok and row.get("canvas_hash") == canvas_hash and row.get("webgl_hash") == webgl_hash:
        score += WEIGHT_HARDWARE
        matched.append("hardware")
    if ip_ok and row.get("ip_address") and row["ip_address"] == client_ip:
        score += WEIGHT_IP
        matched.append("ip")
    if tg_ok and row.get("tg_platform") == tg_platform and row.get("tg_version") == tg_version:
        score += WEIGHT_TG_DEVICE
        matched.append("tg_device")
    return score, matched


async def evaluate_clone_risk(
    new_user_id: int,
    referrer_id: int,
    client_ip: str,
    fingerprint: str,
    tg_platform: str = "",
    tg_version: str = "",
    tg_app_version: str = "",
    canvas_hash: str = "",
    webgl_hash: str = "",
    screen_sig: str = "",
    username: str = "",
) -> tuple[bool, str, str]:
    """
    Returns (should_ban: bool, reason: str, detail: str).
    Owner policy (2026-11-26): blocking is decided ONLY inside the
    inviter↔invitee pair — self-invite, or the invitee sharing the
    inviter's own device (section 3). A signal match against an
    UNRELATED account never blocks: same phone model + shared CGNAT /
    Wi-Fi IP + same Telegram version is normal for strangers, so those
    matches are logged for admin review instead. (Section 4's IP-farm
    rule also still blocks, but only on device REUSE at scale — many
    accounts cycling a few fingerprints — never on distinct phones.)
    `detail` carries the matched_uid/categories/score info
    (plus the flagged user's own @username, when known) so the admin fraud
    log can show exactly who a ban correlated with.
    """
    uname_tag = f"flagged_username=@{username}" if username else "flagged_username=none"

    # ── 1. Self-referral ──────────────────────────────────────────────────
    if referrer_id and referrer_id == new_user_id:
        return True, "self_invite", f"referrer_id={referrer_id} {uname_tag}"

    ip_ok = bool(client_ip) and client_ip not in ("127.0.0.1", "::1", "unknown")
    fp_ok = bool(fingerprint) and fingerprint not in ("undefined", "null", "")
    hw_ok = bool(canvas_hash) and len(canvas_hash) > 8 and bool(webgl_hash) and len(webgl_hash) > 8
    tg_ok = bool(tg_platform) and bool(tg_version)

    # ── 2. Correlate against every candidate that shares ≥1 signal ────────
    if fp_ok or hw_ok or ip_ok or tg_ok:
        conditions = []
        params = []
        if fp_ok:
            conditions.append("fingerprint = ?")
            params.append(fingerprint)
        if hw_ok:
            conditions.append("(canvas_hash = ? AND webgl_hash = ?)")
            params.extend([canvas_hash, webgl_hash])
        if ip_ok:
            conditions.append("ip_address = ?")
            params.append(client_ip)
        if tg_ok:
            conditions.append("(tg_platform = ? AND tg_version = ?)")
            params.extend([tg_platform, tg_version])

        query = (
            "SELECT user_id, fingerprint, canvas_hash, webgl_hash, ip_address, "
            "tg_platform, tg_version FROM verifications "
            f"WHERE ({' OR '.join(conditions)}) AND user_id != ?"
        )
        params.append(new_user_id)

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(query, params)
            candidates = await cur.fetchall()

        best_score, best_uid, best_matched = 0, None, []
        for row in candidates:
            score, matched = _category_match_score(
                fingerprint, fp_ok, canvas_hash, webgl_hash, hw_ok,
                client_ip, ip_ok, tg_platform, tg_version, tg_ok, dict(row)
            )
            if score > best_score:
                best_score, best_uid, best_matched = score, row["user_id"], matched

        if best_score >= 1:
            # Never auto-block on an unrelated-account match (see the
            # policy note in this function's docstring) — log it for
            # admin review, strongly worded when the correlation is high.
            strong = best_score >= CORRELATION_BAN_THRESHOLD
            logger.warning(
                f"[FRAUD-WARN] {'Strong' if strong else 'Partial'} signal match "
                f"vs unrelated account (not banning): new_uid={new_user_id} "
                f"matches uid={best_uid} on {best_matched} (score={best_score})"
            )
            await DataEngine.log_fraud_attempt(
                new_user_id,
                "correlated_match_review" if strong else "partial_signal_match",
                client_ip,
                f"matches_uid={best_uid} categories={best_matched} score={best_score} {uname_tag}"
            )

    # ── 3. Referrer device check ────────────────────────────────────────
    #    Weighted score ≥ 3 against the referrer specifically (fingerprint=2,
    #    ip=1, hardware=1) — needs the strong fingerprint signal plus one
    #    more, since ip+hardware alone (=2, no fingerprint match) isn't
    #    enough. A single matching signal alone (e.g. just sharing IP, just
    #    sharing hardware) is normal for family/friends and is only logged.
    if referrer_id and (fp_ok or ip_ok or hw_ok):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT ip_address, fingerprint, canvas_hash, webgl_hash "
                "FROM verifications WHERE user_id = ?",
                (referrer_id,)
            )
            inv_row = await cur.fetchone()
            if inv_row:
                inv_ip = inv_row["ip_address"] or ""
                inv_fp = inv_row["fingerprint"] or ""
                is_real_ip = inv_ip not in ("", "127.0.0.1", "::1", "unknown", "BYPASS_ADMIN")

                fp_matches_ref = fp_ok and inv_fp and inv_fp == fingerprint
                ip_matches_ref = ip_ok and is_real_ip and inv_ip == client_ip
                hw_matches_ref = (
                    hw_ok and inv_row["canvas_hash"] == canvas_hash
                    and inv_row["webgl_hash"] == webgl_hash
                )

                ref_score = (
                    (WEIGHT_FINGERPRINT if fp_matches_ref else 0)
                    + (WEIGHT_IP if ip_matches_ref else 0)
                    + (WEIGHT_HARDWARE if hw_matches_ref else 0)
                )

                # Weighted like the main correlation check above: fingerprint
                # counts double since it's far more unique than IP or
                # hardware alone. Threshold 3 means either fingerprint +
                # one more signal, or all three weaker signals combined
                # (ip+hw alone = 2, not enough — two unrelated people on the
                # same phone model + shared network shouldn't auto-ban).
                if ref_score >= 3:
                    logger.warning(
                        f"[FRAUD] Multi-signal match with referrer — banning: "
                        f"ref={referrer_id} new={new_user_id} "
                        f"fp={fp_matches_ref} ip={ip_matches_ref} hw={hw_matches_ref} score={ref_score}"
                    )
                    return True, "same_device_as_referrer", (
                        f"referrer={referrer_id} fp={fp_matches_ref} "
                        f"ip={ip_matches_ref} hw={hw_matches_ref} score={ref_score} {uname_tag}"
                    )
                elif ref_score >= 1:
                    logger.warning(
                        f"[FRAUD-WARN] Single-signal match with referrer (not banning): "
                        f"ref={referrer_id} new={new_user_id} "
                        f"fp={fp_matches_ref} ip={ip_matches_ref} hw={hw_matches_ref}"
                    )
                    await DataEngine.log_fraud_attempt(
                        new_user_id, "referrer_signal_match", client_ip,
                        f"referrer={referrer_id} fp={fp_matches_ref} ip={ip_matches_ref} hw={hw_matches_ref} {uname_tag}"
                    )

    # ── 4. IP farm detection — aware of shared-NAT / CGNAT networks ───────
    #    A high user count on one IP is, by itself, completely normal in
    #    Ethiopia where mobile carriers use CGNAT (many real customers
    #    share one public IP). We only treat it as a farm when the SAME IP
    #    also shows low fingerprint diversity — i.e. the same handful of
    #    device fingerprints being reused across many accounts, which is
    #    what a scripted farm looks like. Distinct real phones on a shared
    #    network are never banned for this alone.
    if ip_ok:
        ip_count = await DataEngine.count_ip_users(client_ip)
        if ip_count >= MAX_USERS_PER_IP:
            distinct_fp = await DataEngine.count_ip_distinct_fingerprints(client_ip)
            fp_ratio = (distinct_fp / ip_count) if ip_count else 1.0
            logger.warning(
                f"[FRAUD-WARN] High IP usage: ip={client_ip} count={ip_count} "
                f"distinct_fp={distinct_fp} ratio={fp_ratio:.2f} new_uid={new_user_id}"
            )
            await DataEngine.log_fraud_attempt(
                new_user_id, "high_ip_usage", client_ip,
                f"users={ip_count} distinct_fp={distinct_fp} ratio={fp_ratio:.2f} {uname_tag}"
            )
            if ip_count >= MAX_USERS_PER_IP_BAN and fp_ratio < IP_FARM_MIN_FP_RATIO:
                return True, "ip_farm", f"users={ip_count} distinct_fp={distinct_fp} ratio={fp_ratio:.2f} {uname_tag}"

    return False, "", ""


def extract_real_ip(request: Request) -> str:
    # SECURITY (BUG-REPORT #2): the proxy headers below are only honoured
    # when TRUSTED_PROXY=1 says a trusted proxy (e.g. Cloudflare) really
    # sits in front of this backend and overwrites them. By default the
    # backend is directly exposed (Railway, no Cloudflare), so the headers
    # are fully client-controlled — trusting them let anyone rotate a
    # header value to bypass the banned-IP check, the verify IP cooldown,
    # clone-risk IP correlation and the VPN lookup in /api/verify. The
    # anti-fraud engine now always sees the real direct client IP unless
    # an operator explicitly opts into proxy mode.
    if TRUSTED_PROXY:
        cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
        if cf_ip:
            return cf_ip
        real_ip = request.headers.get("X-Real-IP", "").strip()
        if real_ip:
            return real_ip
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                return parts[0]
    if request.client:
        return request.client.host
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# WORKFLOW STATES
# ─────────────────────────────────────────────────────────────────────────────
class UserWithdrawalWorkflow(StatesGroup):
    select_payout_gateway = State()
    input_cash_volume     = State()
    provide_mobile_digits = State()
    provide_account_title = State()
    provide_binance_uid   = State()
    payout_final_approval = State()

class AdminConsoleWorkflow(StatesGroup):
    modify_referral_bounty   = State()
    modify_minimum_cashout   = State()
    append_mandatory_id      = State()
    append_mandatory_title   = State()
    append_mandatory_url     = State()
    append_noadmin_link      = State()
    append_noadmin_title     = State()
    direct_balance_target_id = State()
    direct_balance_volume    = State()
    broadcast_intel_payload  = State()
    broadcast_confirmation   = State()
    lookup_individual_id     = State()
    ban_individual_id        = State()
    banish_individual_id     = State()
    pardon_individual_full   = State()
    pardon_individual_std    = State()
    write_reject_reason      = State()
    modify_withdrawal_schedule = State()

bot         = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp          = Dispatcher(storage=MemoryStorage())
core_router = Router()

# ── Shared "invite card" builder — used both by /api/invite/share_card
#    (sends it straight into the user's own chat with the bot) and by the
#    inline_query handler below (lets the user pick a friend/group from
#    Telegram's native chat list and have it delivered there directly).
_bot_photo_file_id: str | None = None  # cached after first lookup


async def _get_bot_photo_file_id() -> str | None:
    global _bot_photo_file_id
    if _bot_photo_file_id:
        return _bot_photo_file_id
    try:
        photos = await bot.get_user_profile_photos(BOT_ID or (await bot.get_me()).id, limit=1)
        if photos.total_count > 0:
            _bot_photo_file_id = photos.photos[0][-1].file_id  # largest size
    except Exception:
        pass
    return _bot_photo_file_id


async def _build_invite_card(uid: int):
    uname = BOT_USERNAME or (await bot.get_me()).username
    referral_link = f"https://t.me/{uname}?start={uid}"
    caption = (
        "🎉 <b>I'm earning real money with this bot — and you can too!</b>\n\n"
        "✅ Complete simple tasks\n"
        "✅ Watch short ads\n"
        "✅ Invite friends for bonus rewards\n"
        "✅ Withdraw straight to Telebirr\n\n"
        "Tap the button below to join — it only takes a minute 👇"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Join & Start Earning", url=referral_link)],
    ])
    photo_id = await _get_bot_photo_file_id()
    return caption, kb, photo_id


@core_router.inline_query()
async def handle_invite_inline_query(inline_query: InlineQuery):
    """
    Powers "Share with Friends": the Mini App calls tg.switchInlineQuery(),
    the user picks a friend/group from Telegram's native chat picker, and
    Telegram calls this handler to ask what to actually send there.

    This matters because a manually-forwarded copy of a bot message loses
    its inline keyboard — Telegram only keeps buttons on messages sent
    live by the bot. Answering an inline query IS the bot sending the
    message directly into that chat (just triggered through the picker
    instead of a /start), so the photo + button both survive. This is the
    same mechanism most invite/referral bots rely on for exactly that reason.
    """
    caption, kb, photo_id = await _build_invite_card(inline_query.from_user.id)
    if photo_id:
        result = InlineQueryResultCachedPhoto(
            id=str(inline_query.from_user.id), photo_file_id=photo_id,
            caption=caption, reply_markup=kb,
        )
    else:
        result = InlineQueryResultArticle(
            id=str(inline_query.from_user.id), title="📤 Share your invite",
            description="Tap to send your invite card with a Join button",
            input_message_content=InputTextMessageContent(message_text=caption),
            reply_markup=kb,
        )
    try:
        await bot.answer_inline_query(inline_query.id, results=[result], cache_time=0, is_personal=True)
    except Exception as e:
        logger.warning(f"answer_inline_query failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# DEBUG: catch-all callback logger
#
# Registered as a plain middleware-style outer handler so EVERY incoming
# callback_query is logged before any filter-specific handler runs. If a
# button tap produces zero output in Railway logs, this proves the update
# never reached the bot process at all (Telegram delivery / polling issue,
# not a code-logic bug) — vs. seeing this log line but no further handler
# log, which would point at a filter mismatch instead.
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query.middleware()
async def debug_log_all_callbacks(handler, event: CallbackQuery, data):
    logger.info(f"[CALLBACK-IN] uid={event.from_user.id} data={event.data!r}")
    try:
        return await handler(event, data)
    except Exception as e:
        err_str = str(e)
        # These happen constantly when a user double/triple-taps a button
        # fast (which is exactly what causes the popup the user is
        # complaining about): the handler's actual work (send/update/etc.)
        # already went through on the first tap — Telegram is just
        # rejecting the *second* tap's redundant edit_text/answer call
        # because nothing changed, the query is stale, or the message was
        # already replaced. None of these indicate a real failure, so stay
        # silent instead of alarming the user with "Internal error".
        benign_markers = (
            "message is not modified",
            "query is too old",
            "query id is invalid",
            "message to edit not found",
            "message can't be edited",
            "message to delete not found",
        )
        if any(marker in err_str.lower() for marker in benign_markers):
            logger.info(f"[CALLBACK-BENIGN] uid={event.from_user.id} data={event.data!r}: {e}")
            try:
                await event.answer()
            except Exception:
                pass
            return
        logger.exception(f"[CALLBACK-ERROR] uid={event.from_user.id} data={event.data!r}: {e}")
        try:
            await event.answer("⚠️ Internal error, please try again.", show_alert=True)
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# FORCE JOIN LOGIC
# ─────────────────────────────────────────────────────────────────────────────
async def inspect_compulsory_memberships(user_id: int) -> list:
    channels = await DataEngine.get_force_channels()
    unjoined = []
    for ch in channels:
        if ch["bot_added"] == 1:
            continue
        try:
            m = await bot.get_chat_member(chat_id=ch["channel_id"], user_id=user_id)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED):
                unjoined.append(dict(ch))
        except Exception as e:
            # IMPORTANT: if the bot itself isn't an admin in this channel
            # (or the channel ID is wrong), get_chat_member() always throws
            # — and treating that as "user hasn't joined" locks EVERY
            # regular user out of the bot forever, on every button, while
            # admins (who bypass this gate via BYPASS_ADMIN verification)
            # never notice. So a misconfigured channel must NOT block
            # users; it's logged instead so it's visible to the admin.
            logger.warning(
                f"[GATE] Could not check membership for channel={ch['channel_id']} "
                f"user={user_id}: {e} — skipping this channel's check "
                f"(bot may not be admin there)."
            )
    return unjoined

async def enforce_membership_gate(event, user_id: int) -> bool:
    unjoined = await inspect_compulsory_memberships(user_id)
    is_callback  = isinstance(event, CallbackQuery)
    current_data = event.data if is_callback else ""

    if not is_callback or current_data in ("ui_return_home", ""):
        already_seen_fake = await DataEngine.has_seen_fake_join(user_id)
        if not already_seen_fake:
            all_channels = await DataEngine.get_force_channels()
            for ch in all_channels:
                if ch["bot_added"] == 1:
                    if not any(x['channel_id'] == ch['channel_id'] for x in unjoined):
                        unjoined.append(dict(ch))

    if not unjoined:
        return True

    if any(x.get('bot_added') == 1 for x in unjoined):
        await DataEngine.mark_fake_join_seen(user_id)

    buttons = []
    for ch in unjoined:
        buttons.append([InlineKeyboardButton(text=f"➕ Join: {ch['channel_name']}", url=ch["invite_link"])])
    buttons.append([InlineKeyboardButton(text="✅ Joined", callback_data="ui_revalidate_channels")])

    txt = (
        "👋 <b>Welcome!</b>\n\n"
        "Please join all the channels below, then tap <b>'Joined'</b>."
    )

    if isinstance(event, Message):
        await event.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    elif isinstance(event, CallbackQuery):
        await event.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await event.answer()
    return False

@core_router.callback_query(F.data == "ui_revalidate_channels")
async def process_channel_revalidation(callback: CallbackQuery, state: FSMContext):
    uid      = callback.from_user.id
    channels = await DataEngine.get_force_channels()
    real_unjoined = []
    for ch in channels:
        if ch["bot_added"] == 0:
            try:
                m = await bot.get_chat_member(chat_id=ch["channel_id"], user_id=uid)
                if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED):
                    real_unjoined.append(ch)
            except Exception as e:
                logger.warning(
                    f"[GATE] Could not check membership for channel={ch['channel_id']} "
                    f"user={uid}: {e} — skipping this channel's check "
                    f"(bot may not be admin there)."
                )

    if real_unjoined:
        return await callback.answer("❌ Please join all channels and continue.", show_alert=True)

    try:
        await callback.message.delete()
    except Exception:
        pass

    s   = await state.get_data()
    ref = s.get("stashed_referrer_id", 0)
    await state.clear()
    if not ref:
        # FSM memory is gone (bot restarted while they were off joining
        # channels) — recover the referrer from the durable DB stash instead.
        ref = await DataEngine.get_pending_referral(uid) or 0

    if await DataEngine.is_verified(uid):
        await callback.message.answer("✅ Identity clear!", reply_markup=generate_dashboard_matrix(uid))
    else:
        acc = await DataEngine.get_user(uid)
        if acc and acc["last_verify_msg_id"]:
            try:
                await bot.delete_message(chat_id=uid, message_id=acc["last_verify_msg_id"])
            except Exception:
                pass

        sent = await callback.message.answer(
            f"{BOT_RULES_CAPTION}\n\n🛡️ <b>Almost there:</b> complete the quick security check:",
            reply_markup=generate_verification_widget(uid, ref, 0)
        )
        await sent.edit_reply_markup(reply_markup=generate_verification_widget(uid, ref, sent.message_id))
        await DataEngine.set_last_verify_msg(uid, sent.message_id)

# ─────────────────────────────────────────────────────────────────────────────
# CORE BOT HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
@core_router.message(CommandStart())
async def process_start_command(message: Message, state: FSMContext):
    await state.clear()
    uid  = message.from_user.id
    args = message.text.split()
    arg  = args[1] if len(args) > 1 else ""
    ref  = int(arg) if arg.isdigit() and int(arg) != uid else 0

    if ref:
        # Written to disk immediately — the membership-gate wait that
        # follows can take anywhere from seconds to hours, and the old
        # FSMContext-only stash below doesn't survive a bot restart in
        # that window. This does.
        await DataEngine.stash_pending_referral(uid, ref)

    acc = await DataEngine.get_user(uid)
    if acc and acc["is_banned"]:
        return await message.answer("🚫 <b>Banned:</b> Your profile has been blacklisted.")

    if not await enforce_membership_gate(message, uid):
        if ref:
            await state.update_data(stashed_referrer_id=ref)
        return

    if await DataEngine.is_verified(uid):
        return await message.answer("👋 <b>Good to see you again!</b>\nYour Anon Earn menu is ready — pick what you need:", reply_markup=generate_dashboard_matrix(uid))

    # ── Clean up any stale, unfinished verification widget ────────────────
    # If the user restarts the bot (or taps an old shared /start link)
    # before completing verification, the previous "Open Mini App" message
    # is deleted first and a brand-new one is sent in its place. This stops
    # someone from passing around / re-using an old verification link and
    # keeps the chat from filling up with duplicate widgets.
    if acc and acc["last_verify_msg_id"]:
        try:
            await bot.delete_message(chat_id=uid, message_id=acc["last_verify_msg_id"])
        except Exception:
            pass

    _welcome_caption = f"{BOT_RULES_CAPTION}\n\n🛡️ <b>One quick security check</b> to unlock your account:"
    try:
        sent = await message.answer_photo(
            FSInputFile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "welcome.jpg")),
            caption=_welcome_caption,
            reply_markup=generate_verification_widget(uid, ref, 0)
        )
    except Exception:
        sent = await message.answer(
            _welcome_caption,
            reply_markup=generate_verification_widget(uid, ref, 0)
        )
    await sent.edit_reply_markup(reply_markup=generate_verification_widget(uid, ref, sent.message_id))
    await DataEngine.set_last_verify_msg(uid, sent.message_id)

@core_router.callback_query(F.data == "ui_return_home")
async def process_navigation_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = callback.from_user.id
    if not await DataEngine.is_verified(uid):
        if not await enforce_membership_gate(callback, uid):
            return
    await callback.message.edit_text(
        "🏠 <b>Main Menu</b>",
        reply_markup=generate_dashboard_matrix(uid)
    )

@core_router.callback_query(F.data == "ui_fetch_balance")
async def process_balance_query(callback: CallbackQuery):
    uid = callback.from_user.id
    # Already-verified users joined the required channels at verification
    # time. Re-checking membership (one Telegram API call per channel) on
    # every single button tap is what makes the bot look "stuck" once many
    # users are tapping buttons concurrently — this skips that re-check.
    if not await DataEngine.is_verified(uid):
        if not await enforce_membership_gate(callback, uid):
            return
    acc = await DataEngine.get_user(uid)
    if acc is None:
        # Self-heal: this account has a verifications row (so is_verified
        # was True) but somehow never got a users row — most commonly
        # caused by the admin "Full Unban" path before this fix. Create
        # the missing row instead of crashing.
        await DataEngine.create_user(
            uid, callback.from_user.username or "", callback.from_user.full_name or ""
        )
        acc = await DataEngine.get_user(uid)
    min_w = await DataEngine.get_setting("min_withdrawal", "50")
    await callback.message.edit_text(
        f"💰 <b>Your Anon Earn Balance</b>\n\n"
        f"🏦 Available: <code>{float(acc['balance']):.2f} Birr</code>\n"
        f"💸 Withdrawals start from <code>{min_w} Birr</code>",
        reply_markup=generate_fallback_navigation()
    )

@core_router.callback_query(F.data == "ui_fetch_referrals")
async def process_referral_query(callback: CallbackQuery):
    uid = callback.from_user.id
    if not await DataEngine.is_verified(uid):
        if not await enforce_membership_gate(callback, uid):
            return
    direct, _    = await DataEngine.get_paid_referral_metrics(uid)
    rate         = float(await DataEngine.get_setting("reward_per_referral", "10"))
    me           = await bot.get_me()
    link         = f"https://t.me/{me.username}?start={uid}"
    await callback.message.edit_text(
        f"👥 <b>Your Team</b>\n\n"
        f"🙋 People you invited: <b>{direct}</b>\n"
        f"🎁 You earn: <b>{rate:.2f} Birr</b> per person\n"
        f"💵 Team earnings so far: <b>{direct * rate:.2f} Birr</b>\n\n"
        f"🔗 Your invite link:\n<code>{link}</code>",
        reply_markup=generate_fallback_navigation()
    )

@core_router.callback_query(F.data == "ui_fetch_link")
async def process_link_generation(callback: CallbackQuery):
    uid = callback.from_user.id
    if not await DataEngine.is_verified(uid):
        if not await enforce_membership_gate(callback, uid):
            return
    me   = await bot.get_me()
    rate = float(await DataEngine.get_setting("reward_per_referral", "10"))
    link = f"https://t.me/{me.username}?start={uid}"
    share_url = build_invite_share_url(uid) or (
        "https://t.me/share/url?url=&text=" + urllib.parse.quote(link, safe="")
    )
    rows = [
        [InlineKeyboardButton(text=SHARE_BTN_TEXT, url=share_url)],
        [InlineKeyboardButton(text="🔙 Back", callback_data="ui_return_home")],
    ]
    await callback.message.edit_text(
        f"🔗 <b>Grow your team, grow your balance</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"Every friend who joins and verifies puts <b>{rate:.2f} Birr</b> in your balance.\n"
        f"👇 Hit <b>Share</b> and send it to your friends.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )

@core_router.message(F.photo)
async def process_get_file_id(message: Message):
    if not evaluate_admin_access(message.from_user.id):
        return
    file_id = message.photo[-1].file_id
    await message.answer(
        f"📸 <b>File ID Captured:</b>\n\n<code>{file_id}</code>\n\n"
        f"⚠️ Copy this value and replace <code>TELEBIRR_PROOF_IMAGE</code> in the code."
    )

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_admin_access(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def parse_telegram_webapp_handshake(init_data: str) -> dict | None:
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        vh     = parsed.pop("hash", "")
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        key    = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        sig    = hmac.new(key, check_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, vh):
            return None

        # Reject stale payloads. initData carries a unix `auth_date` set by
        # Telegram at the moment the Mini App was opened — if it's older
        # than our allowed window, treat it as invalid rather than trusting
        # a copy-pasted link opened outside Telegram / hours later.
        try:
            auth_date = int(parsed.get("auth_date", "0"))
        except ValueError:
            return None
        age = time.time() - auth_date
        if age < 0 or age > MAX_INIT_DATA_AGE_SECONDS:
            return None

        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None

async def execute_network_vpn_lookup(client_ip: str) -> bool:
    """
    VPN CHECK:
    'operator' field (ISP name) is intentionally ignored — every legitimate
    Ethiopian user is on an ISP like Ethio Telecom / Safaricom, so keying off
    that field would ban normal users. Only an explicit VPN/TOR `type` from
    the lookup service counts as a VPN hit.
    """
    if not client_ip or client_ip in ("127.0.0.1", "::1", "unknown"):
        return False
    try:
        param = f"&key={PROXYCHECK_API_KEY}" if PROXYCHECK_API_KEY else ""
        url = f"https://proxycheck.io/v2/{client_ip}?vpn=1&asn=1{param}"
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(url)
            payload = r.json()
            d = payload.get(client_ip, {})
            ptype = (d.get("type") or "").upper()
            logger.info(f"proxycheck result ip={client_ip} type={ptype}")
            return ptype in ("VPN", "TOR")
    except Exception:
        logger.warning(f"proxycheck lookup failed for ip={client_ip}")
        return False



async def notify_ad_reward_claimed(user_id: int, reward: float, new_balance: float):
    try:
        text = (
            f"🎉 **Reward Credited!**\n\n"
            f"You earned **+{reward:.2f} Birr** for watching the ad! 💰\n"
            f"Current Balance: **{new_balance:.2f} Birr**\n\n"
            f"Watch more ads or invite friends to earn even more:"
        )
        await bot.send_message(
            user_id,
            text,
            parse_mode="Markdown",
            reply_markup=generate_dashboard_matrix(user_id)
        )
    except Exception as e:
        logger.warning(f"Failed to send reward notification to {user_id}: {e}")

async def notify_user_limit_reached(user_id: int):
    try:
        text = (
            "⚠️ **Daily Limit Reached!**\n\n"
            "Dear user, you have reached today's ad viewing limit! 🚀\n"
            "Your limit will reset automatically after 24 hours. You can check your balance or referrals in the meantime:"
        )
        await bot.send_message(
            user_id,
            text,
            parse_mode="Markdown",
            reply_markup=generate_dashboard_matrix(user_id)
        )
    except Exception as e:
        logger.warning(f"Failed to send limit notification to {user_id}: {e}")

# Substrings aiogram/Telegram commonly raise when a user is simply gone
# (blocked the bot, deactivated their account, or the chat no longer
# exists) — these are expected background noise for reminders sent to
# a large/dormant audience, not real bugs. Anything else is bucketed
# separately as a genuine delivery error worth checking in logs.
_EXPECTED_UNREACHABLE_MARKERS = (
    "bot was blocked", "forbidden", "user is deactivated",
    "chat not found", "kicked", "chat_id is empty",
)


async def send_ads_limit_reset_reminder(user_id: int) -> str:
    """For users who HIT their daily ad-watch limit yesterday — tells them
    the limit has reset and invites them back to watch more.
    Returns 'sent', 'blocked' (user unreachable — expected), or 'error'
    (unexpected failure worth checking logs for)."""
    text = (
        "🎉 <b>Daily Limit Reset!</b>\n\n"
        "You watched ads yesterday! Your limit has reset for today 🚀\n"
        "Come back and watch more ads to earn money!"
    )
    try:
        await bot.send_message(
            user_id, text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎬 Watch Now", web_app=WebAppInfo(url=f"{FRONTEND_URL}/app.html?uid={user_id}"))
            ]])
        )
        return "sent"
    except Exception as e:
        err = str(e).lower()
        if any(m in err for m in _EXPECTED_UNREACHABLE_MARKERS):
            return "blocked"
        logger.warning(f"Failed to send ads-reset reminder to {user_id}: {e}")
        return "error"


async def send_ads_start_earning_reminder(user_id: int) -> str:
    """For users who watched ZERO ads yesterday — a different message
    inviting them to start watching ads to earn, rather than implying
    they're returning to something they already do.
    Returns 'sent', 'blocked' (user unreachable — expected), or 'error'
    (unexpected failure worth checking logs for)."""
    text = (
        "🎬 <b>Start Earning Today!</b>\n\n"
        "You can easily earn by watching ads.\n"
        "Open now and get started! 🎬"
    )
    try:
        await bot.send_message(
            user_id, text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎬 Watch Ads", web_app=WebAppInfo(url=f"{FRONTEND_URL}/app.html?uid={user_id}"))
            ]])
        )
        return "sent"
    except Exception as e:
        err = str(e).lower()
        if any(m in err for m in _EXPECTED_UNREACHABLE_MARKERS):
            return "blocked"
        logger.warning(f"Failed to send ads-start-earning reminder to {user_id}: {e}")
        return "error"


# Ethiopian "morning" window this broadcast fires within — roughly
# ጥዋት 1 ሰዓት to ጥዋት 3 ሰዓት (07:00–09:00 EAT). Ethiopian clocks start
# counting from dawn (~06:00 EAT = 0/12 o'clock). EAT is UTC+3 year-round
# (no DST), so that's a fixed 04:00–06:00 UTC window.
ADS_RESET_WINDOW_START_UTC_MINUTE = 4 * 60   # 04:00 UTC = 07:00 EAT
ADS_RESET_WINDOW_END_UTC_MINUTE   = 6 * 60   # 06:00 UTC = 09:00 EAT


async def notify_ad_limit_reset_loop():
    """Runs once a day at a RANDOM time inside the Ethiopian-morning window
    (07:00–09:00 EAT) — a new random minute is picked each day so the send
    time isn't identical every day. Can be turned off entirely from the
    bot-chat admin panel (⚙️ Admin → 🔔 Ads Reset Broadcast) via the
    'ads_reset_broadcast_enabled' setting.

    Two separate audiences, each notified once per run:
      • Hit yesterday's daily ad-watch limit  -> 'limit reset, come back'
      • Watched zero ads yesterday (but seen recently, not banned)
                                               -> 'start watching, earn money'
    daily_notify_log still guards against a duplicate send on the same
    day (e.g. if the process restarts shortly after a run).
    """
    while True:
        try:
            now = datetime.utcnow()
            random_minute = random.randint(
                ADS_RESET_WINDOW_START_UTC_MINUTE, ADS_RESET_WINDOW_END_UTC_MINUTE
            )
            target = now.replace(hour=0, minute=0, second=0, microsecond=0) \
                + timedelta(minutes=random_minute)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            broadcast_enabled = (await DataEngine.get_setting("ads_reset_broadcast_enabled", "1")) == "1"
            if not broadcast_enabled:
                continue
            ads_enabled = (await DataEngine.get_setting("ads_enabled", "0")) == "1"
            if not ads_enabled:
                continue
            ad_daily_limit = int(await DataEngine.get_setting("ad_daily_limit", "10"))

            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row

                # Group 1: hit the daily limit yesterday
                cur = await db.execute(
                    "SELECT user_id, COUNT(*) c FROM ad_events "
                    "WHERE kind=? AND status='completed' AND date(completed_at)=date('now','-1 day') "
                    "GROUP BY user_id HAVING c >= ?",
                    (AD_KIND_VIDEO, ad_daily_limit),
                )
                limit_reached_users = [r["user_id"] for r in await cur.fetchall()]

                # Group 2: zero ads watched yesterday, but active recently
                # (last 30 days) and not banned — avoids blasting long-dead
                # accounts that never engage with ads at all.
                cur2 = await db.execute(
                    "SELECT user_id FROM users WHERE is_banned=0 "
                    "AND date(last_seen) >= date('now', '-30 day') "
                    "AND user_id NOT IN ("
                    "  SELECT DISTINCT user_id FROM ad_events WHERE kind=? AND status='completed' "
                    "  AND date(completed_at)=date('now','-1 day')"
                    ")",
                    (AD_KIND_VIDEO,),
                )
                inactive_users = [r["user_id"] for r in await cur2.fetchall()]

            sent_limit, blocked_limit, error_limit, skipped_limit = 0, 0, 0, 0
            for i, uid in enumerate(limit_reached_users, start=1):
                if await DataEngine.mark_notified_today(uid, "ads_reset_limit"):
                    result = await send_ads_limit_reset_reminder(uid)
                    if result == "sent":
                        sent_limit += 1
                    elif result == "blocked":
                        blocked_limit += 1
                    else:
                        error_limit += 1
                else:
                    skipped_limit += 1
                await asyncio.sleep(BROADCAST_DELAY)
                if i % BROADCAST_BATCH_SIZE == 0:
                    await asyncio.sleep(BROADCAST_BATCH_PAUSE)

            sent_earn, blocked_earn, error_earn, skipped_earn = 0, 0, 0, 0
            for i, uid in enumerate(inactive_users, start=1):
                if await DataEngine.mark_notified_today(uid, "ads_start_earn"):
                    result = await send_ads_start_earning_reminder(uid)
                    if result == "sent":
                        sent_earn += 1
                    elif result == "blocked":
                        blocked_earn += 1
                    else:
                        error_earn += 1
                else:
                    skipped_earn += 1
                await asyncio.sleep(BROADCAST_DELAY)
                if i % BROADCAST_BATCH_SIZE == 0:
                    await asyncio.sleep(BROADCAST_BATCH_PAUSE)

            await report_ads_reset_broadcast_to_admins(
                sent_limit, blocked_limit, error_limit, skipped_limit,
                sent_earn, blocked_earn, error_earn, skipped_earn,
            )

        except Exception as e:
            logger.warning(f"notify_ad_limit_reset_loop error: {e}")


async def report_ads_reset_broadcast_to_admins(
    sent_limit: int, blocked_limit: int, error_limit: int, skipped_limit: int,
    sent_earn: int, blocked_earn: int, error_earn: int, skipped_earn: int,
):
    """Sends a short delivery summary to every configured admin after each
    daily ads-reset broadcast run, so admins know how many users actually
    got each message without checking logs. Failures are split into
    'Blocked' (user unreachable — expected background noise for a
    dormant audience) and 'Other errors' (unexpected — worth checking
    logs for) so a high blocked-count doesn't read as a delivery bug."""
    total_sent = sent_limit + sent_earn
    total_blocked = blocked_limit + blocked_earn
    total_error = error_limit + error_earn
    text = (
        "🔔 <b>Ads Reset Broadcast — Delivery Report</b>\n\n"
        "🎉 <b>Limit Reset</b> (watched ads yesterday, hit limit):\n"
        f"   ✅ Delivered: {sent_limit}\n"
        f"   🚫 Blocked/unreachable: {blocked_limit}\n"
        f"   ❌ Other errors: {error_limit}\n\n"
        "💰 <b>Start Earning</b> (watched 0 ads yesterday):\n"
        f"   ✅ Delivered: {sent_earn}\n"
        f"   🚫 Blocked/unreachable: {blocked_earn}\n"
        f"   ❌ Other errors: {error_earn}\n\n"
        f"📊 <b>Total delivered:</b> {total_sent}\n"
        f"📊 <b>Total blocked/unreachable:</b> {total_blocked}\n"
        f"📊 <b>Total other errors:</b> {total_error}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Failed to send ads-broadcast report to admin {admin_id}: {e}")

def generate_verification_widget(user_id: int, ref: int, msg_id: int = 0):
    # index.html (verify screen) is served from the FRONTEND (Vercel), NOT
    # from this backend, so this must point at FRONTEND_URL, not WEBAPP_URL.
    url = f"{WEBAPP_URL}/verify?uid={user_id}&ref={ref}&msg_id={msg_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Start Verification", web_app=WebAppInfo(url=url))
    ]])

def generate_app_webapp_button(user_id: int, admin: bool = False) -> InlineKeyboardMarkup:
    url = f"{FRONTEND_URL}/app.html?uid={user_id}" + ("&admin=1" if admin else "")
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📱 Open Anon Earn", web_app=WebAppInfo(url=url))
    ]])

def build_invite_share_url(user_id: int) -> str | None:
    """
    Telegram "send to…" picker URL carrying the exact same invite text the
    Mini App's Share button sends (see shareLink() in frontend/app.html).
    The user's referral link sits at the END of the text, so `url=` is left
    empty on purpose — otherwise Telegram puts the link before the text.
    Returns None only if the bot username isn't known yet (before startup).
    """
    if not BOT_USERNAME:
        return None
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    text = (
        "🎉 I'm earning real money with this bot — and you can too!\n\n"
        "✅ Complete simple tasks\n"
        "✅ Watch short ads\n"
        "✅ Invite friends for bonus rewards\n"
        "✅ Withdraw straight to Telebirr\n\n"
        "Join now 👇\n"
        f"{link}"
    )
    return "https://t.me/share/url?url=&text=" + urllib.parse.quote(text, safe="")

# Share button label: plain circular-arrow glyph instead of an emoji.
# Change this one constant to swap the symbol everywhere.
SHARE_ARROW = "↻"
SHARE_BTN_TEXT = f"{SHARE_ARROW} Share"

async def get_withdrawal_process_text() -> str:
    """Admin-editable processing time, e.g. '24-48 hours' or '3 days'."""
    val  = (await DataEngine.get_setting("withdrawal_process_value", "24-48") or "24-48").strip()
    unit = (await DataEngine.get_setting("withdrawal_process_unit", "hours") or "hours").strip()
    if unit not in ("hours", "days"):
        unit = "hours"
    return f"{val} {unit}"

def generate_dashboard_matrix(user_id: int) -> InlineKeyboardMarkup:
    # Anon Earn's own menu style (not the legacy layout): earn actions first.
    # Layout:  [        Open Anon Earn        ]   <- full width
    #          [ Balance ]  [ Withdraw ]          <- money row
    #          [  Team   ]  [ Invite Link ]       <- growth row
    rows = [
        [InlineKeyboardButton(
            text="🚀 Open Anon Earn",
            web_app=WebAppInfo(url=f"{FRONTEND_URL}/app.html?uid={user_id}")
        )],
        [
            InlineKeyboardButton(text="💰 Balance",  callback_data="ui_fetch_balance"),
            InlineKeyboardButton(text="💸 Withdraw", callback_data="ui_initiate_withdrawal"),
        ],
        [
            InlineKeyboardButton(text="👥 Team",        callback_data="ui_fetch_referrals"),
            InlineKeyboardButton(text="🔗 Invite Link", callback_data="ui_fetch_link"),
        ],
    ]
    if evaluate_admin_access(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Admin Panel", callback_data="ui_admin_core")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def generate_admin_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🧩 Tasks & Full Admin (Mini App)",
            web_app=WebAppInfo(url=f"{FRONTEND_URL}/app.html?admin=1")
        )],
        [
            InlineKeyboardButton(text="💎 Set Referral Reward",  callback_data="adm_cmd_reward"),
            InlineKeyboardButton(text="💵 Set Min Withdrawal",   callback_data="adm_cmd_min_wd"),
        ],
        [
            InlineKeyboardButton(text="✍️ Edit User Balance",    callback_data="adm_cmd_edit_bal"),
            InlineKeyboardButton(text="📊 Bot Statistics",       callback_data="adm_cmd_stats"),
        ],
        [
            InlineKeyboardButton(text="🔴 Force Join (Real Admin)", callback_data="adm_cmd_add_mand"),
            InlineKeyboardButton(text="🟡 Fake Join (No Admin)",    callback_data="adm_cmd_add_noadmin"),
        ],
        [InlineKeyboardButton(text="🗑 Remove Force Channel",   callback_data="adm_cmd_rm_node")],
        [InlineKeyboardButton(text="📋 List Force Channels",    callback_data="adm_cmd_list_channels")],
        [
            InlineKeyboardButton(text="📥 Pending Withdrawals", callback_data="adm_cmd_pending_tickets"),
            InlineKeyboardButton(text="📢 Broadcast Message",   callback_data="adm_cmd_broadcast"),
        ],
        [InlineKeyboardButton(text="🔔 Ads Reset Broadcast",    callback_data="adm_cmd_ads_reset_menu")],
        [InlineKeyboardButton(text="🕵️ Multi-Account Detect",   callback_data="adm_cmd_multi_menu")],
        [InlineKeyboardButton(text="🧾 Proof Channel Post",     callback_data="adm_cmd_proof_menu")],
        [InlineKeyboardButton(text="🗓 Withdrawal Time",    callback_data="adm_cmd_wd_sched")],
        [InlineKeyboardButton(text="🔔 Withdrawal Alerts",  callback_data="adm_cmd_wd_notify")],
        [InlineKeyboardButton(text="🔍 Search User",            callback_data="adm_cmd_search")],
        [InlineKeyboardButton(text="⚠️ Fraud Log",              callback_data="adm_cmd_fraud_log")],
        [
            InlineKeyboardButton(text="🚫 Ban User",            callback_data="adm_cmd_ban"),
            InlineKeyboardButton(text="✅ Unban Dashboard",     callback_data="adm_cmd_unban_menu"),
        ],
        [InlineKeyboardButton(text="💾 Backup DB Now",         callback_data="adm_cmd_backup_now")],
        [InlineKeyboardButton(text="🛑 STOP BOT ENGINE",        callback_data="adm_stop_bot_confirm1")],
        [InlineKeyboardButton(text="🔙 Back to Main Menu",      callback_data="ui_return_home")],
    ])

def generate_fallback_navigation(target="ui_return_home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Back", callback_data=target)
    ]])

# ─────────────────────────────────────────────────────────────────────────────
# WITHDRAWAL
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "ui_initiate_withdrawal")
async def process_withdrawal_start(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not await DataEngine.is_verified(uid):
        if not await enforce_membership_gate(callback, uid):
            return
    user = await DataEngine.get_user(uid)
    if user is None:
        # Self-heal — see explanation in process_balance_query above.
        await DataEngine.create_user(
            uid, callback.from_user.username or "", callback.from_user.full_name or ""
        )
        user = await DataEngine.get_user(uid)
    # BUG-REPORT #1: banned users were only blocked at /start and in the
    # REST layer — the bot-chat withdrawal flow itself never checked, so a
    # banned user who still had the chat open could cash out freely.
    if user and user["is_banned"]:
        return await callback.answer(
            "🚫 Your account is banned — withdrawals are disabled.",
            show_alert=True
        )
    min_w = float(await DataEngine.get_setting("min_withdrawal", "50"))
    current_bal = round(float(user["balance"]), 2)
    if current_bal < min_w:
        return await callback.answer(
            f"❌ Minimum payout baseline is {min_w:.2f} Birr. Your balance is {current_bal:.2f} Birr.",
            show_alert=True
        )
    await state.set_state(UserWithdrawalWorkflow.select_payout_gateway)
    await state.update_data(cached_balance=current_bal, cached_minimum=min_w)
    await callback.message.edit_text(
        "💸 <b>How do you want your money?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📲 Telebirr", callback_data="gateway_telebirr")],
            [InlineKeyboardButton(text="🟡 Binance Pay", callback_data="gateway_binance")],
            [InlineKeyboardButton(text="❌ Cancel",     callback_data="ui_return_home")],
        ])
    )

@core_router.callback_query(F.data == "gateway_telebirr", UserWithdrawalWorkflow.select_payout_gateway)
async def process_telebirr_selection(callback: CallbackQuery, state: FSMContext):
    await state.update_data(gateway="telebirr")
    await state.set_state(UserWithdrawalWorkflow.input_cash_volume)
    await callback.message.edit_text(
        "<b>How much do you want to withdraw? (Birr)</b>",
        reply_markup=generate_fallback_navigation()
    )

@core_router.callback_query(F.data == "gateway_binance", UserWithdrawalWorkflow.select_payout_gateway)
async def process_binance_selection(callback: CallbackQuery, state: FSMContext):
    await state.update_data(gateway="binance")
    await state.set_state(UserWithdrawalWorkflow.input_cash_volume)
    usdt_rate = float(await DataEngine.get_setting("usdt_rate", "155"))
    await callback.message.edit_text(
        f"<b>How much do you want to withdraw? (Birr)</b>\n\n"
        f"💱 Rate: 1 USDT ≈ {usdt_rate:.2f} Birr",
        reply_markup=generate_fallback_navigation()
    )

@core_router.message(UserWithdrawalWorkflow.input_cash_volume)
async def process_cashout_volume(message: Message, state: FSMContext):
    s = await state.get_data()
    try:
        val = round(float(message.text.strip()), 2)
        if val < s["cached_minimum"] or val > s["cached_balance"]:
            return await message.answer(
                f"❌ That amount doesn't work. The minimum is {s['cached_minimum']:.2f} Birr.\n"
                f"Your balance: <b>{s['cached_balance']:.2f} Birr</b>."
            )
    except Exception:
        return await message.answer(
            f"❌ Please send a number only.\n"
            f"Your balance: <b>{s['cached_balance']:.2f} Birr</b>."
        )
    await state.update_data(validated_volume=val)
    if s.get("gateway") == "binance":
        usdt_rate = float(await DataEngine.get_setting("usdt_rate", "155"))
        usdt_amount = val / usdt_rate if usdt_rate > 0 else 0
        await state.set_state(UserWithdrawalWorkflow.provide_binance_uid)
        await message.answer(
            f"💱 <b>≈ {usdt_amount:.2f} USDT</b> at the current rate.\n\n"
            f"🆔 <b>Send your Binance UID:</b>"
        )
    else:
        await state.set_state(UserWithdrawalWorkflow.provide_mobile_digits)
        await message.answer("📱 <b>Send the Telebirr number that should receive the money:</b>")

@core_router.message(UserWithdrawalWorkflow.provide_mobile_digits)
async def process_mobile_digits(message: Message, state: FSMContext):
    phone = message.text.strip()
    if len(phone) < 9:
        return await message.answer("❌ That doesn't look like a valid phone number — try again.")
    await state.update_data(validated_phone=phone)
    await state.set_state(UserWithdrawalWorkflow.provide_account_title)
    await message.answer("📝 <b>Send the full name on that Telebirr account:</b>")

@core_router.message(UserWithdrawalWorkflow.provide_account_title)
async def process_account_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if len(title) < 3:
        return await message.answer("❌ That name is too short — send the full name.")
    await state.update_data(validated_title=title)
    s = await state.get_data()
    safe_title = sanitize_html(title)
    await message.answer(
        f"🧾 <b>Check your payout details</b>\n\n"
        f"• Platform: <code>Telebirr</code>\n"
        f"• Amount: <code>{s['validated_volume']:.2f} ETB</code>\n"
        f"• Holder: <code>{safe_title}</code>\n"
        f"• Number: <code>{sanitize_html(s['validated_phone'])}</code>\n\n"
        f"Authorization requested.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yes, Send My Money",  callback_data="action_payout_dispatch"),
            InlineKeyboardButton(text="❌ Abort",     callback_data="ui_return_home"),
        ]])
    )
    await state.set_state(UserWithdrawalWorkflow.payout_final_approval)

@core_router.message(UserWithdrawalWorkflow.provide_binance_uid)
async def process_binance_uid(message: Message, state: FSMContext):
    binance_uid = message.text.strip()
    if len(binance_uid) < 5:
        return await message.answer("❌ That Binance UID doesn't look right — try again.")
    await state.update_data(validated_binance_uid=binance_uid)
    s = await state.get_data()
    usdt_rate = float(await DataEngine.get_setting("usdt_rate", "155"))
    usdt_amount = s["validated_volume"] / usdt_rate if usdt_rate > 0 else 0
    await message.answer(
        f"🧾 <b>Check your payout details</b>\n\n"
        f"• Platform: <code>Binance Pay</code>\n"
        f"• Amount: <code>{s['validated_volume']:.2f} ETB</code> (≈ {usdt_amount:.2f} USDT)\n"
        f"• Binance UID: <code>{sanitize_html(binance_uid)}</code>\n\n"
        f"Authorization requested.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yes, Send My Money",  callback_data="action_payout_dispatch"),
            InlineKeyboardButton(text="❌ Abort",     callback_data="ui_return_home"),
        ]])
    )
    await state.set_state(UserWithdrawalWorkflow.payout_final_approval)

def build_payout_admin_markup(tid: int, uid: int) -> InlineKeyboardMarkup:
    """Original 'act on this ticket' keyboard shown to admins in bot chat.
    Also used to restore the buttons if an admin taps Approve and then
    backs out of the confirmation step."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve (Both)",  callback_data=f"adm_payout_apask_both_{tid}")],
        [InlineKeyboardButton(text="📢 Approve (Channel Only)", callback_data=f"adm_payout_apask_co_{tid}")],
        [InlineKeyboardButton(text="❌ Deny", callback_data=f"adm_payout_rjmenu_{tid}")],
        [InlineKeyboardButton(text="👥 View Referrals", callback_data=f"adm_view_invites_{uid}")]
    ])

async def dispatch_withdrawal_core(
    uid: int, amount: float, full_name: str, phone: str,
    method: str = "telebirr", binance_uid: str = ""
) -> dict:
    """
    Shared withdrawal-submission logic used by BOTH the bot-chat flow and
    the Mini App REST endpoint (/api/withdraw), so there's exactly one
    place that creates the ledger row and notifies admins.

    method: "telebirr" (default, unchanged behaviour) or "binance" —
    Binance Pay withdrawals skip full_name/phone and use binance_uid
    instead.
    """
    is_binance = method == "binance"

    # Check balance BEFORE the ads-requirement gate. Otherwise someone who
    # simply doesn't have enough money sees a confusing "watch more ads"
    # message instead of the real reason their request can't go through.
    user = await DataEngine.get_user(uid)
    # BUG-REPORT #1: defence in depth at the one choke point both the
    # bot-chat flow and /api/withdraw funnel through — a banned account
    # can never create a withdrawal ticket, whatever path it takes in.
    if user and user["is_banned"]:
        return {"status": "error", "reason": "banned"}
    if not user or round(float(user["balance"]), 2) < round(amount, 2):
        return {"status": "error", "reason": "insufficient_funds"}

    min_ads_req = _safe_int(await DataEngine.get_setting("min_ads_for_withdrawal", "30"), 30)
    if min_ads_req > 0:
        total_ads = await DataEngine.count_total_completed_ads(uid)
        if total_ads < min_ads_req:
            return {
                "status": "error",
                "code": "ads_required",
                "reason": f"⚠️ To complete your withdrawal request, you must watch at least {min_ads_req} ads first. You have currently watched {total_ads}/{min_ads_req} ads. Please watch more ads and try again!"
            }

    tid, ok = await DataEngine.create_withdrawal_atomic(
        uid, amount, full_name, phone, method=method, binance_uid=binance_uid
    )
    if not ok:
        return {"status": "error", "reason": "insufficient_funds"}

    user = await DataEngine.get_user(uid)
    me   = await bot.get_me()
    proof_channel_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Invite Now", url=f"https://t.me/{me.username}")
    ]])

    method_label = "Binance Pay" if is_binance else "Telebirr Portal"
    recipient_line = (
        f"🆔 <b>Binance UID:</b> <code>{sanitize_html(binance_uid)}</code>\n\n"
        if is_binance else
        f"👤 <b>Account Holder Name:</b> {sanitize_html(full_name)}\n\n"
    )
    post_id = 0
    # Payment channel posts (this request post AND the later "WITHDRAWAL COMPLETED"
    # proof post) are only sent while Admin → Proof Channel Post is Enabled.
    proof_post_on = (await DataEngine.get_setting("proof_post_enabled", "1")) == "1"
    if PAYMENT_LOG_CHANNEL and proof_post_on:
        try:
            txt = (
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📥 <b>NEW WITHDRAWAL REQUEST</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{recipient_line}"
                f"🆔 <b>User ID:</b> <code>{uid}</code>\n\n"
                f"💰 <b>Requested Amount:</b> ETB {amount:.2f}\n\n"
                f"📱 <b>Method:</b> {method_label}\n\n"
                f"📊 <b>Status:</b> Pending Verification ⏳\n\n"
                f"⏰ <b>Timestamp:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            receipt = await bot.send_message(PAYMENT_LOG_CHANNEL, txt, reply_markup=proof_channel_keyboard)
            post_id = receipt.message_id
            await DataEngine.update_withdrawal_status(tid, "pending", post_id)
        except Exception as e:
            logger.error(f"Log Channel Post Error: {e}")

    direct_ref, tier2_ref = await DataEngine.get_referral_metrics(uid)
    alias_str  = f"@{sanitize_html(user['username'])}" if user["username"] else "None"
    contact_line = (
        f"🆔 <b>Binance UID:</b> <code>{sanitize_html(binance_uid)}</code>\n"
        if is_binance else
        f"👤 <b>Holder Name:</b> {sanitize_html(full_name)}\n"
        f"📲 <b>Phone:</b> <code>{sanitize_html(phone)}</code>\n"
    )
    admin_txt = (
        f"📥 <b>Incoming Ticket #{tid}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>Method:</b> {method_label}\n"
        f"{contact_line}"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
        f"👤 <b>Username:</b> {alias_str}\n"
        f"💰 <b>Amount:</b> <b>{amount:.2f} Birr</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>NETWORK INTEGRITY REPORT:</b>\n"
        f"• Direct Referrals: <b>{direct_ref} people</b>\n"
        f"• Tier-2 Network Activity: <b>{tier2_ref} people</b>"
    )
    # Admin alert routing:
    #   "both" (default) -> ticket is sent to the admin's bot chat AND is in the Mini App
    #   "app"            -> nothing is sent to the admin's chat; the ticket just waits in
    #                       the Mini App pending list (/api/admin/withdrawals/list)
    notify_mode = ((await DataEngine.get_setting("withdrawal_notify_mode", "both")) or "both").strip()
    if notify_mode != "app":
        markup = build_payout_admin_markup(tid, uid)
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, admin_txt, reply_markup=markup)
            except Exception:
                pass

    return {"status": "submitted", "ticket_id": tid, "process_time": await get_withdrawal_process_text()}


@core_router.callback_query(F.data == "action_payout_dispatch", UserWithdrawalWorkflow.payout_final_approval)
async def process_payout_dispatch(callback: CallbackQuery, state: FSMContext):
    s   = await state.get_data()
    uid = callback.from_user.id

    if s.get("gateway") == "binance":
        result = await dispatch_withdrawal_core(
            uid, s["validated_volume"], "", "",
            method="binance", binance_uid=s["validated_binance_uid"]
        )
    else:
        result = await dispatch_withdrawal_core(
            uid, s["validated_volume"], s["validated_title"], s["validated_phone"],
            method="telebirr"
        )
    if result["status"] == "error":
        reason = result.get("reason", "❌ Insufficient funds.")
        if result.get("code") == "ads_required":
            await state.clear()
            return await callback.message.edit_text(
                f"{reason}\n\n👇 <b>Tap below to open the Mini App and watch your required ads:</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎬 Watch Ads in Mini App", web_app=WebAppInfo(url=f"{FRONTEND_URL}/app.html?uid={uid}"))],
                    [InlineKeyboardButton(text="🔙 Back", callback_data="ui_return_home")]
                ])
            )
        if reason == "insufficient_funds":
            reason = "❌ Insufficient funds."
        if reason == "banned":
            reason = "🚫 Your account is banned — withdrawals are disabled."
        return await callback.answer(reason, show_alert=True)

    await state.clear()
    _submitted_txt = f"✅ <b>Request received!</b> Your money is on its way — processing within {await get_withdrawal_process_text()}."
    await callback.message.edit_text(
        _submitted_txt,
        reply_markup=generate_dashboard_matrix(uid)
    )

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — VIEW REFERRALS
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data.startswith("adm_view_invites_"))
async def process_admin_view_invites(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    target_uid    = int(callback.data.split("_")[3])
    invited_nodes = await DataEngine.get_all_invited_users(target_uid)
    if not invited_nodes:
        return await callback.answer("📭 This user has not invited anyone yet.", show_alert=True)
    lines = []
    for idx, node in enumerate(invited_nodes, 1):
        uname = f"@{node['username']}" if node['username'] else "No Username"
        lines.append(
            f"{idx}. {sanitize_html(node['full_name'])} ({sanitize_html(uname)}) — ID: <code>{node['user_id']}</code>\n"
            f"📅 Joined: {node['joined_at']}"
        )
    chunk_txt = f"👥 <b>Invited Users for ID {target_uid}:</b>\n\n" + "\n\n".join(lines)
    if len(chunk_txt) > 4000:
        chunk_txt = chunk_txt[:4000] + "\n\n⚠️...List truncated."
    await bot.send_message(
        chat_id=callback.from_user.id,
        text=chunk_txt,
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )
    await callback.answer()

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — FRAUD LOG
# ─────────────────────────────────────────────────────────────────────────────
FRAUD_LOG_PAGE_SIZE = 5   # entries per page — keeps each message short and readable

@core_router.callback_query(F.data == "adm_cmd_fraud_log")
async def process_fraud_log(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    await render_fraud_log_page(callback, 0)

@core_router.callback_query(F.data.startswith("adm_fraud_pg_"))
async def process_fraud_log_page(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    try:
        page = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        page = 0
    await render_fraud_log_page(callback, page)

@core_router.callback_query(F.data == "adm_fraud_noop")
async def process_fraud_log_noop(callback: CallbackQuery):
    await callback.answer()   # page-number button: does nothing

async def render_fraud_log_page(callback: CallbackQuery, page: int):
    page = max(0, page)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COUNT(*) FROM fraud_log")
        total = (await cur.fetchone())[0]
        pages = max(1, -(-total // FRAUD_LOG_PAGE_SIZE))
        page  = min(page, pages - 1)
        cur = await db.execute(
            "SELECT * FROM fraud_log ORDER BY logged_at DESC, id DESC LIMIT ? OFFSET ?",
            (FRAUD_LOG_PAGE_SIZE, page * FRAUD_LOG_PAGE_SIZE),
        )
        rows = await cur.fetchall()
    if not rows:
        return await callback.message.edit_text(
            "📭 No fraud attempts logged.", reply_markup=generate_fallback_navigation("ui_admin_core")
        )

    # Collect every user_id we'll need a username for: the flagged user on
    # each row, plus any "matches_uid=<id>" referenced inside details.
    ids_needed = set()
    matched_uid_by_row = {}
    for r in rows:
        ids_needed.add(r["user_id"])
        details = r["details"] or ""
        if "matches_uid=" in details:
            token = details.split("matches_uid=", 1)[1].split(" ", 1)[0].strip()
            if token.isdigit():
                matched_uid = int(token)
                matched_uid_by_row[r["id"]] = matched_uid
                ids_needed.add(matched_uid)

    usernames = {}
    if ids_needed:
        placeholders = ",".join("?" for _ in ids_needed)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT user_id, username FROM users WHERE user_id IN ({placeholders})",
                tuple(ids_needed),
            )
            for u in await cur.fetchall():
                usernames[u["user_id"]] = u["username"]

    def _uname(user_id: int) -> str:
        uname = usernames.get(user_id)
        return f"@{uname}" if uname else "no username"

    def _flagged_uname(details: str, user_id: int) -> str:
        # Newer rows carry "flagged_username=@x" / "flagged_username=none"
        # straight from Telegram's initData at the moment of the attempt —
        # prefer that (it's known even if the user was blocked before ever
        # getting a `users` row). Fall back to the DB lookup for older rows.
        if "flagged_username=" in details:
            token = details.split("flagged_username=", 1)[1].split(" ", 1)[0].strip()
            if token and token != "none":
                return token if token.startswith("@") else f"@{token}"
            return "no username"
        return _uname(user_id)

    lines = []
    for r in rows:
        details = r["details"] or ""
        flagged_display = _flagged_uname(details, r["user_id"])
        # Drop the raw tag from the body since it's now shown in the header.
        if "flagged_username=" in details:
            before, _, after_tag = details.partition("flagged_username=")
            rest = after_tag.split(" ", 1)
            remainder = rest[1] if len(rest) > 1 else ""
            details = (before.strip() + " " + remainder.strip()).strip()
        matched_uid = matched_uid_by_row.get(r["id"])
        if matched_uid:
            details = details.replace(
                f"matches_uid={matched_uid}",
                f"matches_uid={matched_uid} ({_uname(matched_uid)})",
            )
        if len(details) > 400:          # safety: one huge entry can't blow Telegram's 4096-char limit
            details = details[:400] + "…"
        lines.append(
            f"⚠️ <code>{r['reason']}</code>\n"
            f"   uid=<code>{r['user_id']}</code> ({flagged_display}) ip=<code>{r['ip_address']}</code>\n"
            f"   {details}\n"
            f"   🕐 {r['logged_at']}"
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"adm_fraud_pg_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="adm_fraud_noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"adm_fraud_pg_{page + 1}"))
    await callback.message.edit_text(
        f"⚠️ <b>Fraud Log</b> — page {page + 1}/{pages} ({total} total)\n\n" + "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            nav,
            [InlineKeyboardButton(text="🔙 Back", callback_data="ui_admin_core")],
        ])
    )

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — APPROVE WITHDRAWAL
# ─────────────────────────────────────────────────────────────────────────────
async def approve_withdrawal_core(tid: int, notify_mode: str = "both") -> dict:
    """
    Shared approval logic used by BOTH the bot-chat inline button and the
    Mini App REST endpoint (/api/admin/withdrawals/approve), so there's
    exactly one place that posts the Telebirr proof photo and notifies
    the user.

    notify_mode:
      "both"          — post proof to the channel AND DM the user (default)
      "channel_only"  — post proof to the channel only, no DM to the user
    """
    ticket = await DataEngine.get_withdrawal(tid)
    if not ticket or ticket["status"] != "pending":
        return {"ok": False, "reason": "already_processed"}
    is_binance = (ticket["method"] if "method" in ticket.keys() else "telebirr") == "binance"
    # BUG-REPORT #4: the read above is only a fast path — the status flip
    # itself must be the atomic guard, or a rejection (with its refund)
    # landing between the read and the old unconditional update was
    # overwritten back to 'approved': refund kept AND payout sent. Only
    # the caller that wins claim_withdrawal_approval may post proof / DM.
    if not await DataEngine.claim_withdrawal_approval(tid):
        return {"ok": False, "reason": "already_processed"}
    me = await bot.get_me()
    proof_channel_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Invite Now", url=f"https://t.me/{me.username}")
    ]])
    recipient_label = "Binance UID" if is_binance else "Recipient"
    recipient_value = ticket["binance_uid"] if is_binance else ticket["full_name"]
    proof_post_on = (await DataEngine.get_setting("proof_post_enabled", "1")) == "1"
    if proof_post_on and PAYMENT_LOG_CHANNEL and ticket["channel_post_id"]:
        txt = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ <b>WITHDRAWAL COMPLETED</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 <b>{recipient_label}:</b> {sanitize_html(recipient_value)}\n\n"
            f"💰 <b>Amount:</b> ETB {ticket['amount']:.2f}\n\n"
            f"🚀 <b>Operational Registry:</b> Success ✅\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        try:
            if is_binance:
                # Binance Pay payouts have no fixed proof image on file —
                # post as text so we don't attach the Telebirr receipt photo.
                await bot.send_message(
                    chat_id=PAYMENT_LOG_CHANNEL,
                    text=txt,
                    reply_to_message_id=ticket["channel_post_id"],
                    reply_markup=proof_channel_keyboard
                )
            else:
                await bot.send_photo(
                    chat_id=PAYMENT_LOG_CHANNEL,
                    photo=TELEBIRR_PROOF_IMAGE,
                    caption=txt,
                    reply_to_message_id=ticket["channel_post_id"],
                    reply_markup=proof_channel_keyboard
                )
        except Exception as e:
            logger.error(f"Proof Photo Error: {e}")
            try:
                await bot.send_message(
                    chat_id=PAYMENT_LOG_CHANNEL,
                    text=txt,
                    reply_to_message_id=ticket["channel_post_id"],
                    reply_markup=proof_channel_keyboard
                )
            except Exception as e2:
                logger.error(f"Proof Text Fallback Error: {e2}")
    if notify_mode != "channel_only":
        try:
            method_txt = "Binance Pay" if is_binance else "Telebirr"
            await bot.send_message(
                ticket["user_id"],
                f"🎉 <b>Paid!</b> Your {ticket['amount']:.2f} Birr cashout was approved and sent via {method_txt}. Enjoy!"
            )
        except Exception:
            pass
    return {"ok": True}

@core_router.callback_query(F.data.startswith("adm_payout_apask_"))
async def process_admin_approval_ask(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    # data shape: adm_payout_apask_<both|co>_<tid>
    parts = callback.data.split("_")
    mode, tid = parts[3], parts[4]
    mode_label = "Both (user + channel)" if mode == "both" else "Channel Only"
    confirm_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Confirm Approve — {mode_label}", callback_data=f"adm_payout_apconfirm_{mode}_{tid}")],
        [InlineKeyboardButton(text="🔙 Back", callback_data=f"adm_payout_apback_{tid}")],
    ])
    try:
        await callback.message.edit_reply_markup(reply_markup=confirm_markup)
    except Exception:
        pass
    await callback.answer("⚠️ Tap Confirm to send the payout, or Back to cancel.", show_alert=False)

@core_router.callback_query(F.data.startswith("adm_payout_apback_"))
async def process_admin_approval_back(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    tid = int(callback.data.split("_")[3])
    ticket = await DataEngine.get_withdrawal(tid)
    if not ticket:
        return await callback.answer("Ticket not found.")
    try:
        await callback.message.edit_reply_markup(reply_markup=build_payout_admin_markup(tid, ticket["user_id"]))
    except Exception:
        pass
    await callback.answer("Cancelled.")

@core_router.callback_query(F.data.startswith("adm_payout_apconfirm_"))
async def process_admin_approval_confirmed(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    # data shape: adm_payout_apconfirm_<both|co>_<tid>
    parts = callback.data.split("_")
    mode, tid = parts[3], int(parts[4])
    notify_mode = "both" if mode == "both" else "channel_only"
    result = await approve_withdrawal_core(tid, notify_mode=notify_mode)
    if not result["ok"]:
        return await callback.answer("Already processed.")
    suffix = "✅ Ticket Approved (Both notified)." if notify_mode == "both" else "✅ Ticket Approved (Channel only — user not DMed)."
    await callback.message.edit_text(callback.message.text + f"\n\n{suffix}")
    await callback.answer()

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — REJECT WITHDRAWAL
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data.startswith("adm_payout_rjmenu_"))
async def process_admin_rejection_menu(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    tid = int(callback.data.split("_")[3])
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Multi-bot / Clone Account",      callback_data=f"rj_select_{tid}_multi_bot")],
        [InlineKeyboardButton(text="❌ Fake Activity / Emulators",       callback_data=f"rj_select_{tid}_fake_activity")],
        [InlineKeyboardButton(text="👥 No Organic Invites",              callback_data=f"rj_select_{tid}_no_invites")],
        [InlineKeyboardButton(text="✍️ Write Custom Reason",             callback_data=f"rj_select_{tid}_write_custom")],
        [InlineKeyboardButton(text="🔙 Cancel",                          callback_data="ui_admin_core")],
    ])
    await callback.message.edit_text("❌ <b>Select Rejection Reason:</b>", reply_markup=markup)

@core_router.callback_query(F.data.startswith("rj_select_"))
async def process_reason_selection(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id):
        return
    parts  = callback.data.split("_")
    tid    = int(parts[2])
    choice = "_".join(parts[3:])
    ticket = await DataEngine.get_withdrawal(tid)
    if not ticket or ticket["status"] != "pending":
        return await callback.answer("Already processed.")
    if choice == "write_custom":
        await state.set_state(AdminConsoleWorkflow.write_reject_reason)
        await state.update_data(active_reject_tid=tid)
        return await callback.message.edit_text("✍️ <b>Write the rejection reason to send to user:</b>")
    reason_map = {
        "multi_bot":     "Multi-account / Clone system detected.",
        "fake_activity": "Fake verification / Fraudulent activity detected.",
        "no_invites":    "Insufficient organic or active referrals.",
    }
    reason = reason_map.get(choice, "Violated bot usage policies.")
    await execute_withdrawal_rejection(callback.message, tid, ticket, reason)

@core_router.message(AdminConsoleWorkflow.write_reject_reason)
async def process_custom_written_reason(message: Message, state: FSMContext):
    s = await state.get_data()
    await state.clear()
    tid    = s["active_reject_tid"]
    ticket = await DataEngine.get_withdrawal(tid)
    if not ticket or ticket["status"] != "pending":
        return await message.answer("Ticket already processed.")
    await execute_withdrawal_rejection(message, tid, ticket, message.text.strip())

async def execute_withdrawal_rejection(msg_obj, tid, ticket, reason):
    # Atomic re-check-and-refund: the caller's own "status == pending"
    # check above can be raced (double-tap, two admins), so this is the
    # actual guard against refunding the same ticket's reserved balance
    # twice — status flip and balance credit happen in one transaction.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN EXCLUSIVE")
        try:
            cur = await db.execute("SELECT status FROM withdrawals WHERE id=?", (tid,))
            row = await cur.fetchone()
            if not row or row[0] != "pending":
                await db.execute("ROLLBACK")
                return
            await db.execute(
                "UPDATE withdrawals SET status='rejected', channel_post_id=?, reason=?, resolved_at=datetime('now') WHERE id=?",
                (ticket["channel_post_id"], reason, tid),
            )
            await db.execute(
                "UPDATE users SET balance = ROUND(balance + ?, 2) WHERE user_id = ?",
                (float(ticket["amount"]), ticket["user_id"]),
            )
            await db.execute("COMMIT")
        except Exception:
            await db.execute("ROLLBACK")
            raise
    warning_notice = (
        f"❌ <b>Your payout was not approved</b>\n\n"
        f"💰 <b>Amount:</b> <code>{ticket['amount']:.2f} Birr</code>\n"
        f"⚠️ <b>Reason:</b> <code>{sanitize_html(reason)}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 <b>Please note:</b> Only invite real, active people — fake invites put your account at risk.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        await bot.send_message(chat_id=ticket["user_id"], text=warning_notice)
    except Exception:
        pass
    reply_text = f"✅ Ticket #{tid} rejected. User notified."
    if msg_obj is None:
        return
    if isinstance(msg_obj, Message):
        await msg_obj.answer(reply_text, reply_markup=generate_admin_dashboard())
    else:
        await msg_obj.edit_text(reply_text, reply_markup=generate_admin_dashboard())

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — MANUAL/SCREENSHOT TASK REVIEW (Telegram-native, no image storage)
# Screenshots never touch our own database or disk: the Mini App uploads
# them as base64 to /api/tasks/manual/submit, which decodes them in memory
# and relays them straight to every admin's Telegram DM alongside an
# Approve/Reject prompt. Only the resulting Telegram file_ids (tiny text)
# are kept, purely for record-keeping — Telegram itself hosts the photos.
# ─────────────────────────────────────────────────────────────────────────────
def build_manual_review_markup(submission_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve & Pay", callback_data=f"msrev_ap_{submission_id}")],
        [InlineKeyboardButton(text="❌ Reject",         callback_data=f"msrev_rj_{submission_id}")],
    ])


async def notify_admins_manual_submission(submission_id: int, task: dict, user_row: dict, image_bytes: list) -> list:
    """Sends the screenshot(s) to every admin with an Approve/Reject
    prompt. Returns the Telegram file_ids from whichever admin received
    them first (for the audit trail only — never used to re-fetch the
    image ourselves)."""
    username = user_row.get("username") or ""
    alias = f"@{sanitize_html(username)}" if username else "None"
    caption = (
        f"📸 <b>Manual Task Proof</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧩 <b>Task:</b> {sanitize_html(task['title'])}\n"
        f"🆔 <b>User ID:</b> <code>{user_row['user_id']}</code>\n"
        f"👤 <b>Username:</b> {alias}\n"
        f"💰 <b>Reward:</b> {float(task['reward']):.2f} Birr"
    )
    markup = build_manual_review_markup(submission_id)
    file_ids: list = []
    for aid in ADMIN_IDS:
        try:
            if len(image_bytes) == 1:
                msg = await bot.send_photo(
                    aid, BufferedInputFile(image_bytes[0], filename="proof1.jpg")
                )
                this_admin_ids = [msg.photo[-1].file_id] if msg.photo else []
            else:
                media = [
                    InputMediaPhoto(media=BufferedInputFile(b, filename=f"proof{i+1}.jpg"))
                    for i, b in enumerate(image_bytes)
                ]
                msgs = await bot.send_media_group(aid, media)
                this_admin_ids = [m.photo[-1].file_id for m in msgs if m.photo]
            # Telegram's sendMediaGroup has no reply_markup parameter, so
            # the Approve/Reject buttons always go on a separate message
            # right after the photo(s) — same for the single-photo case,
            # to keep exactly one code path instead of two.
            await bot.send_message(aid, caption, reply_markup=markup)
            if not file_ids:
                file_ids = this_admin_ids
        except Exception as e:
            logger.error(f"Manual submission notify error (admin {aid}): {e}")
    return file_ids


async def review_manual_submission_core(submission_id: int, approve: bool) -> dict:
    """Shared by both the Telegram Approve/Reject buttons and the
    /api/admin/tasks/manual_submissions/review REST endpoint, so whichever
    path an admin uses, the same guarded state transition and payout logic
    runs exactly once."""
    sub = await DataEngine.get_manual_submission(submission_id)
    if not sub or sub["status"] != "pending":
        return {"ok": False, "reason": "already_processed"}
    task = await DataEngine.get_task(sub["task_id"])
    if approve:
        paid = await DataEngine.approve_manual_submission(submission_id)
        if paid and task:
            await DataEngine.add_balance(sub["user_id"], float(task["reward"]))
            try:
                await bot.send_message(
                    sub["user_id"],
                    f"✅ Your submission for \"{task['title']}\" was approved — "
                    f"{float(task['reward']):.2f} Birr credited!"
                )
            except Exception as e:
                # Balance is already credited regardless — this only
                # affects the DM notification, so log it instead of
                # hiding it. Most common cause: the user has never
                # pressed /start on the bot, or has blocked it — Telegram
                # refuses bot-initiated DMs in both cases, and no code
                # change here can work around that.
                logger.error(f"Manual-approve DM to user {sub['user_id']} failed: {e}")
    else:
        await DataEngine.reject_manual_submission(submission_id)
        if task:
            try:
                await bot.send_message(
                    sub["user_id"],
                    f"❌ Your submission for \"{task['title']}\" was rejected."
                )
            except Exception as e:
                logger.error(f"Manual-reject DM to user {sub['user_id']} failed: {e}")
    return {"ok": True}


@core_router.callback_query(F.data.startswith("msrev_ap_"))
async def process_manual_submission_approve(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    submission_id = int(callback.data.rsplit("_", 1)[1])
    result = await review_manual_submission_core(submission_id, approve=True)
    if not result["ok"]:
        return await callback.answer("Already processed.")
    await callback.message.edit_text(callback.message.text + "\n\n✅ Approved & paid.")
    await callback.answer()


@core_router.callback_query(F.data.startswith("msrev_rj_"))
async def process_manual_submission_reject(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    submission_id = int(callback.data.rsplit("_", 1)[1])
    result = await review_manual_submission_core(submission_id, approve=False)
    if not result["ok"]:
        return await callback.answer("Already processed.")
    await callback.message.edit_text(callback.message.text + "\n\n❌ Rejected.")
    await callback.answer()

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — PANEL & SETTINGS
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "ui_admin_core")
async def process_admin_panel(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    await callback.message.edit_text("⚙️ <b>Operational Admin Master Engine</b>", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_add_mand")
async def process_add_channel_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.append_mandatory_id)
    await callback.message.edit_text(
        "🔴 <b>Force Join — Real Check</b>\nEnter Channel ID (e.g. <code>-1001234567890</code>):",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.append_mandatory_id)
async def process_add_channel_id(message: Message, state: FSMContext):
    await state.update_data(ch_id=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_mandatory_title)
    await message.answer("📝 <b>Enter Channel Display Title:</b>")

@core_router.message(AdminConsoleWorkflow.append_mandatory_title)
async def process_add_channel_title(message: Message, state: FSMContext):
    await state.update_data(ch_title=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_mandatory_url)
    await message.answer("🔗 <b>Enter Channel Invite Link:</b>")

@core_router.message(AdminConsoleWorkflow.append_mandatory_url)
async def process_add_channel_finalize(message: Message, state: FSMContext):
    s = await state.get_data()
    await state.clear()
    await DataEngine.add_force_channel(s["ch_id"], s["ch_title"], message.text.strip(), bot_added=0)
    await message.answer(f"✅ <b>Force channel added!</b>\n📌 {sanitize_html(s['ch_title'])}", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_add_noadmin")
async def process_add_noadmin_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.append_noadmin_link)
    await callback.message.edit_text(
        "🟡 <b>Fake Join (No Admin Required)</b>\n\nSend the channel link:",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.append_noadmin_link)
async def process_noadmin_link(message: Message, state: FSMContext):
    await state.update_data(na_link=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_noadmin_title)
    await message.answer("📝 <b>Enter Display Name:</b>")

@core_router.message(AdminConsoleWorkflow.append_noadmin_title)
async def process_noadmin_title(message: Message, state: FSMContext):
    s = await state.get_data()
    await state.clear()
    fake_key = "fake_" + hashlib.md5(s["na_link"].encode()).hexdigest()[:8]
    await DataEngine.add_force_channel(
        channel_id=fake_key, channel_name=message.text.strip(), invite_link=s["na_link"], bot_added=1
    )
    await message.answer(f"✅ <b>Fake channel added!</b>\n📌 {sanitize_html(message.text.strip())}", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_list_channels")
async def process_list_channels(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    channels = await DataEngine.get_force_channels()
    if not channels:
        return await callback.message.edit_text("📭 No channels.", reply_markup=generate_fallback_navigation("ui_admin_core"))
    lines = []
    for ch in channels:
        mode = "🟡 Fake" if ch["bot_added"] else "🔴 Real"
        lines.append(f"{mode} — <b>{sanitize_html(ch['channel_name'])}</b>\n🔗 {ch['invite_link']}")
    await callback.message.edit_text(
        f"📋 <b>Force Channels ({len(channels)})</b>\n\n" + "\n\n".join(lines),
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.callback_query(F.data == "adm_cmd_rm_node")
async def process_rm_channel_menu(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    channels = await DataEngine.get_force_channels()
    if not channels:
        return await callback.message.edit_text("📭 No channels.", reply_markup=generate_fallback_navigation("ui_admin_core"))
    buttons = []
    for ch in channels:
        mode = "🟡" if ch["bot_added"] else "🔴"
        buttons.append([InlineKeyboardButton(text=f"🗑 {mode} {sanitize_html(ch['channel_name'])}", callback_data=f"execute_rm_node_{ch['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="ui_admin_core")])
    await callback.message.edit_text("<b>Select channel to remove:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@core_router.callback_query(F.data.startswith("execute_rm_node_"))
async def process_rm_channel_action(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    row_id = int(callback.data.replace("execute_rm_node_", ""))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM force_channels WHERE id = ?", (row_id,))
        await db.commit()
    await callback.message.edit_text("✅ Channel removed.", reply_markup=generate_admin_dashboard())

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — ADS RESET BROADCAST TOGGLE (bot-chat panel)
#
# Lets the admin enable/disable the daily 'ads limit reset' / 'start
# earning' notification broadcast (notify_ad_limit_reset_loop) without
# touching ads_enabled itself — so ads can stay on for users while the
# daily reminder broadcast is paused, or vice versa.
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "adm_cmd_ads_reset_menu")
async def show_ads_reset_broadcast_menu(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    enabled = (await DataEngine.get_setting("ads_reset_broadcast_enabled", "1")) == "1"
    status_line = "✅ Currently: <b>Enabled</b>" if enabled else "🚫 Currently: <b>Disabled</b>"
    await callback.message.edit_text(
        "🔔 <b>Ads Reset Broadcast</b>\n\n"
        "Sends a daily message to users, once a day at a random time in "
        "the Ethiopian morning window (07:00–09:00 EAT):\n"
        "• Watched ads yesterday & hit the limit → 'limit reset, come back'\n"
        "• Watched zero ads yesterday → 'start watching, earn money'\n\n"
        f"{status_line}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Enable",  callback_data="adm_ads_reset_enable")],
            [InlineKeyboardButton(text="🚫 Disable", callback_data="adm_ads_reset_disable")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="ui_admin_core")],
        ])
    )

@core_router.callback_query(F.data == "adm_ads_reset_enable")
async def enable_ads_reset_broadcast(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    await DataEngine.set_setting("ads_reset_broadcast_enabled", "1")
    await callback.answer("✅ Ads reset broadcast enabled")
    await show_ads_reset_broadcast_menu(callback)

@core_router.callback_query(F.data == "adm_ads_reset_disable")
async def disable_ads_reset_broadcast(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    await DataEngine.set_setting("ads_reset_broadcast_enabled", "0")
    await callback.answer("🚫 Ads reset broadcast disabled")
    await show_ads_reset_broadcast_menu(callback)


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — MULTI-ACCOUNT DETECT / PROOF POST TOGGLES + WITHDRAWAL SCHEDULE
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "adm_cmd_multi_menu")
async def show_multi_detect_menu(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    enabled = (await DataEngine.get_setting("multi_account_detect_enabled", "1")) == "1"
    status_line = "✅ Currently: <b>Enabled</b>" if enabled else "🚫 Currently: <b>Disabled</b>"
    await callback.message.edit_text(
        "🕵️ <b>Multi-Account Detection</b>\n\n"
        "When ON, new verifications are checked for clone / multi-account signals "
        "(device, IP farm, IP cooldown).\n"
        "When OFF, those checks are skipped — banned-IP, VPN and fingerprint "
        "checks stay active.\n\n"
        f"{status_line}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Enable",  callback_data="adm_multi_enable")],
            [InlineKeyboardButton(text="🚫 Disable", callback_data="adm_multi_disable")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="ui_admin_core")],
        ])
    )

@core_router.callback_query(F.data.in_({"adm_multi_enable", "adm_multi_disable"}))
async def toggle_multi_detect(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    on = callback.data == "adm_multi_enable"
    await DataEngine.set_setting("multi_account_detect_enabled", "1" if on else "0")
    await callback.answer("✅ Multi-account detect enabled" if on else "🚫 Multi-account detect disabled")
    await show_multi_detect_menu(callback)

@core_router.callback_query(F.data == "adm_cmd_proof_menu")
async def show_proof_post_menu(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    enabled = (await DataEngine.get_setting("proof_post_enabled", "1")) == "1"
    status_line = "✅ Currently: <b>Enabled</b>" if enabled else "🚫 Currently: <b>Disabled</b>"
    await callback.message.edit_text(
        "🧾 <b>Proof Channel Post</b>\n\n"
        "Controls everything posted to the payment channel: the 'NEW WITHDRAWAL "
        "REQUEST' post when a user requests a withdrawal, and the 'WITHDRAWAL "
        "COMPLETED' proof post when it is approved.\n"
        "When OFF, requests and approvals still work (and the user is still DMed in "
        "'Approve (Both)') but nothing is posted to the channel.\n\n"
        f"{status_line}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Enable",  callback_data="adm_proof_enable")],
            [InlineKeyboardButton(text="🚫 Disable", callback_data="adm_proof_disable")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="ui_admin_core")],
        ])
    )

@core_router.callback_query(F.data.in_({"adm_proof_enable", "adm_proof_disable"}))
async def toggle_proof_post(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    on = callback.data == "adm_proof_enable"
    await DataEngine.set_setting("proof_post_enabled", "1" if on else "0")
    await callback.answer("✅ Proof post enabled" if on else "🚫 Proof post disabled")
    await show_proof_post_menu(callback)

@core_router.callback_query(F.data == "adm_cmd_wd_notify")
async def show_wd_notify_menu(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    mode = ((await DataEngine.get_setting("withdrawal_notify_mode", "both")) or "both").strip()
    app_only = mode == "app"
    status_line = (
        "📱 Currently: <b>Only in App</b>" if app_only
        else "🔔 Currently: <b>Both (bot chat + Mini App)</b>"
    )
    await callback.message.edit_text(
        "🔔 <b>Withdrawal Request Alerts</b>\n\n"
        "Where should new user withdrawal requests be delivered?\n\n"
        "• <b>Enable Both</b> — ticket is sent to your bot chat and shown in the Mini App.\n"
        "• <b>Only in App</b> — nothing is sent to your bot chat; requests appear in the "
        "Mini App's pending withdrawals only.\n\n"
        f"{status_line}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=("✅ " if not app_only else "") + "Enable Both", callback_data="adm_wdn_both")],
            [InlineKeyboardButton(text=("✅ " if app_only else "") + "📱 Only in App",  callback_data="adm_wdn_app")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="ui_admin_core")],
        ])
    )

@core_router.callback_query(F.data.in_({"adm_wdn_both", "adm_wdn_app"}))
async def toggle_wd_notify(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    app_only = callback.data == "adm_wdn_app"
    await DataEngine.set_setting("withdrawal_notify_mode", "app" if app_only else "both")
    await callback.answer("📱 Withdrawal requests: Mini App only" if app_only else "🔔 Withdrawal requests: bot chat + Mini App")
    await show_wd_notify_menu(callback)

async def show_wd_time_menu(callback: CallbackQuery):
    current = await get_withdrawal_process_text()
    await callback.message.edit_text(
        "🗓 <b>Withdrawal Processing Time</b>\n\n"
        "Users see this after sending a withdrawal request:\n"
        f"<i>Processing within <b>{sanitize_html(current)}</b>.</i>\n\n"
        "Edit the number (e.g. <code>48</code> or <code>24-48</code>) and choose Hours or Days.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Edit Number", callback_data="adm_wd_edit_num")],
            [
                InlineKeyboardButton(text="🕐 Hours", callback_data="adm_wd_unit_hours"),
                InlineKeyboardButton(text="📅 Days",  callback_data="adm_wd_unit_days"),
            ],
            [InlineKeyboardButton(text="🔙 Back", callback_data="ui_admin_core")],
        ])
    )

@core_router.callback_query(F.data == "adm_cmd_wd_sched")
async def process_wd_time_menu(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.clear()
    await show_wd_time_menu(callback)

@core_router.callback_query(F.data.in_({"adm_wd_unit_hours", "adm_wd_unit_days"}))
async def process_wd_unit(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    unit = "hours" if callback.data == "adm_wd_unit_hours" else "days"
    await DataEngine.set_setting("withdrawal_process_unit", unit)
    await callback.answer(f"✅ Unit: {unit}")
    await show_wd_time_menu(callback)

@core_router.callback_query(F.data == "adm_wd_edit_num")
async def process_wd_num_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.modify_withdrawal_schedule)
    await callback.message.edit_text(
        "✏️ <b>Send the new number</b>\n\n"
        "Examples: <code>48</code> or <code>24-48</code> or <code>3</code>",
        reply_markup=generate_fallback_navigation("adm_cmd_wd_sched")
    )

@core_router.message(AdminConsoleWorkflow.modify_withdrawal_schedule)
async def process_wd_num_execute(message: Message, state: FSMContext):
    if not evaluate_admin_access(message.from_user.id): return
    txt = (message.text or "").strip().replace("–", "-").replace(" ", "")
    if not re.fullmatch(r"\d{1,3}(-\d{1,3})?", txt):
        return await message.answer("❌ Send a number like <code>48</code> or <code>24-48</code>.")
    await state.clear()
    await DataEngine.set_setting("withdrawal_process_value", txt)
    await message.answer(
        f"✅ Updated: Processing within <b>{sanitize_html(await get_withdrawal_process_text())}</b>.",
        reply_markup=generate_admin_dashboard()
    )

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — STOP / RESUME BOT ENGINE
#
# Stopping the bot ONLY stops Telegram long-polling (no new /start, button
# taps, or admin replies are processed). It does NOT touch the SQLite
# database, the Mini App, or any stored balances/verifications/withdrawals —
# all of that data is safe on disk the entire time. The "Resume Polling"
# button below restarts the polling task in the same running process, so an
# admin can pause and resume the bot without losing anything or having to
# redeploy — as long as the underlying process/server itself stays alive.
# ─────────────────────────────────────────────────────────────────────────────
_polling_task: asyncio.Task | None = None
BOT_USERNAME: str = ""
BOT_ID: int = 0

@core_router.callback_query(F.data == "adm_stop_bot_confirm1")
async def stop_bot_first_confirmation(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ I take responsibility — continue", callback_data="adm_stop_bot_confirm2")],
        [InlineKeyboardButton(text="❌ Cancel / Back",               callback_data="ui_admin_core")],
    ])
    await callback.message.edit_text(
        "🚨 <b>FIRST WARNING!</b>\n\n"
        "Are you sure you want to stop the bot? (This only stops chat polling — data and balances are NOT deleted)",
        reply_markup=markup
    )

@core_router.callback_query(F.data == "adm_stop_bot_confirm2")
async def stop_bot_final_confirmation(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Stop the bot now!", callback_data="adm_stop_bot_execute")],
        [InlineKeyboardButton(text="❌ Back",          callback_data="ui_admin_core")],
    ])
    await callback.message.edit_text("🛑 <b>FINAL CONFIRMATION!</b>", reply_markup=markup)

@core_router.callback_query(F.data == "adm_stop_bot_execute")
async def execute_bot_shutdown(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    await callback.message.edit_text(
        "🛑 <b>Bot paused.</b>\n\n"
        "✅ All data, balances, and Mini App verification are safe and untouched.\n"
        "▶️ Tap below to resume any time.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="▶️ Resume Polling", callback_data="adm_resume_bot_execute")
        ]])
    )
    # BUG-REPORT #6: this used to call dp.stop_polling() — but a stopped
    # poller can never receive the Resume callback above, so the button
    # was unreachable and only a restart/redeploy could revive the bot.
    # Now "stop" sets a pause flag instead: polling keeps running, the
    # BotPausedGateMiddleware (registered at the wiring section) turns
    # away every non-admin update while the flag is set, and admins keep
    # full access — including this Resume button.
    await DataEngine.set_setting("bot_paused", "1")

@core_router.callback_query(F.data == "adm_resume_bot_execute")
async def execute_bot_resume(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    # Resume = clear the pause flag (see execute_bot_shutdown). Polling
    # was never stopped, so there is nothing to restart here — the gate
    # middleware starts letting user updates through again immediately.
    await DataEngine.set_setting("bot_paused", "0")
    await callback.message.edit_text(
        "✅ <b>Bot resumed.</b> All data is intact.",
        reply_markup=generate_admin_dashboard()
    )

@core_router.callback_query(F.data == "adm_cmd_edit_bal")
async def process_edit_balance_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.direct_balance_target_id)
    await callback.message.edit_text("<b>Enter Targeted Telegram User ID:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.direct_balance_target_id)
async def process_edit_balance_id(message: Message, state: FSMContext):
    # BUG-REPORT #11: unguarded int() crashed the handler on any
    # non-numeric admin typo, killing the FSM flow with a stack trace.
    try:
        target_uid = int(message.text.strip())
    except (TypeError, ValueError):
        await state.clear()
        return await message.answer("❌ Invalid ID — enter a numeric Telegram user ID.", reply_markup=generate_admin_dashboard())
    await state.update_data(target_uid=target_uid)
    await state.set_state(AdminConsoleWorkflow.direct_balance_volume)
    await message.answer("<b>Enter Adjustment Volume (e.g. 50 or -50):</b>")

@core_router.message(AdminConsoleWorkflow.direct_balance_volume)
async def process_edit_balance_final(message: Message, state: FSMContext):
    s = await state.get_data()
    await state.clear()
    try:
        amount = float(message.text.strip())
    except (TypeError, ValueError):
        return await message.answer("❌ Invalid amount — enter a number like 50 or -50.", reply_markup=generate_admin_dashboard())
    await DataEngine.add_balance(s["target_uid"], amount)
    await message.answer("✅ Balance adjusted.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_stats")
async def process_stats(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        cur  = await db.execute("SELECT COUNT(*), SUM(balance) FROM users")
        row  = await cur.fetchone()
        cur2 = await db.execute("SELECT COUNT(*) FROM banned_ips")
        ip_count = (await cur2.fetchone())[0] or 0
        cur3 = await db.execute("SELECT COUNT(*) FROM fraud_log")
        fraud_count = (await cur3.fetchone())[0] or 0
    await callback.message.edit_text(
        f"📊 <b>Bot Analytics:</b>\n\n"
        f"• Registered Users: <b>{row[0] or 0}</b>\n"
        f"• Outstanding Liabilities: <b>{float(row[1] or 0.0):.2f} ETB</b>\n"
        f"• Banned IPs: <b>{ip_count}</b>\n"
        f"• Fraud Log Entries: <b>{fraud_count}</b>",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.callback_query(F.data == "adm_cmd_backup_now")
async def process_backup_now(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    if not DB_BACKUP_CHANNEL_ID:
        await callback.answer("⚠️ DB_BACKUP_CHANNEL_ID is not set in Railway Variables.", show_alert=True)
        return
    await callback.answer("📤 Sending backup…")
    await send_db_backup()
    await callback.message.edit_text(
        "✅ <b>Backup sent</b> to your private backup channel.",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.callback_query(F.data == "adm_cmd_search")
async def process_search_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.lookup_individual_id)
    await callback.message.edit_text("🔍 <b>Enter Target User Telegram ID:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.lookup_individual_id)
async def process_search_execute(message: Message, state: FSMContext):
    await state.clear()
    try:
        target = int(message.text.strip())
    except ValueError:
        return await message.answer("❌ Invalid ID.", reply_markup=generate_admin_dashboard())
    user = await DataEngine.get_user(target)
    if not user:
        return await message.answer("❌ User not found.", reply_markup=generate_admin_dashboard())
    direct, tier2 = await DataEngine.get_referral_metrics(target)
    verif    = await DataEngine.get_verification(target)
    ip_info  = f"<code>{verif['ip_address']}</code>"  if verif else "Not verified"
    ref_ip   = f"<code>{verif['referrer_ip']}</code>" if (verif and verif['referrer_ip']) else "N/A"
    tg_plat  = (verif['tg_platform']    or "N/A") if verif else "N/A"
    tg_ver   = (verif['tg_version']     or "N/A") if verif else "N/A"
    tg_app   = (verif['tg_app_version'] or "N/A") if verif else "N/A"
    # aiosqlite.Row has no .get() — index it (BUG-REPORT #5: the .get()
    # calls crashed Search User for every user WITH a verifications row).
    canvas   = (verif['canvas_hash'][:16] + "…" if verif and verif['canvas_hash'] else "N/A")
    webgl    = (verif['webgl_hash'][:16]  + "…" if verif and verif['webgl_hash']  else "N/A")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT reason, logged_at FROM fraud_log WHERE user_id = ? ORDER BY logged_at DESC LIMIT 3",
            (target,)
        )
        fraud_rows = await cur.fetchall()
    fraud_txt = ""
    if fraud_rows:
        fraud_txt = "\n⚠️ <b>Fraud Flags:</b>\n" + "\n".join(
            f"  • <code>{r['reason']}</code> @ {r['logged_at']}" for r in fraud_rows
        )

    await message.answer(
        f"👤 <b>User Profile:</b>\n\n"
        f"• Name: {sanitize_html(user['full_name'])}\n"
        f"• Alias: @{sanitize_html(user['username'] or 'N/A')}\n"
        f"• Balance: <b>{float(user['balance']):.2f} Birr</b>\n"
        f"• Direct Invites: <b>{direct}</b>\n"
        f"• Tier-2 Network: <b>{tier2}</b>\n"
        f"• Banned: <b>{'Yes' if user['is_banned'] else 'No'}</b>\n"
        f"• IP Address: {ip_info}\n"
        f"• Referrer IP: {ref_ip}\n"
        f"• TG Platform: <b>{tg_plat}</b>\n"
        f"• TG Version: <b>{tg_ver}</b>\n"
        f"• TG App Version: <b>{tg_app}</b>\n"
        f"• Canvas Hash: <code>{canvas}</code>\n"
        f"• WebGL Hash: <code>{webgl}</code>\n"
        f"• Joined: {user['joined_at']}"
        f"{fraud_txt}",
        reply_markup=generate_admin_dashboard()
    )

@core_router.callback_query(F.data == "adm_cmd_ban")
async def process_ban_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.ban_individual_id)
    await callback.message.edit_text(
        "🚫 <b>Ban User</b>\n\nEnter the Telegram User ID to ban:",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.ban_individual_id)
async def process_ban_execute(message: Message, state: FSMContext):
    await state.clear()
    try:
        target = int(message.text.strip())
    except ValueError:
        return await message.answer("❌ Invalid ID.", reply_markup=generate_admin_dashboard())
    await DataEngine.ban_user(target, 1)
    try:
        await bot.send_message(target, "🚫 <b>Your account has been banned from this bot.</b>")
    except Exception:
        pass
    await message.answer(f"✅ User <code>{target}</code> has been banned.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_reward")
async def process_reward_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.modify_referral_bounty)
    await callback.message.edit_text("<b>Enter New Reward Per Referral (Birr):</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.modify_referral_bounty)
async def process_reward_execute(message: Message, state: FSMContext):
    # BUG-REPORT #11: the raw text used to be stored verbatim — one typo
    # ("ten") poisoned reward_per_referral, and every float() conversion
    # of it in user-facing endpoints (e.g. /api/me) then raised ValueError
    # and broke the app for EVERYONE until an admin fixed the setting.
    try:
        val = float(message.text.strip())
        if not (0 < val <= 10000):
            raise ValueError
    except (TypeError, ValueError):
        await state.clear()
        return await message.answer("❌ Invalid amount — enter a number like 10.", reply_markup=generate_admin_dashboard())
    await DataEngine.set_setting("reward_per_referral", str(val))
    await state.clear()
    await message.answer("✅ Bounty updated.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_min_wd")
async def process_min_wd_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.modify_minimum_cashout)
    await callback.message.edit_text("<b>Enter New Minimum Cashout Threshold (Birr):</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.modify_minimum_cashout)
async def process_min_wd_execute(message: Message, state: FSMContext):
    # BUG-REPORT #11: same unvalidated-store bug as the referral bounty —
    # min_withdrawal is float()-converted in user endpoints, so a
    # non-numeric value here broke withdrawals for everyone.
    try:
        val = float(message.text.strip())
        if not (0 < val <= 1000000):
            raise ValueError
    except (TypeError, ValueError):
        await state.clear()
        return await message.answer("❌ Invalid amount — enter a number like 50.", reply_markup=generate_admin_dashboard())
    await DataEngine.set_setting("min_withdrawal", str(val))
    await state.clear()
    await message.answer("✅ Minimum withdrawal updated.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_pending_tickets")
async def process_pending_inventory(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    pending = await DataEngine.get_pending_withdrawals()
    if not pending:
        return await callback.message.edit_text("📭 No pending withdrawals.", reply_markup=generate_fallback_navigation("ui_admin_core"))
    lines = [f"• <b>#{t['id']}</b> — {sanitize_html(t['full_name'])} — <code>{float(t['amount']):.2f} ETB</code>" for t in pending]
    await callback.message.edit_text(
        f"📥 <b>Pending Withdrawals ({len(pending)})</b>\n\n" + "\n".join(lines),
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )


broadcast_button_select = State()

@core_router.callback_query(F.data == "adm_cmd_broadcast")
async def process_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.broadcast_intel_payload)
    await callback.message.edit_text("📢 <b>Enter Broadcast Message (Text):</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.broadcast_intel_payload)
async def process_broadcast_button_prompt(message: Message, state: FSMContext):
    text = message.text
    await state.update_data(bc_payload=text)
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Watch Ads Now", callback_data="bc_btn_watch")],
        [InlineKeyboardButton(text="🎁 Invite & Earn Rewards", callback_data="bc_btn_invite")],
        [InlineKeyboardButton(text="🎯 Complete Tasks & Earn", callback_data="bc_btn_tasks")],
        [InlineKeyboardButton(text="🎁 Claim Daily Bonus", callback_data="bc_btn_daily")],
        [InlineKeyboardButton(text="📱 Open Mini App", callback_data="bc_btn_app")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="ui_admin_core")]
    ])
    await message.answer("🔘 <b>Select Inline Button Title & Emoji for Broadcast:</b>", reply_markup=markup)

@core_router.callback_query(F.data.startswith("bc_btn_"))
async def process_broadcast_preview(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    action = callback.data
    btn_map = {
        "bc_btn_watch": "🎬 Watch Ads Now",
        "bc_btn_invite": "🎁 Invite & Earn Rewards",
        "bc_btn_tasks": "🎯 Complete Tasks & Earn",
        "bc_btn_daily": "🎁 Claim Daily Bonus",
        "bc_btn_app": "📱 Open Mini App"
    }
    btn_text = btn_map.get(action, "📱 Open Mini App")
    await state.update_data(bc_btn_text=btn_text)
    
    data = await state.get_data()
    text = data.get("bc_payload", "")
    
    await state.set_state(AdminConsoleWorkflow.broadcast_confirmation)
    
    # Generate preview markup
    # We need a sample uid or placeholder webapp
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_text, web_app=WebAppInfo(url=f"{FRONTEND_URL}/app.html?uid={callback.from_user.id}"))],
        [InlineKeyboardButton(text="🚀 Send Now", callback_data="bc_action_confirm")],
        [InlineKeyboardButton(text="✍️ Restart Broadcast", callback_data="adm_cmd_broadcast")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="ui_admin_core")],
    ])
    await callback.message.edit_text(f"📝 <b>Broadcast Preview:</b>\n\n{text}\n\n🔘 <i>Button: {btn_text}</i>\n\n⚠️ Send to all users?", reply_markup=markup)


@core_router.callback_query(F.data == "bc_action_confirm", AdminConsoleWorkflow.broadcast_confirmation)
async def process_broadcast_execute(callback: CallbackQuery, state: FSMContext):
    global _broadcast_running
    if _broadcast_running:
        await callback.answer("⚠️ A broadcast is already in progress — please wait for it to finish.", show_alert=True)
        return
    async with _broadcast_lock:
        if _broadcast_running:
            await callback.answer("⚠️ A broadcast is already in progress — please wait for it to finish.", show_alert=True)
            return
        _broadcast_running = True
    await callback.answer()

    s    = await state.get_data()
    text = s["bc_payload"]
    btn_text = s.get("bc_btn_text", "📱 Open Mini App")
    await state.clear()
    progress = await callback.message.edit_text("⏳ Sending broadcast with custom button...")

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur   = await db.execute("SELECT user_id FROM users")
            nodes = await cur.fetchall()
    except Exception:
        _broadcast_running = False
        raise
        
    sent_count = 0
    fail_count = 0
    try:
        for i, (uid,) in enumerate(nodes, start=1):
            try:
                url = f"{FRONTEND_URL}/app.html?uid={uid}"
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=btn_text, web_app=WebAppInfo(url=url))
                ]])
                await bot.send_message(uid, text, reply_markup=markup)
                sent_count += 1
            except Exception as e:
                err_str = str(e)
                if "RetryAfter" in err_str:
                    try:
                        wait = int(''.join(filter(str.isdigit, err_str))) + 1
                    except Exception:
                        wait = 30
                    await asyncio.sleep(wait)
                    try:
                        await bot.send_message(uid, text)
                        sent_count += 1
                    except Exception:
                        fail_count += 1
                else:
                    fail_count += 1

            # Pace the broadcast so we don't trip Telegram's flood limits: a
            # small delay after every message, plus a longer breather every
            # BROADCAST_BATCH_SIZE messages.
            await asyncio.sleep(BROADCAST_DELAY)
            if i % BROADCAST_BATCH_SIZE == 0:
                await asyncio.sleep(BROADCAST_BATCH_PAUSE)
                try:
                    await progress.edit_text(f"⏳ Sending broadcast... {i}/{len(nodes)} sent so far.")
                except Exception:
                    pass
    finally:
        _broadcast_running = False

    try:
        await progress.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"✅ Broadcast complete.\n• Sent: {sent_count}\n• Failed: {fail_count}",
        reply_markup=generate_admin_dashboard()
    )

@core_router.callback_query(F.data == "adm_cmd_unban_menu")
async def process_unban_dashboard(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Standard Unban (Requires MiniApp)", callback_data="unban_trigger_std")],
        [InlineKeyboardButton(text="🔥 Full Unban (Bypass MiniApp)",       callback_data="unban_trigger_full")],
        [InlineKeyboardButton(text="🔙 Back",                               callback_data="ui_admin_core")],
    ])
    await callback.message.edit_text("🔓 <b>Select Unban Method:</b>", reply_markup=markup)

@core_router.callback_query(F.data == "unban_trigger_std")
async def process_std_unban_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.pardon_individual_std)
    await callback.message.edit_text(
        "👤 <b>[Standard Unban]</b> Enter Telegram User ID:",
        reply_markup=generate_fallback_navigation("adm_cmd_unban_menu")
    )

@core_router.message(AdminConsoleWorkflow.pardon_individual_std)
async def process_std_unban_execute(message: Message, state: FSMContext):
    try:
        target = int(message.text.strip())
        await DataEngine.ban_user(target, 0)
        await DataEngine.full_clear_verification(target)
        await state.clear()
        await message.answer(
            f"✅ Standard Unban done. User <code>{target}</code> must reverify via Mini App.",
            reply_markup=generate_admin_dashboard()
        )
    except ValueError:
        await message.answer("❌ Invalid ID.")

@core_router.callback_query(F.data == "unban_trigger_full")
async def process_full_unban_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.pardon_individual_full)
    await callback.message.edit_text(
        "🔥 <b>[Full Unban]</b> Enter Telegram User ID:",
        reply_markup=generate_fallback_navigation("adm_cmd_unban_menu")
    )

@core_router.message(AdminConsoleWorkflow.pardon_individual_full)
async def process_full_unban_execute(message: Message, state: FSMContext):
    try:
        target = int(message.text.strip())
        # IMPORTANT: inject_fake_verification() writes straight into the
        # verifications table, which makes is_verified(target)=True even
        # though the user never actually completed the Mini App fingerprint
        # flow. This is intentionally scoped to ONLY the target user_id
        # passed in here — it must never be applied in bulk, since it
        # bypasses fraud/fingerprint checks for whoever it's used on.
        await DataEngine.ban_user(target, 0)
        await DataEngine.inject_fake_verification(target)
        await state.clear()
        await message.answer(
            f"🔥 Full Unban done. User <code>{target}</code> is unbanned and "
            f"marked verified — they will NOT be asked to open the Mini App again.",
            reply_markup=generate_admin_dashboard()
        )
    except ValueError:
        await message.answer("❌ Invalid ID.")


# ═════════════════════════════════════════════════════════════════════════
# WIRING — attach all @core_router handlers to the Dispatcher
#
# CRITICAL FIX: this call was missing entirely. Every @core_router.message
# and @core_router.callback_query handler defined above (process_start_
# command, the whole admin panel, withdrawals, everything) was NEVER
# reachable, because a Router only does anything once it's registered on
# the Dispatcher that's actually polling Telegram.
# ═════════════════════════════════════════════════════════════════════════
dp.include_router(core_router)


class BotPausedGateMiddleware(BaseMiddleware):
    """Implements the admin 'Stop Bot' pause (BUG-REPORT #6 fix). While
    the bot_paused setting is on, every non-admin message/callback is
    turned away before any handler runs; admins are never gated, so the
    admin panel — and crucially the Resume button, which the old
    stop_polling() implementation made permanently unreachable — keeps
    working."""

    async def __call__(self, handler, event, data):
        try:
            paused = (await DataEngine.get_setting("bot_paused", "0")) == "1"
        except Exception:
            paused = False
        if paused:
            from_user = getattr(event, "from_user", None)
            if not (from_user and evaluate_admin_access(from_user.id)):
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer(
                            "🛑 The bot is temporarily paused. Please try again later.",
                            show_alert=True,
                        )
                    except Exception:
                        pass
                return None
        return await handler(event, data)


dp.message.outer_middleware(BotPausedGateMiddleware())
dp.callback_query.outer_middleware(BotPausedGateMiddleware())

# ═════════════════════════════════════════════════════════════════════════
# WEB API — Mini App REST backend (FastAPI)
#
# CRITICAL FIX: fastapi/uvicorn were imported at the top of this file and
# referenced in comments ("...the Mini App REST endpoint (/api/withdraw)")
# as if this layer already existed, but no FastAPI app, no routes, and no
# uvicorn server were ever defined anywhere in the file. frontend/app.html
# was written entirely against this contract, so none of it could work
# until now.
#
# Every route is called by app.html's api() helper, which POSTs
# { initData, ...extra } as JSON. initData is Telegram's signed WebApp
# payload — validated below via parse_telegram_webapp_handshake() so a
# user cannot spoof another user's Telegram ID from the browser.
# ═════════════════════════════════════════════════════════════════════════
api_app = FastAPI()

api_app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@api_app.middleware("http")
async def _no_store_cache_headers(request, call_next):
    """Every /api/* response carries per-user data (balance, referrals,
    withdrawal history, ...). Without explicit no-store headers, some
    mobile browsers/webviews will happily serve a cached copy of a
    previous response — which on a shared device could mean showing one
    Telegram account's balance to whoever opens the Mini App next. This
    guarantees every response is always freshly fetched."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


class ApiBase(BaseModel):
    initData: str = ""


async def _authenticate(body: ApiBase) -> dict:
    """Validates Telegram WebApp initData and returns the users-table row
    as a plain dict. Raises 403 'not_verified' if this user has never
    completed /api/verify — this is what actually enforces the channel
    gate, fingerprint/VPN/clone-risk checks, and referral attribution,
    since none of those run anywhere else. Without this check, anyone
    opening app.html directly (Telegram's native bot-profile "Open App"
    button, or any other direct link) would get a fully working, fully
    unverified account with zero fraud checks and no referrer ever
    attributed — the entire index.html verification flow would be
    optional rather than required."""
    tg_user = parse_telegram_webapp_handshake(body.initData)
    if not tg_user or "id" not in tg_user:
        raise HTTPException(status_code=401, detail="invalid_init_data")
    user_id = int(tg_user["id"])
    if not await DataEngine.is_verified(user_id):
        raise HTTPException(status_code=403, detail="not_verified")
    row = await DataEngine.get_user(user_id)
    if not row:
        # Verified (has a verifications row) but somehow missing its users
        # row — self-heal instead of crashing, same pattern used elsewhere
        # (e.g. the admin "Full Unban" path). No referrer here since a
        # genuinely fresh signup would have gone through /api/verify,
        # which always creates the users row itself.
        full_name = (
            f"{tg_user.get('first_name', '')} {tg_user.get('last_name', '')}".strip()
            or tg_user.get("username", "") or str(user_id)
        )
        await DataEngine.create_user(user_id, tg_user.get("username", "") or "", full_name)
        row = await DataEngine.get_user(user_id)
    await DataEngine.touch_last_seen(user_id)
    if row["is_banned"]:
        raise HTTPException(status_code=403, detail="banned")
    return dict(row)


def _require_admin(user: dict):
    if not evaluate_admin_access(user["user_id"]):
        raise HTTPException(status_code=403, detail="not_admin")


async def broadcast_to_all_users(text: str, reply_markup: InlineKeyboardMarkup | None = None) -> tuple[int, int]:
    """Shared broadcast sender — used by /api/admin/broadcast (free-text
    announcements) and /api/admin/tasks/broadcast (new-task announcements
    with an Open App button)."""
    global _broadcast_running
    if _broadcast_running:
        raise HTTPException(status_code=409, detail="broadcast_already_in_progress")
    async with _broadcast_lock:
        if _broadcast_running:
            raise HTTPException(status_code=409, detail="broadcast_already_in_progress")
        _broadcast_running = True

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
    sent, failed = 0, 0
    try:
        for i, (uid,) in enumerate(rows, start=1):
            try:
                markup = reply_markup if reply_markup else generate_app_webapp_button(uid)
                await bot.send_message(uid, text, reply_markup=markup)
                sent += 1
            except Exception as e:
                err_str = str(e)
                if "RetryAfter" in err_str:
                    try:
                        wait = int("".join(filter(str.isdigit, err_str))) + 1
                    except Exception:
                        wait = 30
                    await asyncio.sleep(wait)
                    try:
                        await bot.send_message(uid, text, reply_markup=reply_markup)
                        sent += 1
                    except Exception:
                        failed += 1
                else:
                    failed += 1

            # Same pacing as the admin-panel broadcast: slow, steady delivery
            # instead of blasting all users at once.
            await asyncio.sleep(BROADCAST_DELAY)
            if i % BROADCAST_BATCH_SIZE == 0:
                await asyncio.sleep(BROADCAST_BATCH_PAUSE)
    finally:
        _broadcast_running = False
    return sent, failed



@api_app.get("/api/withdrawals/recent")
async def api_recent_withdrawals():
    enabled = (await DataEngine.get_setting("ticker_enabled", "1")) == "1"
    if not enabled:
        return {"enabled": False, "withdrawals": []}

    # BUG-REPORT #9: this endpoint is unauthenticated (the public
    # home-screen ticker fetches it without login), so it must never
    # return real users' data — the old query leaked the last 20 real
    # withdrawals' full names and amounts to anyone on the internet.
    # The ticker now shows only the admin-configured custom names and
    # the built-in placeholder list below.
    real_list = []

    custom_raw = await DataEngine.get_setting("ticker_custom_names", "")
    custom_list = []
    if custom_raw:
        try:
            custom_list = json.loads(custom_raw)
        except Exception:
            pass

    # No built-in names: the ticker shows ONLY names the admin adds
    # (ticker_custom_names). With none configured it stays empty.
    combined = real_list + custom_list
    return {"enabled": True, "withdrawals": combined}

@api_app.get("/health")
async def health_check():
    return {"ok": True}


@api_app.get("/verify")
async def verify_page():
    # The standalone "Security Check" page (legacy index.html, restored in
    # hf-v5): served by THIS backend so the bot's verification widget can
    # point at WEBAPP_URL/verify. It collects the device fingerprint and
    # POSTs it to /api/verify on this same origin.
    from pathlib import Path
    html = (Path(__file__).parent / "verify.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ── /api/verify — identity/fingerprint verification (index.html) ────────
#
# CRITICAL FIX #2: this endpoint was ALSO completely missing. It's not
# part of the app.html contract — it's the fraud-detection intake used by
# index.html (the "Security Check" screen linked from generate_verifica-
# tion_widget()). This is where evaluate_clone_risk(), the IP cooldown,
# the VPN check, and the referral payout all actually run. Without this
# route, /start would send a "Verify identity" button whose Mini App link
# posted to /api/verify and got a 404 forever — which is exactly what
# showed up in the Railway logs.
#
# Response contract (matches index.html exactly):
#   200 {"status": "verified"}            → new account created & passed
#   200 {"status": "already_verified"}     → idempotent replay of an old link
#   200 {"status": "blocked", "reason": X} → soft or hard block, NOT an
#                                            HTTP error (index.html only
#                                            treats non-2xx as "server
#                                            error", so blocks must be 200)
#   429                                    → per-user rate limit hit
class VerifyRequest(BaseModel):
    uid: int
    refId: int = 0
    msgId: int = 0
    initData: str = ""
    ua: str = ""
    fingerprint: str = ""
    canvasHash: str = ""
    webglHash: str = ""
    screenSig: str = ""
    tgPlatform: str = ""
    tgVersion: str = ""
    tgAppVersion: str = ""


@api_app.post("/api/verify")
async def api_verify(body: VerifyRequest, request: Request):
    # The uid comes from a URL query param on index.html (?uid=...), which
    # is user-editable — so it must be cross-checked against the
    # cryptographically signed initData, which always reflects whoever is
    # ACTUALLY logged into the current Telegram session.
    tg_user = parse_telegram_webapp_handshake(body.initData)
    if not tg_user or "id" not in tg_user or int(tg_user["id"]) != body.uid:
        raise HTTPException(status_code=401, detail="invalid_init_data")

    uid = body.uid
    ref = body.refId if body.refId and body.refId != uid else 0
    if not ref:
        ref = await DataEngine.get_pending_referral(uid) or 0

    if await DataEngine.is_verified(uid):
        return {"status": "already_verified"}

    if not await verify_limiter.is_allowed(str(uid)):
        raise HTTPException(status_code=429, detail="too_many_attempts")

    client_ip = extract_real_ip(request)

    multi_detect_on = (await DataEngine.get_setting("multi_account_detect_enabled", "1")) == "1"

    if multi_detect_on:
        on_cooldown, remaining = await ip_cooldown.is_on_cooldown(client_ip)
        if on_cooldown:
            return {"status": "blocked", "reason": "ip_cooldown", "retry_after": remaining}

    fp_ok = bool(body.fingerprint) and body.fingerprint not in ("undefined", "null", "")
    if not fp_ok:
        return {"status": "blocked", "reason": "no_fingerprint"}

    flagged_username = tg_user.get("username", "") or ""

    if await DataEngine.is_ip_banned(client_ip):
        await DataEngine.log_fraud_attempt(
            uid, "banned_ip_attempt", client_ip,
            f"flagged_username=@{flagged_username}" if flagged_username else "flagged_username=none"
        )
        return {"status": "blocked", "reason": "banned_ip"}

    if await execute_network_vpn_lookup(client_ip):
        return {"status": "blocked", "reason": "vpn"}

    if multi_detect_on:
        should_ban, reason, ban_detail = await evaluate_clone_risk(
            uid, ref, client_ip, body.fingerprint,
            body.tgPlatform, body.tgVersion, body.tgAppVersion,
            body.canvasHash, body.webglHash, body.screenSig,
            flagged_username,
        )
    else:
        # Admin turned multi-account detection OFF (clone/IP-farm checks + IP cooldown skipped).
        should_ban, reason, ban_detail = False, "", ""
    if should_ban:
        detail = f"{ban_detail} — auto-blocked at verification" if ban_detail else "auto-blocked at verification"
        await DataEngine.log_fraud_attempt(uid, reason, client_ip, detail)
        if reason == "ip_farm":
            await DataEngine.ban_ip(client_ip, reason)
        return {"status": "blocked", "reason": reason}

    # Passed every check — atomically claim the verification slot FIRST.
    # This is the actual guard against double-paying a referral: if a
    # duplicate/concurrent request for this exact uid is racing this one
    # (double-tap, retry, two tabs), only one of the two INSERTs below can
    # win. The loser returns immediately, before touching the account or
    # the referrer's balance at all.
    claimed = await DataEngine.save_verification(
        uid, client_ip, body.ua, body.fingerprint,
        referrer_ip="", tg_platform=body.tgPlatform, tg_version=body.tgVersion,
        tg_app_version=body.tgAppVersion, canvas_hash=body.canvasHash,
        webgl_hash=body.webglHash, screen_sig=body.screenSig,
    )
    if not claimed:
        return {"status": "already_verified"}

    await ip_cooldown.mark_verified(client_ip)

    # Create the account (linking the referrer only on first creation) and
    # pay the referrer's bonus — safe now, since `claimed` above guarantees
    # this is the one and only request that will ever reach this point for
    # this uid.
    referrer_row = await DataEngine.get_user(ref) if ref else None
    referred_by = ref if referrer_row else None

    existing = await DataEngine.get_user(uid)
    if not existing:
        full_name = (
            f"{tg_user.get('first_name', '')} {tg_user.get('last_name', '')}".strip()
            or tg_user.get("username", "") or str(uid)
        )
        await DataEngine.create_user(uid, tg_user.get("username", "") or "", full_name, referred_by)
    if ref:
        # Used (or superseded by an existing account) — clear it either way
        # so it doesn't linger and get picked up by some unrelated future flow.
        await DataEngine.clear_pending_referral(uid)

    if referred_by:
        direct_count, _ = await DataEngine.get_referral_metrics(referred_by)
        # direct_count already includes the referral we just created above,
        # so it IS this referral's 1-indexed position for this referrer.
        skipped = await DataEngine.is_referral_skipped(referred_by, direct_count)
        if not skipped:
            rate = float(await DataEngine.get_setting("reward_per_referral", "10"))
            await DataEngine.add_balance(referred_by, rate)
            try:
                await bot.send_message(
                    referred_by,
                    f"🎉 Someone joined using your referral link! +{rate:.2f} Birr credited to your balance."
                )
            except Exception:
                pass
        else:
            # Not paid — and hidden from the referrer's own "Direct" count
            # too (get_paid_referral_metrics), so the skip pattern stays
            # invisible to them in every number they can see, not just in
            # the missing payout message.
            await DataEngine.mark_referral_unpaid(uid)

    if body.msgId:
        try:
            await bot.delete_message(chat_id=uid, message_id=body.msgId)
        except Exception:
            pass
    try:
        await bot.send_message(
            uid, "✅ <b>Verified!</b> Welcome in.",
            reply_markup=generate_dashboard_matrix(uid)
        )
    except Exception:
        pass

    return {"status": "verified"}


# ── /api/me ──────────────────────────────────────────────────────────────
@api_app.post("/api/me")
async def api_me(body: ApiBase):
    user = await _authenticate(body)
    direct, tier2 = await DataEngine.get_paid_referral_metrics(user["user_id"])
    rate = float(await DataEngine.get_setting("reward_per_referral", "10"))
    min_w = float(await DataEngine.get_setting("min_withdrawal", "50"))
    usdt_rate = float(await DataEngine.get_setting("usdt_rate", "155"))
    uname = BOT_USERNAME or (await bot.get_me()).username
    return {
        "user_id": user["user_id"],
        "balance": float(user["balance"]),
        "referrals": direct,
        "tier2_referrals": tier2,
        "reward_per_referral": rate,
        "min_withdrawal": min_w,
        "usdt_rate": usdt_rate,
        "total_earned_refs": round(direct * rate, 2),
        "referral_link": f"https://t.me/{uname}?start={user['user_id']}",
        "is_admin": evaluate_admin_access(user["user_id"]),
        "support_username": (await DataEngine.get_setting("support_username", "") or "").lstrip("@"),
        "withdrawal_process_time": await get_withdrawal_process_text(),
    }


# ── /api/invite/share_card — sends the user a ready-to-forward invite
#    message (bot's photo + a written invite caption + an inline button
#    holding their referral link) in their own chat with the bot, so they
#    can just long-press → Forward it straight to friends. ────────────────
SHARE_CARD_COOLDOWN_SECONDS = 25 * 60


@api_app.post("/api/invite/share_card")
async def api_invite_share_card(body: ApiBase):
    user = await _authenticate(body)
    uid = user["user_id"]

    last_at = await DataEngine.get_last_ad_event(uid, "share_card")
    if last_at:
        try:
            elapsed = (datetime.utcnow() - datetime.strptime(last_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
            remaining = int(SHARE_CARD_COOLDOWN_SECONDS - elapsed)
            if remaining > 0:
                raise HTTPException(status_code=400, detail=f"cooldown:{remaining}")
        except HTTPException:
            raise
        except Exception:
            pass  # bad timestamp format — don't block the user over it

    caption, kb, photo_id = await _build_invite_card(uid)
    try:
        if photo_id:
            await bot.send_photo(chat_id=uid, photo=photo_id, caption=caption, reply_markup=kb)
        else:
            await bot.send_message(chat_id=uid, text=caption, reply_markup=kb)
    except Exception:
        raise HTTPException(status_code=503, detail="send_failed")
    await DataEngine.record_ad_event(uid, "share_card", 0)
    return {"ok": True}


# ── /api/gate/* — force-join gate ────────────────────────────────────────
@api_app.post("/api/gate/status")
async def api_gate_status(body: ApiBase):
    user = await _authenticate(body)
    uid = user["user_id"]
    force_unjoined = []
    if not evaluate_admin_access(uid):
        force_unjoined = await inspect_compulsory_memberships(uid)
    all_channels = await DataEngine.get_force_channels()
    fake_channels = [dict(c) for c in all_channels if c["bot_added"] == 1]
    fake_seen = await DataEngine.has_seen_fake_join(uid)
    return {
        "force_unjoined": [dict(c) for c in force_unjoined],
        "fake_channels": fake_channels,
        "fake_seen": fake_seen,
    }


@api_app.post("/api/gate/fake_seen")
async def api_gate_fake_seen(body: ApiBase):
    user = await _authenticate(body)
    await DataEngine.mark_fake_join_seen(user["user_id"])
    return {"ok": True}


# ── /api/ads/* — AdsGram rewarded video + Direct Link ────────────────────
AD_KIND_VIDEO  = "video"
AD_KIND_DIRECT = "direct_link"

_adsgram_secret_cache: str | None = None

async def get_adsgram_s2s_secret() -> str:
    """The secret that guards /api/ads/adsgram-reward. Prefers the
    ADSGRAM_S2S_SECRET env var if an admin set one; otherwise a random
    secret is generated ONCE and persisted in settings, so the reward
    postback is never left unprotected just because nobody configured
    an env var. Cached in-process to avoid a DB hit on every ad reward."""
    global _adsgram_secret_cache
    if _adsgram_secret_cache:
        return _adsgram_secret_cache

    env_secret = os.getenv("ADSGRAM_S2S_SECRET", "").strip()
    if env_secret:
        _adsgram_secret_cache = env_secret
        return env_secret

    stored = (await DataEngine.get_setting("adsgram_s2s_secret", "") or "").strip()
    if not stored:
        stored = secrets.token_urlsafe(24)
        await DataEngine.set_setting("adsgram_s2s_secret", stored)
    _adsgram_secret_cache = stored
    return stored


_tads_webhook_secret_cache: str | None = None

async def get_tads_webhook_secret() -> str:
    """The secret that guards /api/ads/tads-webhook. Same pattern as
    get_adsgram_s2s_secret: prefers the TADS_WEBHOOK_SECRET env var if
    an admin set one; otherwise a random secret is generated ONCE and
    persisted in settings. If you already pasted a specific secret into
    Tads' widget 'Webhook URL' field before this existed, set
    TADS_WEBHOOK_SECRET to that exact value so it keeps matching —
    otherwise copy the URL shown in /api/admin/settings (admin
    dashboard) into Tads' widget config instead."""
    global _tads_webhook_secret_cache
    if _tads_webhook_secret_cache:
        return _tads_webhook_secret_cache

    env_secret = os.getenv("TADS_WEBHOOK_SECRET", "").strip()
    if env_secret:
        _tads_webhook_secret_cache = env_secret
        return env_secret

    stored = (await DataEngine.get_setting("tads_webhook_secret", "") or "").strip()
    if not stored:
        stored = secrets.token_urlsafe(24)
        await DataEngine.set_setting("tads_webhook_secret", stored)
    _tads_webhook_secret_cache = stored
    return stored


class AdClickRequest(ApiBase):
    kind: str = "video"


@api_app.post("/api/ads/click")
async def api_ads_click(body: AdClickRequest):
    """Fire-and-forget analytics ping — called the instant the user taps
    'Watch Ad', before we know if they'll actually finish it. Purely for
    the admin 'clicks vs completions' dashboard; never affects rewards
    or daily limits."""
    user = await _authenticate(body)
    kind = body.kind if body.kind in (AD_KIND_VIDEO, AD_KIND_DIRECT) else AD_KIND_VIDEO
    await DataEngine.log_ad_click(user["user_id"], kind)
    return {"ok": True}


@api_app.post("/api/ads/status")
async def api_ads_status(body: ApiBase):
    user = await _authenticate(body)
    uid = user["user_id"]

    ads_enabled      = (await DataEngine.get_setting("ads_enabled", "0")) == "1"
    block_id         = await DataEngine.get_setting("adsgram_block_id", "") or ""
    reward_amount    = float(await DataEngine.get_setting("ad_reward_amount", "0.5"))
    daily_limit      = int(await DataEngine.get_setting("ad_daily_limit", "10"))
    cooldown_seconds = int(await DataEngine.get_setting("ad_cooldown_seconds", "30"))
    batch_size             = int(await DataEngine.get_setting("ad_batch_size", "0"))
    batch_cooldown_seconds = int(await DataEngine.get_setting("ad_batch_cooldown_seconds", "3600"))

    watched_today = await DataEngine.count_ad_events_today(uid, AD_KIND_VIDEO)
    seconds_left = 0
    is_batch_wait = False
    last_at = await DataEngine.get_last_ad_event(uid, AD_KIND_VIDEO)
    if last_at:
        try:
            elapsed = (datetime.utcnow() - datetime.strptime(last_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
            # Same rule as claim_video_ad_atomic: if the next ad would
            # start a fresh batch, show the longer batch wait instead of
            # the normal per-ad one, so the countdown the user sees
            # matches what will actually be enforced when they claim.
            is_batch_wait = batch_size > 0 and watched_today > 0 and watched_today % batch_size == 0
            effective_cooldown = batch_cooldown_seconds if is_batch_wait else cooldown_seconds
            seconds_left = max(0, int(effective_cooldown - elapsed))
        except Exception:
            pass

    direct_link         = (await DataEngine.get_setting("adsgram_direct_link", "") or "").strip()
    dl_reward           = float(await DataEngine.get_setting("direct_link_reward_amount", "0.3"))
    dl_daily_limit       = int(await DataEngine.get_setting("direct_link_daily_limit", "10"))
    dl_wait_seconds      = int(await DataEngine.get_setting("direct_link_wait_seconds", "15"))
    dl_cooldown_seconds  = int(await DataEngine.get_setting("direct_link_cooldown_seconds", "30"))
    dl_watched_today     = await DataEngine.count_ad_events_today(uid, AD_KIND_DIRECT)
    dl_seconds_left = 0
    dl_last_at = await DataEngine.get_last_ad_event(uid, AD_KIND_DIRECT)
    if dl_last_at:
        try:
            elapsed = (datetime.utcnow() - datetime.strptime(dl_last_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
            dl_seconds_left = max(0, int(dl_cooldown_seconds - elapsed))
        except Exception:
            pass

    # Monetag — fallback rewarded network, tried client-side only when
    # AdsGram reports no fill. Doesn't have its own limit/cooldown; it
    # shares the video ad's daily_limit/cooldown/reward above.
    monetag_zone_id = (await DataEngine.get_setting("monetag_zone_id", "") or "").strip()
    monetag_sdk_url = (await DataEngine.get_setting("monetag_sdk_url", "") or "").strip()

    # Tads — second/gating ad shown right after AdsGram's video ad
    # finishes; see gate_adsgram_confirmation for the payout logic.
    tads_enabled   = (await DataEngine.get_setting("tads_enabled", "0")) == "1"
    tads_widget_id = (await DataEngine.get_setting("tads_widget_id", "") or "").strip()

    reminder_enabled   = (await DataEngine.get_setting("ad_click_reminder_enabled", "1")) == "1"
    reminder_disguised = (await DataEngine.get_setting("ad_click_reminder_disguised", "0")) == "1"
    reminder_text      = await DataEngine.get_setting("ad_click_reminder_text", "Please wait until the ad finishes — do not close it early")
    reminder_disguise_text = await DataEngine.get_setting("ad_click_reminder_disguise_text", "⚠️ Please tap the ad! You must click the ad to earn your reward.")
    reminder_network_text  = await DataEngine.get_setting("ad_click_reminder_network_text", "⚠️ Network error. Please try again.")
    require_banner_click   = (await DataEngine.get_setting("ad_require_banner_click", "0")) == "1"

    return {
        "enabled": ads_enabled and bool(block_id or monetag_zone_id),
        "block_id": block_id,
        "reward_amount": reward_amount,
        "daily_limit": daily_limit,
        "watched_today": watched_today,
        "seconds_left": seconds_left,
        "batch_size": batch_size,
        "is_batch_wait": is_batch_wait,
        "will_pay_next": await DataEngine.will_next_video_attempt_pay(uid),
        "require_banner_click": require_banner_click,
        "click_reminder": {
            "enabled": reminder_enabled,
            "disguised": reminder_disguised,
            "text": reminder_text,
            "disguise_text": reminder_disguise_text,
            "network_text": reminder_network_text,
        },
        "monetag": {
            "zone_id": monetag_zone_id,
            "sdk_url": monetag_sdk_url,
        },
        "tads": {
            "enabled": tads_enabled and bool(tads_widget_id),
            "widget_id": tads_widget_id,
        },
        "direct_link": {
            "enabled": ads_enabled and bool(direct_link),
            "url": direct_link,
            "reward_amount": dl_reward,
            "daily_limit": dl_daily_limit,
            "watched_today": dl_watched_today,
            "seconds_left": dl_seconds_left,
            "wait_seconds": dl_wait_seconds,
        },
    }


# ── /api/dailycheck — watch one ad, then claim a once-a-day bonus ────────
@api_app.post("/api/dailycheck/status")
async def api_dailycheck_status(body: ApiBase):
    user = await _authenticate(body)
    uid = user["user_id"]
    enabled = (await DataEngine.get_setting("daily_checkin_enabled", "1")) == "1"
    reward  = float(await DataEngine.get_setting("daily_checkin_reward", "5"))
    reminder_text = await DataEngine.get_setting(
        "daily_checkin_reminder_text", "⚠️ Couldn't confirm your ad view — try again in a moment"
    )
    status  = await DataEngine.get_daily_checkin_status(uid)
    return {
        "enabled": enabled,
        "reward": reward,
        "claimed_today": status["claimed_today"],
        "streak": status["streak"],
        "ad_watched_recently": await DataEngine.has_recent_checkin_ad(uid),
        "reminder_text": reminder_text,
    }


@api_app.post("/api/dailycheck/arm")
async def api_dailycheck_arm(body: ApiBase):
    """Called right before the frontend shows the AdsGram ad for the
    Daily Check-in flow. Marks the next /api/ads/adsgram-reward postback
    for this user as belonging to Daily Check-in, so it skips the
    separate ad_reward_amount payout (see claim_video_ad_atomic /
    daily_checkin_ad_pending) instead of paying that on top of the daily
    bonus itself.

    Server-side gate: this is the ONLY reliable place to stop repeat
    check-in ad views for the day — a disabled button on the frontend
    is just UI state (stale after a reload, a second tab, or a replayed
    request), not a real limit. Refusing to arm once today's check-in
    is already claimed means no ad gets shown at all for a repeat
    attempt, instead of showing one that was always going to be
    pointless (and, before this, still got recorded as a view)."""
    user = await _authenticate(body)
    uid = user["user_id"]
    status = await DataEngine.get_daily_checkin_status(uid)
    if status["claimed_today"]:
        raise HTTPException(status_code=400, detail="already_claimed")
    await DataEngine.mark_daily_checkin_ad_pending(uid)
    return {"ok": True}


@api_app.post("/api/dailycheck/claim")
async def api_dailycheck_claim(body: ApiBase):
    """Requires a genuinely S2S-confirmed ad view (see
    has_recent_completed_ad) in the last few minutes before the once-daily
    bonus can be claimed — same trust model as the regular ad reward, so
    this can't be claimed by just calling the API without watching
    anything."""
    user = await _authenticate(body)
    uid = user["user_id"]

    enabled = (await DataEngine.get_setting("daily_checkin_enabled", "1")) == "1"
    if not enabled:
        raise HTTPException(status_code=400, detail="disabled")

    # Fast-fail pre-check only — claim_daily_checkin_atomic re-verifies
    # (and consumes) the armed view inside its transaction. A regular
    # paid Watch & Earn view does not satisfy this (BUG-REPORT #7).
    if not await DataEngine.has_recent_checkin_ad(uid):
        raise HTTPException(status_code=400, detail="watch_ad_first")

    reward = float(await DataEngine.get_setting("daily_checkin_reward", "5"))
    ok, reason, streak = await DataEngine.claim_daily_checkin_atomic(uid, reward)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    fresh = await DataEngine.get_user(uid)
    return {"status": "claimed", "reward": reward, "streak": streak, "balance": float(fresh["balance"])}


@api_app.post("/api/ads/claim")
async def api_ads_claim(body: ApiBase):
    """Advisory pre-check — allows showing the ad, while reward payout
    is controlled atomically in claim_video_ad_atomic."""
    user = await _authenticate(body)
    uid = user["user_id"]

    ads_enabled = (await DataEngine.get_setting("ads_enabled", "0")) == "1"
    block_id = (await DataEngine.get_setting("adsgram_block_id", "") or "").strip()
    monetag_zone_id = (await DataEngine.get_setting("monetag_zone_id", "") or "").strip()
    if not ads_enabled or not (block_id or monetag_zone_id):
        raise HTTPException(status_code=400, detail="ads_disabled")

    daily_limit      = int(await DataEngine.get_setting("ad_daily_limit", "10"))
    cooldown_seconds = int(await DataEngine.get_setting("ad_cooldown_seconds", "30"))
    batch_size             = int(await DataEngine.get_setting("ad_batch_size", "0"))
    batch_cooldown_seconds = int(await DataEngine.get_setting("ad_batch_cooldown_seconds", "3600"))

    watched_today = await DataEngine.count_ad_events_today(uid, AD_KIND_VIDEO)
    if watched_today >= daily_limit:
        raise HTTPException(status_code=400, detail="daily_limit_reached")
    last_at = await DataEngine.get_last_ad_event(uid, AD_KIND_VIDEO)
    if last_at:
        try:
            elapsed = (datetime.utcnow() - datetime.strptime(last_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
            # Mirrors claim_video_ad_atomic exactly — this is only a
            # pre-check (the real gate is atomic, at reward time), but it
            # needs to agree with that gate, or a user sails through this
            # check mid-batch-cooldown, sits through a full ad, and only
            # then discovers at payout time that it was never going to
            # pay. Same batch-boundary rule: watched_today already lands
            # on a multiple of batch_size means the NEXT ad needs the
            # longer wait, not the short one.
            starting_new_batch = (
                batch_size > 0 and watched_today > 0 and watched_today % batch_size == 0
            )
            effective_cooldown = batch_cooldown_seconds if starting_new_batch else cooldown_seconds
            if elapsed < effective_cooldown:
                raise HTTPException(
                    status_code=400,
                    detail="batch_cooldown_active" if starting_new_batch else "cooldown_active",
                )
        except HTTPException:
            raise
        except Exception:
            pass
    # A brand-new attempt is being granted — wipe any leftover click
    # signal (and any leftover AdsGram/Tads gate state) from an earlier,
    # already-abandoned attempt so it can never be mistaken for evidence
    # belonging to this new one.
    await DataEngine.clear_banner_click_signal(uid)
    await DataEngine.clear_ad_reward_gate(uid)
    will_pay = await DataEngine.will_next_video_attempt_pay(uid)
    return {"ok": True, "will_pay": will_pay}


@api_app.get("/api/ads/adsgram-reward")
async def api_ads_adsgram_reward(userid: int, secret: Optional[str] = None,
                                 transaction_id: Optional[str] = None,
                                 event_id: Optional[str] = None):
    """AdsGram server-to-server reward postback with secret token security.
    Set this as the 'Reward URL' on the video ad block in the AdsGram publisher dashboard:
        https://<this-backend-domain>/api/ads/adsgram-reward?userid=[userId]&secret=YOUR_SECRET
    The secret is ALWAYS required (see get_adsgram_s2s_secret) — either the
    ADSGRAM_S2S_SECRET env var, or an auto-generated one persisted in
    settings (visible to admins via /api/admin/settings). Without a
    correct secret this endpoint always refuses, so nobody can fabricate
    free ad-reward calls by guessing the URL shape."""
    required_secret = await get_adsgram_s2s_secret()
    if not secret or not hmac.compare_digest(secret, required_secret):
        return {"ok": False, "reason": "unauthorized"}

    user = await DataEngine.get_user(userid)
    if not user or user["is_banned"]:
        return {"ok": False}

    # BUG-REPORT #8a: honour the master ads switch — a postback must
    # never credit while the admin has ads turned off (the old handler
    # never looked at ads_enabled at all).
    if (await DataEngine.get_setting("ads_enabled", "0")) != "1":
        return {"ok": False, "reason": "ads_disabled"}

    # BUG-REPORT #8b: idempotency. AdsGram can redeliver the same S2S
    # postback (its own retries, network hiccups); a redelivery was
    # indistinguishable from a brand-new view and could credit again
    # once the per-view cooldown passed. When the reward URL passes a
    # delivery id (configure [transaction_id] in the AdsGram dashboard,
    # or event_id), the first delivery wins and any redelivery of the
    # same view is acknowledged but never re-processed. When no id is
    # sent, the pre-existing guards still apply (atomic cooldown/daily
    # cap in claim_video_ad_atomic + the one-shot consumption of the
    # daily-check-in pending mark), but operators should configure the
    # id — see UPDATES.md.
    delivery_key = (transaction_id or event_id or "").strip()
    if delivery_key:
        first_delivery = await DataEngine.mark_adsgram_postback_seen(
            f"{userid}:{delivery_key}", userid)
        if not first_delivery:
            return {"ok": True, "duplicate": True}

    # Optional extra gate: require real evidence the user's Telegram
    # WebApp was actually backgrounded (blur/visibilitychange) while the
    # ad was showing — i.e. they genuinely left the app for the ad's
    # banner/link — before honoring AdsGram's own completion signal.
    #
    # CTR control: if enabled, once today's count of rewards that were
    # actually gated on a verified click hits ad_banner_click_daily_cap,
    # the gate is quietly relaxed for the rest of the day (falls back to
    # paying on impression like the gate being Off) — so the fraction of
    # "confirmed click" traffic AdsGram observes doesn't sit unnaturally
    # high all day and risk a fraud flag.
    require_click = (await DataEngine.get_setting("ad_require_banner_click", "0")) == "1"
    click_was_verified = False
    if require_click:
        cap_enabled = (await DataEngine.get_setting("ad_banner_click_cap_enabled", "0")) == "1"
        cap_reached = False
        if cap_enabled:
            daily_cap = int(await DataEngine.get_setting("ad_banner_click_daily_cap", "124"))
            cap_reached = (await DataEngine.count_banner_click_rewards_today()) >= daily_cap
        if cap_reached:
            require_click = False  # quota used up for today — pay every view from here on
        else:
            click_window = int(await DataEngine.get_setting("ad_banner_click_window_seconds", "120"))
            if not await DataEngine.consume_recent_banner_click(userid, click_window):
                return {"ok": False, "reason": "no_click_detected"}
            click_was_verified = True

    daily_limit      = int(await DataEngine.get_setting("ad_daily_limit", "10"))
    cooldown_seconds = int(await DataEngine.get_setting("ad_cooldown_seconds", "30"))
    reward_amount    = float(await DataEngine.get_setting("ad_reward_amount", "0.5"))
    reminder_enabled = (await DataEngine.get_setting("ad_click_reminder_enabled", "1")) == "1"
    batch_size            = int(await DataEngine.get_setting("ad_batch_size", "0"))
    batch_cooldown_seconds = int(await DataEngine.get_setting("ad_batch_cooldown_seconds", "3600"))

    # If this view was shown by the Daily Check-in flow (see
    # mark_daily_checkin_ad_pending / /api/dailycheck/arm), don't also
    # pay the regular per-view ad_reward_amount here — the daily bonus
    # is credited separately by claim_daily_checkin_atomic, and paying
    # both for the same ad view was doubling the user's reward.
    skip_balance_credit = await DataEngine.consume_daily_checkin_ad_pending(userid)

    # Tads gate — when configured, this view's regular per-ad payout is
    # withheld until Tads (shown by the frontend right after this
    # AdsGram ad) also reports its outcome: pay if Tads showed and the
    # user completed it, or if Tads had no ad to show at all (no-fill);
    # never pay if Tads had an ad the user didn't finish. Doesn't apply
    # to Daily Check-in views (skip_balance_credit) — that flow doesn't
    # show Tads and already has its own trust model.
    tads_enabled   = (await DataEngine.get_setting("tads_enabled", "0")) == "1"
    tads_widget_id = (await DataEngine.get_setting("tads_widget_id", "") or "").strip()
    tads_gate_required = tads_enabled and bool(tads_widget_id) and not skip_balance_credit

    if tads_gate_required:
        ok, _ = await DataEngine.gate_adsgram_confirmation(
            userid,
            {
                "reward": reward_amount, "daily_limit": daily_limit,
                "cooldown_seconds": cooldown_seconds, "skip_enabled": reminder_enabled,
                "batch_size": batch_size, "batch_cooldown_seconds": batch_cooldown_seconds,
                "skip_balance_credit": skip_balance_credit,
            },
        )
    else:
        ok, _ = await DataEngine.claim_video_ad_atomic(
            userid, reward_amount, daily_limit, cooldown_seconds, reminder_enabled,
            batch_size, batch_cooldown_seconds, skip_balance_credit=skip_balance_credit,
        )
    if ok and click_was_verified:
        await DataEngine.log_banner_click_reward(userid)
    return {"ok": ok}


@api_app.post("/api/ads/mark-clicked")
async def api_ads_mark_clicked(body: ApiBase):
    """Called by the frontend the instant it detects the Mini App was
    actually backgrounded (blur/visibilitychange) while an AdsGram ad
    was showing. Purely records a short-lived signal — see
    ad_require_banner_click, which is what actually gates payment."""
    user = await _authenticate(body)
    await DataEngine.mark_banner_clicked(user["user_id"])
    return {"ok": True}


class AdTadsResultRequest(ApiBase):
    result: str  # 'completed' | 'no_fill' | 'failed'


@api_app.post("/api/ads/tads-result")
async def api_ads_tads_result(body: AdTadsResultRequest):
    """Called by the frontend right after it shows (or tries to show)
    the Tads ad that follows AdsGram's video ad. 'no_fill' = Tads had
    nothing to show (ok to pay via AdsGram alone), 'failed' = Tads had
    an ad but it wasn't completed (closed early, error, etc. — do not
    pay). 'completed' is also accepted here as a fast client-side signal,
    but the AUTHORITATIVE confirmation is the server-to-server
    /api/ads/tads-webhook call Tads itself makes on ad view — this
    client call is redundant-but-harmless backup for it, never the sole
    source of truth for a real payout.
    'failed' explicitly wipes any pending gate so a stray later postback
    can never be paid against it."""
    user = await _authenticate(body)
    uid = user["user_id"]
    result = body.result if body.result in ("completed", "no_fill") else "failed"
    if result == "failed":
        await DataEngine.clear_ad_reward_gate(uid)
        return {"ok": True, "finalized": False}
    finalized = await DataEngine.record_tads_result(uid, result)
    return {"ok": True, "finalized": finalized}


async def _handle_tads_webhook(request: Request):
    """Tads' own server-to-server webhook (see
    https://tads.gitbook.io/docs/getting-started/publishers/webhooks) —
    for Fullscreen-format widgets this fires on ad VIEW, i.e. it's a real
    S2S confirmation the ad was actually shown, the same trust model as
    AdsGram's reward postback. Configure this exact URL (with the secret
    from /api/admin/settings' 'tads_webhook_secret'/the readonly field in
    the dashboard) as the widget's Webhook URL in the Tads publisher
    dashboard — GET or POST both work; Tads sends telegram_id + widget_id
    either as query params (GET) or JSON body (POST)."""
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            body_json = await request.json()
            if isinstance(body_json, dict):
                params = {**body_json, **params}  # query params win if both present
        except Exception:
            pass

    required_secret = await get_tads_webhook_secret()
    secret = params.get("secret")
    if not secret or not hmac.compare_digest(str(secret), required_secret):
        return {"ok": False, "reason": "unauthorized"}

    telegram_id_raw = params.get("telegram_id")
    widget_id_raw   = params.get("widget_id")
    try:
        telegram_id = int(telegram_id_raw)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "missing_telegram_id"}

    configured_widget_id = (await DataEngine.get_setting("tads_widget_id", "") or "").strip()
    if configured_widget_id and widget_id_raw is not None and str(widget_id_raw) != configured_widget_id:
        # Doesn't match the widget this deployment is configured for —
        # ignore rather than let an unrelated/old widget credit rewards.
        return {"ok": False, "reason": "widget_id_mismatch"}

    user = await DataEngine.get_user(telegram_id)
    if not user or user["is_banned"]:
        return {"ok": False, "reason": "unknown_user"}

    finalized = await DataEngine.record_tads_result(telegram_id, "completed")
    return {"ok": True, "finalized": finalized}


@api_app.get("/api/ads/tads-webhook")
async def api_ads_tads_webhook_get(request: Request):
    return await _handle_tads_webhook(request)


@api_app.post("/api/ads/tads-webhook")
async def api_ads_tads_webhook_post(request: Request):
    return await _handle_tads_webhook(request)


@api_app.post("/api/ads/direct-link/open")
async def api_ads_direct_link_open(body: ApiBase):
    """Direct Link has no SDK callback to confirm anything happened, so
    the reward is gated behind a server-timed wait (same trust model as
    the 'fake' task type already used for unverifiable channel joins)."""
    user = await _authenticate(body)
    uid = user["user_id"]

    ads_enabled = (await DataEngine.get_setting("ads_enabled", "0")) == "1"
    direct_link = (await DataEngine.get_setting("adsgram_direct_link", "") or "").strip()
    if not ads_enabled or not direct_link:
        raise HTTPException(status_code=400, detail="direct_link_disabled")

    daily_limit      = int(await DataEngine.get_setting("direct_link_daily_limit", "10"))
    cooldown_seconds = int(await DataEngine.get_setting("direct_link_cooldown_seconds", "30"))
    wait_seconds     = int(await DataEngine.get_setting("direct_link_wait_seconds", "15"))

    watched_today = await DataEngine.count_ad_events_today(uid, AD_KIND_DIRECT)
    if watched_today >= daily_limit:
        raise HTTPException(status_code=400, detail="daily_limit_reached")

    last_at = await DataEngine.get_last_ad_event(uid, AD_KIND_DIRECT)
    if last_at:
        try:
            elapsed = (datetime.utcnow() - datetime.strptime(last_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if elapsed < cooldown_seconds:
                raise HTTPException(status_code=400, detail="cooldown_active")
        except HTTPException:
            raise
        except Exception:
            pass

    event_id = await DataEngine.start_ad_event(uid, AD_KIND_DIRECT)
    return {"url": direct_link, "event_id": event_id, "wait_seconds": wait_seconds}


class AdDirectLinkClaimRequest(ApiBase):
    event_id: int


@api_app.post("/api/ads/direct-link/claim")
async def api_ads_direct_link_claim(body: AdDirectLinkClaimRequest):
    user = await _authenticate(body)
    uid = user["user_id"]

    wait_seconds  = int(await DataEngine.get_setting("direct_link_wait_seconds", "15"))
    reward_amount = float(await DataEngine.get_setting("direct_link_reward_amount", "0.3"))
    daily_limit   = int(await DataEngine.get_setting("direct_link_daily_limit", "10"))

    event = await DataEngine.get_ad_event(body.event_id, uid)
    if not event or event["kind"] != AD_KIND_DIRECT or event["status"] == "completed":
        raise HTTPException(status_code=400, detail="not_claimable")

    try:
        started_dt = datetime.strptime(event["started_at"], "%Y-%m-%d %H:%M:%S")
        elapsed = (datetime.utcnow() - started_dt).total_seconds()
    except Exception:
        elapsed = wait_seconds

    if elapsed < wait_seconds:
        raise HTTPException(status_code=400, detail=f"wait:{int(wait_seconds - elapsed)}")

    # Same optional gate as the AdsGram video ad — here it's even more
    # meaningful, since we ourselves triggered the tg.openLink() call, so
    # a verified click is real proof it actually opened (not just that
    # we asked it to). Uses the same admin toggle/window/CTR-cap so both
    # ad types share one consistent "did they really leave" standard.
    require_click = (await DataEngine.get_setting("ad_require_banner_click", "0")) == "1"
    click_was_verified = False
    if require_click:
        cap_enabled = (await DataEngine.get_setting("ad_banner_click_cap_enabled", "0")) == "1"
        cap_reached = False
        if cap_enabled:
            daily_cap = int(await DataEngine.get_setting("ad_banner_click_daily_cap", "124"))
            cap_reached = (await DataEngine.count_banner_click_rewards_today()) >= daily_cap
        if cap_reached:
            require_click = False
        else:
            click_window = int(await DataEngine.get_setting("ad_banner_click_window_seconds", "120"))
            if not await DataEngine.consume_recent_banner_click(uid, click_window):
                raise HTTPException(status_code=400, detail="no_click_detected")
            click_was_verified = True

    claimed, claim_reason = await DataEngine.claim_direct_link_atomic(
        body.event_id, uid, reward_amount, daily_limit
    )
    if not claimed:
        raise HTTPException(status_code=400, detail=claim_reason or "not_claimable")
    await DataEngine.add_balance(uid, reward_amount)
    if click_was_verified:
        await DataEngine.log_banner_click_reward(uid)
    fresh = await DataEngine.get_user(uid)
    return {"credited": reward_amount, "balance": float(fresh["balance"])}


# ── /api/tasks — user-facing task list + join/check/claim ───────────────
async def _build_task_view(user_id: int) -> list:
    tasks = await DataEngine.get_tasks(active_only=True)
    progress = await DataEngine.get_all_task_progress_for_user(user_id)
    manual_progress = await DataEngine.get_latest_manual_submissions_for_user(user_id)
    out = []
    for row in tasks:
        t = dict(row)
        if t["task_type"] == "manual":
            # No join/wait/check timer for these — status comes entirely
            # from this user's latest screenshot submission, if any.
            m = manual_progress.get(t["id"])
            if m is None:
                t["status"] = "none"
            elif m["status"] == "pending":
                t["status"] = "review"
            elif m["status"] == "rejected":
                t["status"] = "rejected"
            elif m["status"] == "approved":
                t["status"] = "completed"
            else:
                t["status"] = "none"
            out.append(t)
            continue
        p = progress.get(t["id"])
        if p is None:
            t["status"] = "none"
        elif p["status"] == "completed":
            t["status"] = "completed"
        else:
            try:
                joined_dt = datetime.strptime(p["joined_at"], "%Y-%m-%d %H:%M:%S")
                elapsed = (datetime.utcnow() - joined_dt).total_seconds()
            except Exception:
                elapsed = TASK_JOIN_WAIT_SECONDS
            wait_left = max(0, int(TASK_JOIN_WAIT_SECONDS - elapsed))
            t["status"] = "waiting" if wait_left > 0 else "claimable"
            t["wait_left"] = wait_left
        out.append(t)
    return out


@api_app.post("/api/tasks")
async def api_tasks(body: ApiBase):
    user = await _authenticate(body)
    return {
        "tasks": await _build_task_view(user["user_id"]),
        "user_task_config": {
            "enabled": (await DataEngine.get_setting("user_task_creation_enabled", "0")) == "1",
            "min_reward": float(await DataEngine.get_setting("user_task_min_reward", "1")),
            "max_reward": float(await DataEngine.get_setting("user_task_max_reward", "20")),
            "min_slots": int(await DataEngine.get_setting("user_task_min_slots", "5")),
            "max_slots": int(await DataEngine.get_setting("user_task_max_slots", "500")),
            "bot_username": BOT_USERNAME,
        },
    }


class TaskActionRequest(ApiBase):
    task_id: int


async def _force_membership_ok(task, uid) -> str:
    """REAL membership verdict for a 'force' task: 'ok', 'not_member' or
    'check_failed'. Callers fail CLOSED on anything but 'ok' — a force
    reward is only paid to a proven member. (Owner report 2026-11-26:
    the old fail-open 'except: pass' in /api/tasks/check meant the
    member check silently never happened whenever the lookup hiccuped
    or the stored channel ref was a raw invite link.)"""
    if task["task_type"] != "force":
        return "ok"
    ref = (task["channel_id"] or "").strip()
    if not ref:
        return "check_failed"
    m = None
    try:
        m = await bot.get_chat_member(chat_id=ref, user_id=uid)
    except Exception:
        # Older tasks may store a t.me link — retry once as @username.
        uname = ref
        for p in ("https://t.me/", "http://t.me/", "t.me/"):
            if uname.startswith(p):
                uname = uname[len(p):]
                break
        uname = uname.strip("/").split("/")[0].split("?")[0]
        if uname and not uname.startswith("+") and uname != ref:
            try:
                m = await bot.get_chat_member(chat_id=f"@{uname}", user_id=uid)
            except Exception:
                return "check_failed"
        else:
            return "check_failed"
    if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED):
        return "not_member"
    return "ok"


@api_app.post("/api/tasks/join")
async def api_tasks_join(body: TaskActionRequest):
    user = await _authenticate(body)
    task = await DataEngine.get_task(body.task_id)
    if not task or not task["is_active"]:
        raise HTTPException(status_code=404, detail="task_not_found")
    await DataEngine.mark_task_joined(user["user_id"], body.task_id)
    return {"wait_seconds": TASK_JOIN_WAIT_SECONDS}


@api_app.post("/api/tasks/check")
async def api_tasks_check(body: TaskActionRequest):
    user = await _authenticate(body)
    uid = user["user_id"]
    task = await DataEngine.get_task(body.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    prog = await DataEngine.get_task_progress(uid, body.task_id)
    if not prog:
        raise HTTPException(status_code=400, detail="not_joined")
    try:
        joined_dt = datetime.strptime(prog["joined_at"], "%Y-%m-%d %H:%M:%S")
        elapsed = (datetime.utcnow() - joined_dt).total_seconds()
    except Exception:
        elapsed = TASK_JOIN_WAIT_SECONDS
    remaining = int(TASK_JOIN_WAIT_SECONDS - elapsed)
    if remaining > 0:
        raise HTTPException(status_code=400, detail=f"wait:{remaining}")
    if task["task_type"] == "force":
        verdict = await _force_membership_ok(task, uid)
        if verdict != "ok":
            raise HTTPException(status_code=400, detail=verdict)
    # Only reachable once the wait timer has elapsed AND (for a "force"
    # task) real membership has been confirmed above. Record that this
    # user has passed a genuine check — /api/tasks/claim will refuse to
    # pay out without this, so a reward is never handed out for free.
    await DataEngine.mark_task_checked(uid, body.task_id)
    return {"ok": True}


@api_app.post("/api/tasks/claim")
async def api_tasks_claim(body: TaskActionRequest):
    user = await _authenticate(body)
    uid = user["user_id"]
    task = await DataEngine.get_task(body.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    prog = await DataEngine.get_task_progress(uid, body.task_id)
    if not prog or prog["status"] == "completed":
        raise HTTPException(status_code=400, detail="not_claimable")
    if prog["status"] != "checked":
        # Guards against calling /api/tasks/claim directly (skipping
        # /api/tasks/check) to collect a reward without ever actually
        # joining / passing verification.
        raise HTTPException(status_code=400, detail="not_checked")
    if task["task_type"] == "force":
        # Re-verify at payout: passing /check and then leaving the
        # channel before claiming must not pay either.
        verdict = await _force_membership_ok(task, uid)
        if verdict != "ok":
            raise HTTPException(status_code=400, detail=verdict)
    # BUG-REPORT #3: the snapshot above can go stale (creator cancels,
    # admin deactivates, escrow sells out) between /check and this call,
    # and the old flow paid first and counted the slot afterwards — so a
    # claim landing after sell-out/cancel/deactivate still minted a
    # reward the escrow no longer covered, double-spending it against
    # the creator's cancel refund. Re-validate claimability up front…
    if not task["is_active"] or task["review_status"] != "approved":
        raise HTTPException(status_code=400, detail="task_unavailable")
    if task["created_by"] and task["budget_slots"] > 0 and task["slots_used"] >= task["budget_slots"]:
        raise HTTPException(status_code=400, detail="task_sold_out")
    # …then, for escrow tasks, atomically take the paid slot BEFORE
    # paying anyone. If the slot is gone, nobody gets paid for it.
    slot_sold_out = False
    if task["created_by"]:
        slot_taken, slot_sold_out = await DataEngine.try_take_task_slot(body.task_id)
        if not slot_taken:
            raise HTTPException(status_code=400, detail="task_sold_out")
    claimed = await DataEngine.mark_task_completed(uid, body.task_id)
    if not claimed:
        # Lost a race to a concurrent/duplicate claim for this exact task
        # (or the prior checks above were stale by the time we got here) —
        # do not pay out a second time, and hand the escrow slot back.
        if task["created_by"]:
            await DataEngine.release_task_slot(body.task_id)
        raise HTTPException(status_code=400, detail="not_claimable")
    await DataEngine.add_balance(uid, float(task["reward"]))
    # The reward above came out of the creator's escrow (the slot taken
    # just before paying) — if it was the last one, tell the creator.
    if task["created_by"] and slot_sold_out:
        try:
            await bot.send_message(
                task["created_by"],
                f"🎯 Your task \"{task['title']}\" has reached its target — "
                f"all {task['budget_slots']} people have joined. It's now removed from the task list."
            )
        except Exception:
            pass
    fresh = await DataEngine.get_user(uid)
    return {"status": "completed", "reward": float(task["reward"]), "balance": float(fresh["balance"])}


# ── /api/tasks/manual/submit — screenshot-proof tasks, no auto-check ────
# The admin decides via /api/admin/tasks/manual_submissions/review;
# nothing here pays a reward directly.
class ManualSubmitRequest(ApiBase):
    task_id: int
    screenshots: List[str]


@api_app.post("/api/tasks/manual/submit")
async def api_tasks_manual_submit(body: ManualSubmitRequest):
    user = await _authenticate(body)
    uid = user["user_id"]
    task = await DataEngine.get_task(body.task_id)
    if not task or not task["is_active"] or task["task_type"] != "manual":
        raise HTTPException(status_code=404, detail="task_not_found")
    required = int(task["screenshot_count"] or 1)
    raw_shots = [s for s in (body.screenshots or []) if s][:required]
    if len(raw_shots) < required:
        raise HTTPException(status_code=400, detail="screenshots_required")
    latest = await DataEngine.get_latest_manual_submission(uid, body.task_id)
    if latest:
        # One attempt per person per task — a rejection is final, not a
        # "try again". No status check needed here: pending, approved, or
        # rejected all block a second submission the same way.
        raise HTTPException(status_code=400, detail="already_submitted")

    # Decode each data: URL to raw bytes here and relay them straight to
    # Telegram below — nothing here is ever written to our own DB/disk.
    image_bytes = []
    for shot in raw_shots:
        try:
            b64data = shot.split(",", 1)[1] if "," in shot else shot
            image_bytes.append(base64.b64decode(b64data))
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_screenshot")

    submission_id = await DataEngine.create_manual_submission(uid, body.task_id, [])
    file_ids = await notify_admins_manual_submission(submission_id, task, user, image_bytes)
    if file_ids:
        await DataEngine.set_manual_submission_screenshots(submission_id, file_ids)
    return {"status": "review"}


# ── /api/tasks/create — users pay from their own balance to advertise
#    their own channel as a task other users can complete ─────────────────
class UserTaskCreateRequest(ApiBase):
    title: str
    invite_link: str
    reward: float = Field(gt=0)
    budget_slots: int = Field(gt=0)
    channel_id: str = ""  # if set, we re-verify bot-admin status server-side


# ── /api/tasks/verify_admin — step 1 of self-serve task creation: confirm
#    the bot has actually been made admin in the user's channel before
#    letting them create a real ("force") verified task ───────────────────
class VerifyAdminRequest(ApiBase):
    channel_id: str


@api_app.post("/api/tasks/verify_admin")
async def api_tasks_verify_admin(body: VerifyAdminRequest):
    await _authenticate(body)
    channel_id = body.channel_id.strip()
    if not channel_id:
        raise HTTPException(status_code=400, detail="missing_channel")
    bot_id = BOT_ID
    if not bot_id:
        try:
            bot_id = (await bot.get_me()).id
        except Exception:
            raise HTTPException(status_code=503, detail="bot_unavailable")
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        # Channel not found, bot not in it at all, wrong ID format, etc.
        is_admin = False
    # Best-effort — works for any public channel regardless of admin
    # status, so the user never has to type the channel name themselves.
    title = None
    try:
        chat = await bot.get_chat(chat_id=channel_id)
        title = (chat.title or "").strip() or None
    except Exception:
        pass
    return {"is_admin": is_admin, "bot_username": BOT_USERNAME, "title": title}


@api_app.post("/api/tasks/create")
async def api_tasks_create(body: UserTaskCreateRequest):
    user = await _authenticate(body)
    uid = user["user_id"]

    if (await DataEngine.get_setting("user_task_creation_enabled", "0")) != "1":
        raise HTTPException(status_code=400, detail="feature_disabled")

    title = body.title.strip()[:80]
    link = body.invite_link.strip()
    channel_id = body.channel_id.strip()
    if not title or not link:
        raise HTTPException(status_code=400, detail="missing_fields")
    if not link.lower().startswith(("https://t.me/", "http://t.me/", "tg://")):
        raise HTTPException(status_code=400, detail="invalid_link")

    min_reward = float(await DataEngine.get_setting("user_task_min_reward", "1"))
    max_reward = float(await DataEngine.get_setting("user_task_max_reward", "20"))
    min_slots  = int(await DataEngine.get_setting("user_task_min_slots", "5"))
    max_slots  = int(await DataEngine.get_setting("user_task_max_slots", "500"))
    if not (min_reward <= body.reward <= max_reward):
        raise HTTPException(status_code=400, detail="reward_out_of_range")
    if not (min_slots <= body.budget_slots <= max_slots):
        raise HTTPException(status_code=400, detail="slots_out_of_range")

    total_cost = round(body.reward * body.budget_slots, 2)
    fresh = await DataEngine.get_user(uid)
    if not fresh or float(fresh["balance"]) < total_cost:
        # Fast, friendly pre-check — the real enforcement is the atomic
        # deduct right before escrow below, which is race-safe.
        raise HTTPException(status_code=400, detail="insufficient_balance")

    # Re-verify bot-admin status ourselves — never trust the client's word
    # for whether the "Check" step passed. If it checks out, this becomes
    # a real "force" task (genuine membership check on every join);
    # otherwise it falls back to "fake" (wait-timer only), same as any
    # admin-made task for a channel the bot isn't in.
    task_type = "fake"
    if channel_id:
        bot_id = BOT_ID or (await bot.get_me()).id
        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
            if member.status in ("administrator", "creator"):
                task_type = "force"
        except Exception:
            pass
    if task_type == "force":
        # Store the NUMERIC chat id so the member check at claim time
        # always resolves, whatever form the creator typed.
        try:
            chat = await bot.get_chat(chat_id=channel_id)
            channel_id = str(chat.id)
        except Exception:
            pass
    if task_type != "force":
        channel_id = ""

    # Escrow the full budget immediately — this is what lets us pay each
    # completer out of the same pool later without minting new balance.
    # Atomic re-check-and-deduct: two concurrent /api/tasks/create calls
    # can no longer both pass the earlier balance check and overdraw it.
    escrowed = await DataEngine.deduct_balance_atomic(uid, total_cost)
    if not escrowed:
        raise HTTPException(status_code=400, detail="insufficient_balance")
    task_id = await DataEngine.create_task(
        title, channel_id, link, task_type, body.reward,
        created_by=uid, budget_slots=body.budget_slots, review_status="pending",
    )
    return {"ok": True, "task_id": task_id, "total_cost": total_cost, "status": "pending_review", "task_type": task_type}


@api_app.post("/api/tasks/mine")
async def api_tasks_mine(body: ApiBase):
    user = await _authenticate(body)
    rows = await DataEngine.get_user_tasks(user["user_id"])
    return {"tasks": [dict(r) for r in rows]}


@api_app.post("/api/tasks/cancel")
async def api_tasks_cancel(body: TaskActionRequest):
    """
    Lets a creator pull their own task early and get back the escrow for
    whatever slots haven't been used yet. Can't touch anyone else's task.
    """
    user = await _authenticate(body)
    uid = user["user_id"]
    task = await DataEngine.get_task(body.task_id)
    if not task or task["created_by"] != uid:
        raise HTTPException(status_code=404, detail="task_not_found")
    ok, refund = await DataEngine.cancel_user_task_atomic(body.task_id, uid)
    if not ok:
        raise HTTPException(status_code=400, detail="nothing_to_cancel")
    return {"ok": True, "refunded": refund}


# ── /api/withdraw ─────────────────────────────────────────────────────────
class WithdrawRequest(ApiBase):
    amount: float = Field(gt=0)
    method: str = "telebirr"          # "telebirr" | "binance"
    phone: str = ""
    full_name: str = ""
    binance_uid: str = ""


@api_app.post("/api/withdraw")
async def api_withdraw(body: WithdrawRequest):
    user = await _authenticate(body)
    min_w = float(await DataEngine.get_setting("min_withdrawal", "50"))
    if body.amount < min_w:
        raise HTTPException(status_code=400, detail=f"min_withdrawal_is_{min_w:.2f}")

    method = body.method.strip().lower() if body.method.strip().lower() == "binance" else "telebirr"
    if method == "binance":
        binance_uid = body.binance_uid.strip()
        if len(binance_uid) < 5:
            raise HTTPException(status_code=400, detail="invalid_binance_uid")
        result = await dispatch_withdrawal_core(
            user["user_id"], body.amount, "", "", method="binance", binance_uid=binance_uid
        )
    else:
        phone = body.phone.strip()
        full_name = body.full_name.strip()
        if len(phone) < 9:
            raise HTTPException(status_code=400, detail="invalid_phone")
        if len(full_name) < 3:
            raise HTTPException(status_code=400, detail="invalid_full_name")
        result = await dispatch_withdrawal_core(
            user["user_id"], body.amount, full_name, phone, method="telebirr"
        )
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["reason"])
    return result


@api_app.post("/api/withdraw/history")
async def api_withdraw_history(body: ApiBase):
    user = await _authenticate(body)
    rows = await DataEngine.get_user_withdrawals(user["user_id"])
    return {"withdrawals": [dict(r) for r in rows]}


# ── /api/admin/stats/overview — total & online users ─────────────────────
@api_app.post("/api/admin/stats/overview")
async def api_admin_stats_overview(body: ApiBase):
    user = await _authenticate(body)
    _require_admin(user)
    total, online = await DataEngine.get_user_activity_stats()
    return {"total_users": total, "online_users": online}


# ── /api/admin/ads/analytics — clicks vs completions + daily-limit list ──
@api_app.post("/api/admin/ads/analytics")
async def api_admin_ads_analytics(body: ApiBase):
    user = await _authenticate(body)
    _require_admin(user)
    video_daily_limit = int(await DataEngine.get_setting("ad_daily_limit", "10"))
    dl_daily_limit     = int(await DataEngine.get_setting("direct_link_daily_limit", "10"))
    return await DataEngine.get_ads_admin_analytics(video_daily_limit, dl_daily_limit)


# ── /api/admin/tasks/review — approve/reject user-submitted (escrowed)
#    tasks before they go live to other users ───────────────────────────
@api_app.post("/api/admin/tasks/pending")
async def api_admin_tasks_pending(body: ApiBase):
    user = await _authenticate(body)
    _require_admin(user)
    rows = await DataEngine.get_pending_review_tasks()
    return {"tasks": [dict(r) for r in rows]}


class AdminTaskReviewRequest(ApiBase):
    id: int
    approve: bool


@api_app.post("/api/admin/tasks/review")
async def api_admin_tasks_review(body: AdminTaskReviewRequest):
    user = await _authenticate(body)
    _require_admin(user)
    # BUG-REPORT #10: status flip + escrow refund are one atomic step
    # now (review_user_task_atomic) — exactly one of approve/reject (or a
    # racing creator-cancel) can win, so the escrow can never be refunded
    # twice and approve+reject can never both apply.
    won, info = await DataEngine.review_user_task_atomic(body.id, body.approve)
    if not won:
        raise HTTPException(status_code=404, detail="task_not_found")
    if body.approve:
        note = "✅ Your task has been approved and is now live!"
    else:
        # Full refund — none of the escrowed slots were ever usable.
        note = f"❌ Your submitted task was rejected. {info['refund']:.2f} Birr has been refunded to your balance."
    if info["created_by"]:
        try:
            await bot.send_message(info["created_by"], note)
        except Exception:
            pass
    return {"ok": True}


# ── /api/admin/tasks/list ────────────────────────────────────────────────
@api_app.post("/api/admin/tasks/list")
async def api_admin_tasks_list(body: ApiBase):
    user = await _authenticate(body)
    _require_admin(user)
    tasks = await DataEngine.get_tasks(active_only=False)
    return {"tasks": [dict(t) for t in tasks]}


# ── /api/admin/tasks/manual_submissions — screenshot-proof review queue ──
@api_app.post("/api/admin/tasks/manual_submissions")
async def api_admin_manual_submissions(body: ApiBase):
    user = await _authenticate(body)
    _require_admin(user)
    return {"submissions": await DataEngine.get_pending_manual_submissions()}


class ManualSubmissionReviewRequest(ApiBase):
    id: int
    approve: bool


@api_app.post("/api/admin/tasks/manual_submissions/review")
async def api_admin_manual_submissions_review(body: ManualSubmissionReviewRequest):
    user = await _authenticate(body)
    _require_admin(user)
    result = await review_manual_submission_core(body.id, body.approve)
    if not result["ok"]:
        raise HTTPException(status_code=404, detail="submission_not_found")
    return {"ok": True}


class AdminTaskCreateRequest(ApiBase):
    title: str
    task_type: str = "fake"
    channel_id: str = ""
    invite_link: str = ""
    reward: float = 0
    instructions: str = ""
    screenshot_count: int = 1


@api_app.post("/api/admin/tasks/create")
async def api_admin_tasks_create(body: AdminTaskCreateRequest):
    user = await _authenticate(body)
    _require_admin(user)
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title_required")
    _ch = body.channel_id.strip()
    if body.task_type == "force" and _ch:
        try:
            _chat = await bot.get_chat(chat_id=_ch)
            _ch = str(_chat.id)
        except Exception:
            pass
    task_id = await DataEngine.create_task(
        body.title.strip(), _ch, body.invite_link.strip(),
        body.task_type, body.reward,
        instructions=body.instructions.strip(),
        screenshot_count=max(1, min(2, int(body.screenshot_count or 1))),
    )
    return {"ok": True, "task_id": task_id}


class AdminIdRequest(ApiBase):
    id: int


@api_app.post("/api/admin/tasks/delete")
async def api_admin_tasks_delete(body: AdminIdRequest):
    user = await _authenticate(body)
    _require_admin(user)
    await DataEngine.delete_task(body.id)
    return {"ok": True}


class AdminTaskUpdateRequest(ApiBase):
    id: int
    is_active: Optional[int] = None
    title: Optional[str] = None
    task_type: Optional[str] = None
    channel_id: Optional[str] = None
    invite_link: Optional[str] = None
    reward: Optional[float] = None
    instructions: Optional[str] = None
    screenshot_count: Optional[int] = None


@api_app.post("/api/admin/tasks/update")
async def api_admin_tasks_update(body: AdminTaskUpdateRequest):
    user = await _authenticate(body)
    _require_admin(user)
    fields = {
        "is_active": body.is_active,
        "title": body.title.strip() if body.title is not None else None,
        "task_type": body.task_type,
        "channel_id": body.channel_id.strip() if body.channel_id is not None else None,
        "invite_link": body.invite_link.strip() if body.invite_link is not None else None,
        "reward": body.reward,
        "instructions": body.instructions.strip() if body.instructions is not None else None,
        "screenshot_count": (max(1, min(2, int(body.screenshot_count))) if body.screenshot_count is not None else None),
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    await DataEngine.update_task(body.id, **fields)
    return {"ok": True}


class AdminTaskReorderRequest(ApiBase):
    order: List[int]


@api_app.post("/api/admin/tasks/reorder")
async def api_admin_tasks_reorder(body: AdminTaskReorderRequest):
    user = await _authenticate(body)
    _require_admin(user)
    await DataEngine.reorder_tasks(body.order)
    return {"ok": True}


# NEW FEATURE: after adding a task, the admin panel offers "Broadcast this
# new task to all users?" — this endpoint sends that announcement with an
# inline "Open App" button (a Telegram WebApp button) so users can jump
# straight into the Mini App and do the task.
class AdminTaskBroadcastRequest(ApiBase):
    task_id: int


@api_app.post("/api/admin/tasks/broadcast")
async def api_admin_tasks_broadcast(body: AdminTaskBroadcastRequest):
    user = await _authenticate(body)
    _require_admin(user)
    task = await DataEngine.get_task(body.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    text = (
        f"🆕 <b>New Task Available!</b>\n\n"
        f"📢 {sanitize_html(task['title'])}\n"
        f"💰 Reward: <b>{float(task['reward']):.2f} Birr</b>\n\n"
        f"👇 Open the app and complete it now."
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🚀 Open App",
            web_app=WebAppInfo(url=f"{FRONTEND_URL}/app.html"),
        )
    ]])
    sent, failed = await broadcast_to_all_users(text, markup)
    return {"ok": True, "sent": sent, "failed": failed}


# ── /api/admin/channels/* — force/fake gate channels ─────────────────────
@api_app.post("/api/admin/channels/list")
async def api_admin_channels_list(body: ApiBase):
    user = await _authenticate(body)
    _require_admin(user)
    channels = await DataEngine.get_force_channels()
    return {"channels": [dict(c) for c in channels]}


class AdminChannelCreateRequest(ApiBase):
    bot_added: int
    channel_id: str = ""
    channel_name: str
    invite_link: str


@api_app.post("/api/admin/channels/create")
async def api_admin_channels_create(body: AdminChannelCreateRequest):
    user = await _authenticate(body)
    _require_admin(user)
    if not body.channel_name.strip() or not body.invite_link.strip():
        raise HTTPException(status_code=400, detail="name_and_link_required")
    await DataEngine.add_force_channel(
        body.channel_id.strip(), body.channel_name.strip(), body.invite_link.strip(), body.bot_added
    )
    return {"ok": True}


@api_app.post("/api/admin/channels/delete")
async def api_admin_channels_delete(body: AdminIdRequest):
    user = await _authenticate(body)
    _require_admin(user)
    await DataEngine.remove_force_channel(body.id)
    return {"ok": True}


# ── /api/admin/withdrawals/* ──────────────────────────────────────────────
@api_app.post("/api/admin/withdrawals/list")
async def api_admin_withdrawals_list(body: ApiBase):
    user = await _authenticate(body)
    _require_admin(user)
    rows = await DataEngine.get_pending_withdrawals()
    return {"withdrawals": [dict(r) for r in rows]}


class AdminWithdrawalApproveRequest(ApiBase):
    id: int
    notify_mode: str = "both"  # "both" | "channel_only"


@api_app.post("/api/admin/withdrawals/approve")
async def api_admin_withdrawals_approve(body: AdminWithdrawalApproveRequest):
    user = await _authenticate(body)
    _require_admin(user)
    mode = body.notify_mode if body.notify_mode in ("both", "channel_only") else "both"
    result = await approve_withdrawal_core(body.id, notify_mode=mode)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["reason"])
    return {"ok": True}


class AdminWithdrawalRejectRequest(ApiBase):
    id: int
    reason: str = "No reason given."


@api_app.post("/api/admin/withdrawals/reject")
async def api_admin_withdrawals_reject(body: AdminWithdrawalRejectRequest):
    user = await _authenticate(body)
    _require_admin(user)
    ticket = await DataEngine.get_withdrawal(body.id)
    if not ticket or ticket["status"] != "pending":
        raise HTTPException(status_code=400, detail="already_processed")
    await execute_withdrawal_rejection(None, body.id, ticket, body.reason)
    return {"ok": True}


# ── /api/admin/users/* ────────────────────────────────────────────────────
class AdminUserIdRequest(ApiBase):
    user_id: int


@api_app.post("/api/admin/users/search")
async def api_admin_users_search(body: AdminUserIdRequest):
    user = await _authenticate(body)
    _require_admin(user)
    target = await DataEngine.get_user(body.user_id)
    if not target:
        raise HTTPException(status_code=404, detail="not_found")
    direct, _ = await DataEngine.get_referral_metrics(body.user_id)
    return {
        "user_id": target["user_id"],
        "username": target["username"],
        "full_name": target["full_name"],
        "balance": float(target["balance"]),
        "referrals": direct,
        "is_banned": bool(target["is_banned"]),
    }


class AdminUserBanRequest(ApiBase):
    user_id: int
    banned: int


@api_app.post("/api/admin/users/ban")
async def api_admin_users_ban(body: AdminUserBanRequest):
    user = await _authenticate(body)
    _require_admin(user)
    await DataEngine.ban_user(body.user_id, body.banned)
    return {"ok": True}


class AdminUserBalanceRequest(ApiBase):
    user_id: int
    amount: float


@api_app.post("/api/admin/users/balance")
async def api_admin_users_balance(body: AdminUserBalanceRequest):
    user = await _authenticate(body)
    _require_admin(user)
    await DataEngine.add_balance(body.user_id, body.amount)
    return {"ok": True}


def _safe_int(val, default: int) -> int:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


def _safe_float(val, default: float) -> float:
    try:
        return float(str(val).strip())
    except (TypeError, ValueError):
        return default


# ── /api/admin/settings ───────────────────────────────────────────────────
@api_app.post("/api/admin/settings")
async def api_admin_settings(body: ApiBase):
    user = await _authenticate(body)
    _require_admin(user)
    rate = await DataEngine.get_setting("reward_per_referral", "10")
    min_w = await DataEngine.get_setting("min_withdrawal", "50")
    min_ads = await DataEngine.get_setting("min_ads_for_withdrawal", "30")
    usdt_rate = await DataEngine.get_setting("usdt_rate", "155")
    return {
        "reward_per_referral": _safe_float(rate, 10.0),
        "min_withdrawal": _safe_float(min_w, 50.0),
        "min_ads_for_withdrawal": _safe_int(min_ads, 30),
        # Birr-per-1-USDT rate shown on the Binance Pay withdrawal form
        "usdt_rate": _safe_float(usdt_rate, 155.0),
        # AdsGram — rewarded video
        "ads_enabled": (await DataEngine.get_setting("ads_enabled", "0")) == "1",
        "adsgram_block_id": await DataEngine.get_setting("adsgram_block_id", "") or "",
        # Read-only — paste this into AdsGram's "Reward URL" secret field.
        # Never generated by the client; always server-side (see
        # get_adsgram_s2s_secret), so the postback can't be forged.
        "adsgram_s2s_secret": await get_adsgram_s2s_secret(),
        "ad_reward_amount": _safe_float(await DataEngine.get_setting("ad_reward_amount", "0.5"), 0.5),
        "ad_daily_limit": _safe_int(await DataEngine.get_setting("ad_daily_limit", "10"), 10),
        "ad_cooldown_seconds": _safe_int(await DataEngine.get_setting("ad_cooldown_seconds", "30"), 30),
        "ad_batch_size": _safe_int(await DataEngine.get_setting("ad_batch_size", "0"), 0),
        "ad_batch_cooldown_seconds": _safe_int(await DataEngine.get_setting("ad_batch_cooldown_seconds", "3600"), 3600),
        # AdsGram — Direct Link
        "adsgram_direct_link": await DataEngine.get_setting("adsgram_direct_link", "") or "",
        "direct_link_reward_amount": _safe_float(await DataEngine.get_setting("direct_link_reward_amount", "0.3"), 0.3),
        "direct_link_daily_limit": _safe_int(await DataEngine.get_setting("direct_link_daily_limit", "10"), 10),
        "direct_link_wait_seconds": _safe_int(await DataEngine.get_setting("direct_link_wait_seconds", "15"), 15),
        "direct_link_cooldown_seconds": _safe_int(await DataEngine.get_setting("direct_link_cooldown_seconds", "30"), 30),
        # Monetag — fallback rewarded network when AdsGram has no fill
        "monetag_zone_id": await DataEngine.get_setting("monetag_zone_id", "") or "",
        "monetag_sdk_url": await DataEngine.get_setting("monetag_sdk_url", "") or "",
        # Tads — second/gating ad shown right after AdsGram's video ad
        "tads_enabled": (await DataEngine.get_setting("tads_enabled", "0")) == "1",
        "tads_widget_id": await DataEngine.get_setting("tads_widget_id", "") or "",
        # Read-only — paste this into Tads' widget "Webhook URL" field
        # (GET or POST). Server-generated, same trust model as AdsGram's
        # Reward URL secret above.
        "tads_webhook_secret": await get_tads_webhook_secret(),
        # Referral skip — silently withhold payment for some referrals
        "referral_skip_enabled": (await DataEngine.get_setting("referral_skip_enabled", "0")) == "1",
        "referral_skip_batch_size": _safe_int(await DataEngine.get_setting("referral_skip_batch_size", "6"), 6),
        "referral_skip_min": _safe_int(await DataEngine.get_setting("referral_skip_min", "1"), 1),
        "referral_skip_max": _safe_int(await DataEngine.get_setting("referral_skip_max", "3"), 3),
        # User self-serve task creation (users advertise their own channel,
        # paid from their own balance)
        "user_task_creation_enabled": (await DataEngine.get_setting("user_task_creation_enabled", "0")) == "1",
        "user_task_min_reward": _safe_float(await DataEngine.get_setting("user_task_min_reward", "1"), 1.0),
        "user_task_max_reward": _safe_float(await DataEngine.get_setting("user_task_max_reward", "20"), 20.0),
        "user_task_min_slots": _safe_int(await DataEngine.get_setting("user_task_min_slots", "5"), 5),
        "user_task_max_slots": _safe_int(await DataEngine.get_setting("user_task_max_slots", "500"), 500),
        # Support contact — shown as a floating support button in the dashboard
        "support_username": (await DataEngine.get_setting("support_username", "") or "").lstrip("@"),
        # Ad-view reward-skip reminder (the message shown when a claim is
        # withheld by the odd/even skip pattern)
        "ad_click_reminder_enabled": (await DataEngine.get_setting("ad_click_reminder_enabled", "1")) == "1",
        "ad_click_reminder_disguised": (await DataEngine.get_setting("ad_click_reminder_disguised", "0")) == "1",
        "ad_click_reminder_text": await DataEngine.get_setting(
            "ad_click_reminder_text", "Please wait until the ad finishes — do not close it early"
        ),
        "ad_click_reminder_disguise_text": await DataEngine.get_setting(
            "ad_click_reminder_disguise_text", "⚠️ Please tap the ad! You must click the ad to earn your reward."
        ),
        "ad_click_reminder_network_text": await DataEngine.get_setting(
            "ad_click_reminder_network_text", "⚠️ Network error. Please try again."
        ),
        # Real click-away detection: require blur/visibilitychange
        # evidence the user left the app for the ad's banner/link before
        # the AdsGram S2S reward postback is honored (see mark-clicked).
        "ad_require_banner_click": (await DataEngine.get_setting("ad_require_banner_click", "0")) == "1",
        "ad_banner_click_window_seconds": _safe_int(
            await DataEngine.get_setting("ad_banner_click_window_seconds", "120"), 120
        ),
        "ad_banner_click_cap_enabled": (await DataEngine.get_setting("ad_banner_click_cap_enabled", "0")) == "1",
        "ad_banner_click_daily_cap": _safe_int(
            await DataEngine.get_setting("ad_banner_click_daily_cap", "124"), 124
        ),
        "ad_banner_click_rewards_today": await DataEngine.count_banner_click_rewards_today(),
        "daily_checkin_enabled": (await DataEngine.get_setting("daily_checkin_enabled", "1")) == "1",
        "daily_checkin_reward": float(await DataEngine.get_setting("daily_checkin_reward", "5")),
        "daily_checkin_reminder_text": await DataEngine.get_setting(
            "daily_checkin_reminder_text", "⚠️ Couldn't confirm your ad view — try again in a moment"
        ),
        "multi_account_detect_enabled": (await DataEngine.get_setting("multi_account_detect_enabled", "1")) == "1",
        "proof_post_enabled": (await DataEngine.get_setting("proof_post_enabled", "1")) == "1",
        "withdrawal_process_value": (await DataEngine.get_setting("withdrawal_process_value", "24-48") or "24-48"),
        "withdrawal_process_unit": (await DataEngine.get_setting("withdrawal_process_unit", "hours") or "hours"),
    }


class AdminSettingsUpdateRequest(ApiBase):
    reward_per_referral: float
    min_withdrawal: float
    min_ads_for_withdrawal: int = 30
    usdt_rate: float = 155
    ads_enabled: bool = False
    adsgram_block_id: str = ""
    ad_reward_amount: float = 0.5
    ad_daily_limit: int = 10
    ad_cooldown_seconds: int = 30
    ad_batch_size: int = 0
    ad_batch_cooldown_seconds: int = 3600
    adsgram_direct_link: str = ""
    direct_link_reward_amount: float = 0.3
    direct_link_daily_limit: int = 10
    direct_link_wait_seconds: int = 15
    direct_link_cooldown_seconds: int = 30
    monetag_zone_id: str = ""
    monetag_sdk_url: str = ""
    tads_enabled: bool = False
    tads_widget_id: str = ""
    referral_skip_enabled: bool = False
    referral_skip_batch_size: int = 6
    referral_skip_min: int = 1
    referral_skip_max: int = 3
    user_task_creation_enabled: bool = False
    user_task_min_reward: float = 1
    user_task_max_reward: float = 20
    user_task_min_slots: int = 5
    user_task_max_slots: int = 500
    support_username: str = ""
    # Optional (None = leave unchanged), like multi_account_detect_enabled
    # below: the Mini App's saveSettings() never sends these two fields,
    # and the old non-Optional defaults (True / "[]") were written back on
    # EVERY settings save — silently re-enabling the ticker and erasing
    # the admin's custom ticker names each time (BUG-REPORT #13).
    ticker_enabled: bool | None = None
    ticker_custom_names: str | None = None
    ad_click_reminder_enabled: bool = True
    ad_click_reminder_disguised: bool = False
    ad_click_reminder_text: str = "Please wait until the ad finishes — do not close it early"
    ad_click_reminder_disguise_text: str = "⚠️ Please tap the ad! You must click the ad to earn your reward."
    ad_click_reminder_network_text: str = "⚠️ Network error. Please try again."
    ad_require_banner_click: bool = False
    ad_banner_click_window_seconds: int = 120
    ad_banner_click_cap_enabled: bool = False
    ad_banner_click_daily_cap: int = 124
    daily_checkin_enabled: bool = True
    daily_checkin_reward: float = 5
    daily_checkin_reminder_text: str = "⚠️ Couldn't confirm your ad view — try again in a moment"
    # Optional (None = leave unchanged) so an older frontend that doesn't send
    # these can't accidentally reset what was set from the bot admin panel.
    multi_account_detect_enabled: bool | None = None
    proof_post_enabled: bool | None = None
    withdrawal_process_value: str | None = None
    withdrawal_process_unit: str | None = None


@api_app.post("/api/admin/settings/update")
async def api_admin_settings_update(body: AdminSettingsUpdateRequest):
    user = await _authenticate(body)
    _require_admin(user)
    await DataEngine.set_setting("reward_per_referral", str(body.reward_per_referral))
    await DataEngine.set_setting("min_withdrawal", str(body.min_withdrawal))
    await DataEngine.set_setting("min_ads_for_withdrawal", str(body.min_ads_for_withdrawal))
    await DataEngine.set_setting("usdt_rate", str(body.usdt_rate))
    await DataEngine.set_setting("ads_enabled", "1" if body.ads_enabled else "0")
    await DataEngine.set_setting("adsgram_block_id", body.adsgram_block_id.strip())
    await DataEngine.set_setting("ad_reward_amount", str(body.ad_reward_amount))
    await DataEngine.set_setting("ad_daily_limit", str(body.ad_daily_limit))
    await DataEngine.set_setting("ad_cooldown_seconds", str(body.ad_cooldown_seconds))
    await DataEngine.set_setting("ad_batch_size", str(max(0, body.ad_batch_size)))
    await DataEngine.set_setting("ad_batch_cooldown_seconds", str(max(0, body.ad_batch_cooldown_seconds)))
    await DataEngine.set_setting("adsgram_direct_link", body.adsgram_direct_link.strip())
    await DataEngine.set_setting("direct_link_reward_amount", str(body.direct_link_reward_amount))
    await DataEngine.set_setting("direct_link_daily_limit", str(body.direct_link_daily_limit))
    await DataEngine.set_setting("direct_link_wait_seconds", str(body.direct_link_wait_seconds))
    await DataEngine.set_setting("direct_link_cooldown_seconds", str(body.direct_link_cooldown_seconds))
    await DataEngine.set_setting("monetag_zone_id", body.monetag_zone_id.strip())
    await DataEngine.set_setting("monetag_sdk_url", body.monetag_sdk_url.strip())
    await DataEngine.set_setting("tads_enabled", "1" if body.tads_enabled else "0")
    await DataEngine.set_setting("tads_widget_id", body.tads_widget_id.strip())
    await DataEngine.set_setting("referral_skip_enabled", "1" if body.referral_skip_enabled else "0")
    await DataEngine.set_setting("referral_skip_batch_size", str(max(2, body.referral_skip_batch_size)))
    await DataEngine.set_setting("referral_skip_min", str(max(0, body.referral_skip_min)))
    await DataEngine.set_setting("referral_skip_max", str(max(body.referral_skip_min, body.referral_skip_max)))
    await DataEngine.set_setting("user_task_creation_enabled", "1" if body.user_task_creation_enabled else "0")
    await DataEngine.set_setting("user_task_min_reward", str(body.user_task_min_reward))
    await DataEngine.set_setting("user_task_max_reward", str(body.user_task_max_reward))
    await DataEngine.set_setting("user_task_min_slots", str(body.user_task_min_slots))
    await DataEngine.set_setting("user_task_max_slots", str(body.user_task_max_slots))
    await DataEngine.set_setting("support_username", body.support_username.strip().lstrip("@"))
    if body.ticker_enabled is not None:
        await DataEngine.set_setting("ticker_enabled", "1" if body.ticker_enabled else "0")
    if body.ticker_custom_names is not None:
        await DataEngine.set_setting("ticker_custom_names", body.ticker_custom_names)
    await DataEngine.set_setting("ad_click_reminder_enabled", "1" if body.ad_click_reminder_enabled else "0")
    await DataEngine.set_setting("ad_click_reminder_disguised", "1" if body.ad_click_reminder_disguised else "0")
    await DataEngine.set_setting("ad_click_reminder_text", body.ad_click_reminder_text.strip() or "Please wait until the ad finishes — do not close it early")
    await DataEngine.set_setting("ad_click_reminder_disguise_text", body.ad_click_reminder_disguise_text.strip() or "⚠️ Please tap the ad! You must click the ad to earn your reward.")
    await DataEngine.set_setting("ad_click_reminder_network_text", body.ad_click_reminder_network_text.strip() or "⚠️ Network error. Please try again.")
    await DataEngine.set_setting("ad_require_banner_click", "1" if body.ad_require_banner_click else "0")
    await DataEngine.set_setting("ad_banner_click_window_seconds", str(max(10, body.ad_banner_click_window_seconds)))
    await DataEngine.set_setting("ad_banner_click_cap_enabled", "1" if body.ad_banner_click_cap_enabled else "0")
    await DataEngine.set_setting("ad_banner_click_daily_cap", str(max(1, body.ad_banner_click_daily_cap)))
    await DataEngine.set_setting("daily_checkin_enabled", "1" if body.daily_checkin_enabled else "0")
    await DataEngine.set_setting("daily_checkin_reward", str(body.daily_checkin_reward))
    await DataEngine.set_setting("daily_checkin_reminder_text", body.daily_checkin_reminder_text.strip() or "⚠️ Couldn't confirm your ad view — try again in a moment")
    if body.multi_account_detect_enabled is not None:
        await DataEngine.set_setting("multi_account_detect_enabled", "1" if body.multi_account_detect_enabled else "0")
    if body.proof_post_enabled is not None:
        await DataEngine.set_setting("proof_post_enabled", "1" if body.proof_post_enabled else "0")
    if body.withdrawal_process_value is not None and re.fullmatch(r"\d{1,3}(-\d{1,3})?", body.withdrawal_process_value.strip()):
        await DataEngine.set_setting("withdrawal_process_value", body.withdrawal_process_value.strip())
    if body.withdrawal_process_unit in ("hours", "days"):
        await DataEngine.set_setting("withdrawal_process_unit", body.withdrawal_process_unit)
    return {"ok": True}


# ── /api/admin/ads/verify — connection check shown in the dashboard ─────
@api_app.post("/api/admin/ads/verify")
async def api_admin_ads_verify(body: ApiBase):
    user = await _authenticate(body)
    _require_admin(user)
    block_id     = (await DataEngine.get_setting("adsgram_block_id", "") or "").strip()
    direct_link  = (await DataEngine.get_setting("adsgram_direct_link", "") or "").strip()
    monetag_zone = (await DataEngine.get_setting("monetag_zone_id", "") or "").strip()
    monetag_url  = (await DataEngine.get_setting("monetag_sdk_url", "") or "").strip()
    tads_enabled_setting = (await DataEngine.get_setting("tads_enabled", "0")) == "1"
    tads_widget_id       = (await DataEngine.get_setting("tads_widget_id", "") or "").strip()
    ads_enabled  = (await DataEngine.get_setting("ads_enabled", "0")) == "1"
    video_ready  = ads_enabled and bool(block_id)
    link_ready   = ads_enabled and direct_link.lower().startswith(("http://", "https://"))
    monetag_ready = ads_enabled and bool(monetag_zone) and monetag_url.lower().startswith(("http://", "https://"))
    tads_ready   = ads_enabled and video_ready and tads_enabled_setting and bool(tads_widget_id)
    return {
        # Unknown from the server's side: the SDK is lazy-loaded in the
        # WebView and can fail per-network. This used to be hardcoded
        # True, so 'Check Connection' reported the SDK as loaded even
        # when it wasn't (BUG-REPORT #15) — the frontend now checks
        # window.Adsgram itself and ignores this field.
        "sdk_script_present": None,
        "ads_enabled": ads_enabled,
        "block_id_configured": bool(block_id),
        "direct_link_configured": bool(direct_link),
        "direct_link_valid_url": link_ready,
        "video_ready": video_ready,
        "direct_link_ready": link_ready,
        "monetag_configured": bool(monetag_zone) and bool(monetag_url),
        "monetag_ready": monetag_ready,
        "tads_configured": tads_enabled_setting and bool(tads_widget_id),
        "tads_ready": tads_ready,
    }


# ── /api/admin/broadcast — free-text announcement to all users ──────────
class AdminBroadcastRequest(ApiBase):
    text: str


@api_app.post("/api/admin/broadcast")
async def api_admin_broadcast(body: AdminBroadcastRequest):
    user = await _authenticate(body)
    _require_admin(user)
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text_required")
    sent, failed = await broadcast_to_all_users(body.text.strip())
    return {"sent": sent, "failed": failed}


# ═════════════════════════════════════════════════════════════════════════
# DATABASE BACKUPS — periodically ships a copy of the SQLite file to a
# private Telegram channel (only you should be a member of it). This is
# a safety net in case the Railway Volume itself is ever lost/corrupted;
# it does NOT replace the Volume, it's a second, independent copy.
# ═════════════════════════════════════════════════════════════════════════
async def send_db_backup():
    if not DB_BACKUP_CHANNEL_ID:
        return
    if not os.path.exists(DB_PATH):
        logger.warning(f"DB backup skipped — {DB_PATH} does not exist yet")
        return
    try:
        stamp = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-UTC")
        await bot.send_document(
            chat_id=DB_BACKUP_CHANNEL_ID,
            document=FSInputFile(DB_PATH, filename=f"referral_bot_{stamp}.db"),
            caption=f"🗄 Automatic DB backup — {stamp}",
        )
        logger.info(f"DB backup sent to {DB_BACKUP_CHANNEL_ID}")
    except Exception as e:
        logger.warning(f"DB backup failed: {e}")


async def db_backup_loop():
    # Send one right away at startup so there's always a fresh copy, then
    # repeat on the configured interval for as long as the process runs.
    await send_db_backup()
    while True:
        await asyncio.sleep(DB_BACKUP_INTERVAL_HOURS * 3600)
        await send_db_backup()


# ═════════════════════════════════════════════════════════════════════════
# ENTRYPOINT — runs the Telegram long-poller AND the Mini App web server
# in the same process (this is what Railway's "web: python bot.py" and
# healthcheckPath "/health" in railway.toml expect).
# ═════════════════════════════════════════════════════════════════════════
async def _run_web_server():
    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(api_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()





async def _main():
    global _polling_task, BOT_USERNAME, BOT_ID
    await DataEngine.init_database()
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        BOT_ID = me.id
    except Exception as e:
        logger.warning(f"Could not fetch bot identity at startup: {e}")
    # Pre-warm the invite-card photo cache so the very first time someone
    # taps "Share with Friends", Telegram's inline query gets answered
    # instantly instead of waiting on a first-time get_user_profile_photos
    # round-trip (which is what made it feel like a slow "search").
    try:
        await _get_bot_photo_file_id()
    except Exception as e:
        logger.warning(f"Could not pre-cache bot photo at startup: {e}")
    _polling_task = asyncio.create_task(dp.start_polling(bot, skip_updates=True))
    asyncio.create_task(db_backup_loop())
    asyncio.create_task(notify_ad_limit_reset_loop())
    await _run_web_server()


if __name__ == "__main__":
    asyncio.run(_main())
