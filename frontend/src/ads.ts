/* Ad-network helpers — AdsGram (rewarded video, S2S-confirmed) and the
   Monetag fallback, ported from the fixed legacy app.html logic:
   - AdsGram SDK is lazy-loaded (a blocking 3rd-party script must never
     stall the whole Mini App).
   - The reward is ONLY trusted once the backend's watched count moves
     (AdsGram's server-to-server postback), never from the SDK promise.
   - Monetag is the no-fill fallback (BUG-REPORT #12 wiring, kept). */

let adsgramLoading: Promise<boolean> | null = null;

export function ensureAdsgramSdk(): Promise<boolean> {
  if (window.Adsgram) return Promise.resolve(true);
  if (adsgramLoading) return adsgramLoading;
  adsgramLoading = new Promise((resolve) => {
    const s = document.createElement("script");
    s.src = "https://sad.adsgram.ai/js/sad.min.js";
    s.onload = () => resolve(!!window.Adsgram);
    s.onerror = () => { adsgramLoading = null; resolve(false); };
    document.head.appendChild(s);
  });
  return adsgramLoading;
}

export async function waitForAdsgramSdk(timeoutMs = 15000): Promise<boolean> {
  if (window.Adsgram) return true;
  const loaded = await Promise.race([
    ensureAdsgramSdk(),
    new Promise<boolean>((r) => setTimeout(() => r(false), timeoutMs)),
  ]);
  return loaded || !!window.Adsgram;
}

let controller: any = null;

/** Shows an AdsGram rewarded ad. Resolves true when the SDK reports the
 *  ad was watched (reward/complete or a resolved show()); false on
 *  no-fill / load failure. Financial confirmation is the caller's job
 *  (backend postback polling), exactly like the legacy flow. */
export async function showAdsgram(blockId: string): Promise<boolean> {
  const ready = await waitForAdsgramSdk();
  if (!ready || !window.Adsgram || !blockId) return false;
  if (!controller) {
    controller = window.Adsgram.init({ blockId });
  }
  let watched = false;
  try {
    controller.addEventListener?.("onReward", () => { watched = true; });
    controller.addEventListener?.("onComplete", () => { watched = true; });
  } catch { /* older SDK without events */ }
  try {
    await controller.show();
    return true;
  } catch (e: any) {
    if (e?.state === "load" || e?.state === "render") {
      // Transient — the creative often finishes loading a moment later.
      await new Promise((r) => setTimeout(r, 1500));
      try {
        await controller.show();
        return true;
      } catch {
        return watched;
      }
    }
    return watched;
  }
}

/* ---------------- Monetag fallback ---------------- */

let monetagLoading: string | null = null;

export function ensureMonetagSdk(zoneId: string, sdkUrl: string): void {
  if (!zoneId || !sdkUrl) return;
  const fnName = `show_${zoneId}`;
  if ((window as any)[fnName]) return;
  if (monetagLoading === sdkUrl) return;
  monetagLoading = sdkUrl;
  const s = document.createElement("script");
  s.src = sdkUrl;
  s.setAttribute("data-zone", zoneId);
  s.setAttribute("data-sdk", fnName);
  document.head.appendChild(s);
}

/** Tries the Monetag fallback ad. True = an ad was shown to completion.
 *  Note (same as legacy): Monetag has no reward postback in this
 *  codebase, so this fills the impression but cannot itself credit
 *  Birr — callers must not treat it as a paid view. */
export async function tryMonetagFallback(zoneId: string, sdkUrl: string): Promise<boolean> {
  if (!zoneId) return false;
  ensureMonetagSdk(zoneId, sdkUrl);
  const fnName = `show_${zoneId}`;
  for (let i = 0; i < 10 && typeof (window as any)[fnName] !== "function"; i++) {
    await new Promise((r) => setTimeout(r, 300));
  }
  const fn = (window as any)[fnName];
  if (typeof fn !== "function") return false;
  try {
    await fn();
    return true;
  } catch {
    return false;
  }
}
