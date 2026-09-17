import { DEFAULT_SETTINGS, loadSettings, saveSettings } from "./settings.js";
import { formatClock, videoIdFromUrl } from "./geometry.js";

const $ = (id) => document.getElementById(id);
const STAGES = {
  queued: "Waiting in queue",
  download: "Downloading video",
  decode: "Decoding frames",
  detect: "Finding the ball",
  track: "Tracking the ball",
  finalize: "Finishing up",
  done: "Done",
};

let tab = null;
let serviceUp = null;
let refreshTimer = 0;

function send(msg) {
  return chrome.runtime.sendMessage(msg).catch((e) => ({ ok: false, error: "internal", detail: String(e) }));
}

function sendTab(msg) {
  if (!tab) return Promise.resolve(null);
  return chrome.tabs.sendMessage(tab.id, msg).catch(() => null);
}

// ---- settings ------------------------------------------------------------------

const fmt = {
  ratio: (v) => `${v.toFixed(2)}×`,
  minStroke: (v) => `${v.toFixed(2).replace(/0$/, "")} px`,
  minConf: (v) => `${Math.round(v * 100)}%`,
};

function paintRange(input) {
  const min = Number(input.min);
  const max = Number(input.max);
  input.style.setProperty("--fill", `${((Number(input.value) - min) / (max - min)) * 100}%`);
  $(`${input.id}-out`).textContent = fmt[input.id](Number(input.value));
}

function applySettings(s) {
  $("enabled").checked = s.enabled;
  $("autoOffer").checked = s.autoOffer;
  $("debug").checked = s.debug;
  for (const k of ["ratio", "minStroke", "minConf"]) {
    $(k).value = String(s[k]);
    paintRange($(k));
  }
}

async function initSettings() {
  applySettings(await loadSettings());
  for (const k of ["enabled", "autoOffer", "debug"]) {
    $(k).addEventListener("change", (e) => saveSettings({ [k]: e.target.checked }).then(() => renderVideo()));
  }
  for (const k of ["ratio", "minStroke", "minConf"]) {
    const input = $(k);
    input.addEventListener("input", () => {
      paintRange(input);
      saveSettings({ [k]: Number(input.value) });
    });
  }
  $("reset").addEventListener("click", async () => {
    await chrome.storage.sync.set({ settings: { ...DEFAULT_SETTINGS } });
    applySettings(await loadSettings());
  });
}

// ---- service + video -----------------------------------------------------------

async function checkService() {
  const res = await send({ type: "health" });
  serviceUp = Boolean(res && res.ok);
  $("svc-dot").className = `dot ${serviceUp ? "up" : "down"}`;
  if (serviceUp) {
    const d = res.data || {};
    $("svc-text").textContent = `Helper service ${d.version || ""} connected${d.detector ? ` (${d.detector})` : ""}`;
  } else {
    $("svc-text").textContent = "Helper service not running";
  }
}

function button(label, onClick, primary = false) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = primary ? "btn primary" : "btn";
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

let lastKey = "";

function setVideo({ lead, sub = "", subErr = false, start = false, progress = null, actions = [], segments = null }) {
  // Re-rendering every refresh would steal keyboard focus from the buttons.
  const key = JSON.stringify([lead, sub, subErr, start, progress, segments, actions.map((b) => b.textContent)]);
  if (key === lastKey) return;
  lastKey = key;
  $("v-lead").textContent = lead;
  const s = $("v-sub");
  s.hidden = !sub;
  s.textContent = sub;
  s.classList.toggle("err", subErr);
  $("v-start").hidden = !start;
  $("v-progress").hidden = !progress;
  if (progress) {
    const pct = Math.round((progress.progress || 0) * 100);
    $("p-stage").textContent = STAGES[progress.stage] || "Working";
    $("p-pct").textContent = `${pct}%`;
    $("p-bar").style.width = `${pct}%`;
    $("p-bar").parentElement.setAttribute("aria-valuenow", String(pct));
  }
  const old = $("video").querySelector(".segs");
  if (old) old.remove();
  if (segments && segments.length) {
    const ul = document.createElement("ul");
    ul.className = "segs";
    ul.setAttribute("aria-label", "Tracked ranges");
    for (const seg of segments.slice(0, 12)) {
      const li = document.createElement("li");
      li.textContent = `${formatClock(seg.start)} to ${formatClock(seg.end)}`;
      ul.append(li);
    }
    if (segments.length > 12) {
      const li = document.createElement("li");
      li.textContent = `+${segments.length - 12} more`;
      ul.append(li);
    }
    $("v-progress").before(ul);
  }
  $("v-actions").replaceChildren(...actions);
}

