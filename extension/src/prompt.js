// In-player "create tracking" prompt. Lives in a shadow root inside the player,
// top-right, and collapses to a small icon button when idle.
import { defaultRange, formatClock, parseClock } from "./geometry.js";

const AUTO_COLLAPSE_MS = 6000;
const READY_HIDE_MS = 3500;

const STAGES = {
  queued: "Waiting in queue",
  download: "Downloading video",
  decode: "Decoding frames",
  detect: "Finding the ball",
  track: "Tracking the ball",
  finalize: "Finishing up",
  done: "Done",
};

const BALL_SVG = `
<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
  <circle cx="12" cy="12" r="10.2" fill="none" stroke="currentColor" stroke-width="1.8"/>
  <circle cx="12" cy="12" r="7" fill="#d6f23a"/>
  <path d="M5.6 9.3c2.4.5 3.9 1.6 3.9 2.7s-1.5 2.2-3.9 2.7M18.4 9.3c-2.4.5-3.9 1.6-3.9 2.7s1.5 2.2 3.9 2.7"
        fill="none" stroke="#f7fbe6" stroke-width="1.1" stroke-linecap="round"/>
</svg>`;

const CSS = `
:host {
  all: initial;
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 61;
  font-family: "YouTube Sans", Roboto, "Noto Sans", Arial, sans-serif;
  font-size: 13px;
  line-height: 18px;
  color: #f1f1f1;
  -webkit-font-smoothing: antialiased;
  --ball: #d6f23a;
  --panel: rgba(28, 28, 28, 0.92);
  --line: rgba(255, 255, 255, 0.14);
  --muted: rgba(255, 255, 255, 0.66);
  --field: rgba(255, 255, 255, 0.08);
  --danger: #ff8a80;
}
:host-context(.ytp-fullscreen) { top: 72px; right: 24px; }
:host([hidden]) { display: none; }
/* During ads the player's timeline belongs to the ad: stay out of the way. */
:host-context(.ad-showing) { display: none; }
* { box-sizing: border-box; }
button, input { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--ball); outline-offset: 2px; }

.fab {
  position: relative;
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  margin-left: auto;
  border: 0;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  cursor: pointer;
  transition: opacity 0.25s ease, background-color 0.15s ease;
}
.fab:hover { background: rgba(0, 0, 0, 0.8); }
:host-context(.ytp-autohide) .fab:not(:focus-visible) { opacity: 0; }
:host-context(.ytp-autohide) .fab.busy:not(:focus-visible) { opacity: 0.55; }
.fab .arc { position: absolute; inset: 0; transform: rotate(-90deg); pointer-events: none; }
.fab .arc circle { fill: none; stroke-width: 2.5; }
.fab .arc .track { stroke: rgba(255, 255, 255, 0.15); }
.fab .arc .fill { stroke: var(--ball); stroke-linecap: round; transition: stroke-dashoffset 0.4s ease; }
.fab:not(.busy) .arc { display: none; }

.panel {
  width: 292px;
  margin-top: 8px;
  padding: 14px 14px 12px;
  border-radius: 12px;
  background: var(--panel);
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.45);
  transform-origin: top right;
  animation: open 0.16s ease-out;
}
@keyframes open { from { opacity: 0; transform: scale(0.96); } }
@media (prefers-reduced-motion: reduce) {
  .panel { animation: none; }
  .fab, .fab .arc .fill, .bar i { transition: none; }
}
.root:not(.expanded) .panel { display: none; }

.head { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 10px; }
.title { flex: 1; margin: 0; font-size: 14px; font-weight: 500; line-height: 20px; }
.x {
  flex: none; width: 28px; height: 28px; margin: -5px -6px 0 0;
  border: 0; border-radius: 50%; background: transparent; cursor: pointer; color: var(--muted);
}
.x:hover { background: rgba(255, 255, 255, 0.1); color: #fff; }
.x svg { display: block; margin: auto; }

.chips { display: flex; gap: 6px; margin-bottom: 10px; }
.chip {
  height: 28px; padding: 0 12px; border: 0; border-radius: 8px;
  background: rgba(255, 255, 255, 0.1); cursor: pointer; font-weight: 500;
}
.chip:hover { background: rgba(255, 255, 255, 0.18); }
.chip[aria-checked="true"] { background: #f1f1f1; color: #0f0f0f; }

.range { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.range[hidden] { display: none; }
.range label { color: var(--muted); }
.range input {
  width: 68px; height: 30px; padding: 0 8px;
  border: 1px solid transparent; border-radius: 6px; background: var(--field);
  font-variant-numeric: tabular-nums; text-align: center;
}
.range input:focus { outline: none; border-color: var(--ball); }
.range input::placeholder { color: rgba(255, 255, 255, 0.4); }
.range input[aria-invalid="true"] { border-color: var(--danger); }
.range .dash { color: var(--muted); }
.now {
  margin-left: auto; height: 28px; padding: 0 8px; border: 0; border-radius: 6px;
  background: transparent; color: var(--muted); cursor: pointer;
}
.now:hover { color: #fff; background: rgba(255, 255, 255, 0.08); }
.hint { min-height: 18px; margin: 2px 0 8px; color: var(--muted); font-size: 12px; }
.hint.err { color: var(--danger); }

.actions { display: flex; align-items: center; gap: 4px; }
.primary {
  height: 32px; padding: 0 14px; border: 0; border-radius: 16px;
  background: var(--ball); color: #0f0f0f; font-weight: 500; cursor: pointer;
}
.primary:hover { background: #e4ff5c; }
.ghost {
  height: 32px; padding: 0 10px; border: 0; border-radius: 16px;
  background: transparent; color: var(--muted); cursor: pointer;
}
.ghost:hover { background: rgba(255, 255, 255, 0.1); color: #fff; }
.actions .spacer { flex: 1; }

.status { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
.status .stage { flex: 1; font-weight: 500; }
.status .pct { font-variant-numeric: tabular-nums; color: var(--muted); }
.bar { height: 4px; border-radius: 2px; background: rgba(255, 255, 255, 0.15); overflow: hidden; }
.bar i { display: block; height: 100%; width: 0; background: var(--ball); transition: width 0.4s ease; }
.msg { min-height: 18px; margin-top: 8px; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.errtext { margin: 0 0 10px; color: var(--danger); font-size: 12px; overflow-wrap: anywhere; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
`;

