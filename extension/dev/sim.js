// Page sim: runs app.js (the same controller the content script uses) on a
// fake YouTube DOM, talking straight to a (mock) service over CORS.
// Query params: v (video id), api (service base, default http://127.0.0.1:8766), offset.
import { createApp } from "../src/app.js";
import { createClient } from "../src/client.js";
import { DEFAULT_SETTINGS } from "../src/settings.js";

const q = new URLSearchParams(location.search);
const API = q.get("api") || "http://127.0.0.1:8766";
const OFFSET = Number(q.get("offset") ?? 100);
const CLIP = q.get("clip") || "synthetic/synthetic.mp4";
if (!q.get("v")) {
  q.set("v", "SYNTHETIC00");
  history.replaceState(null, "", `?${q}`);
}
document.getElementById("api").textContent = API;

const store = {
  async get(key) {
    try { return JSON.parse(localStorage.getItem(`tbh:${key}`) ?? "null") ?? undefined; } catch { return undefined; }
  },
  async set(key, value) {
    try { localStorage.setItem(`tbh:${key}`, JSON.stringify(value)); } catch { /* ignore */ }
  },
};

const $ = (s) => document.querySelector(s);
const player = $("#movie_player");
let video = $("video.html5-main-video");

function setupVideo(v, t = 5) {
  v.src = CLIP;
  v.addEventListener("loadedmetadata", () => { v.currentTime = t; }, { once: true });
}
setupVideo(video);

const app = createApp({
  client: createClient(fetch.bind(window), API),
  store,
  settings: { ...DEFAULT_SETTINGS, debug: q.get("debug") === "1" },
  timeOffset: OFFSET,
  videoId: () => new URLSearchParams(location.search).get("v"),
  findPlayer: () => {
    const p = document.querySelector("#movie_player");
    const v = p && p.querySelector("video.html5-main-video");
    return v ? { player: p, video: v } : null;
  },
  suppressed: (p, v) => {
    if (p.classList.contains("ad-showing")) return "ad";
    if (document.querySelector("ytd-app").hasAttribute("miniplayer-is-active")) return "miniplayer";
    if (document.pictureInPictureElement === v) return "pip";
    return "";
  },
  report: (s) => { $("#state").textContent = JSON.stringify(s, null, 2); },
});
app.start();
window.sim = { app, store };

// ---- fake YouTube behaviour ----------------------------------------------------

function navigate(id) {
  const p = new URLSearchParams(location.search);
  p.set("v", id);
  history.pushState(null, "", `?${p}`);
  setupVideo(video); // YouTube reuses the same <video> element across navigations
  document.dispatchEvent(new CustomEvent("yt-navigate-finish"));
  app.navigated();
}
document.querySelectorAll("[data-nav]").forEach((b) => b.addEventListener("click", () => navigate(b.dataset.nav)));
window.addEventListener("popstate", () => app.navigated());

$("#replace").addEventListener("click", () => {
  const t = video.currentTime;
  const nv = video.cloneNode(false);
  nv.removeAttribute("src");
  video.replaceWith(nv);
  video = nv;
  setupVideo(nv, t);
  setTimeout(() => app.navigated(), 50);
});
$("#ad").addEventListener("click", () => player.classList.toggle("ad-showing"));
$("#mini").addEventListener("click", () => document.querySelector("ytd-app").toggleAttribute("miniplayer-is-active"));
$("#theater").addEventListener("click", () => document.querySelector("ytd-watch-flexy").toggleAttribute("theater"));
$("#autohide").addEventListener("click", () => player.classList.toggle("ytp-autohide"));
$("#undismiss").addEventListener("click", () => { localStorage.removeItem("tbh:dismissed"); app.recheck(); });
$("#yt-play").addEventListener("click", () => (video.paused ? video.play() : video.pause()));

// YouTube-style global hotkeys: record any that reach the document.
const seen = [];
document.addEventListener("keydown", (e) => {
  if (e.target !== document.body && !(e.target instanceof HTMLElement && e.target.closest(".tbh-prompt-host"))) return;
  seen.push(e.key);
  $("#keys").textContent = seen.slice(-20).join(" ");
  if (e.key === "k") $("#yt-play").click();
});
window.sim.seenKeys = seen;

setInterval(() => {
  if (video.duration) $("#yt-prog").style.width = `${(video.currentTime / video.duration) * 100}%`;
}, 250);
