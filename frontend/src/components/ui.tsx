import { useState, type ButtonHTMLAttributes, type ReactNode } from "react";

/** Small inline spinner — shown inside buttons while their action runs. */
export function Spinner() {
  return <span className="spin" aria-hidden="true" />;
}

interface BtnProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => unknown;
  children: ReactNode;
}

/** Button with a built-in busy state: tap it and it disables itself,
 *  shows a small spinner and gently pulses until the (async) onClick
 *  settles — the pro "something is happening" feel the owner asked for. */
export function Btn({ onClick, children, disabled, className, ...rest }: BtnProps) {
  const [busy, setBusy] = useState(false);
  async function handle(e: React.MouseEvent<HTMLButtonElement>) {
    if (!onClick || busy) return;
    try {
      const r = onClick(e);
      if (r && typeof (r as { then?: unknown }).then === "function") {
        setBusy(true);
        await r;
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <button
      {...rest}
      className={`${className ?? "btn"}${busy ? " is-busy" : ""}`}
      disabled={disabled || busy}
      onClick={onClick ? handle : undefined}
    >
      {busy && <Spinner />}
      {children}
    </button>
  );
}

/** Shimmering skeleton placeholder — replaces the old plain "Loading…"
 *  text with a soft light-sweep over placeholder shapes. */
export function Shimmer({ rows = 3 }: { rows?: number }) {
  return (
    <div className="shimmer" aria-label="Loading">
      <div className="skel skel-hero" />
      {Array.from({ length: rows }).map((_, i) => (
        <div className="skel skel-row" key={i}>
          <span className="skel-dot" />
          <span className="skel-line" style={{ width: `${62 - i * 9}%` }} />
          <span className="skel-line short" />
        </div>
      ))}
    </div>
  );
}