const X_SVG = `<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;

function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") e.textContent = v;
    else if (k === "html") e.innerHTML = v; // static, extension-authored markup only
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of children) if (c) e.append(c);
  return e;
}

export class Prompt {
  /**
   * @param {object} o
   * @param {HTMLElement} o.player element to mount in (positioned)
   * @param {() => {current:number, duration:number, valid:boolean}} o.getTimes
   * @param {(req: {start?: number, end?: number}) => void} o.onCreate
   * @param {() => void} o.onDismiss remembered "hide for this video"
   */
  constructor({ player, getTimes, onCreate, onDismiss }) {
    this.getTimes = getTimes;
    this.onCreate = onCreate;
    this.onDismiss = onDismiss;
    this.state = "hidden";
    this.mode = "range";
    this.edited = false;
    this.lastRequest = null;
    this._timer = 0;
    this._hover = false;

    this.hostEl = el("div", { class: "tbh-prompt-host", hidden: true });
    this.root = this.hostEl.attachShadow({ mode: "open" });
    // Keep YouTube's hotkeys (k, f, m, digits...) and click-to-pause away from our controls.
    for (const ev of ["keydown", "keyup", "keypress", "click", "dblclick", "mousedown", "mouseup", "pointerdown", "pointerup", "contextmenu"]) {
      this.root.addEventListener(ev, (e) => e.stopPropagation());
    }
    this.root.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.expanded) {
        e.preventDefault();
        this.collapse(true);
      }
    });
    this.hostEl.addEventListener("mouseenter", () => { this._hover = true; this._clearTimer(); this._refreshDefaults(); });
    this.hostEl.addEventListener("mouseleave", () => { this._hover = false; this._armTimer(); });
    this.root.addEventListener("focusin", (e) => {
      this._clearTimer();
      if (e.target !== this.startIn && e.target !== this.endIn) this._refreshDefaults();
    });
    this.root.addEventListener("focusout", () => this._armTimer());

    const style = el("style", { text: CSS });
    this.fab = el("button", {
      class: "fab",
      type: "button",
      "aria-expanded": "false",
      "aria-label": "Ball highlighter",
      title: "Ball highlighter",
      html: `${BALL_SVG}<svg class="arc" viewBox="0 0 40 40" aria-hidden="true"><circle class="track" cx="20" cy="20" r="18"/><circle class="fill" cx="20" cy="20" r="18" stroke-dasharray="113.1" stroke-dashoffset="113.1"/></svg>`,
      onclick: () => (this.expanded ? this.collapse(true) : this.expand(true)),
    });
    this.arcFill = this.fab.querySelector(".fill");
    this.panel = el("div", { class: "panel", role: "dialog", "aria-label": "Ball highlighter" });
    this.live = el("div", { class: "sr", "aria-live": "polite" });
    this.wrap = el("div", { class: "root" }, [this.fab, this.panel, this.live]);
    this.root.append(style, this.wrap);
    player.appendChild(this.hostEl);
  }

  get expanded() {
    return this.wrap.classList.contains("expanded");
  }

  destroy() {
    this._clearTimer();
    clearInterval(this._staleTimer);
    this.hostEl.remove();
  }

  hide() {
    this._clearTimer();
    this.state = "hidden";
    this.hostEl.hidden = true;
  }

  /** Keep "current time +/- 30 s" current until the user edits the range. */
  _refreshDefaults() {
    if (this.state === "offer" && !this.edited && this.expanded) this._fillDefaults();
  }

  expand(focus = false) {
    if (this.state === "offer" && !this.edited) this._fillDefaults();
    this.wrap.classList.add("expanded");
    this.fab.setAttribute("aria-expanded", "true");
    if (focus) {
      const f = this.panel.querySelector(".primary, .chip, button");
      if (f) f.focus();
    }
    this._armTimer();
  }

  collapse(focusFab = false) {
    this._clearTimer();
    this.wrap.classList.remove("expanded");
    this.fab.setAttribute("aria-expanded", "false");
    if (focusFab) this.fab.focus();
  }

  // ---- states ------------------------------------------------------------

  showOffer({ expanded = true, title = "Highlight the ball in this video?" } = {}) {
    this.state = "offer";
    this._setBusy(false);
    this.edited = false;
    const chip = (mode, label) => el("button", {
      class: "chip",
      type: "button",
      role: "radio",
      "aria-checked": String(this.mode === mode),
      text: label,
      onclick: () => this._setMode(mode),
    });
    this.startIn = el("input", { id: "tbh-start", type: "text", inputmode: "numeric", autocomplete: "off", spellcheck: "false", placeholder: "m:ss", "aria-label": "Start time (m:ss)", oninput: () => this._onEdit() });
    this.endIn = el("input", { id: "tbh-end", type: "text", inputmode: "numeric", autocomplete: "off", spellcheck: "false", placeholder: "m:ss", "aria-label": "End time (m:ss)", oninput: () => this._onEdit() });
    this.chipWhole = chip("whole", "Whole video");
    this.chipRange = chip("range", "Time range");
    this.rangeRow = el("div", { class: "range" }, [
      this.startIn,
      el("span", { class: "dash", text: "to", "aria-hidden": "true" }),
      this.endIn,
      el("button", { class: "now", type: "button", text: "Around now", title: "Current time ± 30 s", onclick: () => { this.edited = false; this._fillDefaults(); this._validate(); } }),
    ]);
    this.hint = el("div", { class: "hint", id: "tbh-hint", "aria-live": "polite" });
    this.startIn.setAttribute("aria-describedby", "tbh-hint");
    this.endIn.setAttribute("aria-describedby", "tbh-hint");
    const form = el("form", {
      onsubmit: (e) => { e.preventDefault(); this._submit(); },
    }, [
      el("div", { class: "chips", role: "radiogroup", "aria-label": "What to track", onkeydown: (e) => this._chipKeys(e) }, [this.chipWhole, this.chipRange]),
      this.rangeRow,
      this.hint,
      el("div", { class: "actions" }, [
        el("button", { class: "primary", type: "submit", text: "Create tracking" }),
        el("span", { class: "spacer" }),
        el("button", { class: "ghost", type: "button", text: "Don't ask again", title: "Don't offer tracking for this video again", onclick: () => this._dismiss() }),
      ]),
    ]);
    this._renderPanel(title, form);
    this._setMode(this.mode);
    this._fillDefaults();
    this._show(expanded);
    clearInterval(this._staleTimer);
    this._staleTimer = setInterval(() => {
      if (this.state !== "offer") clearInterval(this._staleTimer);
      else if (this._stale && !this.edited) this._fillDefaults();
    }, 1000);
  }

  showProgress({ stage, progress, message } = {}) {
    const pct = Math.round(Math.min(1, Math.max(0, Number(progress) || 0)) * 100);
    const label = STAGES[stage] || "Working";
    if (this.state !== "progress") {
      this.state = "progress";
      this.pStage = el("span", { class: "stage" });
      this.pPct = el("span", { class: "pct" });
      this.pBar = el("i");
      this.pMsg = el("div", { class: "msg" });
      const body = el("div", {}, [
        el("div", { class: "status" }, [this.pStage, this.pPct]),
        el("div", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-label": "Tracking progress" }, [this.pBar]),
        this.pMsg,
        el("div", { class: "actions" }, [
          el("span", { class: "spacer" }),
          el("button", { class: "ghost", type: "button", text: "Hide", onclick: () => this.collapse(true) }),
        ]),
      ]);
      this._renderPanel("Creating ball tracking", body);
      this._show(this.expanded || !this.hostEl.hidden ? this.expanded : true);
      this.live.textContent = "Tracking started";
    }
    this.pStage.textContent = label;
    this.pPct.textContent = `${pct}%`;
    this.pBar.style.width = `${pct}%`;
    this.pBar.parentElement.setAttribute("aria-valuenow", String(pct));
    this.pMsg.textContent = message || "";
    this._setBusy(true, pct / 100);
    this.fab.setAttribute("aria-label", `Ball tracking ${pct}%`);
  }

  showReady(text = "Ball highlighting is on") {
    this.state = "ready";
    this._setBusy(false);
    this._renderPanel(text, el("div", { class: "msg", text: "The ring follows the ball where it was tracked." }));
    this._show(true);
    this.live.textContent = text;
    this._clearTimer();
    this._timer = setTimeout(() => { if (!this._hover) this.hide(); }, READY_HIDE_MS);
  }

  showError(message) {
    this.state = "error";
    this._setBusy(false);
    const body = el("div", {}, [
      el("p", { class: "errtext", text: message || "Tracking failed." }),
      el("div", { class: "actions" }, [
        el("button", { class: "primary", type: "button", text: "Try again", onclick: () => this.lastRequest ? this.onCreate(this.lastRequest) : this.showOffer() }),
        el("button", { class: "ghost", type: "button", text: "Change range", onclick: () => this.showOffer() }),
        el("span", { class: "spacer" }),
        el("button", { class: "ghost", type: "button", text: "Close", onclick: () => this.hide() }),
      ]),
    ]);
    this._renderPanel("Couldn't create tracking", body);
    this._show(true);
    this.live.textContent = `Tracking failed. ${message || ""}`;
  }

  // ---- internals -----------------------------------------------------------

  _renderPanel(title, body) {
    const h = el("div", { class: "head" }, [
      el("h2", { class: "title", text: title }),
      el("button", { class: "x", type: "button", "aria-label": "Minimise", title: "Minimise", html: X_SVG, onclick: () => this.collapse(true) }),
    ]);
    this.panel.replaceChildren(h, body);
  }

  _show(expanded) {
    this.hostEl.hidden = false;
    if (expanded) this.expand(false);
    else this.collapse(false);
  }

  _setBusy(busy, frac = 0) {
    this.fab.classList.toggle("busy", busy);
    if (busy) this.arcFill.setAttribute("stroke-dashoffset", String(113.1 * (1 - frac)));
    if (!busy) this.fab.setAttribute("aria-label", "Ball highlighter");
  }

  _armTimer() {
    this._clearTimer();
    if (this._hover || !this.expanded || this.root.activeElement) return;
    if (this.state === "ready") return;
    this._timer = setTimeout(() => this.collapse(false), AUTO_COLLAPSE_MS);
  }

  _clearTimer() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = 0;
  }

  _setMode(mode) {
    this.mode = mode;
    this.chipWhole.setAttribute("aria-checked", String(mode === "whole"));
    this.chipRange.setAttribute("aria-checked", String(mode === "range"));
    this.chipWhole.tabIndex = mode === "whole" ? 0 : -1;
    this.chipRange.tabIndex = mode === "range" ? 0 : -1;
    this.rangeRow.hidden = mode !== "range";
    this._validate();
  }

  _chipKeys(e) {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
    e.preventDefault();
    const next = this.mode === "whole" ? "range" : "whole";
    this._setMode(next);
    (next === "whole" ? this.chipWhole : this.chipRange).focus();
  }

  _fillDefaults() {
    if (!this.startIn) return;
    const times = this.getTimes();
    if (!times.valid) {
      // e.g. an ad is playing: its clock isn't the video's. Fill in later.
      this._stale = true;
      return;
    }
    this._stale = false;
    const r = defaultRange(times.current, times.duration);
    this.startIn.value = formatClock(r.start);
    this.endIn.value = formatClock(r.end);
    this._validate();
  }

  _onEdit() {
    this.edited = true;
    this._validate();
  }

  _validate() {
    if (!this.hint) return null;
    const setErr = (msg, bad = []) => {
      this.startIn.setAttribute("aria-invalid", String(bad.includes("s")));
      this.endIn.setAttribute("aria-invalid", String(bad.includes("e")));
      this.hint.classList.toggle("err", Boolean(msg));
      return msg;
    };
    const times = this.getTimes();
    const dur = times.valid && Number.isFinite(times.duration) && times.duration > 0 ? times.duration : null;
    if (this.mode === "whole") {
      setErr("");
      this.hint.textContent = dur
        ? `All ${formatClock(dur)}. ${dur > 1800 ? "Long videos take a while." : ""}`.trim()
        : "Tracks the entire video.";
      return { req: {} };
    }
    const s = parseClock(this.startIn.value);
    const e = parseClock(this.endIn.value);
    let msg = "";
    if (Number.isNaN(s) && Number.isNaN(e)) msg = setErr("Enter times as m:ss, for example 5:59.", ["s", "e"]);
    else if (Number.isNaN(s)) msg = setErr("Enter the start as m:ss, for example 5:59.", ["s"]);
    else if (Number.isNaN(e)) msg = setErr("Enter the end as m:ss, for example 6:12.", ["e"]);
    else if (e <= s) msg = setErr("The end must be after the start.", ["e"]);
    else if (dur && s >= dur) msg = setErr(`The video is only ${formatClock(dur)} long.`, ["s"]);
    if (msg) {
      this.hint.textContent = msg;
      return null;
    }
    setErr("");
    const end = dur ? Math.min(e, dur) : e;
    this.hint.textContent = `${formatClock(end - s)} of video`;
    return { req: { start: s, end } };
  }

  _submit() {
    const v = this._validate();
    if (!v) {
      (this.startIn.getAttribute("aria-invalid") === "true" ? this.startIn : this.endIn).focus();
      return;
    }
    this.lastRequest = v.req;
    this.onCreate(v.req);
  }

  _dismiss() {
    this.hide();
    this.onDismiss();
  }
}
