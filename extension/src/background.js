// Service worker: the only place that talks to the local helper service.
// Content scripts on youtube.com cannot fetch http://127.0.0.1 themselves
// (mixed content / Private Network Access), so they message us instead.
import { createClient, SERVICE_BASE } from "./client.js";

const client = createClient(fetch.bind(globalThis));

// Per-tab UI state reported by content scripts, used for the action badge and popup.
const tabState = new Map();

const BADGE = {
  ready: { text: "on", color: "#3b3b3b" },
  job: { text: "", color: "#3b3b3b" },
  failed: { text: "!", color: "#b3261e" },
  none: { text: "", color: "#3b3b3b" },
};

function setBadge(tabId, state) {
  if (tabId == null) return;
  let b = BADGE[state.kind] || BADGE.none;
  if (state.kind === "job") b = { ...b, text: `${Math.round((state.progress || 0) * 100)}%` };
  if (state.kind === "ready" && !state.enabled) b = { ...b, text: "off" };
  chrome.action.setBadgeText({ tabId, text: b.text }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color: b.color }).catch(() => {});
  if (chrome.action.setBadgeTextColor) chrome.action.setBadgeTextColor({ tabId, color: "#e8ff5a" }).catch(() => {});
}

const handlers = {
  health: () => client.health(),
  getVideo: (m) => client.getVideo(m.videoId),
  getTrack: (m) => client.getTrack(m.videoId),
  listVideos: () => client.listVideos(),
  createJob: (m) => client.createJob(m.url, m.start, m.end),
  getJob: (m) => client.getJob(m.jobId),
  deleteVideo: (m) => client.deleteVideo(m.videoId),
  serviceInfo: () => ({ ok: true, data: { base: SERVICE_BASE } }),
  reportState: (m, sender) => {
    const tabId = sender.tab && sender.tab.id;
    if (tabId != null) {
      tabState.set(tabId, m.state);
      setBadge(tabId, m.state);
    }
    return { ok: true };
  },
  getTabState: (m) => ({ ok: true, data: tabState.get(m.tabId) || null }),
};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const h = msg && handlers[msg.type];
  if (!h) return false;
  Promise.resolve()
    .then(() => h(msg, sender))
    .then(sendResponse, (err) => sendResponse({ ok: false, error: "internal", detail: String(err && err.message || err) }));
  return true; // async response
});

chrome.tabs.onRemoved.addListener((tabId) => tabState.delete(tabId));
