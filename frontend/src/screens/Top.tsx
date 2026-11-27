import { useEffect, useState } from "react";
import { api, fmt, type Leaderboard, type LeaderboardEntry } from "../api";
import { Shimmer } from "../components/ui";

/** Weekly Top 20 — every week the 20 best referrers split the prize
 *  pool by rank % (owner request 2026-11-26). Each rank shows its prize
 *  next to it; prizes are credited automatically when the week ends.
 *  BIG MyAge-style layout: champion hero, medal cards, roomy rows. */
export default function Top() {
  const [board, setBoard] = useState<Leaderboard | null>(null);
  const [left, setLeft] = useState(0);

  useEffect(() => {
    api<Leaderboard>("/api/leaderboard").then((b) => {
      setBoard(b);
      setLeft(b.weekly.seconds_left);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!board) return;
    const t = setInterval(() => setLeft((s) => Math.max(0, s - 1)), 1000);
    return () => clearInterval(t);
  }, [board !== null]);

  const countdown = () => {
    const d = Math.floor(left / 86400), h = Math.floor((left % 86400) / 3600),
      m = Math.floor((left % 3600) / 60), s = left % 60;
    return d > 0 ? `${d}d ${h}h ${m}m` : `${h}h ${m}m ${s}s`;
  };

  const prizeTag = (r: LeaderboardEntry, big = false) => (
    <span className="prize" style={big ? { fontSize: 16, padding: "6px 16px" } : undefined}>
      {fmt(r.prize)} Birr
    </span>
  );

  const champ = (r: LeaderboardEntry) => (
    <div className={`champ${r.is_me ? " me" : ""}`}>
      <div className="medal">🏆</div>
      <div className="crown">👑 #1 THIS WEEK</div>
      <div className="nm">{r.name}{r.is_me ? " (You)" : ""}</div>
      <div className="rf">{r.refs} 👥</div>
      <div style={{ marginTop: 10 }}>{prizeTag(r, true)}</div>
    </div>
  );

  const pod = (r: LeaderboardEntry, medal: string) => (
    <div className={`pod${r.is_me ? " me" : ""}`} key={r.rank}>
      <div className="medal">{medal}</div>
      <div className="nm">{r.name}{r.is_me ? " (You)" : ""}</div>
      <div className="rf">{r.refs} 👥</div>
      <div style={{ marginTop: 8 }}>{prizeTag(r)}</div>
    </div>
  );

  return (
    <section className="screen">
      <div style={{ textAlign: "center", margin: "6px 0 16px" }}>
        <div style={{ fontSize: 30, fontWeight: 900 }}>🏆 Weekly Champions</div>
        <div style={{ color: "var(--muted)", fontSize: 13, fontWeight: 600, marginTop: 4 }}>
          The week's 20 best inviters split the prize pool — paid automatically on Monday.
        </div>
      </div>

      {board === null ? (
        <Shimmer rows={4} />
      ) : (
        <>
          {board.weekly.enabled && (
            <div className="poolbar">
              <div style={{ fontSize: 12, fontWeight: 900, letterSpacing: 2, color: "var(--muted)" }}>💰 WEEKLY PRIZE POOL</div>
              <div className="amt">{fmt(board.weekly.pool)} Birr</div>
              <div className="sub">Split between the Top 20 · ⏳ Ends in {countdown()} · paid automatically</div>
            </div>
          )}
          {board.top.length === 0 ? (
            <div className="empty">No one is on the board yet this week — invite friends and take the 🏆!</div>
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
                      <span className="pz">
                        <span className="vl">{r.refs} 👥</span>
                        <div style={{ marginTop: 4 }}>{prizeTag(r)}</div>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
          <div className="empty" style={{ marginTop: 12 }}>
            {board.me.rank
              ? `🎯 Your rank this week: #${board.me.rank}${board.me.prize > 0 ? ` — prize so far ${fmt(board.me.prize)} Birr` : ""}`
              : board.me.refs > 0
                ? `You have ${board.me.refs} 👥 this week — push into the Top 20!`
                : "Invite friends this week to enter the board 👥"}
            <br />🔄 New week starts Monday — the board resets and prizes are paid out.
          </div>
        </>
      )}
    </section>
  );
}
