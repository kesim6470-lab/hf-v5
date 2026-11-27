import { useCallback, useEffect, useRef, useState } from "react";
import { api, fmt, tg, type AdsStatus, type DailyStatus } from "../api";
import { ensureMonetagSdk, showAdsgram, tryMonetagFallback } from "../ads";
import { useApp } from "../App";
import { Shimmer, Spinner } from "../components/ui";

/** Home: coin-jar hero card (stats live elsewhere: invite earnings in
 *  the jar, team count on the team strip), withdrawals ticker, Daily Check-in
 *  (🔥, arm → ad → S2S-confirmed claim), Watch & Earn (AdsGram video with
 *  Monetag fallback + Direct Link), force-join is a hard gate in App.tsx. */
export default function Home() {
  const { me, refreshMe, toast, go } = useApp();
  const [ads, setAds] = useState<AdsStatus | null>(null);
  const [daily, setDaily] = useState<DailyStatus | null>(null);
  const [ticker, setTicker] = useState<{ full_name: string; amount: number; status: string }[]>([]);
  const [busyAd, setBusyAd] = useState(false);
  const [busyCheckin, setBusyCheckin] = useState(false);
  const [busyDl, setBusyDl] = useState(false);
  const [shown, setShown] = useState(0);
  const animRef = useRef(0);

  const load = useCallback(async () => {
    const [a, d, t] = await Promise.all([
      api<AdsStatus>("/api/ads/status").catch(() => null),
      api<DailyStatus>("/api/dailycheck/status").catch(() => null),
      api<{ enabled: boolean; withdrawals: any[] }>("/api/withdrawals/recent").catch(() => null),
    ]);
    setAds(a); setDaily(d);
    if (t?.enabled) setTicker(t.withdrawals || []);
    if (a) ensureMonetagSdk(a.monetag?.zone_id || "", a.monetag?.sdk_url || "");
  }, []);

  useEffect(() => { load(); }, [load]);

  // Balance count-up (same feel as the approved design)
  useEffect(() => {
    if (!me) return;
    const from = shown, to = me.balance, t0 = performance.now(), anim = ++animRef.current;
    const step = (t: number) => {
      if (anim !== animRef.current) return;
      const p = Math.min(1, (t - t0) / 600);
      setShown(from + (to - from) * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me?.balance]);

  /* Poll until the backend's watched count moves = AdsGram's S2S
     postback landed. The SDK finishing is never trusted for money. */
  const pollConfirmed = useCallback(async (before: number, tries = 12): Promise<boolean> => {
    for (let i = 0; i < tries; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      try {
        const fresh = await api<AdsStatus>("/api/ads/status");
        setAds(fresh);
        if ((fresh.watched_today ?? 0) > before) return true;
      } catch { /* keep polling */ }
    }
    return false;
  }, []);

  async function watchAd() {
    if (!ads || busyAd) return;
    if (ads.maintenance) return toast("🚧 Watch Ads is under maintenance — coming soon");
    if (!ads.block_id && !ads.monetag?.zone_id) return toast("❌ Ads not configured yet");
    setBusyAd(true);
    try { await api("/api/ads/click", { kind: "video" }); } catch { /* analytics only */ }
    let willPay = true;
    try {
      const claim = await api<{ will_pay: boolean }>("/api/ads/claim");
      willPay = claim?.will_pay !== false;
    } catch (e: any) {
      const msgs: Record<string, string> = {
        daily_limit_reached: "❌ Daily limit reached",
        batch_cooldown_active: "🕐 This batch is done — check back after the wait",
        cooldown_active: "❌ Please wait a bit before the next ad",
        ads_disabled: "❌ Ads are disabled right now",
      };
      toast(msgs[e?.message] || "❌ Couldn't start — try again");
      setBusyAd(false);
      if (["daily_limit_reached", "batch_cooldown_active", "cooldown_active"].includes(e?.message)) load();
      return;
    }
    const before = ads.watched_today ?? 0;
    let watched = await showAdsgram(ads.block_id);
    if (!watched && ads.monetag?.zone_id) {
      // Monetag fallback (kept from the Phase-1 fix): fills the
      // impression on AdsGram no-fill, but cannot itself credit Birr.
      const m = await tryMonetagFallback(ads.monetag.zone_id, ads.monetag.sdk_url);
      if (m) {
        toast("✅ Ad watched — no paying ad was available this time");
        setBusyAd(false);
        return;
      }
    }
    if (!watched) {
      toast("⌛ No ads available right now — please try again later");
      setBusyAd(false);
      return;
    }
    if (!willPay) {
      toast(ads.click_reminder?.text || "⚠️ Watch the ad to the end and tap it to earn");
      setBusyAd(false);
      return;
    }
    toast("⏳ Confirming your reward…");
    const confirmed = await pollConfirmed(before);
    if (confirmed) {
      tg?.HapticFeedback?.notificationOccurred("success");
      toast(`🎉 +${fmt(ads.reward_amount)} Birr credited!`);
      refreshMe();
    } else {
      toast(ads.click_reminder?.network_text || "⚠️ Couldn't confirm your ad view — try again in a moment");
    }
    setBusyAd(false);
  }

  async function checkIn() {
    if (!daily || busyCheckin || daily.claimed_today) return;
    // No-ad mode (owner directive, until AdsGram is approved): the
    // bonus claims directly — no arm, no ad, no S2S wait.
    if (daily.ad_required === false) {
      setBusyCheckin(true);
      try {
        const r = await api<{ reward: number; streak: number }>("/api/dailycheck/claim");
        toast(`+${fmt(r.reward)} Birr — streak ${r.streak} 🔥`);
        tg?.HapticFeedback?.notificationOccurred("success");
        setDaily({ ...daily, claimed_today: true, streak: r.streak });
        refreshMe();
      } catch (e: any) {
        if (e?.message === "already_claimed") {
          setDaily({ ...daily, claimed_today: true });
          toast("✅ You already checked in today — come back tomorrow!");
        } else if (e?.message === "join_required") {
          toast("🔒 Join the required channels first");
        } else {
          toast(e?.message || "Claim failed");
        }
      }
      setBusyCheckin(false);
      return;
    }
    if (!ads?.block_id && !ads?.monetag?.zone_id) return toast("❌ Ads not configured yet");
    setBusyCheckin(true);
    try {
      await api("/api/dailycheck/arm");
    } catch (e: any) {
      if (e?.message === "already_claimed") {
        setDaily({ ...daily, claimed_today: true });
        toast("✅ You already checked in today — come back tomorrow!");
        setBusyCheckin(false);
        return;
      }
      /* other arm failures are best-effort (legacy behaviour) */
    }
    let watched = ads?.block_id ? await showAdsgram(ads.block_id) : false;
    if (!watched && ads?.monetag?.zone_id) {
      watched = await tryMonetagFallback(ads.monetag.zone_id, ads.monetag.sdk_url);
    }
    if (!watched) {
      toast("⌛ No ads available right now — please try again later");
      setBusyCheckin(false);
      return;
    }
    // Wait for the S2S-confirmed check-in view, then claim (the backend
    // re-verifies + consumes the armed view atomically).
    for (let i = 0; i < 12; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      try {
        const r = await api<{ reward: number; streak: number }>("/api/dailycheck/claim");
        toast(`+${fmt(r.reward)} Birr — streak ${r.streak} 🔥`);
        tg?.HapticFeedback?.notificationOccurred("success");
        setDaily({ ...daily, claimed_today: true, streak: r.streak });
        refreshMe();
        setBusyCheckin(false);
        return;
      } catch (e: any) {
        if (e?.message !== "watch_ad_first") {
          toast(e?.message || "Claim failed");
          setBusyCheckin(false);
          return;
        }
      }
    }
    toast(daily.reminder_text || "⚠️ Couldn't confirm your ad view — try again in a moment");
    setBusyCheckin(false);
  }

  async function directLink() {
    if (!ads?.direct_link?.enabled || busyDl) return;
    setBusyDl(true);
    try {
      const open = await api<{ url: string; event_id: number; wait_seconds: number }>("/api/ads/direct-link/open");
      if (tg?.openLink) tg.openLink(open.url); else window.open(open.url, "_blank");
      toast(`⏳ Stay on the page ${open.wait_seconds}s, then claim`);
      await new Promise((r) => setTimeout(r, (open.wait_seconds + 1) * 1000));
      const r = await api<{ reward: number }>("/api/ads/direct-link/claim", { event_id: open.event_id });
      toast(`🎉 +${fmt(r.reward)} Birr credited!`);
      refreshMe(); load();
    } catch (e: any) {
      const msgs: Record<string, string> = {
        daily_limit_reached: "❌ Daily limit reached",
        cooldown_active: "❌ Please wait a bit before the next one",
        direct_link_disabled: "❌ Direct Link is disabled",
        no_click_detected: "⚠️ Open the link and stay on the page to earn",
      };
      toast(msgs[e?.message] || e?.message || "Failed");
    }
    setBusyDl(false);
  }

  if (!me) return <Shimmer rows={3} />;

  const dl = ads?.direct_link;
  const videoCapped = ads ? ads.watched_today >= ads.daily_limit : false;
  const dlCapped = dl ? dl.watched_today >= dl.daily_limit : false;

  return (
    <section className="screen">
      <div className="hero">
        <div className="wm">🪙</div>
        <div className="jar-top">
          <div className="label">🪙 My Coin Jar</div>
          {daily?.enabled && daily.streak > 0 && <div className="chip">🔥 {daily.streak}-day streak</div>}
        </div>
        <div className="bal">{fmt(shown)} <small>Birr</small></div>
        <div className="bar"><i style={{ width: `${Math.min(100, (me.balance / me.min_withdrawal) * 100)}%` }} /></div>
        <div className="goal"><span>Saved {fmt(me.balance)} Birr</span><span>{me.balance >= me.min_withdrawal ? "🎉 Cash out is open" : `🎯 ${fmt(me.min_withdrawal - me.balance)} Birr to go — cash out opens`}</span></div>
        <div className="jar-line">🏆 Earned from invites: <b>{fmt(me.total_earned_refs)} Birr</b></div>
      </div>

      {ticker.length > 0 && (
        <div className="ticker">
          {ticker.slice(0, 10).map((w, i) => (
            <span className="tk" key={i}>✅ {w.full_name} withdrew {fmt(w.amount)} Birr</span>
          ))}
        </div>
      )}

      {daily?.enabled && (
        <>
          <h3 className="sect">⚡ Daily</h3>
          <div className="grid2">
            <div className="tile c1">
              <div className="icon">🔥</div><div className="amt">+{fmt(daily.reward)} Birr</div>
              <div className="name">Daily Check In</div>
              <div className="sub">Streak: {daily.streak} days 🔥</div>
              <button className="btn pulse" disabled={daily.claimed_today || busyCheckin} onClick={checkIn}>
                {busyCheckin ? <><Spinner />Checking…</> : daily.claimed_today ? "✔ Done today" : "Check In"}
              </button>
            </div>
            <div className="tile c2">
              <div className="icon">🎬</div><div className="amt">+{fmt(ads?.reward_amount ?? 0.5)} Birr</div>
              <div className="name">Watch Ad</div>
              <div className="sub">{ads?.maintenance ? "Under maintenance 🔧" : ads ? `${ads.watched_today}/${ads.daily_limit} today${ads.seconds_left > 0 ? ` · wait ${ads.seconds_left}s` : ""}` : "…"}</div>
              <button className="btn" disabled={!!ads?.maintenance || !ads?.enabled || videoCapped || busyAd} onClick={watchAd}>
                {busyAd ? <><Spinner />Loading ad…</> : ads?.maintenance ? "🚧 Under Maintenance" : videoCapped ? "Limit reached" : "Watch Ad"}
              </button>
            </div>
          </div>
        </>
      )}

      {dl?.enabled && (
        <>
          <h3 className="sect">🔗 Direct Link</h3>
          <div className="row">
            <div className="tic">🔗</div>
            <div className="grow">
              <div className="t">Direct Link Ad</div>
              <div className="s">Earn {fmt(dl.reward_amount)} Birr per visit · {dl.watched_today}/{dl.daily_limit} today{dl.seconds_left > 0 ? ` · wait ${dl.seconds_left}s` : ""}</div>
            </div>
            <button className="btn sm" disabled={dlCapped || busyDl || dl.seconds_left > 0} onClick={directLink}>
              {busyDl ? <Spinner /> : dlCapped ? "Done" : "Open"}
            </button>
          </div>
        </>
      )}

      <div className="row team-strip" onClick={() => go("invite")}>
        <div className="tic">👥</div>
        <div className="grow">
          <div className="t">{me.referrals} friends in your team</div>
          <div className="s">+{fmt(me.reward_per_referral)} Birr for every friend who joins</div>
        </div>
        <button className="btn sm" onClick={() => go("invite")}>Invite</button>
      </div>

      <h3 className="sect">Quick Actions</h3>
      <div className="grid2">
        <button className="btn gold" style={{ marginTop: 0 }} onClick={() => go("tasks")}>🎯 Do Tasks</button>
        <button className="btn ghost" style={{ marginTop: 0 }} onClick={() => go("invite")}>🔗 Invite Friends</button>
      </div>
      <button className="btn ghost" onClick={() => go("withdraw")}>💸 Request Withdrawal</button>
    </section>
  );
}
