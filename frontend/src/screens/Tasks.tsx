import { useCallback, useEffect, useRef, useState } from "react";
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
  // PRO CHECK FLOW (owner order 2026-11-27: "the check is tedious —
  // make it pro"): membership verifies ITSELF when the user comes back
  // from the channel, the wait counts down live, and Claim is a single
  // tap (it chains the check invisibly when needed). `verified` holds
  // task ids whose membership this session has already proven.
  const [verified, setVerified] = useState<Set<number>>(new Set());
  // Tasks whose last Check failed (not a member): the button flips
  // back to "Join" (owner's flow: Join → Check → Claim, and a failed
  // Check returns the user to Join).
  const [rejoin, setRejoin] = useState<Set<number>>(new Set());
  const [, setTick] = useState(0);
  const loadedAtRef = useRef(0);
  const zeroReloadedRef = useRef<Set<number>>(new Set());

  const load = useCallback(async () => {
    try {
      const r = await api<{ tasks: Task[]; user_task_config: any }>("/api/tasks");
      setTasks(r.tasks || []);
      setCfg(r.user_task_config);
      loadedAtRef.current = Date.now();
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

  // t.me links MUST go through openTelegramLink — openLink hands them
  // to an external browser and the button looks dead inside Telegram
  // (owner report 2026-11-26). Same rule as the App gate.
  function openTaskLink(t: Task) {
    if (!t.invite_link) return;
    let link = t.invite_link.trim();
    if (link.startsWith("@")) link = "https://t.me/" + link.slice(1);
    else if (link.startsWith("t.me/")) link = "https://" + link;
    if (link.startsWith("https://t.me/") && tg?.openTelegramLink) tg.openTelegramLink(link);
    else if (tg?.openLink) tg.openLink(link);
    else window.open(link, "_blank");
  }

  async function join(t: Task) {
    setBusy(t.id);
    try {
      const r = await api<{ wait_seconds: number }>("/api/tasks/join", { task_id: t.id });
      // A fresh join tap clears any failed-check state, so when the
      // user comes back the button shows "Check" again.
      setRejoin((prev) => { const n = new Set(prev); n.delete(t.id); return n; });
      // Show the countdown IMMEDIATELY (owner report 2026-11-27: the
      // countdown never appeared — the list only reloaded long after
      // the 5s wait had already passed). Flip this row to "waiting"
      // with the server's wait_seconds right now; the tick effect
      // counts it down live and auto-checks at zero.
      setTasks((prev) => prev.map((x) => x.id === t.id ? { ...x, status: "waiting", wait_left: r.wait_seconds ?? 5 } : x));
      loadedAtRef.current = Date.now();
      zeroReloadedRef.current.delete(t.id);
      openTaskLink(t);
      toast("⏳ Joined — come back here, we verify automatically");
    } catch (e: any) { toast(e?.message || "Join failed"); }
    setBusy(null);
  }

  // Owner's button flow (2026-11-27): Join → Check → Claim. The
  // "Check" button verifies membership — if the user IS in the
  // channel it flips to "Claim"; if NOT, the button flips back to
  // "Join" and the channel re-opens so they can join properly.
  async function checkBtn(t: Task) {
    setBusy(t.id);
    const ok = await checkSilent(t);
    if (ok) {
      toast("✅ Verified — tap Claim");
    } else {
      toast("❌ You haven't joined yet — opening the channel");
      setRejoin((prev) => new Set(prev).add(t.id));
      openTaskLink(t);
    }
    setBusy(null);
  }

  // Silent membership proof — no toasts, returns true only when the
  // server confirms membership. Used by the auto-check on return and
  // chained inside Claim, so the user never taps a "Check" button.
  const checkSilent = useCallback(async (t: Task): Promise<boolean> => {
    try {
      await api("/api/tasks/check", { task_id: t.id });
      setVerified((prev) => new Set(prev).add(t.id));
      return true;
    } catch { return false; }
  }, []);

  // Auto-check: whenever the app returns to the foreground, prove every
  // joined-but-unverified task in parallel; announce once if any pass.
  useEffect(() => {
    const onVis = async () => {
      if (document.visibilityState !== "visible") return;
      const pending = tasks.filter((t) => t.task_type !== "manual" && t.status === "claimable" && !verified.has(t.id));
      if (pending.length === 0) return;
      const results = await Promise.all(pending.map((t) => checkSilent(t)));
      if (results.some(Boolean)) toast("✅ Verified — tap Claim for your reward");
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [tasks, verified, checkSilent, toast]);

  // Live wait countdown: tick once a second while any task is waiting,
  // so the seconds run down smoothly on screen; when one reaches zero,
  // reload once and auto-check it.
  useEffect(() => {
    if (!tasks.some((t) => t.status === "waiting")) return;
    const iv = setInterval(() => {
      setTick((x) => x + 1);
      const elapsed = Math.floor((Date.now() - loadedAtRef.current) / 1000);
      const done = tasks.filter((t) => t.status === "waiting" && (t.wait_left ?? 0) - elapsed <= 0 && !zeroReloadedRef.current.has(t.id));
      if (done.length > 0) {
        done.forEach((t) => zeroReloadedRef.current.add(t.id));
        load().then(() => { done.forEach((t) => { void checkSilent(t); }); });
      }
    }, 1000);
    return () => clearInterval(iv);
  }, [tasks, load, checkSilent]);

  async function claim(t: Task) {
    setBusy(t.id);
    try {
      // One-tap claim: if this session hasn't verified the task yet,
      // run the membership check invisibly first, then claim.
      if (!verified.has(t.id)) {
        const ok = await checkSilent(t);
        if (!ok) {
          try { await api("/api/tasks/check", { task_id: t.id }); }
          catch (e: any) {
            if (String(e?.message).startsWith("wait:")) toast(`⏳ Wait ${e.message.split(":")[1]}s more`);
            else if (e?.message === "not_member") toast("❌ You haven't joined yet");
            else if (e?.message === "check_failed") toast("⚠️ Couldn't verify membership — try again in a moment");
            else toast(e?.message || "Check failed");
            setBusy(null); return;
          }
        }
      }
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
                  : t.status === "waiting" ? `⏳ Wait ${Math.max(0, (t.wait_left ?? 0) - Math.floor((Date.now() - loadedAtRef.current) / 1000))}s after joining`
                  : t.status === "review" ? "📸 Under review"
                  : t.status === "rejected" ? "❌ Submission rejected"
                  : t.status === "completed" ? "✔ Completed"
                  : t.status === "claimable" ? (verified.has(t.id) ? "✔ Verified — claim your reward" : rejoin.has(t.id) ? "You haven't joined yet — tap Join" : "Joined? Tap Check to verify")
                  : "Join, then claim"}
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
              verified.has(t.id) ? (
                <button className="btn sm" disabled={busy === t.id} onClick={() => claim(t)}>{busy === t.id ? <Spinner /> : null}Claim</button>
              ) : rejoin.has(t.id) ? (
                <button className="btn sm" disabled={busy === t.id} onClick={() => join(t)}>{busy === t.id ? <Spinner /> : null}Join</button>
              ) : (
                <button className="btn sm" disabled={busy === t.id} onClick={() => checkBtn(t)}>{busy === t.id ? <Spinner /> : null}Check</button>
              )
            ) : (
              <button className="btn sm" disabled={busy === t.id || t.status === "waiting"} onClick={() => join(t)}>
                {t.status === "waiting" ? `⏳ ${Math.max(0, (t.wait_left ?? 0) - Math.floor((Date.now() - loadedAtRef.current) / 1000))}s` : "Join"}
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
