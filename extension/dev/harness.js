// Dev harness: runs the real overlay.js against a local clip + track.
// Query params: video, track, offset, box (16x9|4x3|21x9|tall), debug=1,
// fallback=1 (force rAF), t (start at local seconds), rate, preset (synthetic|real).
import { Overlay } from "../src/overlay.js";
import { parseTrack } from "../src/track.js";

const PRESETS = {
  synthetic: { video: "synthetic/synthetic.mp4", track: "synthetic/synthetic.track.json", truth: "synthetic/synthetic.truth.json", offset: 100 },
  real: { video: "/data/clips/YTkyRTsiIaY_359-372.mp4", track: "/samples/YTkyRTsiIaY.track.json", offset: 359 },
};

const $ = (id) => document.getElementById(id);
const q = new URLSearchParams(location.search);
const preset = PRESETS[q.get("preset") || (q.has("video") ? "" : "synthetic")] || {};
const cfg = {
  video: q.get("video") ?? preset.video ?? "",
  track: q.get("track") ?? preset.track ?? "",
  offset: Number(q.get("offset") ?? preset.offset ?? 0),
  box: q.get("box") || "16x9",
  debug: q.get("debug") === "1",
  fallback: q.get("fallback") === "1",
  t: Number(q.get("t") || 0),
  rate: q.get("rate") || "1",
  truth: q.get("truth") ?? preset.truth ?? (/synthetic\.mp4$/.test(q.get("video") || "") ? "synthetic/synthetic.truth.json" : ""),
};
let truth = null;
const synthetic = /synthetic/.test(cfg.video);

const video = $("video");
const player = $("movie_player");
if (cfg.fallback) video.requestVideoFrameCallback = undefined; // shadow the prototype method

const overlay = new Overlay({
  video,
  host: video.parentElement,
  suppressed: () => (player.classList.contains("ad-showing") ? "ad" : ""),
  watch: [player],
  timeOffset: cfg.offset,
  settings: { debug: cfg.debug },
});

// ---- UI wiring -----------------------------------------------------------------

$("videoUrl").value = cfg.video;
$("trackUrl").value = cfg.track;
$("offset").value = String(cfg.offset);
$("box").value = cfg.box;
$("rate").value = cfg.rate;
$("debug").checked = cfg.debug;
$("fallback").checked = cfg.fallback;
player.className = `html5-video-player box-${cfg.box}`;

function reloadWith(extra = {}) {
  const p = new URLSearchParams({
    video: $("videoUrl").value,
    track: $("trackUrl").value,
    offset: $("offset").value,
    box: $("box").value,
    rate: $("rate").value,
    ...(($("debug").checked) ? { debug: "1" } : {}),
    ...(($("fallback").checked) ? { fallback: "1" } : {}),
    ...extra,
  });
  location.search = p.toString();
}
$("load").addEventListener("click", () => reloadWith());
$("fallback").addEventListener("change", () => reloadWith({ t: String(video.currentTime) }));
$("box").addEventListener("change", () => {
  player.className = `html5-video-player box-${$("box").value}${$("ad").checked ? " ad-showing" : ""}`;
});
$("offset").addEventListener("change", () => overlay.setTimeOffset(Number($("offset").value)));
$("rate").addEventListener("change", () => { video.playbackRate = Number($("rate").value); });
$("ad").addEventListener("change", () => { player.classList.toggle("ad-showing", $("ad").checked); overlay.refresh(); });

const fmtNum = { ratio: (v) => `${v.toFixed(2)}×`, minStroke: (v) => `${v} px`, minConf: (v) => v.toFixed(2) };
function pushSettings() {
  const s = { enabled: $("enabled").checked, debug: $("debug").checked, shape: $("shape").value };
  for (const k of ["ratio", "minStroke", "minConf"]) {
    s[k] = Number($(k).value);
    $(`${k}Out`).textContent = fmtNum[k](s[k]);
  }
  overlay.setSettings(s);
}
for (const id of ["enabled", "debug", "ratio", "minStroke", "minConf", "shape"]) $(id).addEventListener("input", pushSettings);
pushSettings();

const FRAME = 1 / 25;
$("play").addEventListener("click", () => (video.paused ? video.play() : video.pause()));
$("back").addEventListener("click", () => { video.pause(); video.currentTime = Math.max(0, video.currentTime - FRAME); });
$("fwd").addEventListener("click", () => { video.pause(); video.currentTime = Math.min(video.duration, video.currentTime + FRAME); });
document.addEventListener("keydown", (e) => {
  if (e.target instanceof HTMLInputElement && e.target.type === "text") return;
  if (e.key === ",") $("back").click();
  else if (e.key === ".") $("fwd").click();
  else if (e.key === " " && e.target === document.body) { e.preventDefault(); $("play").click(); }
});
video.addEventListener("play", () => { $("play").textContent = "Pause"; });
video.addEventListener("pause", () => { $("play").textContent = "Play"; });
video.addEventListener("seeked", () => {
  if (!synthetic) return;
  setTimeout(() => {
    const f = video.paused ? hugCheck() : null;
    $("o-hug").textContent = f ? `${f.kind}: overshoot ${f.overshoot}, side gap ${f.sideGap}, tip gap ${f.tipGap} px; ${f.covered}/${f.core} core px under ring` : "-";
  }, 150);
});
$("scrub").addEventListener("input", () => { video.currentTime = Number($("scrub").value) * (video.duration || 0); });

