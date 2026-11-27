import { fmt, tg } from "../api";
import { useApp } from "../App";
import { Btn, Shimmer } from "../components/ui";

/** Invite / Friends: referral stats, copyable link, and the Telegram
 *  invite-card picker.
 *
 *  Flow the owner asked for: tap "Invite Friends" -> Telegram opens its
 *  chat picker -> the user selects the person/group they want -> the
 *  Anon Earn invite CARD (photo + caption + Join button, built by the
 *  backend's inline-query handler) appears -> they tap the card and it
 *  is sent into that chat. That is tg.switchInlineQuery(): it is NOT a
 *  single fixed recipient, and unlike a forwarded DM the card's Join
 *  button survives, because the bot itself sends the card into the
 *  chosen chat. Fallback (older Telegram clients without
 *  switchInlineQuery): the t.me/share/url picker with the same wording
 *  as plain text. */
export default function Invite() {
  const { me, toast } = useApp();
  if (!me) return <Shimmer rows={2} />;

  const referralLink = me.referral_link;

  const inviteText =
    "🪙 ANON EARN — Birr, little by little, every day.\n\n" +
    "🎬 clips I watch turn into Birr\n" +
    "🎯 small missions, quick rewards\n" +
    "🔥 a daily streak that keeps paying\n" +
    "👥 friends I bring in earn me more\n\n" +
    (me.invite_show_binance === false
      ? "Payouts go to Telebirr.\n\n"
      : "Payouts go to Telebirr or Binance Pay.\n\n") +
    "Your turn — start free 👇\n" +
    referralLink;

  function inviteFriends() {
    try {
      if (tg && typeof tg.switchInlineQuery === "function") {
        tg.switchInlineQuery("", ["users", "bots", "groups", "channels"]);
        return;
      }
    } catch { /* fall through to the share picker */ }
    if (tg && typeof tg.openTelegramLink === "function") {
      tg.openTelegramLink(
        "https://t.me/share/url?url=&text=" + encodeURIComponent(inviteText)
      );
    } else {
      navigator.clipboard?.writeText(referralLink);
      toast("Link copied 📋");
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
      <Btn className="btn" style={{ marginTop: 14, width: "100%" }} onClick={inviteFriends}>
        📤 Invite Friends
      </Btn>
      <div className="empty" style={{ marginTop: 8 }}>
        Pick a friend or group, then tap the Anon Earn card to send it there.
      </div>
      <h3 className="sect">🔗 Your Referral Link</h3>
      <div className="ref-box">{referralLink}</div>
      <Btn className="btn ghost" onClick={() => { navigator.clipboard?.writeText(referralLink); toast("Link copied 📋"); }}>
        📋 Copy link
      </Btn>

      {me.support_username && (
        <div className="empty">Need help? Contact @{me.support_username}</div>
      )}
    </section>
  );
}
