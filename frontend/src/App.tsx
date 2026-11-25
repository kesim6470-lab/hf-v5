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

  useEffect(() => { refreshMe(); }, [refreshMe]);

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

  return (
    <Ctx.Provider value={{ me, refreshMe, toast, go }}>
      <header>
        <div className="avatar">{(user?.first_name || me?.user_id?.toString() || "A").charAt(0).toUpperCase()}</div>
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
              🚀 Verify Now / አረጋግጥ
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
              🔄 Retry / እንደገና
            </button>
          </div>
        ) : gateError === "banned" ? (
          <div className="card" style={{ marginTop: 18 }}>
            <div style={{ fontWeight: 800, fontSize: 17, marginBottom: 8 }}>🚫 Account banned</div>
            <div style={{ color: "var(--muted)", fontSize: 14, lineHeight: 1.55 }}>
              This account has been blacklisted. Contact support if you believe this is a mistake.
            </div>
          </div>
        ) : (
          <>
            {tab === "home" && <Home />}
            {tab === "tasks" && <Tasks />}
            {tab === "invite" && <Invite />}
            {tab === "withdraw" && <Withdraw />}
            {tab === "admin" && me?.is_admin && <Admin />}
          </>
        )}
      </main>

      <nav>
        {tabs.filter((t) => t.show).map((t) => (
          <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => go(t.id)}>
            <span className="ni">{t.icon}</span>{t.label}
          </button>
        ))}
      </nav>

      <div className={`toast ${toastMsg ? "show" : ""}`}>{toastMsg}</div>
    </Ctx.Provider>
  );
}
