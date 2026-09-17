// Canvas overlay that draws the ball ring on top of a <video>, synced to the
// presented frame. Used both by the YouTube content script and dev/harness.html.
import { contentRect, ringGeometry } from "./geometry.js";
import { createSample, sampleTrack } from "./track.js";
import { DEFAULT_SETTINGS } from "./settings.js";

const LAYOUT_RECHECK_MS = 400; // safety net for moves no observer reports
const MAX_FRAME_S = 0.1; // longest frame duration we expect (10 fps)
const FLAG_NAMES = ["interp", "bounce", "hit"];

export class Overlay {
  /**
   * @param {object} o
   * @param {HTMLVideoElement} o.video
   * @param {HTMLElement} [o.host] element that receives the canvas (default: video.parentElement)
   * @param {() => string} [o.suppressed] returns a non-empty reason to hide (ads, miniplayer, ...);
   *   evaluated on layout checks, not every frame
   * @param {Element[]} [o.watch] elements whose class changes should re-evaluate `suppressed` at once (the player)
   * @param {number} [o.timeOffset] added to media time before sampling (harness clips)
   * @param {object} [o.settings]
   */
  constructor({ video, host, suppressed, watch = [], timeOffset = 0, settings } = {}) {
    this.video = video;
    this.host = host || video.parentElement;
    this.suppressed = suppressed || (() => "");
    this.timeOffset = timeOffset;
    this.settings = { ...DEFAULT_SETTINGS, ...settings };
    this.track = null;
    this.sample = createSample();
    this.stats = { frames: 0, drawn: 0, lastTime: NaN, lastMode: "", source: "", hiddenReason: "", last: null };
    this._lastDraw = { cx: 0, cy: 0, radius: 0, width: 0, ring: "" }; // reused every frame
    this._ring = { radius: 0, width: 0, clamped: false };

    this._dirty = true;
    this._lastLayoutCheck = 0;
    this._layout = { left: 0, top: 0, w: 0, h: 0, dpr: 1, visible: false };
    this._painted = false;
    this._running = false;
    this._vfcHandle = 0;
    this._rafHandle = 0;
    this._pendingRedraw = 0;
    this._lastVfcAt = 0;
    this._lastMediaTime = NaN; // PTS of the frame on screen, from rVFC
    this._lastRearm = 0;
    /** Optional hook for tools: (trackTime, stats.last) after each render. */
    this.onDraw = null;
    this._hasVFC = typeof video.requestVideoFrameCallback === "function";

    const c = document.createElement("canvas");
    c.className = "tbh-overlay";
    c.setAttribute("aria-hidden", "true");
    Object.assign(c.style, {
      position: "absolute",
      left: "0px",
      top: "0px",
      width: "0px",
      height: "0px",
      pointerEvents: "none",
      zIndex: "1",
      visibility: "hidden",
    });
    this.canvas = c;
    this.ctx = c.getContext("2d");
    this.host.appendChild(c);

    this._onVFC = this._onVFC.bind(this);
    this._onRAF = this._onRAF.bind(this);
    this._invalidate = this._invalidate.bind(this);
    this._onPlay = this._onPlay.bind(this);
    this._redrawNow = this._redrawNow.bind(this);

    this._ro = new ResizeObserver(this._invalidate);
    this._ro.observe(video);
    this._ro.observe(this.host);
    this._mo = new MutationObserver(this._invalidate);
    this._mo.observe(video, { attributes: true, attributeFilter: ["style", "class"] });
    for (const el of watch) this._mo.observe(el, { attributes: true, attributeFilter: ["class"] });
    window.addEventListener("resize", this._invalidate);
    document.addEventListener("fullscreenchange", this._invalidate);
    for (const ev of ["resize", "loadedmetadata", "seeked", "pause"]) video.addEventListener(ev, this._invalidate);
    video.addEventListener("play", this._onPlay);
  }

  setTrack(track) {
    this.track = track;
    this.sample = createSample();
    this._sync();
  }

  setSettings(settings) {
    this.settings = { ...this.settings, ...settings };
    this._sync();
  }

  setTimeOffset(sec) {
    this.timeOffset = Number(sec) || 0;
    this._invalidate();
  }

  /** Call when something outside the observers may have moved the video. */
  refresh() {
    this._invalidate();
  }

