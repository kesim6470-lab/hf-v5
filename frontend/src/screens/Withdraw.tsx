import { useCallback, useEffect, useState } from "react";
import { api, fmt, type Withdrawal } from "../api";
import { useApp } from "../App";
import { Shimmer, Spinner } from "../components/ui";

/** Brand-style method icons (owner request 2026-11-26, matched to the
 *  real app icons the owner sent): Binance = gold diamond-of-squares on a
 *  dark rounded square; Telebirr = blue swirl mark on a white rounded
 *  square. */
export const BinanceIcon = () => (
  <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true">
    <rect x="1.5" y="1.5" width="29" height="29" rx="8" fill="#1E2329" />
    <g fill="#F0B90B">
      <rect x="13.25" y="13.25" width="5.5" height="5.5" transform="rotate(45 16 16)" />
      <rect x="13.7" y="7" width="4.6" height="4.6" transform="rotate(45 16 9.3)" />
      <rect x="13.7" y="20.4" width="4.6" height="4.6" transform="rotate(45 16 22.7)" />
      <rect x="7" y="13.7" width="4.6" height="4.6" transform="rotate(45 9.3 16)" />
      <rect x="20.4" y="13.7" width="4.6" height="4.6" transform="rotate(45 22.7 16)" />
    </g>
  </svg>
);
export const TelebirrIcon = () => (
  // The REAL telebirr glyph — Ethio telecom's official app-icon artwork
  // (from the Google Play listing), shipped as /telebirr.png. Hand-drawn
  // SVG approximations were rejected by the owner twice (2026-11-26), so
  // the genuine asset is used instead.
  <img src="/telebirr.png" alt="Telebirr" width={22} height={22}
    style={{ borderRadius: 6, background: "#fff", flex: "none" }} />
);

/** Withdraw (💸 — the user's naming, not "Wallet"): Telebirr or Binance
 *  Pay, min-withdrawal from /api/me, full history with status pills.
 *  Methods the admin disabled in Settings are hidden (and refused by the
 *  API), so e.g. disabling Binance leaves Telebirr only. */
export default function Withdraw() {
  const { me, refreshMe, toast } = useApp();
  const [method, setMethod] = useState<"telebirr" | "binance">("telebirr");
  const [amount, setAmount] = useState("");
  const [phone, setPhone] = useState("");
  const [fullName, setFullName] = useState("");
  const [binanceUid, setBinanceUid] = useState("");
  const [history, setHistory] = useState<Withdrawal[]>([]);
  const [busy, setBusy] = useState(false);

  const loadHistory = useCallback(async () => {
    try {
      const r = await api<{ withdrawals: Withdrawal[] }>("/api/withdraw/history");
      setHistory(r.withdrawals || []);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  if (!me) return <Shimmer rows={2} />;
  // Admin-disabled methods are hidden; if the selected one got disabled,
  // fall back to whichever method is still enabled.
  const wm = me.withdraw_methods ?? { telebirr: true, binance: true };
  const m: "telebirr" | "binance" = wm[method] ? method : wm.telebirr ? "telebirr" : "binance";
  const noneEnabled = !wm.telebirr && !wm.binance;
  const usdt = m === "binance" && Number(amount) > 0 ? Number(amount) / (me.usdt_rate || 155) : 0;

  async function submit() {
    const amt = Number(amount);
    if (!amt) return toast("Enter an amount");
    if (amt < me!.min_withdrawal) return toast(`Minimum withdrawal is ${fmt(me!.min_withdrawal)} Birr`);
    if (m === "telebirr" && (phone.trim().length < 9 || fullName.trim().length < 3))
      return toast("Enter a valid phone number and account name");
    if (m === "binance" && binanceUid.trim().length < 5)
      return toast("Enter a valid Binance UID");
    setBusy(true);
    try {
      const r = await api<{ ticket_id: number; process_time: string }>("/api/withdraw", {
        amount: amt, method: m,
        phone: phone.trim(), full_name: fullName.trim(), binance_uid: binanceUid.trim(),
      });
      toast(`📨 Submitted! Processing within ${r.process_time || me!.withdrawal_process_time}.`);
      setAmount("");
      refreshMe(); loadHistory();
    } catch (e: any) {
      const msgs: Record<string, string> = {
        insufficient_funds: "❌ Insufficient balance",
        banned: "🚫 Your account is banned — withdrawals are disabled",
        invalid_phone: "❌ Invalid phone number",
        invalid_full_name: "❌ Invalid account name",
        invalid_binance_uid: "❌ Invalid Binance UID",
        method_disabled: "❌ This withdrawal method is currently disabled",
      };
      toast(msgs[e?.message] || (String(e?.message).startsWith("min_withdrawal_is_")
        ? `Minimum withdrawal is ${e.message.split("_").pop()} Birr`
        : String(e?.message).startsWith("⚠️") ? e.message : "❌ Withdrawal failed"));
    }
    setBusy(false);
  }

  return (
    <section className="screen">
      <h3 className="sect">💸 Withdraw</h3>
      <div className="card">
        <div style={{ color: "var(--muted)", fontSize: 13, fontWeight: 600 }}>
          Balance: <b style={{ color: "var(--accent)" }}>{fmt(me.balance)} Birr</b> · Min {fmt(me.min_withdrawal)} Birr
        </div>
        <div className="seg">
          {wm.telebirr && <button className={m === "telebirr" ? "active" : ""} onClick={() => setMethod("telebirr")}><TelebirrIcon /> Telebirr</button>}
          {wm.binance && <button className={m === "binance" ? "active" : ""} onClick={() => setMethod("binance")}><BinanceIcon /> Binance Pay</button>}
        </div>
        {noneEnabled && <div className="empty">Withdrawals are temporarily unavailable</div>}
        <label className="f">Amount (Birr)</label>
        <input type="number" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder={`Min ${fmt(me.min_withdrawal)}`} min={me.min_withdrawal} />
        {m === "telebirr" ? (
          <>
            <label className="f">Telebirr phone number</label>
            <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="09…" inputMode="tel" />
            <label className="f">Account holder full name</label>
            <input value={fullName} onChange={(e) => setFullName(e.target.value)} placeholder="Full name" />
          </>
        ) : (
          <>
            <label className="f">Binance UID</label>
            <input value={binanceUid} onChange={(e) => setBinanceUid(e.target.value)} placeholder="Your Binance UID" />
            {usdt > 0 && <div style={{ color: "var(--muted)", fontSize: 13 }}>≈ {usdt.toFixed(2)} USDT at {fmt(me.usdt_rate)} Birr/USDT</div>}
          </>
        )}
        {!noneEnabled && <button className="btn" disabled={busy} onClick={submit}>{busy ? <><Spinner />Sending…</> : "💸 Submit withdrawal"}</button>}
      </div>

      <h3 className="sect">🧾 History</h3>
      {history.length === 0 ? <div className="empty">No withdrawals yet</div> :
        history.map((w) => (
          <div className="row" key={w.id}>
            <div className="grow">
              <div className="t">{fmt(w.amount)} Birr → {w.method === "binance" ? "Binance Pay" : "Telebirr"}</div>
              <div className="s">
                {w.method === "binance" ? `UID ${w.binance_uid || ""}` : `${w.full_name} · ${w.phone}`}
                {w.status === "rejected" && w.reason ? ` · ${w.reason}` : ""} · {w.created_at}
              </div>
            </div>
            <span className={`pill p-${w.status}`}>{w.status}</span>
          </div>
        ))}
    </section>
  );
}
