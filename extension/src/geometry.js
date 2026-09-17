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

/** Draw a stadium only when the streak half-length is at least this times r. */
export const STADIUM_MIN_RATIO = 0.5;
const HALF_PI = Math.PI / 2;

/**
 * Ring shape for a ball of radius `rPx` blurred into a streak of half-length
 * `slPx` along direction `sa` (radians, image coordinates, y down).
 *
 * Width and clamping are exactly as for the circle (ringGeometry): `radius` is
 * the stroke-centre offset from the streak's centre line, so the inner edge
 * lies on the streak outline (r_px) and the outer edge at r_px + width.
 * `shape` is "stadium" (follow the streak) or "circle" (always a circle of the
 * same radius at the streak centre). A stadium is used only when
 * slPx >= STADIUM_MIN_RATIO * rPx.
 *
 * Returns (and fills `out`) { radius, width, clamped, kind, half, ux, uy, angle }.
 */
export function ringShape(rPx, slPx, sa, ratio = 1.2, minStroke = 1.5, shape = "stadium", out = {}) {
  ringGeometry(rPx, ratio, minStroke, out);
  const stadium = shape !== "circle" && slPx > 0 && slPx >= STADIUM_MIN_RATIO * rPx;
  out.kind = stadium ? "stadium" : "circle";
  out.half = stadium ? slPx : 0;
  out.angle = stadium ? sa : 0;
  out.ux = stadium ? Math.cos(sa) : 1;
  out.uy = stadium ? Math.sin(sa) : 0;
  return out;
}

/**
 * Trace the ring's centre line as one path (beginPath included, no stroke).
 * Stadium: an arc around each cap centre plus the two straight sides, rotated
 * by the streak angle. `ctx` only needs beginPath/arc/lineTo/closePath.
 */
export function traceRing(ctx, cx, cy, g) {
  ctx.beginPath();
  const R = g.radius;
  if (g.kind !== "stadium") {
    ctx.arc(cx, cy, R, 0, Math.PI * 2);
    return;
  }
  const dx = g.half * g.ux;
  const dy = g.half * g.uy;
  const a = g.angle;
  // Cap at the +u end, sweeping through the tip (angle a).
  ctx.arc(cx + dx, cy + dy, R, a - HALF_PI, a + HALF_PI);
  // Side along -u, offset by R on the +n side, where n = (-uy, ux).
  ctx.lineTo(cx - dx - R * g.uy, cy - dy + R * g.ux);
  // Cap at the -u end, sweeping through its tip (angle a + pi).
  ctx.arc(cx - dx, cy - dy, R, a + HALF_PI, a + 3 * HALF_PI);
  // closePath draws the other side back to the first arc's start.
  ctx.closePath();
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