  destroy() {
    this._stop();
    this._ro.disconnect();
    this._mo.disconnect();
    window.removeEventListener("resize", this._invalidate);
    document.removeEventListener("fullscreenchange", this._invalidate);
    for (const ev of ["resize", "loadedmetadata", "seeked", "pause"]) this.video.removeEventListener(ev, this._invalidate);
    this.video.removeEventListener("play", this._onPlay);
    if (this._pendingRedraw) cancelAnimationFrame(this._pendingRedraw);
    this.canvas.remove();
    this.track = null;
  }

  get active() {
    return Boolean(this.track && this.settings.enabled);
  }

  // ---- loop -------------------------------------------------------------

  _sync() {
    if (this.active) this._start();
    else {
      this._stop();
      this._clear();
      this.canvas.style.visibility = "hidden";
      this._layout.visible = false;
    }
    this._invalidate();
  }

  _start() {
    if (this._running) return;
    this._running = true;
    if (this._hasVFC) this._vfcHandle = this.video.requestVideoFrameCallback(this._onVFC);
    else this._rafHandle = requestAnimationFrame(this._onRAF);
  }

  _stop() {
    this._running = false;
    if (this._vfcHandle && this._hasVFC) this.video.cancelVideoFrameCallback(this._vfcHandle);
    if (this._rafHandle) cancelAnimationFrame(this._rafHandle);
    this._vfcHandle = this._rafHandle = 0;
  }

  _onVFC(_now, metadata) {
    if (!this._running) return;
    this._vfcHandle = this.video.requestVideoFrameCallback(this._onVFC);
    this._lastVfcAt = performance.now();
    this._lastMediaTime = metadata.mediaTime;
    this.stats.source = "rVFC";
    this._render(metadata.mediaTime);
  }

  _onRAF() {
    if (!this._running) return;
    this._rafHandle = requestAnimationFrame(this._onRAF);
    if (this.video.paused && !this._dirty) return;
    this.stats.source = "rAF";
    this._render(this.video.currentTime);
  }

  _onPlay() {
    this._invalidate();
  }

  /** Re-arm the rVFC chain if frames are playing but no callback arrived lately. */
  _checkStall(now) {
    if (!this._running || !this._hasVFC || this.video.paused || this.video.readyState < 3) return;
    if (now - this._lastVfcAt > 1000 && now - this._lastRearm > 1000) {
      this._lastRearm = now;
      this.video.cancelVideoFrameCallback(this._vfcHandle);
      this._vfcHandle = this.video.requestVideoFrameCallback(this._onVFC);
    }
  }

  _invalidate() {
    this._dirty = true;
    // rVFC does not fire while paused, so redraw once on the next frame.
    if (this.active && !this._pendingRedraw) this._pendingRedraw = requestAnimationFrame(this._redrawNow);
  }

  _redrawNow() {
    this._pendingRedraw = 0;
    if (!this.active) return;
    const v = this.video;
    if (!this._hasVFC) {
      if (v.paused || v.ended) this._render(v.currentTime);
    } else if (v.paused || v.ended || v.readyState < 2) {
      // The paused frame's PTS can be up to a frame before currentTime, so
      // prefer it when it plausibly belongs to this position. After a seek
      // elsewhere, rVFC delivers the new frame's time shortly and redraws.
      const d = v.currentTime - this._lastMediaTime;
      this._render(d >= 0 && d < MAX_FRAME_S ? this._lastMediaTime : v.currentTime);
    } else {
      const now = performance.now();
      this._updateLayout(now);
      this._checkStall(now);
    }
  }

  // ---- layout -----------------------------------------------------------

