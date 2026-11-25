import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, API_BASE, initData, tg, type Me } from "./api";
import Home from "./screens/Home";
import Tasks from "./screens/Tasks";
import Invite from "./screens/Invite";
import Withdraw from "./screens/Withdraw";
import Admin from "./screens/Admin";

export type Tab = "home" | "tasks" | "invite" | "withdraw" | "admin";

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

  // Coming back from a channel (after tapping Join) re-checks the gate
  // automatically — the moment every channel is joined, the app opens.
  useEffect(() => {
    const onVis = () => { if (document.visibilityState === "visible" && forceUnjoined && forceUnjoined.length > 0) fetchGate(); };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [fetchGate, forceUnjoined]);

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

  const user = tg?.initDataUnsafe?.user;
  const tabs: { id: Tab; icon: string; label: string; show: boolean }[] = [
    { id: "home", icon: "🏠", label: "Home", show: true },
    { id: "tasks", icon: "🎯", label: "Tasks", show: true },
    { id: "invite", icon: "🔗", label: "Invite", show: true },
    { id: "withdraw", icon: "💸", label: "Withdraw", show: true },
    { id: "admin", icon: "⚙️", label: "Admin", show: !!me?.is_admin },
  ];

  if (!booted) {
    return (
      <div className="splash">
        <img className="splash-logo" src="/logo.png" alt="Anon Earn" />
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
          <div className="card app-enter" style={{ marginTop: 18 }}>
            <div style={{ fontWeight: 800, fontSize: 17, marginBottom: 8 }}>🔒 Join to continue</div>
            <div style={{ color: "var(--muted)", fontSize: 14, lineHeight: 1.55 }}>
              You must join <b>all</b> channels below to use Anon Earn. Tap <b>Join</b> for each one,
              then come back — we re-check automatically, or tap the check button.
            </div>
            {forceUnjoined.map((c: any) => (
              <div className="row" key={c.id} style={{ marginTop: 12, marginBottom: 0 }}>
                <div className="tic">📣</div>
                <div className="grow"><div className="t">{c.channel_name}</div><div className="s">Membership required</div></div>
                <button className="btn sm" onClick={() => {
                  const link = c.invite_link || "";
                  if (link.startsWith("https://t.me/") && tg?.openTelegramLink) tg.openTelegramLink(link);
                  else if (tg?.openLink) tg.openLink(link);
                  else window.open(link, "_blank");
                }}>Join</button>
              </div>
            ))}
            <button className="btn" style={{ marginTop: 14, width: "100%" }} disabled={checkingGate} onClick={checkGateNow}>
              {checkingGate ? "Checking…" : "✅ I Joined — Check"}
            </button>
          </div>
        ) : (
          <div className="app-enter">
            {tab === "home" && <Home />}
            {tab === "tasks" && <Tasks />}
            {tab === "invite" && <Invite />}
            {tab === "withdraw" && <Withdraw />}
            {tab === "admin" && me?.is_admin && <Admin />}
          </div>
        )}
      </main>

      {!(forceUnjoined && forceUnjoined.length > 0 && !gateError) && (
      <nav>
        {tabs.filter((t) => t.show).map((t) => (
          <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => go(t.id)}>
            <span className="ni">{t.icon}</span>{t.label}
          </button>
        ))}
      </nav>
      )}

      <div className={`toast ${toastMsg ? "show" : ""}`}>{toastMsg}</div>
    </Ctx.Provider>
  );
}
