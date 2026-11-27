/* API client for the Hf v5 backend — mirrors the legacy app.html contract:
   every call is a POST with { initData, ...body } as JSON, and errors
   surface as Error(detail) so screens can toast the backend's reason. */

declare global {
  interface Window {
    APP_CONFIG?: { API_BASE?: string };
    Telegram?: any;
    Adsgram?: any;
  }
}

export const tg = typeof window !== "undefined" ? window.Telegram?.WebApp : null;
if (tg) {
  try { tg.ready(); tg.expand(); } catch { /* outside Telegram */ }
}

export const initData: string = tg?.initData || "";

/** Backend base URL (from window.APP_CONFIG in index.html). */
export const API_BASE = (typeof window !== "undefined" && window.APP_CONFIG?.API_BASE) || "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

/* 15-second read cache (owner request 2026-11-26): switching between
   screens re-mounts them and would otherwise re-ask the server for the
   same data every few seconds — congestion + stutter. Read-style
   endpoints are served from memory for 15s; ANY action call (join,
   claim, withdraw, admin saves…) clears the whole cache first, so
   state is never stale after something changes. */
const READ_PATHS = new Set([
  "/api/me", "/api/tasks", "/api/tasks/mine", "/api/leaderboard",
  "/api/ads/status", "/api/dailycheck/status",
  // NOTE: /api/gate/status is not in the 40s read cache — the gate runs
  // on a 5-minute cycle instead (App.tsx interval + the backend's own
  // 5-minute per-user OK cache), so membership is re-verified regularly
  // without hammering the server or Telegram (owner order 2026-11-26).
  "/api/withdraw/history", "/api/withdrawals/recent",
  "/api/admin/stats/overview", "/api/admin/settings",
  "/api/admin/withdrawals/list", "/api/admin/tasks/list",
  "/api/admin/tasks/pending", "/api/admin/tasks/manual_submissions",
  "/api/admin/channels/list", "/api/admin/weekly_top",
  "/api/admin/ads/analytics", "/api/admin/users/search",
]);
const CACHE_TTL_MS = 40_000; // owner order 2026-11-26: reads cached 40s; any action call clears the cache and always hits the server fresh
const _readCache = new Map<string, { at: number; data: any }>();

export function clearApiCache(): void { _readCache.clear(); }

export async function api<T = any>(path: string, body: Record<string, any> = {}): Promise<T> {
  const isRead = READ_PATHS.has(path);
  const cacheKey = path + "|" + JSON.stringify(body);
  if (isRead) {
    const hit = _readCache.get(cacheKey);
    if (hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.data as T;
  } else {
    _readCache.clear();
  }
  let data: any;
  if (path === "/api/withdrawals/recent") {
    // The public ticker is a plain GET in the backend (no initData).
    const res = await fetch(`${API_BASE}${path}`);
    if (!res.ok) throw new ApiError(res.status, res.statusText);
    data = await res.json();
  } else {
    const r = await fetch(API_BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initData, ...body }),
    });
    try { data = await r.json(); } catch { data = null; /* non-JSON */ }
    if (!r.ok) throw new ApiError(r.status, (data && data.detail) || `http_${r.status}`);
  }
  if (isRead) _readCache.set(cacheKey, { at: Date.now(), data });
  return data as T;
}

/* ---------------- types (shapes returned by the v5 backend) ---------------- */

export interface Me {
  user_id: number;
  balance: number;
  referrals: number;
  tier2_referrals: number;
  reward_per_referral: number;
  min_withdrawal: number;
  usdt_rate: number;
  total_earned_refs: number;
  referral_link: string;
  is_admin: boolean;
  support_username: string;
  withdrawal_process_time: string;
  withdraw_methods: { telebirr: boolean; binance: boolean };
  invite_show_binance: boolean;
}

export interface AdsStatus {
  enabled: boolean;
  maintenance: boolean;
  block_id: string;
  reward_amount: number;
  daily_limit: number;
  watched_today: number;
  seconds_left: number;
  is_batch_wait: boolean;
  click_reminder: { enabled: boolean; text: string; disguised: boolean; disguise_text: string; network_text: string };
  monetag: { zone_id: string; sdk_url: string };
  tads: { enabled: boolean; widget_id: string };
  direct_link: {
    enabled: boolean; url: string; reward_amount: number; daily_limit: number;
    watched_today: number; seconds_left: number; wait_seconds: number;
  };
}

export interface LeaderboardEntry {
  rank: number;
  name: string;
  refs: number;
  pct: number;
  prize: number;
  is_me: boolean;
}

export interface Leaderboard {
  updated_at: string;
  weekly: { enabled: boolean; pool: number; week_id: string; ends_at: string; seconds_left: number };
  top: LeaderboardEntry[];
  me: { rank: number | null; refs: number; prize: number };
}

export interface DailyStatus {
  enabled: boolean;
  reward: number;
  claimed_today: boolean;
  streak: number;
  ad_required: boolean;
  ad_watched_recently: boolean;
  reminder_text: string;
}

export interface Task {
  id: number;
  title: string;
  channel_id: string;
  invite_link: string;
  task_type: "force" | "fake" | "manual" | string;
  reward: number;
  status: "none" | "waiting" | "claimable" | "completed" | "review" | "rejected" | string;
  wait_left?: number;
  instructions?: string;
  screenshot_count?: number;
  created_by?: number;
  budget_slots?: number;
  slots_used?: number;
  review_status?: string;
  is_active?: number;
}

export interface Withdrawal {
  id: number;
  user_id: number;
  amount: number;
  full_name: string;
  phone: string;
  method: string;
  binance_uid?: string;
  status: string;
  created_at: string;
  reason?: string;
}

/* ---------------- formatting ---------------- */

export const fmt = (n: number | string): string => {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(2).replace(/\.00$/, "") : "0";
};