  _updateLayout(now) {
    this._dirty = false;
    this._lastLayoutCheck = now;
    const v = this.video;
    const c = this.canvas;
    const L = this._layout;
    const parent = c.offsetParent;
    const reason = this.suppressed();
    let cr = null;
    let vr = null;
    if (!reason && parent && v.isConnected) {
      vr = v.getBoundingClientRect();
      cr = contentRect(vr.width, vr.height, v.videoWidth, v.videoHeight, getComputedStyle(v).objectFit || "contain");
    }
    if (!cr || cr.w < 2 || cr.h < 2) {
      if (L.visible) {
        c.style.visibility = "hidden";
        L.visible = false;
      }
      this.stats.hiddenReason = reason || "no-layout";
      return false;
    }
    this.stats.hiddenReason = "";
    const pr = parent.getBoundingClientRect();
    const left = vr.left + cr.x - pr.left - parent.clientLeft;
    const top = vr.top + cr.y - pr.top - parent.clientTop;
    const dpr = window.devicePixelRatio || 1;
    if (left !== L.left || top !== L.top) {
      c.style.left = `${left}px`;
      c.style.top = `${top}px`;
      L.left = left;
      L.top = top;
    }
    if (cr.w !== L.w || cr.h !== L.h || dpr !== L.dpr) {
      c.style.width = `${cr.w}px`;
      c.style.height = `${cr.h}px`;
      c.width = Math.max(1, Math.round(cr.w * dpr));
      c.height = Math.max(1, Math.round(cr.h * dpr));
      L.w = cr.w;
      L.h = cr.h;
      L.dpr = dpr;
      this._painted = true; // resizing cleared the bitmap; force a clean state
    }
    if (!L.visible) {
      c.style.visibility = "visible";
      L.visible = true;
    }
    return true;
  }

  // ---- drawing ----------------------------------------------------------

  _clear() {
    if (!this._painted) return;
    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this._painted = false;
  }

  _render(mediaTime) {
    const now = performance.now();
    if (this._dirty || now - this._lastLayoutCheck > LAYOUT_RECHECK_MS) this._updateLayout(now);
    this._clear();
    const L = this._layout;
    if (!L.visible || !this.track) return;

    const st = this.settings;
    const t = mediaTime + this.timeOffset;
    const s = this.sample;
    const show = sampleTrack(this.track, t, s, st.minConf);
    this.stats.frames++;
    this.stats.lastTime = t;
    this.stats.lastMode = s.mode;

    const ctx = this.ctx;
    ctx.setTransform(L.dpr, 0, 0, L.dpr, 0, 0);

    if (show || (st.debug && s.mode === "lowconf")) {
      const cx = s.x * L.w;
      const cy = s.y * L.h;
      const rPx = s.r * L.w;
      const g = ringGeometry(rPx, st.ratio, st.minStroke, this._ring);
      ctx.beginPath();
      ctx.arc(cx, cy, g.radius, 0, Math.PI * 2);
      ctx.lineWidth = g.width;
      if (show) {
        ctx.strokeStyle = s.ring;
        ctx.stroke();
        this.stats.drawn++;
      } else {
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = "rgba(255,255,255,0.6)";
        ctx.stroke();
        ctx.setLineDash([]);
      }
      this._painted = true;
      const d = this._lastDraw;
      d.cx = cx;
      d.cy = cy;
      d.radius = g.radius;
      d.width = g.width;
      d.ring = s.ring;
      this.stats.last = d;
    } else {
      this.stats.last = null;
    }

    if (st.debug) this._drawDebug(t, mediaTime, show);
    if (this.onDraw) this.onDraw(t, this.stats.last);
  }

  _drawDebug(t, mediaTime, show) {
    const s = this.sample;
    const tr = this.track;
    const ctx = this.ctx;
    const flags = FLAG_NAMES.filter((_, i) => s.flags & (1 << i)).join("+") || "-";
    const lines = [
      `t ${t.toFixed(3)}  media ${mediaTime.toFixed(3)}  ${this.stats.source}`,
      `${s.mode}  rows ${s.lo}/${s.hi}  near ${s.row}`,
    ];
    if (s.row >= 0) {
      lines.push(`x ${s.x.toFixed(4)}  y ${s.y.toFixed(4)}  r ${s.r.toFixed(4)}`);
      lines.push(`conf ${s.conf.toFixed(2)}  ring ${s.ring}  flags ${flags}${show ? "" : "  (hidden)"}`);
      if (s.lo >= 0) lines.push(`row t ${tr.t[s.lo].toFixed(3)}${s.hi >= 0 ? ` .. ${tr.t[s.hi].toFixed(3)}` : ""}  fps ${tr.fps}`);
    }
    ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textBaseline = "top";
    let w = 0;
    for (const l of lines) w = Math.max(w, ctx.measureText(l).width);
    ctx.fillStyle = "rgba(0,0,0,0.72)";
    ctx.fillRect(8, 8, w + 12, lines.length * 15 + 8);
    ctx.fillStyle = "#e8ff5a";
    lines.forEach((l, i) => ctx.fillText(l, 14, 13 + i * 15));
    this._painted = true;
  }
}
