// Tracking-file parsing and sampling (docs/track-format.md, schema version 1).
// Pure module: no DOM or chrome.* access, so it runs under `node --test`.

export const SCHEMA_VERSION = 1;
export const FLAG_INTERPOLATED = 1;
export const FLAG_BOUNCE = 2;
export const FLAG_HIT = 4;

// Tolerance for float comparisons of times read from JSON (3-decimal rounding).
const EPS = 1e-6;
const HEX_RE = /^#[0-9a-fA-F]{6}$/;
const REQUIRED_FIELDS = ["t", "x", "y", "r", "conf", "ring", "flags"];

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

  const fields = Array.isArray(doc.fields) ? doc.fields : REQUIRED_FIELDS;
  const col = {};
  for (const name of REQUIRED_FIELDS) {
    const i = fields.indexOf(name);
    if (i < 0) throw new TrackError(`fields is missing "${name}"`);
    col[name] = i;
  }

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
  for (let i = 0; i < n; i++) {
    const row = src[i];
    if (!Array.isArray(row)) throw new TrackError(`frames[${i}] is not an array`);
    t[i] = num(row[col.t], `frames[${i}].t`);
    x[i] = num(row[col.x], `frames[${i}].x`);
    y[i] = num(row[col.y], `frames[${i}].y`);
    r[i] = num(row[col.r], `frames[${i}].r`);
    conf[i] = num(row[col.conf], `frames[${i}].conf`);
    const c = row[col.ring];
    ring[i] = typeof c === "string" && HEX_RE.test(c) ? c.toLowerCase() : "#f5f5f5";
    const f = row[col.flags];
    flags[i] = Number.isInteger(f) ? f & 0xff : 0;
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
  return { x: 0, y: 0, r: 0, conf: 0, ring: "", flags: 0, row: -1, lo: -1, hi: -1, mode: "none", hint: -1 };
}

/**
 * Sample the track at time `t` (YouTube timeline, seconds) per the rendering
 * rules. Fills `out` and returns true when something should be drawn.
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
  const maxNear = 1 / track.fps;

  if (hasLo && hasHi && ts[hi] - ts[lo] <= maxGap + EPS) {
    const span = ts[hi] - ts[lo];
    const a = span > 0 ? Math.min(1, Math.max(0, (t - ts[lo]) / span)) : 0;
    const b = 1 - a;
    out.x = track.x[lo] * b + track.x[hi] * a;
    out.y = track.y[lo] * b + track.y[hi] * a;
    out.r = track.r[lo] * b + track.r[hi] * a;
    out.conf = track.conf[lo] * b + track.conf[hi] * a;
    const near = a <= 0.5 ? lo : hi;
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
