// Dev harness: runs the real overlay.js against a local clip + track.
// Query params: video, track, offset, box (16x9|4x3|21x9|tall), debug=1,
// fallback=1 (force rAF), t (start at local seconds), rate, preset (synthetic|real).
import { Overlay } from "../src/overlay.js";
import { parseTrack } from "../src/track.js";

const PRESETS = {
  synthetic: { video: "synthetic/synthetic.mp4", track: "synthetic/synthetic.track.json", offset: 100 },
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
};
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
  const s = { enabled: $("enabled").checked, debug: $("debug").checked };
  for (const k of ["ratio", "minStroke", "minConf"]) {
    s[k] = Number($(k).value);
    $(`${k}Out`).textContent = fmtNum[k](s[k]);
  }
  overlay.setSettings(s);
}
for (const id of ["enabled", "debug", "ratio", "minStroke", "minConf"]) $(id).addEventListener("input", pushSettings);
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
$("scrub").addEventListener("input", () => { video.currentTime = Number($("scrub").value) * (video.duration || 0); });

function clock(t) {
  const m = Math.floor(t / 60);
  return `${m}:${(t - m * 60).toFixed(3).padStart(6, "0")}`;
}

// ---- measurement (synthetic clip: yellow ball on green/white) -----------------

const measure = { frames: 0, nearest: 0, sum: 0, max: 0, last: NaN, samples: [] };
const probe = document.createElement("canvas");
const pctx = probe.getContext("2d", { willReadFrequently: true });

function detectBall() {
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (!w || !h) return null;
  if (probe.width !== w) { probe.width = w; probe.height = h; }
  pctx.drawImage(video, 0, 0, w, h);
  const d = pctx.getImageData(0, 0, w, h).data;
  let sx = 0, sy = 0, n = 0;
  for (let y = 40; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      if (d[i] > 150 && d[i + 1] > 180 && d[i + 2] < 140) { sx += x; sy += y; n++; }
    }
  }
  // Pixel-index convention, same as the generator (cv2): pixel i is centred at i.
  return n > 20 ? { x: sx / n, y: sy / n, n } : null;
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
  // "nearest" frames show a row up to 1/fps old by design (spec rule 1), so
  // they measure the rule, not sync. Count them separately.
  if (mode !== "interp") {
    measure.nearest++;
    return;
  }
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
  $("o-ring").textContent = st.last ? `r ${st.last.radius.toFixed(2)} px, w ${st.last.width.toFixed(2)} px, ${st.last.ring}` : "-";
  $("o-hidden").textContent = st.hiddenReason || "no";
  if (synthetic && measure.frames) {
    const mean = measure.sum / measure.frames;
    const el = $("o-err");
    el.textContent = `last ${measure.last.toFixed(2)}, mean ${mean.toFixed(2)}, max ${measure.max.toFixed(2)} px (${measure.frames} interpolated frames; ${measure.nearest} nearest-row frames not counted)`;
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
    overlay.setTrack(track);
    // Registered after the overlay's callback so it sees this frame's stats.
    if (synthetic && !cfg.fallback) video.requestVideoFrameCallback(onFrame);
    window.harness.track = track;
  } catch (err) {
    $("err").textContent = `Couldn't load the track (${cfg.track}): ${err.message}`;
  }
}

window.harness = { overlay, video, measure, cfg, detectBall, resetMeasure: () => Object.assign(measure, { frames: 0, nearest: 0, sum: 0, max: 0, last: NaN, samples: [] }) };
load();