function clock(t) {
  const m = Math.floor(t / 60);
  return `${m}:${(t - m * 60).toFixed(3).padStart(6, "0")}`;
}

// ---- measurement (synthetic clip: yellow ball on green/white) -----------------

const measure = { frames: 0, nearest: 0, sum: 0, max: 0, last: NaN, samples: [] };
const probe = document.createElement("canvas");
const pctx = probe.getContext("2d", { willReadFrequently: true });

// Yellowness = R - B: about 160 on the ball, about 0 on grass, lines and crowd.
// Motion blur mixes the ball with the background, so weight each pixel by its
// yellowness instead of thresholding colour: the weighted centroid is then the
// mean ball position over the exposure, which is what the track's x/y is.
const YELLOW_MIN = 20;
let frameData = null;

function grabFrame() {
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (!w || !h) return null;
  if (probe.width !== w) { probe.width = w; probe.height = h; }
  pctx.drawImage(video, 0, 0, w, h);
  frameData = pctx.getImageData(0, 0, w, h).data;
  return frameData;
}

function detectBall() {
  const d = grabFrame();
  if (!d) return null;
  const w = video.videoWidth;
  const h = video.videoHeight;
  let sx = 0, sy = 0, sw = 0;
  for (let y = 40; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const k = d[i] - d[i + 2] - YELLOW_MIN;
      if (k > 0) { sx += k * x; sy += k * y; sw += k; }
    }
  }
  // Edge-based coordinates, like the track: pixel i is centred at i + 0.5.
  return sw > 500 ? { x: sx / sw + 0.5, y: sy / sw + 0.5, weight: sw } : null;
}

/**
 * How well the drawn ring fits the ball on the current (paused) frame, in video px.
 *
 * Exact part (needs synthetic.truth.json): the true blur footprint is the union
 * of discs of radius r at the exposure sub-positions. Against the ring actually
 * drawn (overlay stats, mapped back to video px):
 *   overshoot: how far the footprint pokes past the ring's inner edge (<= 0 contained)
 *   sideGap / tipGap: inner edge minus the footprint's reach across / along the axis
 *   (about 0 means it hugs).
 *
 * Pixel part (codec-noisy): `covered` counts eroded ball-core pixels (strongly
 * yellow with all 8 neighbours too) under ring ink; the ring must not cover the
 * ball. `bleed` is the farthest faint-yellow pixel past the inner edge; yuv420
 * chroma subsampling alone spreads colour 1 to 3 px, so it's a loose sanity bound.
 */
function hugCheck() {
  const last = overlay.stats.last;
  const d = grabFrame();
  if (!last || !d) return null;
  const w = video.videoWidth;
  const h = video.videoHeight;
  const L = overlay._layout;
  const scale = w / L.w; // video px per CSS px
  const cx = last.cx * scale;
  const cy = last.cy * scale;
  const half = last.half * scale;
  const ux = Math.cos(last.angle);
  const uy = Math.sin(last.angle);
  const innerR = (last.radius - last.width / 2) * scale;
  const axis = (px, py) => {
    const a = px * ux + py * uy;
    const t = Math.max(-half, Math.min(half, a));
    return { a, dist: Math.hypot(px - t * ux, py - t * uy) };
  };
  const r2 = (v) => +v.toFixed(2);
  const out = { t: +overlay.stats.lastTime.toFixed(3), kind: last.kind, innerR: r2(innerR), half: r2(half) };

  if (truth) {
    const k = Math.round((overlay.stats.lastTime - truth.offset) * truth.fps);
    const pts = truth.frames[k];
    if (pts) {
      let far = 0, side = 0, along = 0;
      for (const [x, y] of pts) {
        const q = axis(x - cx, y - cy);
        far = Math.max(far, q.dist + truth.r);
        side = Math.max(side, q.dist + truth.r);
        along = Math.max(along, Math.abs(q.a) + truth.r);
      }
      Object.assign(out, { overshoot: r2(far - innerR), sideGap: r2(innerR - side), tipGap: r2(half + innerR - along) });
    }
  }

  const ink = overlay.canvas.getContext("2d").getImageData(0, 0, overlay.canvas.width, overlay.canvas.height).data;
  const cw = overlay.canvas.width;
  const ch = overlay.canvas.height;
  const yl = (x, y) => { const i = (y * w + x) * 4; return d[i] - d[i + 2]; };
  const reach = half + innerR + 6;
  let covered = 0, core = 0, bleed = 0;
  for (let y = Math.max(41, Math.floor(cy - reach)); y < Math.min(h - 1, Math.ceil(cy + reach)); y++) {
    for (let x = Math.max(1, Math.floor(cx - reach)); x < Math.min(w - 1, Math.ceil(cx + reach)); x++) {
      const k = yl(x, y);
      if (k < 20) continue;
      const q = axis(x + 0.5 - cx, y + 0.5 - cy);
      if (q.dist <= innerR + 6) bleed = Math.max(bleed, q.dist - innerR);
      if (k < 60) continue;
      let interior = true;
      for (let dy = -1; dy <= 1 && interior; dy++) for (let dx = -1; dx <= 1; dx++) if (yl(x + dx, y + dy) < 60) { interior = false; break; }
      if (!interior) continue;
      core++;
      const qx = Math.floor(((x + 0.5) / scale) * L.dpr);
      const qy = Math.floor(((y + 0.5) / scale) * L.dpr);
      if (qx >= 0 && qy >= 0 && qx < cw && qy < ch && ink[(qy * cw + qx) * 4 + 3] > 128) covered++;
    }
  }
  Object.assign(out, { core, covered, bleed: r2(bleed) });
  return out;
}

