import { api, fmt, tg } from "../api";
import { useApp } from "../App";

/** Invite / Friends: referral stats, copyable link, Telegram share, and
 *  the backend-built share card (photo + caption DM'd by the bot). */
export default function Invite() {
  const { me, toast } = useApp();
  if (!me) return <div className="empty">Loading…</div>;

  async function shareCard() {
    try {
      await api("/api/invite/share_card");
      toast("📨 Invite card sent to your chat with the bot — forward it to friends!");
    } catch (e: any) {
      if (String(e?.message).startsWith("cooldown:")) {
        toast(`⏳ Share card on cooldown — ${Math.ceil(Number(e.message.split(":")[1]) / 60)} min left`);
      } else toast(e?.message || "Couldn't send the card");
    }
  }

  return (
    <section className="screen">
      <h3 className="sect">👥 Invite Friends</h3>
      <div className="stat-row" style={{ marginTop: 0 }}>
        <div className="stat-box"><div className="n">{me.referrals}</div><div className="l">Invited</div></div>
        <div className="stat-box"><div className="n">{me.tier2_referrals}</div><div className="l">Tier-2</div></div>
        <div className="stat-box"><div className="n">{fmt(me.total_earned_refs)}</div><div className="l">Birr earned</div></div>
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div style={{ fontSize: 14, lineHeight: 1.55 }}>
          Earn <b>{fmt(me.reward_per_referral)} Birr</b> for every friend who joins with your
          link and completes verification.
        </div>
      </div>
      <h3 className="sect">🔗 Your Referral Link</h3>
      <div className="ref-box">{me.referral_link}</div>
      <button className="btn" onClick={() => { navigator.clipboard?.writeText(me.referral_link); toast("Link copied 📋"); }}>
        📋 Copy link
      </button>
      <button className="btn ghost" onClick={() => {
        if (tg) tg.openTelegramLink("https://t.me/share/url?url=" + encodeURIComponent(me.referral_link) + "&text=" + encodeURIComponent("Join Hf Earn and earn Birr daily!"));
        else { navigator.clipboard?.writeText(me.referral_link); toast("Link copied 📋"); }
      }}>
        📤 Share
      </button>
      <button className="btn ghost" onClick={shareCard}>🖼 Send me a share card</button>
      {me.support_username && (
        <div className="empty">Need help? Contact @{me.support_username}</div>
      )}
    </section>
  );
}
