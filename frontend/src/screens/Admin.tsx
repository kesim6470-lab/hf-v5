import { useCallback, useEffect, useState } from "react";
import { api, fmt, type Task, type Withdrawal } from "../api";
import { useApp } from "../App";
import { Btn, Shimmer } from "../components/ui";

type Sub = "overview" | "withdrawals" | "tasks" | "users" | "channels" | "ads" | "settings" | "broadcast";

const SUBS: { id: Sub; label: string }[] = [
  { id: "overview", label: "📊 Overview" },
  { id: "withdrawals", label: "💸 Withdrawals" },
  { id: "tasks", label: "🎯 Tasks" },
  { id: "users", label: "👥 Users" },
  { id: "channels", label: "📣 Channels" },
  { id: "ads", label: "🎬 Ads" },
  { id: "settings", label: "⚙️ Settings" },
  { id: "broadcast", label: "📢 Broadcast" },
];

/** Admin Control Center — same backend endpoints as the legacy admin
 *  panel: overview stats, withdrawal settle (exactly-once on the
 *  server), task review queues, user search/ban/balance, force-join
 *  channels, ads analytics + connection check, settings, broadcast. */
export default function Admin() {
  const [sub, setSub] = useState<Sub>("overview");
  return (
    <section className="screen">
      <h3 className="sect">⚙️ Admin Control Center</h3>
      <div className="seg" style={{ flexWrap: "wrap" }}>
        {SUBS.map((s) => (
          <button key={s.id} style={{ flex: "1 1 22%" }} className={sub === s.id ? "active" : ""} onClick={() => setSub(s.id)}>
            {s.label}
          </button>
        ))}
      </div>
      {sub === "overview" && <Overview />}
      {sub === "withdrawals" && <AdminWithdrawals />}
      {sub === "tasks" && <AdminTasks />}
      {sub === "users" && <AdminUsers />}
      {sub === "channels" && <AdminChannels />}
      {sub === "ads" && <AdminAds />}
      {sub === "settings" && <AdminSettings />}
      {sub === "broadcast" && <AdminBroadcast />}
    </section>
  );
}

