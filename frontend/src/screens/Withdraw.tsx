import { useCallback, useEffect, useState } from "react";
import { api, fmt, type Withdrawal } from "../api";
import { useApp } from "../App";
import { Shimmer, Spinner } from "../components/ui";

/** Withdraw (💸 — the user's naming, not "Wallet"): Telebirr or Binance
 *  Pay, min-withdrawal from /api/me, full history with status pills. */
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
  const usdt = method === "binance" && Number(amount) > 0 ? Number(amount) / (me.usdt_rate || 155) : 0;

  async function submit() {
    const amt = Number(amount);
    if (!amt) return toast("Enter an amount");
    if (amt < me!.min_withdrawal) return toast(`Minimum withdrawal is ${fmt(me!.min_withdrawal)} Birr`);
    if (method === "telebirr" && (phone.trim().length < 9 || fullName.trim().length < 3))
      return toast("Enter a valid phone number and account name");
    if (method === "binance" && binanceUid.trim().length < 5)
      return toast("Enter a valid Binance UID");
    setBusy(true);
    try {
      const r = await api<{ ticket_id: number; process_time: string }>("/api/withdraw", {
        amount: amt, method,
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
          <button className={method === "telebirr" ? "active" : ""} onClick={() => setMethod("telebirr")}>📲 Telebirr</button>
          <button className={method === "binance" ? "active" : ""} onClick={() => setMethod("binance")}>🟡 Binance Pay</button>
        </div>
        <label className="f">Amount (Birr)</label>
        <input type="number" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder={`Min ${fmt(me.min_withdrawal)}`} min={me.min_withdrawal} />
        {method === "telebirr" ? (
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
        <button className="btn" disabled={busy} onClick={submit}>{busy ? <><Spinner />Sending…</> : "💸 Submit withdrawal"}</button>
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
