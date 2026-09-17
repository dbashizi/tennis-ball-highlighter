// Page controller: per-video lifecycle, service lookups, job polling, and the
// overlay/prompt wiring. Environment-agnostic: the YouTube content script and
// dev/sim.html inject a `platform` object.
import { Overlay } from "./overlay.js";
import { Prompt } from "./prompt.js";
import { parseTrack, coveredSeconds } from "./track.js";
import { isUnreachable } from "./client.js";

const TICK_MS = 1000;
const POLL_MS = 1000;
const DOWN_RETRY_MS = 60000;
const MAX_POLL_FAILURES = 5;
const DISMISSED_KEY = "dismissed";
const DISMISSED_MAX = 500;

export const START_HINT = "uv run tbh-service run";

/**
 * @param {object} platform
 * @param {object} platform.client       createClient()-shaped API (async, never rejects)
 * @param {object} platform.store        { get(key), set(key, value) } async, local storage
 * @param {() => string|null} platform.videoId
 * @param {() => {player: HTMLElement, video: HTMLVideoElement}|null} platform.findPlayer
 * @param {(player: HTMLElement, video: HTMLVideoElement) => string} platform.suppressed
 * @param {object} platform.settings     initial settings
 * @param {(state: object) => void} [platform.report]
 * @param {(videoId: string) => string} [platform.watchUrl]
 * @param {number} [platform.timeOffset]
 */
