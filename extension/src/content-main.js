// YouTube integration: builds the chrome.* platform for app.js and wires
// SPA navigation, settings and popup messages.
import { createApp } from "./app.js";
import { videoIdFromUrl } from "./geometry.js";
import { loadSettings, onSettingsChanged } from "./settings.js";

function send(msg) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (res) => {
        if (chrome.runtime.lastError || !res) {
          resolve({ ok: false, error: "internal", detail: chrome.runtime.lastError?.message || "no response" });
        } else resolve(res);
      });
    } catch (err) {
      // Extension reloaded/updated: this content script is orphaned.
      resolve({ ok: false, error: "internal", detail: String(err) });
    }
  });
}

// Same call shape as client.js, routed through the service worker.
const client = {
  health: () => send({ type: "health" }),
  getVideo: (videoId) => send({ type: "getVideo", videoId }),
  getTrack: (videoId) => send({ type: "getTrack", videoId }),
  getJob: (jobId) => send({ type: "getJob", jobId }),
  createJob: (url, start, end) => send({ type: "createJob", url, start, end }),
};

const store = {
  async get(key) {
    try {
      return (await chrome.storage.local.get(key))[key];
    } catch {
      return undefined;
    }
  },
  async set(key, value) {
    try {
      await chrome.storage.local.set({ [key]: value });
    } catch {
      /* orphaned or quota: ignore */
    }
  },
};

function findPlayer() {
  const player = document.querySelector("#movie_player");
  if (!player) return null;
  const video = player.querySelector("video.html5-main-video") || player.querySelector("video");
  return video ? { player, video } : null;
}

let ytdApp = null;

function suppressed(player, video) {
  if (player.classList.contains("ad-showing")) return "ad";
  if (!ytdApp || !ytdApp.isConnected) ytdApp = document.querySelector("ytd-app");
  const app = ytdApp;
  if ((app && app.hasAttribute("miniplayer-is-active")) || player.closest("ytd-miniplayer")) return "miniplayer";
  if (document.pictureInPictureElement === video) return "pip";
  if (document.hidden) return "tab-hidden";
  return "";
}

export async function main() {
  if (window.__tbhStarted) return;
  window.__tbhStarted = true;

  const settings = await loadSettings();
  let lastReport = "";
  const app = createApp({
    client,
    store,
    settings,
    findPlayer,
    suppressed,
    videoId: () => videoIdFromUrl(location.href),
    report: (state) => {
      const key = `${state.videoId}|${state.kind}|${Math.round((state.progress || 0) * 100)}|${state.enabled}`;
      if (key === lastReport) return;
      lastReport = key;
      send({ type: "reportState", state });
    },
  });
  app.start();
  // Handle for debugging from DevTools (this content script's context only).
  globalThis.__tbh = app;

  onSettingsChanged((s) => app.setSettings(s));

  const nav = () => app.navigated();
  document.addEventListener("yt-navigate-finish", nav);
  document.addEventListener("yt-page-data-updated", nav);
  window.addEventListener("popstate", nav);
  document.addEventListener("fullscreenchange", () => app.overlay?.refresh());
  document.addEventListener("visibilitychange", () => app.overlay?.refresh());
  // Theater mode / miniplayer toggles change layout without a navigation.
  const flexy = () => document.querySelector("ytd-watch-flexy");
  const appEl = document.querySelector("ytd-app");
  const mo = new MutationObserver(() => app.overlay?.refresh());
  if (appEl) mo.observe(appEl, { attributes: true, attributeFilter: ["miniplayer-is-active"] });
  const watchFlexy = () => {
    const f = flexy();
    if (f) mo.observe(f, { attributes: true, attributeFilter: ["theater", "fullscreen", "hidden"] });
  };
  watchFlexy();
  document.addEventListener("yt-navigate-finish", watchFlexy);

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || typeof msg.type !== "string" || !msg.type.startsWith("tbh:")) return false;
    if (msg.type === "tbh:getState") {
      sendResponse({ ok: true, data: app.state() });
    } else if (msg.type === "tbh:openPrompt") {
      app.openPrompt().then((ok) => sendResponse({ ok }));
      return true;
    } else if (msg.type === "tbh:recheck") {
      app.recheck();
      sendResponse({ ok: true });
    } else {
      return false;
    }
    return false;
  });
}