function Overview() {
  const { toast } = useApp();
  const [stats, setStats] = useState<any>(null);
  useEffect(() => { api("/api/admin/stats/overview").then(setStats).catch(() => {}); }, []);
  // Weekly Top 20 reward config (pool + rank percentages). NOTE: these
  // hooks MUST stay above the early return below — hooks after a
  // conditional return crash React ("Rendered more hooks than during
  // the previous render") and blank the whole app.
  const [wk, setWk] = useState<{ pool: number; percents: string; enabled: boolean; last_paid: string; exclude_ids: string } | null>(null);
  useEffect(() => {
    api<any>("/api/admin/weekly_top").then((r) => setWk({
      pool: r.pool, percents: r.percents.join(", "), enabled: r.enabled, last_paid: r.last_paid || "",
      exclude_ids: (r.exclude_ids || []).join(", "),
    })).catch(() => {});
  }, []);
  if (!stats) return <Shimmer rows={3} />;

  async function seedTest() {
    try { await api("/api/admin/leaderboard/seed_test"); toast("🧪 Test leaderboard added — open the 🏆 Top tab"); }
    catch (e: any) { toast(e?.message || "Failed"); }
  }
  async function clearTest() {
    if (!window.confirm("Remove the test leaderboard names?")) return;
    try { await api("/api/admin/leaderboard/clear_test"); toast("🧹 Test leaderboard removed"); }
    catch (e: any) { toast(e?.message || "Failed"); }
  }
  async function rotateSecrets() {
    if (!window.confirm("Generate NEW webhook secrets? The webhook URLs in the ad dashboards must be updated to match.")) return;
    try {
      const r = await api<{ ok: boolean; reason?: string }>("/api/admin/security/rotate_secrets");
      toast(r.ok ? "🔐 Secrets rotated" : r.reason === "env_override" ? "⚠️ Env vars override — change them on Railway" : "Failed");
    } catch (e: any) { toast(e?.message || "Failed"); }
  }

  async function saveWeekly() {
    if (!wk) return;
    const percents = wk.percents.split(",").map((x) => Number(x.trim())).filter((x) => !Number.isNaN(x));
    const exclude_ids = wk.exclude_ids.split(",").map((x) => Number(x.trim())).filter((x) => Number.isInteger(x) && x > 0);
    try {
      await api("/api/admin/weekly_top/save", { pool: Number(wk.pool), percents, enabled: wk.enabled, exclude_ids });
      toast("🏆 Weekly reward saved");
    } catch (e: any) { toast(e?.message || "Failed — percentages must be 20 numbers adding to 100"); }
  }

  return (
    <>
      <div className="stat-row" style={{ marginTop: 0 }}>
        <div className="stat-box"><div className="n">{stats.total_users}</div><div className="l">Total users</div></div>
        <div className="stat-box"><div className="n">{stats.online_users}</div><div className="l">Online (recent)</div></div>
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div style={{ fontWeight: 800, marginBottom: 6 }}>🏆 Top Referrers — test data</div>
        <div className="s" style={{ color: "var(--muted)", fontSize: 12 }}>
          Adds sample names (Samira, dawit T, Abdi, Mule Mosisa, Yoni, Tame, wildflower, Bura, Suru, Da, Unfair World, Muaze, Kasahun, Nati, Cr 7) to the Top board so you can preview it. Remove them before real users arrive.
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          <Btn className="btn gold" style={{ flex: 1 }} onClick={seedTest}>🧪 Add test names</Btn>
          <Btn className="btn ghost" style={{ flex: 1 }} onClick={clearTest}>🧹 Remove</Btn>
        </div>
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div style={{ fontWeight: 800, marginBottom: 6 }}>🏆 Weekly Top 20 reward</div>
        <div className="s" style={{ color: "var(--muted)", fontSize: 12 }}>
          Every Monday the new week starts; last week's Top 20 are paid automatically from this pool, split by the rank percentages below.
          {wk?.last_paid ? ` Last paid week: ${wk.last_paid}.` : ""}
        </div>
        {wk && (
          <>
            <label className="f">Prize pool (Birr)</label>
            <input type="number" step="any" value={wk.pool} onChange={(e) => setWk({ ...wk, pool: Number(e.target.value) })} />
            <label className="f">Rank percentages — 20 numbers for ranks 1–20, must add to 100</label>
            <input type="text" value={wk.percents} onChange={(e) => setWk({ ...wk, percents: e.target.value })} />
            <label className="f">Removed from Top — user IDs, comma separated (they won't show on the board or be paid)</label>
            <input type="text" value={wk.exclude_ids} onChange={(e) => setWk({ ...wk, exclude_ids: e.target.value })} placeholder="e.g. 6095314644, 6924162851" />
            <label className="f" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input type="checkbox" checked={wk.enabled} onChange={(e) => setWk({ ...wk, enabled: e.target.checked })} style={{ width: 18, height: 18 }} />
              Weekly rewards ON
            </label>
            <Btn className="btn gold" style={{ width: "100%", marginTop: 10 }} onClick={saveWeekly}>💾 Save weekly reward</Btn>
          </>
        )}
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div style={{ fontWeight: 800, marginBottom: 6 }}>🔐 Webhook secrets</div>
        <div className="s" style={{ color: "var(--muted)", fontSize: 12 }}>
          Generates fresh AdsGram + Tads webhook secrets. Do this before pasting the webhook URLs into the ad dashboards — afterwards only the new secrets match.
        </div>
        <Btn className="btn ghost" style={{ width: "100%", marginTop: 12 }} onClick={rotateSecrets}>🔐 Rotate secrets</Btn>
      </div>
    </>
  );
}

function AdminWithdrawals() {
  const { toast } = useApp();
  const [list, setList] = useState<Withdrawal[]>([]);
  const [busy, setBusy] = useState<number | null>(null);
  const load = useCallback(async () => {
    try { const r = await api<{ withdrawals: Withdrawal[] }>("/api/admin/withdrawals/list"); setList(r.withdrawals || []); }
    catch (e: any) { toast(e?.message || "Load failed"); }
  }, [toast]);
  useEffect(() => { load(); }, [load]);

  async function settle(w: Withdrawal, approve: boolean) {
    let reason = "";
    if (!approve) {
      reason = window.prompt("Rejection reason (sent to the user):", "Violated bot usage policies.") || "";
      if (!reason) return;
    }
    setBusy(w.id);
    try {
      if (approve) await api("/api/admin/withdrawals/approve", { id: w.id, notify_mode: "both" });
      else await api("/api/admin/withdrawals/reject", { id: w.id, reason });
      toast(approve ? `✅ Ticket #${w.id} approved` : `❌ Ticket #${w.id} rejected & refunded`);
      load();
    } catch (e: any) {
      toast(e?.message === "already_processed" ? "Already processed" : e?.message || "Settle failed");
      load();
    }
    setBusy(null);
  }

  if (list.length === 0) return <div className="empty">📭 No pending withdrawals</div>;
  return (
    <>
      {list.map((w) => (
        <div className="row" key={w.id}>
          <div className="grow">
            <div className="t">#{w.id} — {fmt(w.amount)} Birr</div>
            <div className="s">{w.full_name} · {w.method === "binance" ? `Binance UID ${w.binance_uid}` : w.phone} · user {w.user_id}</div>
          </div>
          <button className="btn sm" disabled={busy === w.id} onClick={() => settle(w, true)}>✅ Approve</button>
          <button className="btn sm danger" disabled={busy === w.id} onClick={() => settle(w, false)}>❌ Reject</button>
        </div>
      ))}
    </>
  );
}

function AdminTasks() {
  const { toast } = useApp();
  const [pending, setPending] = useState<Task[]>([]);
  const [manual, setManual] = useState<any[]>([]);
  const [all, setAll] = useState<Task[]>([]);
  const [title, setTitle] = useState("");
  const [reward, setReward] = useState("3");
  const [link, setLink] = useState("");
  const [channel, setChannel] = useState("");
  const [type, setType] = useState("fake");

  const load = useCallback(async () => {
    const [p, m, a] = await Promise.all([
      api<{ tasks: Task[] }>("/api/admin/tasks/pending").catch(() => ({ tasks: [] })),
      api<{ submissions: any[] }>("/api/admin/tasks/manual_submissions").catch(() => ({ submissions: [] })),
      api<{ tasks: Task[] }>("/api/admin/tasks/list").catch(() => ({ tasks: [] })),
    ]);
    setPending(p.tasks || []); setManual(m.submissions || []); setAll(a.tasks || []);
  }, []);
  useEffect(() => { load(); }, [load]);

  async function reviewTask(id: number, approve: boolean) {
    try { await api("/api/admin/tasks/review", { id, approve }); toast(approve ? "✅ Task approved & live" : "❌ Task rejected & refunded"); }
    catch (e: any) { toast(e?.message || "Review failed"); }
    load();
  }
  async function reviewManual(id: number, approve: boolean) {
    try { await api("/api/admin/tasks/manual_submissions/review", { id, approve }); toast(approve ? "✅ Approved & paid" : "❌ Rejected"); }
    catch (e: any) { toast(e?.message || "Review failed"); }
    load();
  }
  async function createTask() {
    if (!title.trim()) return toast("Enter a title");
    try {
      await api("/api/admin/tasks/create", { title, task_type: type, invite_link: link, channel_id: channel, reward: Number(reward) });
      toast("✅ Task created"); setTitle(""); setLink(""); setChannel("");
    } catch (e: any) {
      if (e?.message === "bot_not_admin_in_channel") toast("⚠️ Add @AnonEarnAppBot as an admin in that channel first");
      else if (e?.message === "force_needs_channel") toast("⚠️ Force tasks need a channel @username or ID");
      else toast(e?.message || "Create failed");
    }
    load();
  }
  async function toggleActive(t: Task) {
    try { await api("/api/admin/tasks/update", { id: t.id, is_active: t.is_active ? 0 : 1 }); }
    catch (e: any) { toast(e?.message || "Update failed"); }
    load();
  }
  async function del(t: Task) {
    if (!window.confirm(`Delete task "${t.title}"?`)) return;
    try { await api("/api/admin/tasks/delete", { id: t.id }); toast("🗑 Deleted"); }
    catch (e: any) { toast(e?.message || "Delete failed"); }
    load();
  }

  return (
    <>
      {pending.length > 0 && <h3 className="sect">User tasks awaiting review ({pending.length})</h3>}
      {pending.map((t) => (
        <div className="row" key={`p${t.id}`}>
          <div className="grow"><div className="t">{t.title}</div>
            <div className="s">{fmt(t.reward)} Birr × {t.budget_slots} slots · by user {t.created_by}</div></div>
          <button className="btn sm" onClick={() => reviewTask(t.id, true)}>✅ Approve</button>
          <button className="btn sm danger" onClick={() => reviewTask(t.id, false)}>❌ Reject</button>
        </div>
      ))}

      {manual.length > 0 && <h3 className="sect">Screenshot proofs awaiting review ({manual.length})</h3>}
      {manual.map((s) => (
        <div className="row" key={`m${s.id}`}>
          <div className="grow"><div className="t">{s.task_title || `Task #${s.task_id}`}</div>
            <div className="s">User {s.user_id} · screenshots were sent to the admin bot chat</div></div>
          <button className="btn sm" onClick={() => reviewManual(s.id, true)}>✅ Pay</button>
          <button className="btn sm danger" onClick={() => reviewManual(s.id, false)}>❌ Reject</button>
        </div>
      ))}

      <h3 className="sect">Add new task</h3>
      <div className="card">
        <label className="f">Title</label>
        <input value={title} onChange={(e) => setTitle(e.target.value)} />
        <label className="f">Invite link</label>
        <input value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://t.me/…" />
        <label className="f">Type</label>
        <select value={type} onChange={(e) => setType(e.target.value)}>
          <option value="fake">No check (timer only)</option>
          <option value="force">Force check (must join channel)</option>
          <option value="manual">Manual (screenshot proof)</option>
        </select>
        {type === "force" && (
          <>
            <label className="f">Channel (@username or ID) — @AnonEarnAppBot must be an admin there</label>
            <input value={channel} onChange={(e) => setChannel(e.target.value)} placeholder="@YourChannel" />
          </>
        )}
        <label className="f">Reward (Birr)</label>
        <input type="number" value={reward} onChange={(e) => setReward(e.target.value)} />
        <button className="btn" onClick={createTask}>➕ Create task</button>
      </div>

      <h3 className="sect">All tasks ({all.length})</h3>
      {all.map((t) => (
        <div className="row" key={`a${t.id}`}>
          <div className="grow"><div className="t">{t.title}</div>
            <div className="s">{t.task_type} · {fmt(t.reward)} Birr · {t.is_active ? "active" : "inactive"}{t.review_status ? ` · ${t.review_status}` : ""}</div></div>
          <button className="btn sm ghost" onClick={() => toggleActive(t)}>{t.is_active ? "⏸ Off" : "▶️ On"}</button>
          <button className="btn sm danger" onClick={() => del(t)}>🗑</button>
        </div>
      ))}
    </>
  );
}

function AdminUsers() {
  const { toast } = useApp();
  const [uid, setUid] = useState("");
  const [user, setUser] = useState<any>(null);
  const [amount, setAmount] = useState("");

  async function search() {
    try {
      const r = await api<any>("/api/admin/users/search", { user_id: Number(uid) });
      setUser(r);
    } catch (e: any) { toast(e?.message || "Not found"); setUser(null); }
  }
  async function ban(banned: boolean) {
    try { await api("/api/admin/users/ban", { user_id: user.user_id, banned: banned ? 1 : 0 }); toast(banned ? "🚫 Banned" : "✅ Unbanned"); search(); }
    catch (e: any) { toast(e?.message || "Failed"); }
  }
  async function balance() {
    try { await api("/api/admin/users/balance", { user_id: user.user_id, amount: Number(amount) }); toast("✅ Balance adjusted"); setAmount(""); search(); }
    catch (e: any) { toast(e?.message || "Failed"); }
  }

  return (
    <>
      <div className="card">
        <label className="f">Telegram user ID</label>
        <input value={uid} onChange={(e) => setUid(e.target.value)} inputMode="numeric" placeholder="e.g. 123456789" />
        <button className="btn" onClick={search}>🔍 Search</button>
      </div>
      {user && (
        <div className="card" style={{ marginTop: 12 }}>
          <div style={{ fontWeight: 800, fontSize: 16 }}>{user.full_name} {user.username ? `(@${user.username})` : ""}</div>
          <div className="s" style={{ color: "var(--muted)", fontSize: 13, marginTop: 4, lineHeight: 1.6 }}>
            Balance: <b>{fmt(user.balance)} Birr</b> · Referrals: {user.referrals} · Banned: {user.is_banned ? "Yes 🚫" : "No"}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            {user.is_banned
              ? <button className="btn sm" onClick={() => ban(false)}>✅ Unban</button>
              : <button className="btn sm danger" onClick={() => ban(true)}>🚫 Ban</button>}
          </div>
          <label className="f">Adjust balance (Birr, negative to deduct)</label>
          <input type="number" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="50 or -50" />
          <button className="btn ghost" onClick={balance}>Apply</button>
        </div>
      )}
    </>
  );
}

function AdminChannels() {
  const { toast } = useApp();
  const [channels, setChannels] = useState<any[]>([]);
  const [name, setName] = useState("");
  const [link, setLink] = useState("");
  const [cid, setCid] = useState("");
  const [botAdded, setBotAdded] = useState(1);

  const load = useCallback(async () => {
    try { const r = await api<{ channels: any[] }>("/api/admin/channels/list"); setChannels(r.channels || []); }
    catch (e: any) { toast(e?.message || "Load failed"); }
  }, [toast]);
  useEffect(() => { load(); }, [load]);

  async function add() {
    if (!name.trim() || !link.trim()) return toast("Name and link are required");
    try {
      await api("/api/admin/channels/create", { bot_added: botAdded, channel_id: cid, channel_name: name, invite_link: link });
      toast("✅ Channel added"); setName(""); setLink(""); setCid("");
    } catch (e: any) { toast(e?.message || "Failed"); }
    load();
  }
  async function del(id: number) {
    try { await api("/api/admin/channels/delete", { id }); toast("🗑 Removed"); }
    catch (e: any) { toast(e?.message || "Failed"); }
    load();
  }

  return (
    <>
      {channels.map((c) => (
        <div className="row" key={c.id}>
          <div className="tic">📣</div>
          <div className="grow"><div className="t">{c.channel_name}</div>
            <div className="s">{c.bot_added ? "Real admin (membership checked)" : "No-admin (fake join)"} · {c.invite_link}</div></div>
          <button className="btn sm danger" onClick={() => del(c.id)}>🗑</button>
        </div>
      ))}
      <div className="card">
        <label className="f">Channel name</label>
        <input value={name} onChange={(e) => setName(e.target.value)} />
        <label className="f">Invite link</label>
        <input value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://t.me/…" />
        <label className="f">Channel ID (for real-admin channels)</label>
        <input value={cid} onChange={(e) => setCid(e.target.value)} placeholder="-100…" />
        <label className="f">Mode</label>
        <select value={botAdded} onChange={(e) => setBotAdded(Number(e.target.value))}>
          <option value={1}>🔴 Real admin — bot is admin, membership verified</option>
          <option value={0}>🟡 No admin — fake join</option>
        </select>
        <button className="btn" onClick={add}>➕ Add channel</button>
      </div>
    </>
  );
}

function AdminAds() {
  const { toast } = useApp();
  const [analytics, setAnalytics] = useState<any>(null);
  const [verify, setVerify] = useState<any>(null);
  useEffect(() => {
    api("/api/admin/ads/analytics").then(setAnalytics).catch(() => {});
  }, []);
  async function check() {
    try { setVerify(await api("/api/admin/ads/verify")); }
    catch (e: any) { toast(e?.message || "Check failed"); }
  }
  const row = (ok: boolean, label: string) => (
    <div style={{ display: "flex", gap: 8, padding: "4px 0", fontSize: 13 }}><span>{ok ? "✅" : "❌"}</span><span>{label}</span></div>
  );
  return (
    <>
      {analytics && (
        <div className="card">
          <div style={{ fontWeight: 800, marginBottom: 8 }}>📊 Today's ad analytics</div>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, color: "var(--muted)", margin: 0 }}>
            {JSON.stringify(analytics, null, 1).slice(0, 900)}
          </pre>
        </div>
      )}
      <button className="btn" onClick={check}>🔍 Check Connection</button>
      {verify && (
        <div className="card" style={{ marginTop: 12 }}>
          {row(verify.ads_enabled, "Ads enabled in settings")}
          {row(verify.block_id_configured, "Block ID configured (rewarded video)")}
          {row(verify.direct_link_configured, "Direct Link URL configured")}
          {row(verify.direct_link_valid_url, "Direct Link URL looks valid")}
          {row(verify.monetag_configured, "Monetag zone/URL configured (fallback)")}
          {row(verify.tads_configured, "Tads widget configured")}
          <div style={{ marginTop: 8, fontWeight: 700, fontSize: 13 }}>
            {verify.video_ready ? "✅ Rewarded video is live" : "⚠️ Rewarded video not fully configured"}<br />
            {verify.direct_link_ready ? "✅ Direct Link is live" : "⚠️ Direct Link not fully configured"}<br />
            {verify.monetag_ready ? "✅ Monetag fallback is live" : "⚠️ Monetag fallback not configured (optional)"}
          </div>
          <div className="s" style={{ color: "var(--muted)", fontSize: 12, marginTop: 8 }}>
            SDK loading itself is checked in your own WebView by the ad flow — the server can't see it.
          </div>
        </div>
      )}
    </>
  );
}

/** Settings: fetches the full settings object and submits it back whole
 *  (the update endpoint's typed model fills the rest), so no field can
 *  be silently reset by this form. */
function AdminSettings() {
  const { toast } = useApp();
  const [s, setS] = useState<any>(null);
  const load = useCallback(async () => {
    try { setS(await api("/api/admin/settings")); }
    catch (e: any) { toast(e?.message || "Load failed"); }
  }, [toast]);
  useEffect(() => { load(); }, [load]);
  if (!s) return <Shimmer rows={3} />;

  const num = (key: string, label: string, step = "any") => (
    <>
      <label className="f">{label}</label>
      <input type="number" step={step} value={s[key] ?? ""} onChange={(e) => setS({ ...s, [key]: Number(e.target.value) })} />
    </>
  );
  const txt = (key: string, label: string) => (
    <>
      <label className="f">{label}</label>
      <input value={s[key] ?? ""} onChange={(e) => setS({ ...s, [key]: e.target.value })} />
    </>
  );
  const bool = (key: string, label: string) => (
    <>
      <label className="f">{label}</label>
      <select value={s[key] ? "1" : "0"} onChange={(e) => setS({ ...s, [key]: e.target.value === "1" })}>
        <option value="1">Enabled</option>
        <option value="0">Disabled</option>
      </select>
    </>
  );

  async function save() {
    try {
      const { adsgram_s2s_secret, tads_webhook_secret, ...rest } = s;
      await api("/api/admin/settings/update", rest);
      toast("✅ Settings saved");
    } catch (e: any) { toast(e?.message || "Save failed"); }
  }

  return (
    <div className="card">
      {num("reward_per_referral", "Reward per referral (Birr)")}
      {num("min_withdrawal", "Minimum withdrawal (Birr)")}
      {bool("withdraw_telebirr_enabled", "Telebirr withdrawals")}
      {bool("withdraw_binance_enabled", "Binance Pay withdrawals")}
      {num("min_ads_for_withdrawal", "Min ads before withdrawal", "1")}
      {num("usdt_rate", "USDT rate (Birr)")}
      {txt("support_username", "Support username")}
      {bool("ads_enabled", "Ads master switch")}
      {bool("ads_maintenance", "Watch Ads under maintenance")}
      {txt("adsgram_block_id", "AdsGram block ID")}
      {num("ad_reward_amount", "Ad reward (Birr)")}
      {num("ad_daily_limit", "Ad daily limit", "1")}
      {num("ad_cooldown_seconds", "Ad cooldown (seconds)", "1")}
      {txt("adsgram_direct_link", "Direct Link URL")}
      {num("direct_link_reward_amount", "Direct Link reward (Birr)")}
      {num("direct_link_daily_limit", "Direct Link daily limit", "1")}
      {num("direct_link_wait_seconds", "Direct Link wait (seconds)", "1")}
      {num("direct_link_cooldown_seconds", "Direct Link cooldown (seconds)", "1")}
      {txt("monetag_sdk_url", "Monetag SDK URL")}
      {txt("monetag_zone_id", "Monetag zone ID")}
      {bool("tads_enabled", "Tads second ad")}
      {txt("tads_widget_id", "Tads widget ID")}
      {bool("user_task_creation_enabled", "User task creation")}
      {bool("daily_checkin_enabled", "Daily check-in")}
      {bool("daily_checkin_require_ad", "Check-in requires ad view")}
      {num("daily_checkin_reward", "Daily check-in reward (Birr)")}
      {s.adsgram_s2s_secret && (
        <>
          <label className="f">AdsGram S2S secret (read-only — for the AdsGram reward URL)</label>
          <input value={s.adsgram_s2s_secret} readOnly />
        </>
      )}
      <button className="btn" onClick={save}>💾 Save settings</button>
      <div className="s" style={{ color: "var(--muted)", fontSize: 12, marginTop: 8 }}>
        Advanced fields not shown here (click-reminder texts, referral-skip tuning, batch
        cooldowns, ticker) keep their current values — saving never resets them.
      </div>
    </div>
  );
}

function AdminBroadcast() {
  const { toast } = useApp();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  async function send() {
    if (!text.trim()) return toast("Enter a message");
    if (!window.confirm("Send this broadcast to ALL users?")) return;
    setBusy(true);
    try {
      const r = await api<{ sent: number; failed: number }>("/api/admin/broadcast", { text });
      toast(`📢 Sent: ${r.sent ?? 0}, failed: ${r.failed ?? 0}`);
      setText("");
    } catch (e: any) {
      toast(e?.message === "broadcast_already_in_progress" ? "⏳ A broadcast is already running" : e?.message || "Failed");
    }
    setBusy(false);
  }
  return (
    <div className="card">
      <label className="f">Message to all users</label>
      <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} placeholder="Type your announcement…" />
      <button className="btn gold" disabled={busy} onClick={send}>{busy ? "Sending…" : "📢 Send Broadcast"}</button>
    </div>
  );
}
