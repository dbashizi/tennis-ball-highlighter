// Pure geometry helpers for the overlay. No DOM access.

/**
 * Rectangle (relative to the element's box) where the video frame is actually
 * painted for `object-fit: contain` (YouTube's default) with centred
 * `object-position`. `fit` may also be "fill" or "cover".
 * Returns null while the intrinsic size is unknown.
 */
export function contentRect(boxW, boxH, videoW, videoH, fit = "contain") {
  if (!(boxW > 0 && boxH > 0 && videoW > 0 && videoH > 0)) return null;
  if (fit === "fill") return { x: 0, y: 0, w: boxW, h: boxH };
  const scale = fit === "cover"
    ? Math.max(boxW / videoW, boxH / videoH)
    : Math.min(boxW / videoW, boxH / videoH);
  const w = videoW * scale;
  const h = videoH * scale;
  return { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h };
}

/**
 * Ring geometry in CSS pixels for a ball of radius `rPx`.
 * The ring's inner edge sits on the ball edge (rPx) and its outer edge at
 * `ratio * rPx`, drawn as one stroke of width (ratio - 1) * rPx centred at
 * rPx + width / 2 (= 1.1 rPx for the default 1.2). When the width is clamped
 * up to `minStroke`, the inner edge stays at rPx and the outer edge grows.
 * Pass `out` to reuse an object (the renderer does, to avoid per-frame garbage).
 */
export function ringGeometry(rPx, ratio = 1.2, minStroke = 1.5, out = {}) {
  const natural = (ratio - 1) * rPx;
  const width = natural > minStroke ? natural : minStroke;
  out.radius = rPx + width / 2;
  out.width = width;
  out.clamped = width > natural;
  return out;
}

export const SETTINGS_LIMITS = {
  ratio: { min: 1.1, max: 1.5, step: 0.05, def: 1.2 },
  minStroke: { min: 0.5, max: 4, step: 0.25, def: 1.5 },
  minConf: { min: 0, max: 1, step: 0.05, def: 0.5 },
};

export function clamp(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v;
}

/** "m:ss", "h:mm:ss" or plain seconds -> seconds, or NaN. */
export function parseClock(text) {
  const s = String(text ?? "").trim();
  if (!s) return NaN;
  if (/^\d+(\.\d+)?$/.test(s)) return Number(s);
  const m = /^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/.exec(s);
  if (!m) return NaN;
  const h = m[1] ? Number(m[1]) : 0;
  const min = Number(m[2]);
  const sec = Number(m[3]);
  if (sec >= 60 || (m[1] && min >= 60)) return NaN;
  return h * 3600 + min * 60 + sec;
}

/** Seconds -> "m:ss" (or "h:mm:ss" past an hour). Whole seconds. */
export function formatClock(sec) {
  const total = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

/** Default create-tracking range: current time +/- 30 s, clamped to the video. */
export function defaultRange(current, duration, half = 30) {
  const d = Number.isFinite(duration) && duration > 0 ? duration : Infinity;
  const c = clamp(Number(current) || 0, 0, Number.isFinite(d) ? d : Number.MAX_VALUE);
  const start = Math.floor(Math.max(0, c - half));
  const end = Math.ceil(Math.min(d, c + half));
  return { start, end };
}

/** Extract a YouTube video id from a watch-page URL, or null. */
export function videoIdFromUrl(href) {
  let u;
  try { u = new URL(href); } catch { return null; }
  if (!/(^|\.)youtube\.com$/.test(u.hostname)) return null;
  if (u.pathname !== "/watch") return null;
  const v = u.searchParams.get("v");
  return v && /^[A-Za-z0-9_-]{11}$/.test(v) ? v : null;
}
