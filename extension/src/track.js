// Tracking-file parsing and sampling (docs/track-format.md, schema version 1).
// Pure module: no DOM or chrome.* access, so it runs under `node --test`.

export const SCHEMA_VERSION = 1;
export const FLAG_INTERPOLATED = 1;
export const FLAG_BOUNCE = 2;
export const FLAG_HIT = 4;

// Tolerance for float comparisons of times read from JSON (3-decimal rounding).
const EPS = 1e-6;
const HEX_RE = /^#[0-9a-fA-F]{6}$/;
const REQUIRED_FIELDS = ["t", "x", "y", "r", "conf"];
// Optional columns. Missing `ring`/`flags` get defaults; missing `sl`/`sa`
// (or null values in a row) mean a sharp, round ball.
const OPTIONAL_FIELDS = ["ring", "flags", "sl", "sa"];
const DEFAULT_FIELDS = ["t", "x", "y", "r", "conf", "ring", "flags"];
const DEFAULT_RING = "#f5f5f5";

export class TrackError extends Error {}

function num(v, what) {
  if (typeof v !== "number" || !Number.isFinite(v)) {
    throw new TrackError(`${what} must be a finite number`);
  }
  return v;
}

/**
 * Validate a track.json document and convert it into a columnar structure
 * (typed arrays) that is cheap to search every frame.
 */
export function parseTrack(doc) {
  if (!doc || typeof doc !== "object") throw new TrackError("track is not an object");
  if (doc.schema_version !== SCHEMA_VERSION) {
    throw new TrackError(`unsupported schema_version ${doc.schema_version}`);
  }
  const fps = num(doc.video && doc.video.fps, "video.fps");
  if (fps <= 0) throw new TrackError("video.fps must be positive");

  // Columns are looked up by name; unknown columns are ignored.
  const fields = Array.isArray(doc.fields) ? doc.fields : DEFAULT_FIELDS;
  const col = {};
  for (const name of REQUIRED_FIELDS) {
    const i = fields.indexOf(name);
    if (i < 0) throw new TrackError(`fields is missing "${name}"`);
    col[name] = i;
  }
  for (const name of OPTIONAL_FIELDS) col[name] = fields.indexOf(name);

  const segments = (Array.isArray(doc.segments) ? doc.segments : [])
    .map((s, i) => ({ start: num(s && s.start, `segments[${i}].start`), end: num(s && s.end, `segments[${i}].end`) }))
    .filter((s) => s.end > s.start)
    .sort((a, b) => a.start - b.start);
  const segStart = new Float64Array(segments.map((s) => s.start));
  const segEnd = new Float64Array(segments.map((s) => s.end));

  const rows = Array.isArray(doc.frames) ? doc.frames : [];
  // Rows "are sorted by t", but tolerate a sloppy producer: sort if needed.
  let sorted = true;
  for (let i = 1; i < rows.length; i++) {
    if (rows[i][col.t] < rows[i - 1][col.t]) { sorted = false; break; }
  }
  const src = sorted ? rows : rows.slice().sort((a, b) => a[col.t] - b[col.t]);

  const n = src.length;
  const t = new Float64Array(n);
  const x = new Float32Array(n);
  const y = new Float32Array(n);
  const r = new Float32Array(n);
  const conf = new Float32Array(n);
  const flags = new Uint8Array(n);
  const ring = new Array(n);
  const hasStreak = col.sl >= 0;
  const sl = hasStreak ? new Float32Array(n) : null;
  const sa = hasStreak ? new Float32Array(n) : null;
  let maxSl = 0;
  for (let i = 0; i < n; i++) {
    const row = src[i];
    if (!Array.isArray(row)) throw new TrackError(`frames[${i}] is not an array`);
    t[i] = num(row[col.t], `frames[${i}].t`);
    x[i] = num(row[col.x], `frames[${i}].x`);
    y[i] = num(row[col.y], `frames[${i}].y`);
    r[i] = num(row[col.r], `frames[${i}].r`);
    conf[i] = num(row[col.conf], `frames[${i}].conf`);
    const c = col.ring >= 0 ? row[col.ring] : null;
    ring[i] = typeof c === "string" && HEX_RE.test(c) ? c.toLowerCase() : DEFAULT_RING;
    const f = col.flags >= 0 ? row[col.flags] : 0;
    flags[i] = Number.isInteger(f) ? f & 0xff : 0;
    if (hasStreak) {
      const l = row[col.sl];
      const a = col.sa >= 0 ? row[col.sa] : 0;
      sl[i] = typeof l === "number" && Number.isFinite(l) && l > 0 ? l : 0;
      sa[i] = typeof a === "number" && Number.isFinite(a) ? a : 0;
      if (sl[i] > maxSl) maxSl = sl[i];
    }
  }

  return {
    videoId: typeof doc.video_id === "string" ? doc.video_id : null,
    fps,
    width: doc.video.width || 0,
    height: doc.video.height || 0,
    generator: doc.generator || null,
    segments,
    segStart,
    segEnd,
    length: n,
    t, x, y, r, conf, ring, flags,
    sl, // Float32Array or null when the file has no `sl` column
    sa,
    hasStreak: hasStreak && maxSl > 0,
  };
}

