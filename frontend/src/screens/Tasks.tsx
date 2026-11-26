import { useCallback, useEffect, useState } from "react";
import { api, fmt, tg, type Task } from "../api";
import { useApp } from "../App";
import { Spinner } from "../components/ui";

const ICONS = ["📣", "👥", "🐦", "🎬", "⭐", "🎁"];

/** Tasks: join → wait → check → claim for auto tasks; screenshot upload
 *  for manual tasks; My Tasks (escrow status + cancel/refund); and the
 *  Create-Task wizard (verify bot-admin → details → escrow), gated by
 *  the backend's user_task_config. */
export default function Tasks() {
  const { toast, refreshMe } = useApp();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [cfg, setCfg] = useState<any>(null);
  const [view, setView] = useState<"all" | "mine" | "create">("all");
  const [mine, setMine] = useState<Task[]>([]);
  const [busy, setBusy] = useState<number | null>(null);
  // Loading flags: without them the empty-state text ("No tasks right
  // now") flashes on screen while the first fetch is still in flight
  // (owner report 2026-11-26). Empty text shows only after a load
  // has actually completed with zero rows.
  const [loading, setLoading] = useState(true);
  const [mineLoading, setMineLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const r = await api<{ tasks: Task[]; user_task_config: any }>("/api/tasks");
      setTasks(r.tasks || []);
      setCfg(r.user_task_config);
    } catch (e: any) { toast(e?.message || "Couldn't load tasks"); }
    finally { setLoading(false); }
  }, [toast]);

  const loadMine = useCallback(async () => {
    try {
      const r = await api<{ tasks: Task[] }>("/api/tasks/mine");
      setMine(r.tasks || []);
    } catch { /* ignore */ }
    finally { setMineLoading(false); }
  }, []);

  useEffect(() => { load(); loadMine(); }, [load, loadMine]);

  async function join(t: Task) {
    setBusy(t.id);
    try {
      await api("/api/tasks/join", { task_id: t.id });
      if (t.invite_link) {
        // t.me links MUST go through openTelegramLink — openLink hands
        // them to an external browser and the button looks dead inside
        // Telegram (owner report 2026-11-26). Same rule as the App gate.
        let link = t.invite_link.trim();
        if (link.startsWith("@")) link = "https://t.me/" + link.slice(1);
        else if (link.startsWith("t.me/")) link = "https://" + link;
        if (link.startsWith("https://t.me/") && tg?.openTelegramLink) tg.openTelegramLink(link);
        else if (tg?.openLink) tg.openLink(link);
        else window.open(link, "_blank");
      }
      toast("⏳ Joined — wait a few seconds, then Check");
      setTimeout(load, 6000);
    } catch (e: any) { toast(e?.message || "Join failed"); }
    setBusy(null);
  }

  async function check(t: Task) {
    setBusy(t.id);
    try {
      await api("/api/tasks/check", { task_id: t.id });
      toast("✅ Verified — now Claim your reward");
      load();
    } catch (e: any) {
      if (String(e?.message).startsWith("wait:")) toast(`⏳ Wait ${e.message.split(":")[1]}s more`);
      else if (e?.message === "not_member") toast("❌ You haven't joined yet");
      else if (e?.message === "check_failed") toast("⚠️ Couldn't verify membership — try again in a moment");
      else toast(e?.message || "Check failed");
    }
    setBusy(null);
  }

  async function claim(t: Task) {
    setBusy(t.id);
    try {
      const r = await api<{ reward: number }>("/api/tasks/claim", { task_id: t.id });
      toast(`+${fmt(r.reward)} Birr 🎉`);
      tg?.HapticFeedback?.notificationOccurred("success");
      refreshMe(); load();
    } catch (e: any) {
      const msgs: Record<string, string> = {
        not_checked: "Tap Check first", not_claimable: "Not claimable",
        not_member: "❌ You haven't joined yet", check_failed: "⚠️ Couldn't verify membership — try again",
        task_unavailable: "This task is no longer available", task_sold_out: "This task just sold out",
      };
      toast(msgs[e?.message] || e?.message || "Claim failed");
      load();
    }
    setBusy(null);
  }

  async function submitManual(t: Task, files: FileList | null) {
    if (!files || files.length === 0) return;
    setBusy(t.id);
    try {
      const shots: string[] = [];
      for (const f of Array.from(files).slice(0, t.screenshot_count || 1)) {
        shots.push(await new Promise<string>((resolve, reject) => {
          const rd = new FileReader();
          rd.onload = () => resolve(String(rd.result));
          rd.onerror = reject;
          rd.readAsDataURL(f);
        }));
      }
      await api("/api/tasks/manual/submit", { task_id: t.id, screenshots: shots });
      toast("📸 Submitted — an admin will review it");
      load();
    } catch (e: any) { toast(e?.message || "Submit failed"); }
    setBusy(null);
  }

  async function cancelMine(t: Task) {
    setBusy(t.id);
    try {
      const r = await api<{ refunded: number }>("/api/tasks/cancel", { task_id: t.id });
      toast(`↩️ Cancelled — ${fmt(r.refunded)} Birr refunded`);
      refreshMe(); loadMine(); load();
    } catch (e: any) { toast(e?.message || "Cancel failed"); }
    setBusy(null);
  }

  return (
    <section className="screen">
      <div className="seg">
        <button className={view === "all" ? "active" : ""} onClick={() => setView("all")}>🎯 Tasks</button>
        <button className={view === "mine" ? "active" : ""} onClick={() => setView("mine")}>📋 My Tasks</button>
        {cfg?.enabled && (
          <button className={view === "create" ? "active" : ""} onClick={() => setView("create")}>➕ Create</button>
        )}
      </div>

      {view === "all" && (
        loading && tasks.length === 0 ? <div className="empty"><Spinner /> Loading tasks…</div> :
        tasks.length === 0 ? <div className="empty">No tasks right now</div> :
        tasks.map((t, i) => (
          <div className="row" key={t.id}>
            <div className="tic">{ICONS[i % ICONS.length]}</div>
            <div className="grow">
              <div className="t">{t.title}</div>
              <div className="s">
                {t.task_type === "manual" ? (t.instructions || "Screenshot proof task")
                  : t.status === "waiting" ? `⏳ Wait ${t.wait_left ?? 0}s after joining`
                  : t.status === "review" ? "📸 Under review"
                  : t.status === "rejected" ? "❌ Submission rejected"
                  : t.status === "completed" ? "✔ Completed" : "Join, then claim"}
              </div>
            </div>
            <div className="reward">+{fmt(t.reward)}</div>
            {t.task_type === "manual" ? (
              t.status === "none" ? (
                <label className="btn sm" style={{ cursor: "pointer" }}>
                  {busy === t.id ? "…" : "Upload"}
                  <input type="file" accept="image/*" multiple hidden onChange={(e) => submitManual(t, e.target.files)} />
                </label>
              ) : <span className={`pill p-${t.status === "completed" ? "approved" : t.status === "rejected" ? "rejected" : "review"}`}>{t.status}</span>
            ) : t.status === "completed" ? (
              <button className="btn sm" disabled>✔ Done</button>
            ) : t.status === "claimable" ? (
              <>
                <button className="btn sm ghost" disabled={busy === t.id} onClick={() => check(t)}>{busy === t.id ? <Spinner /> : null}Check</button>
                <button className="btn sm" disabled={busy === t.id} onClick={() => claim(t)}>{busy === t.id ? <Spinner /> : null}Claim</button>
              </>
            ) : (
              <button className="btn sm" disabled={busy === t.id} onClick={() => join(t)}>
                {t.status === "waiting" ? `⏳ ${t.wait_left ?? 0}s` : "Join"}
              </button>
            )}
          </div>
        ))
      )}

      {view === "mine" && (
        mineLoading && mine.length === 0 ? <div className="empty"><Spinner /> Loading tasks…</div> :
        mine.length === 0 ? <div className="empty">You haven't created any tasks yet</div> :
        mine.map((t) => (
          <div className="row" key={t.id}>
            <div className="grow">
              <div className="t">{t.title}</div>
              <div className="s">
                {t.review_status} · {t.slots_used ?? 0}/{t.budget_slots ?? 0} slots · {fmt(t.reward)} Birr each
              </div>
            </div>
            {(t.review_status === "pending" || t.review_status === "approved") && (
              <button className="btn sm danger" disabled={busy === t.id} onClick={() => cancelMine(t)}>Cancel</button>
            )}
          </div>
        ))
      )}

      {view === "create" && cfg && <CreateWizard cfg={cfg} onDone={() => { setView("mine"); loadMine(); load(); }} />}
    </section>
  );
}