const openPrompt = async () => {
  const r = await sendTab({ type: "tbh:openPrompt" });
  if (r && r.ok) window.close();
};

async function renderVideo() {
  const videoId = tab && tab.url ? videoIdFromUrl(tab.url) : null;
  if (serviceUp === false) {
    setVideo({
      lead: "The helper service isn't running",
      sub: "The extension needs it to find and track the ball.",
      start: true,
      actions: [button("Check again", async () => {
        await checkService();
        if (serviceUp) await sendTab({ type: "tbh:recheck" });
        setTimeout(renderVideo, 300);
      }, true)],
    });
    return;
  }
  if (!videoId) {
    setVideo({ lead: "Open a YouTube video", sub: "The ring appears on watch pages that have tracking." });
    return;
  }
  const res = await sendTab({ type: "tbh:getState" });
  if (!res || !res.ok) {
    setVideo({
      lead: "Reload the page to start",
      sub: "This tab was open before the extension loaded.",
      actions: [button("Reload tab", () => { chrome.tabs.reload(tab.id); window.close(); }, true)],
    });
    return;
  }
  const st = res.data;
  switch (st.status) {
    case "loading":
    case "idle":
      setVideo({ lead: "Checking this video…" });
      break;
    case "none":
      setVideo({
        lead: "No tracking for this video yet",
        sub: st.dismissed ? "You chose not to be asked on this video." : "Track the whole video or just a rally.",
        actions: [button("Create tracking", openPrompt, true)],
      });
      break;
    case "job":
      setVideo({ lead: "Creating ball tracking", sub: st.job && st.job.message, progress: st.job || {} });
      break;
    case "ready": {
      const total = formatClock(st.covered);
      let sub = st.enabled ? `${total} tracked.` : `${total} tracked. The ring is switched off.`;
      if (st.hidden === "ad") sub = "Hidden while an ad plays.";
      else if (st.hidden === "miniplayer") sub = "Hidden in the miniplayer.";
      setVideo({
        lead: "Ball highlighting is ready",
        sub,
        segments: st.segments,
        actions: [button("Track another part", openPrompt)],
      });
      break;
    }
    case "failed":
      setVideo({ lead: "Tracking failed", sub: st.error, subErr: true, actions: [button("Try again", openPrompt, true)] });
      break;
    default:
      if (st.service === "down") {
        serviceUp = false;
        return renderVideo();
      }
      setVideo({ lead: "Something went wrong", sub: st.error || "Unknown error.", subErr: true, actions: [button("Check again", () => sendTab({ type: "tbh:recheck" }).then(() => setTimeout(renderVideo, 500)))] });
  }
}

async function init() {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  $("copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("cmd").textContent);
      $("copy").setAttribute("aria-label", "Copied");
      $("copy").title = "Copied";
    } catch {
      /* clipboard unavailable */
    }
  });
  await initSettings();
  await checkService();
  await renderVideo();
  refreshTimer = setInterval(async () => {
    await checkService();
    await renderVideo();
  }, 1500);
}

window.addEventListener("unload", () => clearInterval(refreshTimer));
init();