// rVFC callbacks run in registration order, which the overlay may change, so
// pair the frame detection and the overlay draw by track time instead.
let pendingBall = null;
let pendingDraw = null;

function pairUp() {
  if (!pendingBall || !pendingDraw || Math.abs(pendingBall.t - pendingDraw.t) > 1e-6) return;
  const { ball } = pendingBall;
  const { last, mode } = pendingDraw;
  pendingBall = pendingDraw = null;
  if (mode !== "interp") measure.nearest++; // same-frame rows; counted in the error too
  const L = overlay._layout;
  const scale = video.videoWidth / L.w; // CSS px -> video px
  const err = Math.hypot(last.cx * scale - ball.x, last.cy * scale - ball.y);
  measure.frames++;
  measure.sum += err;
  measure.max = Math.max(measure.max, err);
  measure.last = err;
  if (measure.samples.length < 2000) measure.samples.push({ t: overlay.stats.lastTime, err });
}

function onFrame(_now, meta) {
  video.requestVideoFrameCallback(onFrame);
  const ball = detectBall();
  pendingBall = ball ? { t: meta.mediaTime + overlay.timeOffset, ball } : null;
  pairUp();
}

overlay.onDraw = (t, last) => {
  pendingDraw = last ? { t, last, mode: overlay.sample.mode } : null;
  pairUp();
};

function readout() {
  const st = overlay.stats;
  $("o-src").textContent = st.source || "(waiting)";
  $("o-t").textContent = Number.isFinite(st.lastTime) ? st.lastTime.toFixed(3) : "-";
  $("o-mode").textContent = st.lastMode || "-";
  $("o-drawn").textContent = `${st.drawn} of ${st.frames} frames`;
  $("o-ring").textContent = st.last ? `${st.last.kind}, R ${st.last.radius.toFixed(2)} px, w ${st.last.width.toFixed(2)} px${st.last.kind === "stadium" ? `, half ${st.last.half.toFixed(1)} px, ${(st.last.angle * 180 / Math.PI).toFixed(0)}°` : ""}, ${st.last.ring}` : "-";
  $("o-hidden").textContent = st.hiddenReason || "no";
  if (synthetic && measure.frames) {
    const mean = measure.sum / measure.frames;
    const el = $("o-err");
    el.textContent = `last ${measure.last.toFixed(2)}, mean ${mean.toFixed(2)}, max ${measure.max.toFixed(2)} px (${measure.frames} frames, ${measure.nearest} from a same-frame row)`;
    el.className = measure.max < 1.5 ? "good" : "bad";
  } else if (!synthetic) {
    $("o-err").textContent = "synthetic clip only";
  }
  $("clock").textContent = `${clock(video.currentTime)} (track ${clock(video.currentTime + overlay.timeOffset)})`;
  if (video.duration && document.activeElement !== $("scrub")) $("scrub").value = String(video.currentTime / video.duration);
  requestAnimationFrame(readout);
}
requestAnimationFrame(readout);

// ---- load ------------------------------------------------------------------------

async function load() {
  if (!cfg.video || !cfg.track) {
    $("err").textContent = "Set a video and a track URL.";
    return;
  }
  video.src = cfg.video;
  video.playbackRate = Number(cfg.rate);
  video.addEventListener("error", () => { $("err").textContent = `Couldn't load the video: ${cfg.video}`; }, { once: true });
  video.addEventListener("loadedmetadata", () => { if (cfg.t) video.currentTime = cfg.t; }, { once: true });
  try {
    const res = await fetch(cfg.track, { cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const track = parseTrack(await res.json());
    if (cfg.truth) truth = await fetch(cfg.truth, { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    overlay.setTrack(track);
    // Registered after the overlay's callback so it sees this frame's stats.
    if (synthetic && !cfg.fallback) video.requestVideoFrameCallback(onFrame);
    window.harness.track = track;
  } catch (err) {
    $("err").textContent = `Couldn't load the track (${cfg.track}): ${err.message}`;
  }
}

window.harness = { overlay, video, measure, cfg, detectBall, hugCheck, resetMeasure: () => Object.assign(measure, { frames: 0, nearest: 0, sum: 0, max: 0, last: NaN, samples: [] }) };
load();