export function createApp(platform) {
  const P = {
    report: () => {},
    watchUrl: (id) => `https://www.youtube.com/watch?v=${id}`,
    timeOffset: 0,
    ...platform,
  };
  const trackCache = new Map(); // videoId -> parsed track (per tab, in memory)
  let settings = { ...P.settings };
  let player = null;
  let video = null;
  let overlay = null;
  let prompt = null;
  let tickTimer = 0;
  let pollTimer = 0;
  let downTimer = 0;
  let gen = 0; // bumps on every video change; stale async results are dropped

  const S = {
    videoId: null,
    service: "unknown", // unknown | up | down
    status: "idle", // idle | loading | none | ready | job | failed | error
    record: null,
    job: null,
    error: null,
    track: null,
    dismissed: false,
  };

  // ---- binding to the page ----------------------------------------------------

  function bind() {
    const found = P.findPlayer();
    const nextPlayer = found ? found.player : null;
    const nextVideo = found ? found.video : null;
    if (nextPlayer !== player) {
      const hadPrompt = Boolean(prompt && prompt.state !== "hidden");
      if (prompt) prompt.destroy();
      prompt = null;
      player = nextPlayer;
      if (player) restorePrompt(!hadPrompt);
    }
    const moved = overlay && nextVideo && overlay.canvas.parentElement !== nextVideo.parentElement;
    if (nextVideo !== video || moved) {
      if (overlay) overlay.destroy();
      overlay = null;
      video = nextVideo;
      if (video && player) {
        overlay = new Overlay({
          video,
          host: video.parentElement,
          suppressed: () => (player ? P.suppressed(player, video) : "no-player"),
          watch: [player],
          timeOffset: P.timeOffset,
          settings,
        });
        if (S.track) overlay.setTrack(S.track);
      }
    } else if (overlay) {
      overlay.refresh();
    }
  }

  function adShowing() {
    return Boolean(player && player.classList.contains("ad-showing"));
  }

  function ensurePrompt() {
    if (!player) return null;
    if (!prompt) {
      prompt = new Prompt({
        player,
        getTimes: () => {
          const valid = Boolean(video && player && video.readyState >= 1 && !player.classList.contains("ad-showing"));
          return {
            valid,
            current: valid ? video.currentTime + P.timeOffset : 0,
            duration: valid && Number.isFinite(video.duration) ? video.duration + P.timeOffset : NaN,
          };
        },
        onCreate: (req) => createJob(req),
        onDismiss: () => dismiss(),
      });
    }
    return prompt;
  }

  function restorePrompt(expanded) {
    if (S.status === "job" && S.job) ensurePrompt()?.showProgress(S.job);
    else if (S.status === "none" && !S.dismissed && settings.autoOffer !== false) ensurePrompt()?.showOffer({ expanded });
  }

  function tick() {
    const id = P.videoId();
    if (id !== S.videoId) {
      reset(id);
    }
    bind();
  }

  // ---- per-video state --------------------------------------------------------

  function reset(id) {
    gen++;
    stopPolling();
    clearTimeout(downTimer);
    if (prompt) prompt.hide();
    Object.assign(S, { videoId: id, status: id ? "loading" : "idle", record: null, job: null, error: null, track: null, dismissed: false });
    if (overlay) overlay.setTrack(null);
    emit();
    if (id) load(id, gen);
  }

  function emit() {
    P.report(publicState());
  }

  function publicState() {
    return {
      videoId: S.videoId,
      service: S.service,
      status: S.status,
      error: S.error,
      dismissed: S.dismissed,
      enabled: settings.enabled,
      job: S.job && { stage: S.job.stage, progress: S.job.progress, message: S.job.message, status: S.job.status },
      segments: S.track ? S.track.segments : (S.record && S.record.segments) || [],
      covered: S.track ? coveredSeconds(S.track) : 0,
      rows: S.track ? S.track.length : 0,
      currentTime: video && !adShowing() ? video.currentTime + P.timeOffset : null,
      duration: video && !adShowing() && Number.isFinite(video.duration) ? video.duration + P.timeOffset : null,
      hidden: overlay ? overlay.stats.hiddenReason || "" : "",
      // Flattened for the action badge:
      kind: S.status === "job" ? "job" : S.status === "ready" ? "ready" : S.status === "failed" ? "failed" : "none",
      progress: S.job ? S.job.progress : 0,
    };
  }

  async function isDismissed(id) {
    const map = (await P.store.get(DISMISSED_KEY)) || {};
    return Boolean(map[id]);
  }

  async function dismiss() {
    const id = S.videoId;
    if (!id) return;
    S.dismissed = true;
    const map = (await P.store.get(DISMISSED_KEY)) || {};
    map[id] = Date.now();
    const keys = Object.keys(map);
    if (keys.length > DISMISSED_MAX) {
      keys.sort((a, b) => map[a] - map[b]).slice(0, keys.length - DISMISSED_MAX).forEach((k) => delete map[k]);
    }
    await P.store.set(DISMISSED_KEY, map);
    emit();
  }

  async function undismiss(id) {
    const map = (await P.store.get(DISMISSED_KEY)) || {};
    if (map[id]) {
      delete map[id];
      await P.store.set(DISMISSED_KEY, map);
    }
    S.dismissed = false;
  }

  function serviceDown(g) {
    S.service = "down";
    S.status = "error";
    S.error = "unreachable";
    emit();
    // Retry quietly; never nag in-page while the service is off.
    clearTimeout(downTimer);
    downTimer = setTimeout(() => { if (g === gen && S.videoId) load(S.videoId, g); }, DOWN_RETRY_MS);
  }

  async function load(id, g) {
    const [res, dismissed] = await Promise.all([P.client.getVideo(id), isDismissed(id)]);
    if (g !== gen) return;
    S.dismissed = dismissed;
    if (isUnreachable(res)) return serviceDown(g);
    S.service = "up";

    if (!res.ok && res.status === 404) {
      S.status = "none";
      emit();
      if (!dismissed && settings.autoOffer !== false) ensurePrompt()?.showOffer({ expanded: true });
      return;
    }
    if (!res.ok) {
      S.status = "error";
      S.error = res.detail || res.error;
      emit();
      return;
    }

    const rec = res.data || {};
    S.record = rec;
    const hasTrack = Boolean(rec.track_url) && (rec.status === "ready" || (rec.segments && rec.segments.length));
    if (hasTrack) await loadTrack(id, g, false);
    if (g !== gen) return;

    if ((rec.status === "queued" || rec.status === "processing") && rec.job) {
      startPolling(rec.job, g);
    } else if (rec.status === "failed" && !S.track) {
      S.status = "failed";
      S.error = (rec.job && (rec.job.error || rec.job.message)) || "The last tracking attempt failed.";
      emit();
      if (!dismissed) ensurePrompt()?.showError(S.error);
    } else if (!S.track) {
      S.status = "none";
      emit();
      if (!dismissed && settings.autoOffer !== false) ensurePrompt()?.showOffer({ expanded: true });
    }
  }

  async function loadTrack(id, g, fresh) {
    let track = fresh ? null : trackCache.get(id);
    if (!track) {
      const res = await P.client.getTrack(id);
      if (g !== gen) return false;
      if (isUnreachable(res)) { serviceDown(g); return false; }
      if (!res.ok) {
        S.status = "error";
        S.error = `Couldn't load the tracking data (${res.detail || res.error}).`;
        emit();
        return false;
      }
      try {
        track = parseTrack(res.data);
      } catch (err) {
        S.status = "error";
        S.error = `The tracking data is invalid: ${err.message}`;
        emit();
        return false;
      }
      trackCache.set(id, track);
    }
    S.track = track;
    S.status = "ready";
    S.error = null;
    if (overlay) overlay.setTrack(track);
    emit();
    return true;
  }

  // ---- jobs --------------------------------------------------------------------

  async function createJob(req) {
    const id = S.videoId;
    if (!id) return;
    const g = gen;
    await undismiss(id);
    const p = ensurePrompt();
    p?.showProgress({ stage: "queued", progress: 0, message: "Starting…" });
    const res = await P.client.createJob(P.watchUrl(id), req.start, req.end);
    if (g !== gen) return;
    if (isUnreachable(res)) {
      S.service = "down";
      emit();
      p?.showError(`The helper service isn't running. Start it with "${START_HINT}" in the project folder.`);
      return;
    }
    if (!res.ok) {
      p?.showError(res.detail || `The service refused the request (${res.status || res.error}).`);
      return;
    }
    S.service = "up";
    startPolling(res.data, g);
  }

  function startPolling(job, g) {
    stopPolling();
    S.job = job;
    S.status = "job";
    emit();
    ensurePrompt()?.showProgress(job);
    let failures = 0;
    const step = async () => {
      if (g !== gen) return;
      const res = await P.client.getJob(job.job_id);
      if (g !== gen) return;
      if (!res.ok) {
        failures++;
        if (failures >= MAX_POLL_FAILURES) {
          S.status = "failed";
          S.error = isUnreachable(res) ? "Lost contact with the helper service." : res.detail || "Couldn't read the job status.";
          if (isUnreachable(res)) S.service = "down";
          emit();
          ensurePrompt()?.showError(S.error);
          return;
        }
        pollTimer = setTimeout(step, POLL_MS * failures);
        return;
      }
      failures = 0;
      const j = res.data;
      S.job = j;
      if (j.status === "ready" || j.stage === "done") {
        trackCache.delete(S.videoId);
        const ok = await loadTrack(S.videoId, g, true);
        if (g !== gen) return;
        S.job = null;
        if (ok) ensurePrompt()?.showReady();
        else ensurePrompt()?.showError(S.error);
        emit();
        return;
      }
      if (j.status === "failed") {
        S.job = null;
        S.status = S.track ? "ready" : "failed";
        S.error = j.error || j.message || "Tracking failed.";
        emit();
        ensurePrompt()?.showError(S.error);
        return;
      }
      emit();
      ensurePrompt()?.showProgress(j);
      pollTimer = setTimeout(step, POLL_MS);
    };
    pollTimer = setTimeout(step, POLL_MS);
  }

  function stopPolling() {
    clearTimeout(pollTimer);
    pollTimer = 0;
  }

  // ---- public API --------------------------------------------------------------

  return {
    start() {
      tick();
      tickTimer = setInterval(tick, TICK_MS);
    },
    stop() {
      clearInterval(tickTimer);
      stopPolling();
      clearTimeout(downTimer);
      gen++;
      if (overlay) overlay.destroy();
      if (prompt) prompt.destroy();
      overlay = prompt = player = video = null;
    },
    /** Navigation hint (yt-navigate-finish, popstate...). */
    navigated() {
      tick();
    },
    setSettings(next) {
      const wasOffer = settings.autoOffer;
      settings = { ...settings, ...next };
      if (overlay) overlay.setSettings(settings);
      if (wasOffer !== settings.autoOffer && settings.autoOffer === false && prompt && prompt.state === "offer") prompt.hide();
      emit();
    },
    state: publicState,
    /** Popup asked to show the create prompt (also clears "don't ask again"). */
    async openPrompt() {
      if (!S.videoId) return false;
      bind();
      await undismiss(S.videoId);
      const p = ensurePrompt();
      if (!p) return false;
      if (S.status === "job" && S.job) p.showProgress(S.job);
      else p.showOffer({ expanded: true, title: S.track ? "Track another part of this video?" : undefined });
      emit();
      return true;
    },
    recheck() {
      if (S.videoId) {
        const id = S.videoId;
        S.videoId = null;
        reset(id);
      }
    },
    get overlay() {
      return overlay;
    },
  };
}
