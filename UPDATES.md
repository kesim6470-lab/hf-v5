# Hf v5 — Updates on top of the copied Hf-v4 codebase

Base: an unmodified copy of `Hf-v4-main/` (backend/bot.py 6,648 lines,
frontend/app.html ~3,961 lines). Phase 1 fixes every finding from
`hf-v4-review/BUG-REPORT.md` with surgical edits — no features removed,
no design/layout changes. Phase 2 (bottom) adds a new React + TypeScript
frontend; the fixed original frontend stays in `frontend/` as the legacy
fallback.

Line numbers below are for the **v5** files (they shifted vs v4).

## Phase 1 — the 15 findings

### HIGH

**1. Banned users could withdraw via bot chat — FIXED**
`process_withdrawal_start` now answers banned users with an alert and
stops (`backend/bot.py`, withdrawal section). Defence in depth:
`dispatch_withdrawal_core` — the single choke point both the bot-chat
flow and `/api/withdraw` funnel through — returns
`{"status": "error", "reason": "banned"}` for banned users, and the
payout-confirm handler maps that to a clear user message. (REST
`_authenticate` already 403'd banned users; the bot flow was the gap.)

**2. Client IP spoofable via proxy headers — FIXED**
`extract_real_ip` (`bot.py:2433`) only honours `CF-Connecting-IP` /
`X-Real-IP` / `X-Forwarded-For` when the new `TRUSTED_PROXY=1` env flag
is set (`bot.py:89`); by default it returns the direct client address.
Verified by test: headers ignored by default, honoured with the flag.
This also hardens the anti-fraud engine (see "Anti-fraud check" below),
which consumes exactly this one function.

**3. Task claim paid after sell-out / cancel / deactivate — FIXED**
`api_tasks_claim` now re-validates the fresh task row (`is_active`,
`review_status == "approved"`, slots remaining) and, for escrow tasks,
atomically takes the paid slot **before** paying via the new
`DataEngine.try_take_task_slot` (`bot.py:1844`, conditional
`UPDATE … WHERE slots_used < budget_slots`). If the claim race is lost
afterwards, the slot is handed back (`release_task_slot`).
`increment_task_slot`'s own increment is capped the same way. A claim
can therefore never pay out of escrow that no longer exists, and
`slots_used` can never exceed `budget_slots` (which the creator
cancel-refund math relies on). Tested: cap, sold-out refusal, release.

**4. Withdrawal approval not atomic — FIXED**
`approve_withdrawal_core` now flips the status through the new
`DataEngine.claim_withdrawal_approval` (`bot.py:973`): a single
`UPDATE … WHERE status='pending'` whose rowcount decides the winner.
Only the winning caller posts proof / DMs the user. A rejection landing
concurrently (already atomic, with its refund) can no longer be
overwritten back to "approved", and double-approval can't duplicate
the proof post. Tested: first claim wins, second loses, exactly once.

### MEDIUM

**5. Admin Search User crashed for verified users — FIXED**
`verif.get('canvas_hash')` / `verif.get('webgl_hash')` on an aiosqlite
Row (no `.get()`) replaced with plain indexing, with a comment.
Verified: no `verif.get(` remains.

**6. "Stop Bot" could never resume — FIXED (behaviour change, by design)**
`execute_bot_shutdown` no longer calls `dp.stop_polling()` (which made
the Resume callback permanently unreachable). It sets a `bot_paused`
setting instead; the new `BotPausedGateMiddleware` (`bot.py:4852`,
registered on message + callback_query) turns away every non-admin
update while paused, answers callbacks so clients don't spin, and lets
admins through — so Resume (and the whole admin panel) keeps working.
Resume simply clears the flag. Tested: user blocked / admin passes /
user passes after resume.

**7. Daily check-in double-dip — FIXED**
The claim gate accepted ANY recent completed video view, including a
regular Watch & Earn view that had already paid. Now only a
check-in-armed view counts: an `ad_events` row with
status `completed_checkin`, recorded exclusively when the AdsGram
postback consumes the `/api/dailycheck/arm` mark. New helper
`has_recent_checkin_ad` (`bot.py:1287`) backs both the status and claim
endpoints, and `claim_daily_checkin_atomic` re-verifies **and consumes**
the armed view (flips it to `checkin_claimed`) inside its
`BEGIN EXCLUSIVE` transaction — one armed view unlocks exactly one
claim. Tested: regular view rejected (`watch_ad_first`, no credit),
armed view pays once and is consumed, same-day re-claim blocked.

**8. AdsGram S2S postback: no idempotency, no ads_enabled check — FIXED**
(a) The handler now refuses with `ads_disabled` when the master
`ads_enabled` setting is off. (b) It accepts `transaction_id` / `event_id`
query params; when present, the delivery is recorded in the new
`adsgram_postbacks` table via `mark_adsgram_postback_seen`
(`bot.py:993`) and any redelivery of the same view is acknowledged
(`{"ok": true, "duplicate": true}`) without re-processing.
**Operator action required:** add the delivery id to the AdsGram reward
URL, e.g. `…&transaction_id=[transactionId]` (use whatever placeholder
the AdsGram dashboard offers). Until then, the pre-existing guards
still apply (atomic cooldown/daily-cap in `claim_video_ad_atomic`, the
one-shot consumption of the check-in pending mark), but a postback
redelivered *after* the cooldown with no id cannot be distinguished
from a new view — that residual is inherent to an id-less postback.

**9. Unauthenticated `/api/withdrawals/recent` leaked real users — FIXED**
The endpoint is the public home-screen ticker (fetched without login),
so it now returns **no real withdrawal rows at all** — only the
admin-configured custom names and the built-in placeholder list
(`real_list = []`, `bot.py:5029`, with the old query removed and the
reason commented). Real names/amounts are no longer exposed to anyone.

**10. Admin task review could refund escrow twice — FIXED**
`/api/admin/tasks/review` now runs through the new
`DataEngine.review_user_task_atomic` (`bot.py:1947`): the status flip
and (on reject) the full escrow refund happen in one
`BEGIN EXCLUSIVE` transaction keyed on `review_status='pending'`.
Concurrent reject+reject refunds once; approve+reject can't both
apply; a racing creator-cancel also loses cleanly. Tested: double
reject → one refund of exactly the escrow; approve-after-reject loses.
(The sibling manual-submission review was already guarded by a
conditional `UPDATE … WHERE status='pending'` + rowcount — verified,
unchanged.)

**11. Unvalidated admin inputs poisoned settings — FIXED**
The bot FSM handlers for referral bounty and minimum withdrawal stored
raw message text; both now parse and range-check a number first and
refuse with a clear message otherwise (so `float()` conversions in
user endpoints can no longer be poisoned by a typo). The edit-balance
FSM's unguarded `int()` / `float()` are wrapped the same way. The REST
settings endpoint already takes typed pydantic fields, so it was never
poisonable this way.

**12. Monetag fallback never wired — FIXED (frontend; see also Phase 1
frontend list below)** — `watchAd()` and `dailyCheckIn()` in
`frontend/app.html` now call `tryMonetagFallback()` on the AdsGram
no-fill / not-configured path before dead-ending. Honest limit,
unchanged from v4's trust model: Birr rewards are only ever credited
from AdsGram's S2S postback; Monetag has no reward postback in this
codebase, so a Monetag view fills the impression but cannot itself
credit a reward, and a Monetag-only configuration still cannot pay
until a Monetag postback endpoint + dashboard setup exists. What the
fix removes is the dead end: live buttons that could never even show
an ad despite a configured fallback network.

**13. Settings save wiped ticker config — FIXED**
`AdminSettingsUpdateRequest.ticker_enabled` / `ticker_custom_names`
are now `Optional` (`bot.py:6787`, same pattern the model already used
for `multi_account_detect_enabled`), and the update handler only
writes them when actually sent. The Mini App's `saveSettings()` never
sends them, so saves no longer re-enable the ticker or erase custom
names.

**14. Direct Link cooldown never displayed — FIXED (frontend)**
`renderHome` started the cooldown ticker only when `ADS.enabled`
(video) was true. It now starts when either the video flow **or**
`ADS.direct_link.enabled` is live (`app.html:1237`); the ticker
function already handled the Direct Link countdown separately.

**15. "Check Connection" false positive — FIXED (frontend + backend)**
The backend's `/api/admin/ads/verify` no longer hardcodes
`sdk_script_present: true` (it returns `None` — unknowable server-side,
`bot.py:6892`). `checkAdsgramConnection()` in `app.html` now checks the
real thing client-side — `window.Adsgram` after giving the lazy loader
a chance to finish — and renders that row from the local result.

## Phase 1 — frontend audit (beyond the backend findings)

Read `frontend/app.html` and `frontend/index.html` for broken things,
not just the three findings above:

- **Endpoint cross-check:** every `/api/*` path called by either
  frontend file exists in the backend (49/49). The only backend-only
  routes are the expected non-UI ones (AdsGram/Tads S2S webhooks,
  `/api/verify` used by index.html's fetch, `/api/invite/share_card`,
  `/api/withdrawals/recent`, `/health`).
- **Dead buttons:** all 40 `onclick` handlers in app.html resolve to
  defined functions; index.html has no unresolved handlers.
- **JS syntax:** every inline script block in both files passes
  `node --check`.
- Fixes #12, #14, #15 above are the frontend changes. No other broken
  frontend behaviour was found; no design/layout changes were made.

## Anti-fraud check (was it broken by the IP fix?)

Verified in `/api/verify`: the flow still runs, in order — verify rate
limiter → `extract_real_ip` → IP cooldown (when multi-detect on) →
fingerprint presence → banned-IP check → VPN lookup →
`evaluate_clone_risk` (self-referral, weighted correlation ≥4/6,
referrer-device ≥3, IP-farm with fingerprint-diversity guard) → atomic
`save_verification` claim → referral payout. The only gap the bug
report had noted was finding #2 itself: all of these consumed the
spoofable header IP. With the fix they now consume the real direct
client IP by default, so the correlation/cooldown/ban signals can no
longer be rotated away with a header. No other gaps were found in the
detection wiring; thresholds and the correlation model are untouched.

## Verification (Phase 1)

- `python3 -m py_compile backend/bot.py` — OK.
- Frontend JS: `node --check` on every inline script block of
  app.html and index.html — OK.
- Functional test suite (venv, real DataEngine against a throwaway
  SQLite DB, 23 checks) — **23/23 passed**: withdrawal approval
  exactly-once; task-review double-reject refunds exactly once and
  approve-after-reject loses; task slot cap/sold-out/release; check-in
  double-dip blocked (regular view), armed view pays once + consumed +
  same-day re-claim blocked; postback dedupe first/redelivery;
  `extract_real_ip` in both TRUSTED_PROXY modes; pause-gate middleware
  (user blocked, admin passes, resume restores).
- Grep sanity: no `verif.get(` remains; the real-names query in
  `/api/withdrawals/recent` is gone; raw-text setting stores are gone;
  untouched files (`requirements.txt`, `railway.toml`, `Procfile.txt`,
  `config.js`, `index.html`, `vercel.json`) are byte-identical to v4.

## What was NOT changed (Phase 1)

- No features removed; no design, layout, copy, or reward amounts
  changed (except the Stop Bot wording, which now says "paused"
  because that is what it does, and the two "no Monetag fallback"
  comments replaced by the wired fallback).
- The correlation-based anti-fraud model and its thresholds.
- `frontend/index.html` (verification gate page) — audited, no fixes
  needed.
- Deployment files and `config.js` (still points at the v4 Railway
  URL — update when v5 is deployed).
- Known residuals, honestly: finding #8's id-less postback case
  (above); finding #12's Monetag-can't-pay limit (above). Both need
  ad-network dashboard configuration, not more code in this repo.

## Phase 2 — React + TypeScript frontend (new primary)

Built in `frontend-react/` (Vite + React 18 + TypeScript, strict mode),
in the MyAge-style design from the hf-like preview: dark polished
game-like UI, glowing gradient hero card with balance count-up, glass
cards, colourful earn tiles, bottom nav — with the user's refinements
carried over: **💸 Withdraw** (not "Wallet"), **🎯 Tasks**, **🔥 Daily
Check In**, and **toasts that drop from the top**. A light
Telegram-blue theme is included (header toggle, persisted).

- **Telegram SDK route:** the plain `telegram-web-app.js` script
  (chosen over `@telegram-apps/sdk-react` — simpler and reliable;
  typed minimally in `src/api.ts`).
- **API config:** `window.APP_CONFIG = { API_BASE: "" }` in
  `frontend-react/index.html`, mirroring the legacy `config.js`
  approach — set it to the backend's public URL when the API is on a
  different origin. All calls POST `{ initData, ... }` to the SAME
  endpoints as legacy app.html (one exception: the public withdrawals
  ticker is a GET in the backend and is fetched as one).
- **Demo mode:** opened outside Telegram (no initData), the app runs
  on built-in mock data with a DEMO badge, so the design can be
  previewed without a backend — same idea as the hf-like preview.
- **Deployment:** Vercel static hosting — build command
  `npm run build`, output directory `dist/`. The legacy `frontend/`
  stays as fallback (and its `index.html` remains the verification
  gate page the bot links to).

### Screens (same feature set as legacy app.html)

- **Home** — hero balance card + progress-to-min bar + referral stat
  boxes; live withdrawals ticker; force-join gate banner; Daily
  Check-in tile (arm → ad → S2S-confirmed claim with retry, streak);
  Watch Ad tile (claim gate → AdsGram, Monetag fallback on no-fill,
  poll-until-S2S-confirmed, click-reminder handling, batch/cooldown
  errors); Direct Link card (open → wait → claim, cooldown display);
  quick actions.
- **Tasks 🎯** — join → wait → check → claim with all legacy statuses
  (waiting/claimable/completed/review/rejected), manual screenshot
  upload tasks, My Tasks (review status, slots, escrow, cancel with
  refund), and the Create-Task wizard (channel verify → details →
  escrow) gated by the backend's `user_task_config`.
- **Invite** — referral + tier-2 stats, copyable link, Telegram share,
  and the bot-sent share card (`/api/invite/share_card`).
- **Withdraw 💸** — Telebirr (phone + holder name) and Binance Pay
  (UID + live USDT estimate), min-withdrawal from `/api/me`, full
  history with status pills and rejection reasons.
- **Admin** (only rendered when `/api/me` says `is_admin`) — Overview
  stats; Withdrawals settle queue (approve with user+admin notify,
  reject with reason — server-side exactly-once); Tasks (user-task
  review queue, manual-proof review queue, create, active toggle,
  delete); Users (search, ban/unban, balance adjust); Channels
  (force-join list/create/delete, real vs fake mode); Ads (analytics
  + Check Connection — config checks server-side, SDK presence is a
  client concern, per the Phase-1 fix); Settings (core economics + ads
  config — see honest list below); Broadcast.
- **Gate states** — `/api/me` 403 `not_verified` shows a "finish
  verification in the bot" notice; 403 `banned` shows a banned notice.

### Verification (Phase 2)

- `npm install` — OK. `npm run build` (`tsc -b && vite build`) —
  **succeeded**, type-clean, `dist/` produced (~190 kB JS, 59 kB gzip).

### Honest list — legacy app.html features NOT (fully) ported

- **Leaderboard:** legacy Hf has **no leaderboard screen or endpoint**
  (checked) — there was nothing to port. The medal-leaderboard styling
  from hf-like was therefore not applicable.
- **Admin Settings form:** the React form covers the core fields
  (economics, AdsGram, Direct Link, Monetag, Tads toggle, user-task
  creation, daily check-in). It does NOT expose: click-reminder
  texts/disguise, referral-skip tuning, ad batch size/cooldown tuning,
  banner-click window, withdrawal-ticker settings, check-in streak
  bonus table. Safe by design: the form submits the fetched settings
  object back whole, so unshown fields keep their current values and
  are never reset.
- **Task reorder** (legacy's move up/down) — not ported; tasks render
  in backend order.
- **Ads analytics** — shown as a compact JSON summary, not the
  legacy panel's formatted tables.
- **Tads second-ad gate UI** — not ported (backend support untouched;
  the earn flow is AdsGram + Monetag, as in legacy's main path).
- **Monetag catch-up/bonus localStorage scheduling** (`hf_catchup_*`)
  — not ported; the Monetag *fallback* itself is ported (Phase-1 #12).
- **Fake-join gate modal** ("I've joined" + `fake_seen` marking) —
  the React Home shows the force-join list with Join buttons; the
  dedicated fake-channel modal flow is not ported. The real
  verification gate remains legacy `frontend/index.html`.
- **Task Check auto-retry loop** — legacy retries membership checks up
  to 3 times automatically; React does one check per tap (tap again).
- **Manual-proof screenshots in Admin** — React shows the review queue
  and relies on the bot chat copies (legacy forwards them too); no
  in-app image preview.