/**
 * Index of the last element of the sorted array `arr` that is <= v, or -1.
 * `hint` (a previous result) makes sequential playback O(1).
 */
export function floorIndex(arr, v, hint = -1) {
  const n = arr.length;
  if (n === 0 || v < arr[0]) return -1;
  if (hint >= 0 && hint < n && arr[hint] <= v) {
    if (hint === n - 1 || arr[hint + 1] > v) return hint;
    if (hint + 2 >= n || arr[hint + 2] > v) return hint + 1;
  }
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >>> 1;
    if (arr[mid] <= v) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

/** Index of the segment containing time `t`, or -1. */
export function segmentIndex(track, t) {
  const i = floorIndex(track.segStart, t + EPS);
  if (i < 0) return -1;
  return t <= track.segEnd[i] + EPS ? i : -1;
}

export function createSample() {
  // Reused by the renderer every frame: no per-frame allocation.
  return { x: 0, y: 0, r: 0, sl: 0, sa: 0, conf: 0, ring: "", flags: 0, row: -1, lo: -1, hi: -1, mode: "none", hint: -1 };
}

/**
 * Sample the track at time `t` (YouTube timeline, seconds) per the rendering
 * rules: interpolate x, y, r, sl (and conf) between rows at most 2.5/fps
 * apart; otherwise use the nearest row only if it is within 0.5/fps (the same
 * frame). `ring`, `sa` and `flags` come from the nearest row. Fills `out` and returns true when something should be drawn.
 * `out.mode` is "interp" | "nearest" | "none" | "outside" | "lowconf".
 *
 * `minConf` hides rows whose (interpolated) confidence is below it.
 */
export function sampleTrack(track, t, out, minConf = 0.5) {
  out.lo = out.hi = out.row = -1;
  if (track.length === 0 || segmentIndex(track, t) < 0) {
    out.mode = "outside";
    return false;
  }
  const ts = track.t;
  const lo = floorIndex(ts, t + EPS, out.hint);
  out.hint = lo;
  const hi = lo + 1 < track.length ? lo + 1 : -1;
  const hasLo = lo >= 0;
  const hasHi = hi >= 0;
  out.lo = lo;
  out.hi = hi;

  const maxGap = 2.5 / track.fps;
  const maxNear = 0.5 / track.fps;

  if (hasLo && hasHi && ts[hi] - ts[lo] <= maxGap + EPS) {
    const span = ts[hi] - ts[lo];
    const a = span > 0 ? Math.min(1, Math.max(0, (t - ts[lo]) / span)) : 0;
    const b = 1 - a;
    out.x = track.x[lo] * b + track.x[hi] * a;
    out.y = track.y[lo] * b + track.y[hi] * a;
    out.r = track.r[lo] * b + track.r[hi] * a;
    out.conf = track.conf[lo] * b + track.conf[hi] * a;
    const near = a <= 0.5 ? lo : hi;
    if (track.sl) {
      out.sl = track.sl[lo] * b + track.sl[hi] * a;
      out.sa = track.sa[near];
    } else {
      out.sl = out.sa = 0;
    }
    out.row = near;
    out.ring = track.ring[near];
    out.flags = track.flags[near];
    out.mode = "interp";
  } else {
    const dLo = hasLo ? t - ts[lo] : Infinity;
    const dHi = hasHi ? ts[hi] - t : Infinity;
    const near = dLo <= dHi ? lo : hi;
    const d = Math.min(dLo, dHi);
    if (near < 0 || d > maxNear + EPS) {
      out.mode = "none";
      return false;
    }
    out.row = near;
    out.x = track.x[near];
    out.y = track.y[near];
    out.r = track.r[near];
    out.sl = track.sl ? track.sl[near] : 0;
    out.sa = track.sa ? track.sa[near] : 0;
    out.conf = track.conf[near];
    out.ring = track.ring[near];
    out.flags = track.flags[near];
    out.mode = "nearest";
  }
  if (out.conf < minConf) {
    out.mode = "lowconf";
    return false;
  }
  return true;
}

/** Total seconds covered by the track's segments. */
export function coveredSeconds(track) {
  let s = 0;
  for (const seg of track.segments) s += seg.end - seg.start;
  return s;
}
