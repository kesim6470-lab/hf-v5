import { useEffect, useState } from "react";
import { api, fmt, type Leaderboard, type LeaderboardEntry } from "../api";

/** Top Referrers — its own tab so every user can see the board (owner
 *  request 2026-11-26). BIG MyAge-style layout: the #1 referrer gets a
 *  full-width champion hero (huge trophy, big name, big numbers), #2
 *  and #3 get large medal cards, and ranks 4-10 are roomy rows below.
 *  Data comes from the backend's 12-hour snapshot via /api/leaderboard. */
export default function Top() {
  const [board, setBoard] = useState<Leaderboard | null>(null);

  useEffect(() => {
    api<Leaderboard>("/api/leaderboard").then(setBoard).catch(() => {});
  }, []);

  const champ = (r: LeaderboardEntry) => (
    <div className={`champ${r.is_me ? " me" : ""}`}>
      <div className="medal">🏆</div>
      <div className="crown">👑 #1 TOP REFERRER</div>
      <div className="nm">{r.name}{r.is_me ? " (You)" : ""}</div>
      <div className="rf">{r.refs} 👥</div>
      <div className="er">{fmt(r.earned)} Birr earned</div>
    </div>
  );

  const pod = (r: LeaderboardEntry, medal: string) => (
    <div className={`pod${r.is_me ? " me" : ""}`} key={r.rank}>
      <div className="medal">{medal}</div>
      <div className="nm">{r.name}{r.is_me ? " (You)" : ""}</div>
      <div className="rf">{r.refs} 👥</div>
      <div className="er">{fmt(r.earned)} Birr</div>
    </div>
  );

  return (
    <section className="screen">
      <div style={{ textAlign: "center", margin: "6px 0 16px" }}>
        <div style={{ fontSize: 30, fontWeight: 900 }}>🏆 Top Referrers</div>
        <div style={{ color: "var(--muted)", fontSize: 13, fontWeight: 600, marginTop: 4 }}>
          The best inviters on Anon Earn — climb the board and take the trophy!
        </div>
      </div>

      {board === null ? (
        <div className="empty">Loading…</div>
      ) : board.top.length === 0 ? (
        <div className="empty">No one is on the board yet — invite friends and take the 🏆!</div>
      ) : (
        <>
          {board.top[0] && champ(board.top[0])}
          {(board.top[1] || board.top[2]) && (
            <div className="duo">
              {board.top[1] ? pod(board.top[1], "🥈") : <div className="pod" style={{ visibility: "hidden" }} />}
              {board.top[2] ? pod(board.top[2], "🥉") : <div className="pod" style={{ visibility: "hidden" }} />}
            </div>
          )}
          {board.top.length > 3 && (
            <div className="card" style={{ marginTop: 12 }}>
              {board.top.slice(3).map((r) => (
                <div className="lbrow" key={r.rank} style={r.rank === 4 ? { borderTop: "none" } : undefined}>
                  <span className="rk">{r.rank}</span>
                  <span className="nm">{r.name}{r.is_me ? " (You)" : ""}</span>
                  <span className="vl">{r.refs} 👥 <small>· {fmt(r.earned)} Birr</small></span>
                </div>
              ))}
            </div>
          )}
          <div className="empty" style={{ marginTop: 12 }}>
            🕐 Updates every 12 hours
            {board.me.rank
              ? ` · Your rank: #${board.me.rank}`
              : board.me.refs > 0
                ? ` · You have ${board.me.refs} 👥 — keep going!`
                : ""}
          </div>
        </>
      )}
    </section>
  );
}
