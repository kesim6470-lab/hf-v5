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
/** Demo mode = opened in a plain browser (no Telegram initData). The app
 *  then runs on the mock data below so the design can be previewed —
 *  same idea as the hf-like preview the user approved. */
export const DEMO = !initData;

const API_BASE = (typeof window !== "undefined" && window.APP_CONFIG?.API_BASE) || "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

export async function api<T = any>(path: string, body: Record<string, any> = {}): Promise<T> {
  if (DEMO) return demoApi(path, body) as Promise<T>;
  if (path === "/api/withdrawals/recent") {
    // The public ticker is a plain GET in the backend (no initData).
    const res = await fetch(`${API_BASE}${path}`);
    if (!res.ok) throw new ApiError(res.status, res.statusText);
    return (await res.json()) as T;
  }
  const r = await fetch(API_BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ initData, ...body }),
  });
  let data: any = null;
  try { data = await r.json(); } catch { /* non-JSON */ }
  if (!r.ok) throw new ApiError(r.status, (data && data.detail) || `http_${r.status}`);
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
}

export interface AdsStatus {
  enabled: boolean;
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

export interface DailyStatus {
  enabled: boolean;
  reward: number;
  claimed_today: boolean;
  streak: number;
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

/* ---------------- demo-mode mock backend (preview only) ---------------- */

const demoMe: Me = {
  user_id: 1001, balance: 42.5, referrals: 4, tier2_referrals: 11,
  reward_per_referral: 10, min_withdrawal: 50, usdt_rate: 155,
  total_earned_refs: 40, referral_link: "https://t.me/Hfearnmoneyv4bot?start=1001",
  is_admin: true, support_username: "", withdrawal_process_time: "24 hours",
};

const demoState = {
  balance: 42.5,
  checkinClaimed: false,
  streak: 3,
  watched: 2,
  dlWatched: 1,
  tasks: [
    { id: 1, title: "Join our channel", channel_id: "", invite_link: "https://t.me/", task_type: "fake", reward: 3, status: "none" },
    { id: 2, title: "Join our group", channel_id: "", invite_link: "https://t.me/", task_type: "fake", reward: 2, status: "claimable" },
    { id: 3, title: "Post a screenshot proof", channel_id: "", invite_link: "", task_type: "manual", reward: 2.5, status: "none", instructions: "Upload a screenshot of the completed action.", screenshot_count: 1 },
  ] as Task[],
  withdrawals: [
    { id: 1, user_id: 1001, amount: 100, full_name: "Demo User", phone: "09••••1234", method: "telebirr", status: "approved", created_at: "2026-11-20 10:00:00" },
  ] as Withdrawal[],
};

function demoApi(path: string, body: Record<string, any>): any {
  switch (path) {
    case "/api/me":
      return { ...demoMe, balance: demoState.balance };
    case "/api/gate/status":
      return { force_unjoined: [], fake_channels: [], fake_seen: true };
    case "/api/ads/status":
      return {
        enabled: true, block_id: "demo-block", reward_amount: 0.5, daily_limit: 10,
        watched_today: demoState.watched, seconds_left: 0, is_batch_wait: false,
        click_reminder: { enabled: true, text: "Watch the ad to the end", disguised: false, disguise_text: "", network_text: "Network error" },
        monetag: { zone_id: "", sdk_url: "" },
        tads: { enabled: false, widget_id: "" },
        direct_link: { enabled: true, url: "https://example.com", reward_amount: 0.3, daily_limit: 10, watched_today: demoState.dlWatched, seconds_left: 0, wait_seconds: 5 },
      } as AdsStatus;
    case "/api/dailycheck/status":
      return { enabled: true, reward: 5, claimed_today: demoState.checkinClaimed, streak: demoState.streak, ad_watched_recently: false, reminder_text: "Couldn't confirm your ad view" } as DailyStatus;
    case "/api/dailycheck/arm":
      return { ok: true };
    case "/api/dailycheck/claim":
      if (demoState.checkinClaimed) throw new ApiError(400, "already_claimed");
      demoState.checkinClaimed = true; demoState.streak += 1; demoState.balance += 5;
      return { status: "claimed", reward: 5, streak: demoState.streak, balance: demoState.balance };
    case "/api/ads/claim":
      return { ok: true, will_pay: true };
    case "/api/ads/direct-link/open":
      return { url: "https://example.com", event_id: 1, wait_seconds: 5 };
    case "/api/ads/direct-link/claim":
      demoState.dlWatched += 1; demoState.balance += 0.3;
      return { ok: true, reward: 0.3, balance: demoState.balance };
    case "/api/withdrawals/recent":
      return { enabled: true, withdrawals: [
        { full_name: "Abebe", amount: 250, status: "approved" },
        { full_name: "Sara", amount: 500, status: "approved" },
        { full_name: "Yonas", amount: 180, status: "approved" },
      ] };
    case "/api/tasks":
      return { tasks: demoState.tasks, user_task_config: { enabled: true, min_reward: 1, max_reward: 20, min_slots: 5, max_slots: 500, bot_username: "Hfearnmoneyv4bot" } };
    case "/api/tasks/join": {
      const t = demoState.tasks.find((x) => x.id === body.task_id); if (t) t.status = "claimable";
      return { ok: true };
    }
    case "/api/tasks/check":
      return { ok: true };
    case "/api/tasks/claim": {
      const t = demoState.tasks.find((x) => x.id === body.task_id);
      if (!t || t.status === "completed") throw new ApiError(400, "not_claimable");
      t.status = "completed"; demoState.balance += t.reward;
      return { status: "completed", reward: t.reward, balance: demoState.balance };
    }
    case "/api/tasks/manual/submit": {
      const t = demoState.tasks.find((x) => x.id === body.task_id); if (t) t.status = "review";
      return { status: "review" };
    }
    case "/api/tasks/mine":
      return { tasks: [] };
    case "/api/withdraw": {
      const amt = Number(body.amount);
      if (amt < demoMe.min_withdrawal) throw new ApiError(400, `min_withdrawal_is_${demoMe.min_withdrawal}`);
      if (amt > demoState.balance) throw new ApiError(400, "insufficient_funds");
      demoState.balance -= amt;
      demoState.withdrawals.unshift({ id: demoState.withdrawals.length + 1, user_id: 1001, amount: amt, full_name: body.full_name || "", phone: body.phone || "", method: body.method || "telebirr", binance_uid: body.binance_uid || "", status: "pending", created_at: "now" });
      return { status: "submitted", ticket_id: demoState.withdrawals[0].id, process_time: "24 hours" };
    }
    case "/api/withdraw/history":
      return { withdrawals: demoState.withdrawals };
    case "/api/invite/share_card":
      return { ok: true };
    default:
      if (path.startsWith("/api/admin/")) return { ok: true, tasks: [], withdrawals: [], channels: [], submissions: [] };
      return { ok: true };
  }
}

/* ---------------- formatting ---------------- */

export const fmt = (n: number | string): string => {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(2).replace(/\.00$/, "") : "0";
};
