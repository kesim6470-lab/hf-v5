import { useEffect, useState } from "react";
import { api, fmt, type Leaderboard } from "../api";

/** Top Referrers — its own tab so every user can see the board (owner
 *  request 2026-11-26). Ranks 1-3 stand on a podium — the winner in the
 *  middle with the trophy, silver and bronze medals at the sides, like
 *  professional bots do — and ranks 4-10 are plain rows below. Data
 *  comes from the backend's 12-hour snapshot via /api/leaderboard. */
export default function Top() {
  const [board, setBoard] = useState<Leaderboard | null>(null);

  useEffect(() => {
    api<Leaderboard>("/api/leaderboard").then(setBoard).catch(() => {});
  }, []);

  return (
    <section className="screen">
      <h3 className="sect">🏆 Top Referrers</h3>
      <div className="empty" style={{ marginTop: -4 }}>
        The best inviters on Anon Earn — climb the board and take the trophy!
      </div>
      {board === null ? (
        <div className="empty">Loading…</div>
      ) : board.top.length === 0 ? (
        <div className="empty">No one is on the board yet — invite friends and take the 🏆!</div>
      ) : (
        <div className="card">
          {(() => {
            const [first, second, third, ...rest] = board.top;
            const pod = (r: typeof first, medal: string, cls: string) => (
              <div className={`pod ${cls}${r.is_me ? " me" : ""}`} key={r.rank}>
                <div className="medal">{medal}</div>
                <div className="nm">{r.name}{r.is_me ? " (You)" : ""}</div>
                <div className="rf">{r.refs} 👥</div>
                <div className="er">{fmt(r.earned)} Birr</div>
              </div>
            );
            return (
              <>
                <div className="podium">
                  {second ? pod(second, "🥈", "p2") : <div className="pod" style={{ visibility: "hidden" }} />}
                  {first && pod(first, "🏆", "p1")}
                  {third ? pod(third, "🥉", "p3") : <div className="pod" style={{ visibility: "hidden" }} />}
                </div>
                {rest.map((r) => (
                  <div className="lbrow" key={r.rank}>
                    <span className="rk">#{r.rank}</span>
                    <span className="nm">{r.name}{r.is_me ? " (You)" : ""}</span>
                    <span className="vl">{r.refs} 👥 <small>· {fmt(r.earned)} Birr</small></span>
                  </div>
                ))}
              </>
            );
          })()}
          <div className="s" style={{ color: "var(--muted)", fontSize: 12, marginTop: 12 }}>
            🕐 Updates every 12 hours
            {board.me.rank
              ? ` · Your rank: #${board.me.rank}`
              : board.me.refs > 0
                ? ` · You have ${board.me.refs} 👥 — keep going!`
                : ""}
          </div>
        </div>
      )}
    </section>
  );
}