function CreateWizard({ cfg, onDone }: { cfg: any; onDone: () => void }) {
  const { toast, refreshMe } = useApp();
  const [step, setStep] = useState(1);
  const [channelId, setChannelId] = useState("");
  const [verified, setVerified] = useState<boolean | null>(null);
  const [title, setTitle] = useState("");
  const [link, setLink] = useState("");
  const [reward, setReward] = useState(String(cfg.min_reward ?? 1));
  const [slots, setSlots] = useState(String(cfg.min_slots ?? 5));
  const [busy, setBusy] = useState(false);

  async function verify() {
    setBusy(true);
    try {
      const r = await api<{ is_admin: boolean; title: string | null }>("/api/tasks/verify_admin", { channel_id: channelId });
      setVerified(r.is_admin);
      if (r.title && !title) setTitle(r.title);
      if (!channelId) setVerified(null);
      setStep(2);
    } catch (e: any) { toast(e?.message || "Verify failed"); }
    setBusy(false);
  }

  async function create() {
    setBusy(true);
    try {
      const r = await api<{ total_cost: number }>("/api/tasks/create", {
        title, invite_link: link, reward: Number(reward), budget_slots: Number(slots),
        channel_id: verified ? channelId : "",
      });
      toast(`✅ Task submitted for review — ${fmt(r.total_cost)} Birr escrowed`);
      refreshMe(); onDone();
    } catch (e: any) {
      const msgs: Record<string, string> = {
        insufficient_balance: "❌ Insufficient balance for that budget",
        reward_out_of_range: `Reward must be ${cfg.min_reward}–${cfg.max_reward} Birr`,
        slots_out_of_range: `Slots must be ${cfg.min_slots}–${cfg.max_slots}`,
        invalid_link: "Link must be a t.me link", missing_fields: "Fill in title and link",
      };
      toast(msgs[e?.message] || e?.message || "Create failed");
    }
    setBusy(false);
  }

  if (step === 1) {
    return (
      <div className="card">
        <div style={{ fontWeight: 800, marginBottom: 6 }}>Step 1 — Your channel (optional)</div>
        <div className="s" style={{ color: "var(--muted)", fontSize: 13, lineHeight: 1.5 }}>
          Add <b>@{cfg.bot_username}</b> as an admin in your channel, then paste its @username or ID.
          Verified channels get real membership checks; skip to create a wait-timer task instead.
        </div>
        <div style={{ height: 10 }} />
        <input placeholder="@yourchannel or -100…" value={channelId} onChange={(e) => setChannelId(e.target.value)} />
        <button className="btn" disabled={busy} onClick={verify}>{busy ? <><Spinner />Checking…</> : "Check & Continue"}</button>
        <button className="btn ghost" onClick={() => { setVerified(false); setStep(2); }}>Skip — no channel check</button>
      </div>
    );
  }
  const total = (Number(reward) || 0) * (Number(slots) || 0);
  return (
    <div className="card">
      <div style={{ fontWeight: 800, marginBottom: 6 }}>
        Step 2 — Task details {verified ? "✅ (membership will be verified)" : "(wait-timer task)"}
      </div>
      <label className="f">Title</label>
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Join my channel" />
      <label className="f">Invite link</label>
      <input value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://t.me/yourchannel" />
      <label className="f">Reward per person (Birr) — {cfg.min_reward}…{cfg.max_reward}</label>
      <input type="number" value={reward} onChange={(e) => setReward(e.target.value)} />
      <label className="f">People (slots) — {cfg.min_slots}…{cfg.max_slots}</label>
      <input type="number" value={slots} onChange={(e) => setSlots(e.target.value)} />
      <div className="s" style={{ color: "var(--muted)", fontSize: 13 }}>Total escrow from your balance: <b>{fmt(total)} Birr</b> (unused slots are refunded on cancel)</div>
      <button className="btn" disabled={busy} onClick={create}>{busy ? <><Spinner />Creating…</> : "Create Task"}</button>
      <button className="btn ghost" onClick={() => setStep(1)}>← Back</button>
    </div>
  );
}
