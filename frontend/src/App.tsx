import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { api, API_BASE, initData, tg, type Me } from "./api";
import Home from "./screens/Home";
import Tasks from "./screens/Tasks";
import Invite from "./screens/Invite";
import Top from "./screens/Top";
import Withdraw from "./screens/Withdraw";
import Admin from "./screens/Admin";

export type Tab = "home" | "tasks" | "invite" | "top" | "withdraw" | "admin";

interface AppCtx {
  me: Me | null;
  refreshMe: () => Promise<Me | null>;
  toast: (msg: string) => void;
  go: (tab: Tab) => void;
}
const Ctx = createContext<AppCtx>({ me: null, refreshMe: async () => null, toast: () => {}, go: () => {} });
export const useApp = () => useContext(Ctx);

export default function App() {
  const [tab, setTab] = useState<Tab>("home");
  const [me, setMe] = useState<Me | null>(null);
  const [gateError, setGateError] = useState<string | null>(null);
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try { return (localStorage.getItem("hf-theme") as "dark" | "light") || "dark"; } catch { return "dark"; }
  });
  const [booted, setBooted] = useState(false);
  const [forceUnjoined, setForceUnjoined] = useState<any[] | null>(null);
  const [checkingGate, setCheckingGate] = useState(false);
  const [toastMsg, setToastMsg] = useState("");
  const toastTimer = useRef<number | undefined>(undefined);

  const toast = useCallback((msg: string) => {
    setToastMsg(msg);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToastMsg(""), 2600);
  }, []);

  const refreshMe = useCallback(async () => {
    try {
      const m = await api<Me>("/api/me");
      setMe(m);
      setGateError(null);
      return m;
    } catch (e: any) {
      // _authenticate's contract: 403 not_verified / banned, 401 invalid.
      if (e?.status === 403 && e?.message === "not_verified") setGateError("not_verified");
      else if (e?.status === 403 && e?.message === "banned") setGateError("banned");
      else setGateError(initData ? "load_failed" : "not_telegram");
      return null;
    }
  }, []);

  const fetchGate = useCallback(async () => {
    try {
      const g: any = await api("/api/gate/status");
      setForceUnjoined(g?.force_unjoined || []);
      return g?.force_unjoined || [];
    } catch { setForceUnjoined(null); return null; }
  }, []);

  // Boot: /api/me and the force-join gate load IN PARALLEL, so the gate
  // (when it applies) is already known at first paint — no dashboard
  // flash followed by a banner popping in.
  useEffect(() => {
    Promise.all([refreshMe(), fetchGate()]).finally(() => setBooted(true));
  }, [refreshMe, fetchGate]);

  // Re-check the gate whenever the app returns to the foreground —
  // a user who LEAVES a required channel while using the app must be
  // gated again (owner order 2026-11-26), not only users who were
  // already gated at boot.
  useEffect(() => {
    const onVis = () => { if (document.visibilityState === "visible") fetchGate(); };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [fetchGate]);

  // And re-check periodically while the app stays open. Owner order
  // 2026-11-26: every 5 minutes — the backend also caches a passing
  // result per user for 5 minutes, so Telegram is never hammered with
  // repeated membership lookups; a channel-leaver is still caught and
  // forced to re-join within one cycle.
  useEffect(() => {
    const t = setInterval(() => { fetchGate(); }, 300_000);
    return () => clearInterval(t);
  }, [fetchGate]);

  async function checkGateNow() {
    setCheckingGate(true);
    const left = await fetchGate();
    setCheckingGate(false);
    if (left && left.length > 0) toast("❌ Still missing — join all channels first");
    else if (left) toast("✅ All joined — welcome in!");
  }

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("hf-theme", theme); } catch { /* WebView */ }
  }, [theme]);

  const go = useCallback((t: Tab) => { setTab(t); window.scrollTo(0, 0); }, []);

  // Sliding nav pill (MyAge-style animation, user 2026-11-26): one pill
  // element glides to whichever tab is active with a springy ease.
  const navRef = useRef<HTMLElement | null>(null);
  const [pill, setPill] = useState({ x: 0, w: 0, ready: false });

  const user = tg?.initDataUnsafe?.user;
  // Bottom-nav icons: flat filled glyphs (MyAge-style, user screenshot
  // 2026-11-26) tinted by currentColor — blue on the active tab, plain on the
  // rest. Emoji cannot be tinted, so these are inline SVGs.
  const ni = (d: string) => (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d={d} /></svg>
  );
  const tabs: { id: Tab; icon: ReactNode; label: string; show: boolean }[] = [
    { id: "home", icon: ni("M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"), label: "Home", show: true },
    { id: "tasks", icon: ni("M19 3h-4.18C14.4 1.84 13.3 1 12 1c-1.3 0-2.4.84-2.82 2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2zm-7 0a1 1 0 1 1 0 2 1 1 0 0 1 0-2zm-2 14l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z"), label: "Tasks", show: true },
    { id: "invite", icon: ni("M16 11c1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3 1.34 3 3 3zm-8 0c1.66 0 3-1.34 3-3S9.66 5 8 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"), label: "Invite", show: true },
    { id: "top", icon: ni("M19 5h-2V3H7v2H5a2 2 0 0 0-2 2v1a5 5 0 0 0 5 5 5 5 0 0 0 3 3.58V19H7v2h10v-2h-4v-3.42A5 5 0 0 0 16 13a5 5 0 0 0 5-5V7a2 2 0 0 0-2-2zM5 8V7h2v3.82C5.84 10.44 5 9.3 5 8zm14 0c0 1.3-.84 2.44-2 2.82V7h2v1z"), label: "Top", show: true },
    { id: "withdraw", icon: ni("M21 7.28V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-2.28A2 2 0 0 0 22 15V9a2 2 0 0 0-1-1.72zM5 5h14v2H5a2 2 0 0 1 0-2zm13 12a2 2 0 1 1 0-4 2 2 0 0 1 0 4z"), label: "Withdraw", show: true },
    { id: "admin", icon: ni("M19.14 12.94c.04-.3.06-.61.06-.94s-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6a3.6 3.6 0 1 1 0-7.2 3.6 3.6 0 0 1 0 7.2z"), label: "Admin", show: !!me?.is_admin },
  ];
  const visibleTabs = tabs.filter((t) => t.show);

  useLayoutEffect(() => {
    const measure = () => {
      const nav = navRef.current;
      if (!nav) return;
      const btn = nav.querySelector<HTMLElement>(`button[data-tab="${tab}"]`);
      if (btn) setPill({ x: btn.offsetLeft + 3, w: btn.offsetWidth - 6, ready: true });
    };
    measure();
    const t1 = setTimeout(measure, 120);
    const t2 = setTimeout(measure, 500);
    window.addEventListener("resize", measure);
    return () => { clearTimeout(t1); clearTimeout(t2); window.removeEventListener("resize", measure); };
  }, [tab, visibleTabs.length]);

  if (!booted) {
    return (
      <div className="splash">
        <img className="splash-logo" src="/logo.jpg" alt="Anon Earn" />
        <div className="splash-name">Anon Earn</div>
        <div className="splash-bar"><div className="splash-fill" /></div>
        <div className="splash-sub">Loading your account…</div>
      </div>
    );
  }

  return (
    <Ctx.Provider value={{ me, refreshMe, toast, go }}>
      <header>
        <div className="avatar">{user?.photo_url ? <img src={user.photo_url} alt="" /> : (user?.first_name || me?.user_id?.toString() || "A").charAt(0).toUpperCase()}</div>
        <div>
          <div className="uname">{user?.first_name || "Guest"}</div>
          <div className="usub">Anon Earn</div>
        </div>
        <div className="spacer" />
        <button className="theme-btn" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
          {theme === "dark" ? "☀️ Light" : "🌙 Dark"}
        </button>
      </header>

      <main>
        {gateError === "not_verified" ? (
          <div className="card" style={{ marginTop: 18 }}>
            <div style={{ fontWeight: 800, fontSize: 17, marginBottom: 8 }}>🛡️ Verification needed</div>
            <div style={{ color: "var(--muted)", fontSize: 14, lineHeight: 1.55 }}>
              This account hasn't completed the one-time security check yet.
              Tap the button below — it takes a few seconds — then your account opens.
            </div>
            <button
              className="btn"
              style={{ marginTop: 14, width: "100%" }}
              onClick={() => { if (user?.id) location.href = `${API_BASE}/verify?uid=${user.id}&ref=0&msg_id=0`; }}
            >
              🚀 Verify Now
            </button>
          </div>
        ) : gateError === "not_telegram" ? (
          <div className="card" style={{ marginTop: 18 }}>
            <div style={{ fontWeight: 800, fontSize: 17, marginBottom: 8 }}>📲 Open in Telegram</div>
            <div style={{ color: "var(--muted)", fontSize: 14, lineHeight: 1.55 }}>
              Anon Earn only works inside Telegram. Open <b>@AnonEarnAppBot</b> and tap <b>Open Mini App</b>.
            </div>
            <button className="btn" style={{ marginTop: 14, width: "100%" }} onClick={() => tg?.openTelegramLink?.("https://t.me/AnonEarnAppBot")}>
              Open @AnonEarnAppBot
            </button>
          </div>
        ) : gateError === "load_failed" ? (
          <div className="card" style={{ marginTop: 18 }}>
            <div style={{ fontWeight: 800, fontSize: 17, marginBottom: 8 }}>⚠️ Couldn't load</div>
            <div style={{ color: "var(--muted)", fontSize: 14, lineHeight: 1.55 }}>
              We couldn't reach the server. Check your connection and try again.
            </div>
            <button className="btn" style={{ marginTop: 14, width: "100%" }} onClick={() => refreshMe()}>
              🔄 Retry
            </button>
          </div>
        ) : gateError === "banned" ? (
          <div className="card" style={{ marginTop: 18 }}>
            <div style={{ fontWeight: 800, fontSize: 17, marginBottom: 8 }}>🚫 Account banned</div>
            <div style={{ color: "var(--muted)", fontSize: 14, lineHeight: 1.55 }}>
              This account has been blacklisted. Contact support if you believe this is a mistake.
            </div>
          </div>
        ) : forceUnjoined && forceUnjoined.length > 0 ? (
          <div className="gate-wrap app-enter">
            <div className="gate-card">
              <img className="gate-logo" src="/logo.jpg" alt="Anon Earn" />
              <div className="gate-title">Join to continue</div>
              <div className="gate-sub">
                To use Anon Earn, join the channels below — it only takes a few seconds.
                Come back here and we will check automatically.
              </div>
              <div className="gate-count">📣 {forceUnjoined.length} {forceUnjoined.length === 1 ? "channel" : "channels"} to join</div>
              <div className="gate-list">
                {forceUnjoined.map((c: any) => (
                  <div className="gate-row" key={c.id}>
                    <div className="gate-ava">{(c.channel_name || "C").trim().charAt(0).toUpperCase()}</div>
                    <div className="gate-meta">
                      <div className="gate-name">{c.channel_name}</div>
                      <div className="gate-hint">Membership required</div>
                    </div>
                    <button className="gate-join" onClick={() => {
                      const link = c.invite_link || "";
                      if (link.startsWith("https://t.me/") && tg?.openTelegramLink) tg.openTelegramLink(link);
                      else if (tg?.openLink) tg.openLink(link);
                      else window.open(link, "_blank");
                    }}>Join</button>
                  </div>
                ))}
              </div>
              <button className="btn gate-check" disabled={checkingGate} onClick={checkGateNow}>
                {checkingGate ? "Checking…" : "✅ I Joined — Check"}
              </button>
              <div className="gate-note">Stay joined to keep earning 🚀</div>
            </div>
          </div>
        ) : (
          <div className="app-enter" key={tab}>
            {tab === "home" && <Home />}
            {tab === "tasks" && <Tasks />}
            {tab === "invite" && <Invite />}
            {tab === "top" && <Top />}
            {tab === "withdraw" && <Withdraw />}
            {tab === "admin" && me?.is_admin && <Admin />}
          </div>
        )}
      </main>

      {!(forceUnjoined && forceUnjoined.length > 0 && !gateError) && (
      <nav ref={navRef}>
        <span className="nav-pill" style={{ transform: `translateX(${pill.x}px)`, width: pill.w, opacity: pill.ready ? 1 : 0 }} />
        {visibleTabs.map((t) => (
          <button key={t.id} data-tab={t.id} className={tab === t.id ? "active" : ""} onClick={() => go(t.id)}>
            <span className="ni">{t.icon}</span>{t.label}
          </button>
        ))}
      </nav>
      )}

      <div className={`toast ${toastMsg ? "show" : ""}`}>{toastMsg}</div>
    </Ctx.Provider>
  );
}
