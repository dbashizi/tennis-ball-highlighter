// Browser checks in headless Chrome for Testing, driven over CDP.
//
//   python3 extension/dev/serve.py &                              # :8000, repo root
//   python3 extension/dev/mock_service.py --port 8766 --job-seconds 6 &
//   node extension/dev/e2e.mjs [harness] [sim] [extension]        # default: all
//
// The "extension" section loads the unpacked extension and needs a service
// (mock or real) on 127.0.0.1:8765 with a track for YTkyRTsiIaY for the YouTube
// part; it is skipped if none is listening. For example:
//   python3 extension/dev/mock_service.py --preload YTkyRTsiIaY=samples/YTkyRTsiIaY.track.json Screenshots go to $SHOTS (default data/scratch/extension-shots).
import { mkdirSync } from "node:fs";
import { resolve, join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { launch, newPage, sleep } from "./cdp.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "../..");
const BASE = process.env.BASE || "http://127.0.0.1:8000";
const MOCK = process.env.MOCK || "http://127.0.0.1:8766";
const SHOTS = resolve(process.env.SHOTS || join(REPO, "data/scratch/extension-shots"));
mkdirSync(SHOTS, { recursive: true });

const want = new Set(process.argv.slice(2));
const run = (name) => want.size === 0 || want.has(name);
const results = [];
let failed = 0;

function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  if (!ok) failed++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail ? `  (${detail})` : ""}`);
}

const shot = (p, name, clip) => p.screenshot(join(SHOTS, name), clip).then((f) => console.log(`     screenshot ${f}`));
const playerClip = (p) => p.eval(`(() => { const r = document.querySelector('#movie_player').getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; })()`);

async function seekPaused(p, t) {
  await p.eval(`(async () => { const v = harness.video; v.pause(); v.currentTime = ${t}; await new Promise(r => v.addEventListener('seeked', r, { once: true })); await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); })()`);
  await sleep(150);
}

async function harnessSuite(browser) {
  const p = await newPage(browser);
  for (const box of ["16x9", "4x3"]) {
    await p.goto(`${BASE}/extension/dev/harness.html?preset=synthetic&box=${box}`);
    await p.waitFor("harness.track && harness.video.readyState >= 2", 10000, "harness load");
    await p.eval("harness.video.playbackRate = 1; harness.resetMeasure(); harness.video.currentTime = 0; harness.video.play()");
    await sleep(box === "16x9" ? 11500 : 5000);
    const m = await p.eval("({ frames: harness.measure.frames, nearest: harness.measure.nearest, mean: harness.measure.sum / harness.measure.frames, max: harness.measure.max, source: harness.overlay.stats.source, drawn: harness.overlay.stats.drawn, layout: harness.overlay._layout, worst: [...harness.measure.samples].sort((a, b) => b.err - a.err).slice(0, 3) })");
    check(`harness ${box}: rVFC sync`, m.source === "rVFC", m.source);
    check(`harness ${box}: ring centred on ball while playing`, m.frames > 50 && m.max < 1.5,
      `${m.frames} interp frames (+${m.nearest} nearest), mean ${m.mean?.toFixed(3)} px, max ${m.max?.toFixed(3)} px; worst ${JSON.stringify(m.worst)}`);
    if (box === "4x3") {
      const L = m.layout;
      // 16:9 content in a 4:3 box: full width, letterboxed vertically.
      const box43 = await p.eval("(() => { const r = harness.video.getBoundingClientRect(); return { w: r.width, h: r.height }; })()");
      const expH = box43.w * 9 / 16;
      check("harness 4x3: canvas matches letterboxed content rect",
        Math.abs(L.w - box43.w) < 0.01 && Math.abs(L.h - expH) < 0.01 && Math.abs(L.top - (box43.h - expH) / 2) < 0.01,
        `canvas ${L.w.toFixed(1)}x${L.h.toFixed(1)} at top ${L.top.toFixed(1)}; box ${box43.w.toFixed(1)}x${box43.h.toFixed(1)}`);
      await seekPaused(p, 1.5);
      await shot(p, "harness-letterbox-4x3.png", await playerClip(p));
      continue;
    }
    await p.eval("harness.overlay.setSettings({ debug: true })");
    await seekPaused(p, 1.5);
    await shot(p, "harness-synthetic-debug.png", await playerClip(p));
    await p.eval("harness.overlay.setSettings({ debug: false })");

    // Paused seeks to mid-frame times: the ring must match the frame on screen.
    const pausedErr = [];
    for (const t of [1.5, 2.33, 4.41, 9.07]) {
      await seekPaused(p, t);
      pausedErr.push(await p.eval("(() => { const b = harness.detectBall(); const l = harness.overlay.stats.last; const s = harness.video.videoWidth / harness.overlay._layout.w; return b && l ? +Math.hypot(l.cx * s - b.x, l.cy * s - b.y).toFixed(3) : null; })()"));
    }
    check("paused seek: ring matches the displayed frame", pausedErr.every((e) => e !== null && e < 1.5), `errors px ${JSON.stringify(pausedErr)}`);

    // Paused-state checks against the rendering rules.
    const at = async (t) => { await seekPaused(p, t); return p.eval("({ mode: harness.overlay.stats.lastMode, last: harness.overlay.stats.last, t: harness.overlay.stats.lastTime })"); };
    let s = await at(3.04);
    check("rule: interpolate across a 0.04 s gap (<= 2.5/fps)", s.mode === "interp" && s.last, `${s.mode} at ${s.t}`);
    s = await at(6.2);
    check("rule: nothing inside a gap with no rows", s.mode === "none" && !s.last, `${s.mode} at ${s.t}`);
    s = await at(6.0); // last row 5.98, next 6.50: nearest within 1/fps
    check("rule: nearest row within 1/fps at gap edge", s.mode === "nearest" && s.last, `${s.mode} at ${s.t}`);
    s = await at(8.2);
    check("rule: hide below confidence threshold", s.mode === "lowconf" && !s.last, `${s.mode}`);
    s = await at(4.4);
    check("rule: ring colour from track (#101010 on white band)", s.last && s.last.ring === "#101010", s.last?.ring);
    await shot(p, "harness-dark-ring-on-white.png", await playerClip(p));
    const g = s.last;
    const L = await p.eval("harness.overlay._layout");
    const rPx = (await p.eval("harness.overlay.sample.r")) * L.w;
    const w = Math.max(0.2 * rPx, 1.5);
    check("rule: ring geometry (radius = r + w/2, min stroke 1.5)", Math.abs(g.width - w) < 1e-6 && Math.abs(g.radius - (rPx + w / 2)) < 1e-6,
      `r_px ${rPx.toFixed(3)}, stroke ${g.width.toFixed(3)}, radius ${g.radius.toFixed(3)}`);

    await p.eval("document.getElementById('movie_player').classList.add('ad-showing')");
    await sleep(300);
    const adHidden = await p.eval("({ vis: harness.overlay.canvas.style.visibility, reason: harness.overlay.stats.hiddenReason })");
    check("ads: overlay hidden while ad-showing", adHidden.vis === "hidden" && adHidden.reason === "ad", JSON.stringify(adHidden));
    await p.eval("document.getElementById('movie_player').classList.remove('ad-showing')");
    await sleep(300);
    check("ads: overlay back after the ad", (await p.eval("harness.overlay.canvas.style.visibility")) === "visible");

    // Resize the player (theater-mode style) and check the canvas follows.
    await p.eval("document.getElementById('movie_player').style.width = '60%'");
    await sleep(400);
    const rs = await p.eval("(() => { const v = harness.video.getBoundingClientRect(); const c = harness.overlay.canvas.getBoundingClientRect(); return { v: [v.left, v.top, v.width], c: [c.left, c.top, c.width, c.height], dpr: devicePixelRatio, bw: harness.overlay.canvas.width }; })()");
    check("resize: canvas follows the video box", Math.abs(rs.v[0] - rs.c[0]) < 0.5 && Math.abs(rs.v[2] - rs.c[2]) < 0.5 && rs.bw === Math.round(rs.c[2] * rs.dpr), JSON.stringify(rs));
  }

  // rAF fallback
  await p.goto(`${BASE}/extension/dev/harness.html?preset=synthetic&fallback=1`);
  await p.waitFor("harness.track && harness.video.readyState >= 2", 10000, "harness load (fallback)");
  await p.eval("harness.video.currentTime = 0; harness.video.play()");
  await sleep(2500);
  const fb = await p.eval("({ source: harness.overlay.stats.source, drawn: harness.overlay.stats.drawn })");
  check("fallback: rAF + currentTime draws", fb.source === "rAF" && fb.drawn > 30, JSON.stringify(fb));
  if (p.logs.length) console.log("     page logs:", p.logs.slice(0, 10));
  await browser.send("Target.closeTarget", { targetId: p.targetId });
}

const shadow = (sel) => `document.querySelector('.tbh-prompt-host')?.shadowRoot?.querySelector(${JSON.stringify(sel)})`;

async function simSuite(browser) {
  const health = await fetch(`${MOCK}/v1/health`).then((r) => r.ok).catch(() => false);
  if (!health) {
    check("sim: mock service reachable", false, `${MOCK} is not running`);
    return;
  }
  const p = await newPage(browser);
  const sim = (v, extra = "") => `${BASE}/extension/dev/sim.html?v=${v}&api=${encodeURIComponent(MOCK)}${extra}`;

  // Untracked video: prompt offered, keys don't leak to YouTube hotkeys, dismissal remembered.
  await p.goto(sim("UNTRACKED01"));
  await p.eval("localStorage.clear()");
  await p.goto(sim("UNTRACKED01"));
  await p.waitFor(`${shadow(".primary")}?.textContent === 'Create tracking'`, 8000, "offer prompt");
  await p.waitFor("Math.abs(document.querySelector('video').currentTime - 5) < 0.1", 8000, "clip at 5 s");
  // Hovering refreshes the unedited default range to the current time.
  const hb = await p.eval("(() => { const r = document.querySelector('.tbh-prompt-host').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + 20 }; })()");
  await p.mouseMove(hb.x, hb.y);
  await sleep(200);
  const promptInfo = await p.eval(`(() => { const s = ${shadow("#tbh-start")}; const e = ${shadow("#tbh-end")}; return { start: s.value, end: e.value, expanded: ${shadow(".root")}.classList.contains('expanded') }; })()`);
  // Video at local 5 s (+100 offset) = 1:45; range 1:15 to 2:15, clamped to the 1:52 end.
  check("prompt: offered expanded with current time +/- 30 s", promptInfo.expanded && promptInfo.start === "1:15" && promptInfo.end === "1:52", JSON.stringify(promptInfo));
  await shot(p, "sim-prompt-offer.png", await playerClip(p));
  const prompt = await p.eval(`(() => { const h = document.querySelector('.tbh-prompt-host').getBoundingClientRect(); const bar = document.querySelector('.ytp-chrome-bottom').getBoundingClientRect(); const pl = document.querySelector('#movie_player').getBoundingClientRect(); return { overlapsControls: h.bottom > bar.top, right: pl.right - h.right, top: h.top - pl.top }; })()`);
  check("prompt: top-right, clear of the control bar", !prompt.overlapsControls && prompt.right < 20 && prompt.top < 20, JSON.stringify(prompt));

  await p.eval("document.getElementById('ad').click()");
  await sleep(100);
  const duringAd = await p.eval("getComputedStyle(document.querySelector('.tbh-prompt-host')).display");
  await p.eval("document.getElementById('ad').click()");
  check("prompt: hidden while an ad plays", duringAd === "none", duringAd);
  await p.eval(`(() => { const s = ${shadow("#tbh-start")}; s.focus(); s.select(); })()`);
  for (const k of ["k", "f", "1", ":", "3", "0"]) await p.key(k);
  const typed = await p.eval(`({ value: ${shadow("#tbh-start")}.value, leaked: sim.seenKeys.slice(), paused: document.querySelector('video').paused })`);
  check("prompt: typing doesn't trigger player hotkeys", typed.leaked.length === 0 && typed.value === "kf1:30", JSON.stringify(typed));
  const hint = await p.eval(`({ text: ${shadow(".hint")}.textContent, invalid: ${shadow("#tbh-start")}.getAttribute('aria-invalid') })`);
  check("prompt: invalid m:ss is flagged", hint.invalid === "true" && /m:ss/.test(hint.text), JSON.stringify(hint));
  await shot(p, "sim-prompt-invalid.png", await playerClip(p));
  await p.key("Escape");
  await sleep(200);
  const collapsed = await p.eval(`({ expanded: ${shadow(".root")}.classList.contains('expanded'), focus: ${shadow(".fab")} === document.querySelector('.tbh-prompt-host').shadowRoot.activeElement })`);
  check("prompt: Esc collapses to the icon button and focuses it", !collapsed.expanded && collapsed.focus, JSON.stringify(collapsed));
  await shot(p, "sim-prompt-collapsed.png", await playerClip(p));
  await p.eval(`${shadow(".fab")}.click()`);
  await p.eval(`[...document.querySelector('.tbh-prompt-host').shadowRoot.querySelectorAll('.ghost')].find(b => b.textContent === "Don't ask again").click()`);
  await sleep(300);
  await p.goto(sim("UNTRACKED01"));
  await sleep(1500);
  const afterDismiss = await p.eval(`({ hidden: !document.querySelector('.tbh-prompt-host') || document.querySelector('.tbh-prompt-host').hidden, state: sim.app.state().status, dismissed: sim.app.state().dismissed })`);
  check("prompt: dismissal remembered per video", afterDismiss.hidden && afterDismiss.dismissed, JSON.stringify(afterDismiss));

  // Auto-collapse after inactivity.
  await p.eval("localStorage.clear()");
  await p.goto(sim("UNTRACKED01"));
  await p.waitFor(`${shadow(".root")}?.classList.contains('expanded')`, 8000, "offer again");
  await sleep(6800);
  check("prompt: auto-collapses after ~6 s without hover", !(await p.eval(`${shadow(".root")}.classList.contains('expanded')`)));

  // Create -> progress -> ready -> drawing (SYNTHETIC00; mock builds the track from the synthetic source).
  await fetch(`${MOCK}/v1/videos/SYNTHETIC00`, { method: "DELETE" }).catch(() => {});
  await p.goto(sim("SYNTHETIC00"));
  await p.waitFor(`${shadow(".primary")}?.textContent === 'Create tracking'`, 8000, "offer on SYNTHETIC00");
  await p.waitFor(`${shadow("#tbh-start")}?.value !== ''`, 8000, "default range filled");
  await p.eval(`${shadow(".primary")}.click()`);
  await p.waitFor(`${shadow(".stage")}?.textContent === 'Finding the ball'`, 10000, "detect stage");
  const prog = await p.eval(`({ stage: ${shadow(".stage")}.textContent, pct: ${shadow(".pct")}.textContent, msg: ${shadow(".msg")}.textContent, app: sim.app.state().status })`);
  check("job: progress shows stage and percent", prog.app === "job" && /%$/.test(prog.pct), JSON.stringify(prog));
  await shot(p, "sim-progress.png", await playerClip(p));
  await p.eval(`${shadow(".x")}.click()`);
  await sleep(300);
  await shot(p, "sim-progress-collapsed.png", await playerClip(p));
  await p.waitFor("sim.app.state().status === 'ready'", 15000, "job ready");
  await p.eval("document.querySelector('video').play()");
  await sleep(1500);
  const ready = await p.eval(`({ panel: ${shadow(".title")}?.textContent, drawn: sim.app.overlay.stats.drawn, segs: sim.app.state().segments })`);
  check("job: track loaded and ring drawn after completion", ready.drawn > 5 && ready.segs.length === 1, JSON.stringify(ready));
  await shot(p, "sim-ready.png", await playerClip(p));
  await sleep(3000);
  check("job: 'ready' notice hides itself", await p.eval("document.querySelector('.tbh-prompt-host').hidden"));

  // Replaced <video> element: overlay re-binds.
  const before = await p.eval("sim.app.overlay.stats.drawn");
  await p.eval("document.getElementById('replace').click()");
  await sleep(2500);
  await p.eval("document.querySelector('video').play()");
  await sleep(1200);
  const reb = await p.eval("({ same: sim.app.overlay.video === document.querySelector('video'), drawn: sim.app.overlay.stats.drawn, canvases: document.querySelectorAll('canvas.tbh-overlay').length })");
  check("spa: overlay follows a replaced <video>", reb.same && reb.drawn > 0 && reb.canvases === 1, `${JSON.stringify(reb)} (old overlay drew ${before})`);

  // Miniplayer hides, theater re-aligns.
  await p.eval("document.getElementById('mini').click()");
  await sleep(700);
  check("miniplayer: overlay hidden", (await p.eval("sim.app.overlay.stats.hiddenReason")) === "miniplayer");
  await p.eval("document.getElementById('mini').click(); document.getElementById('theater').click()");
  await sleep(900);
  const th = await p.eval("(() => { const v = document.querySelector('video').getBoundingClientRect(); const c = sim.app.overlay.canvas.getBoundingClientRect(); return { v: v.width, c: c.width, vis: sim.app.overlay.canvas.style.visibility }; })()");
  check("theater: canvas resized with the player", Math.abs(th.v - th.c) < 0.5 && th.vis === "visible", JSON.stringify(th));
  await p.eval("document.getElementById('theater').click()");

  // SPA navigation resets per-video state.
  await p.eval("document.querySelector('[data-nav=UNTRACKED01]').click()");
  await sleep(1500);
  const nav = await p.eval("({ id: sim.app.state().videoId, status: sim.app.state().status, track: sim.app.overlay.track })");
  check("spa: navigation resets state", nav.id === "UNTRACKED01" && nav.status === "none" && nav.track === null, JSON.stringify(nav));

  // Failure path.
  await fetch(`${MOCK}/v1/videos/FAILFAILFAI`, { method: "DELETE" }).catch(() => {});
  await p.eval("document.querySelector('[data-nav=FAILFAILFAI]').click()");
  await p.waitFor(`${shadow(".primary")}?.textContent === 'Create tracking'`, 8000, "offer on FAIL");
  await p.eval(`${shadow(".chip")}.click()`); // whole video
  await p.eval(`${shadow(".primary")}.click()`);
  await p.waitFor(`${shadow(".title")}?.textContent === "Couldn't create tracking"`, 15000, "failure UI");
  check("job: failure shown with retry", await p.eval(`${shadow(".primary")}.textContent === 'Try again'`));
  await shot(p, "sim-failed.png", await playerClip(p));

  if (p.logs.length) console.log("     page logs:", p.logs.slice(0, 10));
  await browser.send("Target.closeTarget", { targetId: p.targetId });
}

async function extensionSuite() {
  const extPath = join(REPO, "extension");
  const browser = await launch({ extension: extPath });
  try {
    const sw = await waitForTarget(browser, (t) => t.type === "service_worker" && t.url.endsWith("/src/background.js"), 10000);
    check("extension: loads, service worker running", Boolean(sw), sw?.url);
    if (!sw) return;
    const extId = new URL(sw.url).host;
    const pop = await newPage(browser, `chrome-extension://${extId}/src/popup.html`, { width: 320, height: 600 });
    await sleep(1500);
    const svcUp = await fetch("http://127.0.0.1:8765/v1/health").then((r) => r.ok).catch(() => false);
    const txt = await pop.eval("document.body.innerText");
    check("popup: renders", /Ball highlighter/.test(txt) && /Ring size/.test(txt), txt.split("\n").slice(0, 3).join(" | "));
    const h = await pop.eval("document.documentElement.scrollHeight");
    await pop.send("Emulation.setDeviceMetricsOverride", { width: 320, height: h, deviceScaleFactor: 2, mobile: false });
    await shot(pop, svcUp ? "popup-service-up.png" : "popup-service-down.png");
    if (!svcUp) {
      check("popup: explains how to start the service", /uv run tbh-service run/.test(txt));
    }
    // Settings round-trip via storage.sync.
    await pop.eval("(() => { const r = document.getElementById('ratio'); r.value = '1.4'; r.dispatchEvent(new Event('input')); })()");
    await sleep(300);
    const stored = await pop.eval("chrome.storage.sync.get('settings').then(s => s.settings)");
    check("popup: settings saved to storage.sync", stored && stored.ratio === 1.4, JSON.stringify(stored));
    await pop.eval("document.getElementById('reset').click()");
    if (pop.logs.length) console.log("     popup logs:", pop.logs);

    // Background message router talks to the service.
    const bg = await pop.eval("chrome.runtime.sendMessage({ type: 'health' })");
    check("background: health via message router", svcUp ? bg.ok === true : bg.error === "unreachable", JSON.stringify(bg));

    if (!svcUp) {
      console.log("     (no service on :8765: skipping the YouTube check)");
      return;
    }
    const yt = await newPage(browser, "about:blank");
    await yt.goto("https://www.youtube.com/watch?v=YTkyRTsiIaY", 4000);
    const url = await yt.eval("location.href");
    if (!/youtube\.com\/watch/.test(url)) {
      check("youtube: watch page reachable", false, `landed on ${url}`);
      return;
    }
    const got = await yt.waitFor("document.querySelector('#movie_player canvas.tbh-overlay, .tbh-prompt-host') ? true : false", 20000, "content script UI").catch((e) => e.message);
    const state = await pop.eval(`chrome.tabs.query({ url: 'https://www.youtube.com/*' }).then(ts => chrome.tabs.sendMessage(ts[0].id, { type: 'tbh:getState' }))`).catch((e) => ({ error: String(e) }));
    check("youtube: content script running and reporting state", got === true && state && state.ok, JSON.stringify(state?.data || state).slice(0, 300));
    // Seek into a tracked segment, play muted, and check the canvas sits on the
    // video's content rect and has ink at some point.
    const rec = await fetch("http://127.0.0.1:8765/v1/videos/YTkyRTsiIaY").then((r) => r.json()).catch(() => null);
    const seg = rec?.segments?.[0] || { start: 0, end: 10 };
    const seekTo = seg.start + Math.min(2, (seg.end - seg.start) / 4);
    // YouTube's own player API (main world) is more reliable than poking <video>.
    // Headless YouTube sometimes loads but never buffers past a seek, so the
    // check needs the frame at the seek target, not progressing playback.
    await yt.waitFor("(() => { const v = document.querySelector('#movie_player video.html5-main-video'); return v && v.readyState >= 2 && !document.querySelector('#movie_player').classList.contains('ad-showing'); })()", 60000, "video loaded, no ad").catch(() => {});
    await yt.eval(`(() => { const p = document.getElementById('movie_player'); p.mute(); p.seekTo(${seekTo}, true); p.playVideo(); })()`).catch(() => {});
    await yt.waitFor(`(() => { const v = document.querySelector('#movie_player video.html5-main-video'); return v.currentTime >= ${seg.start} && v.currentTime < ${seg.end}; })()`, 20000, "time in segment").catch(() => {});
    const progressed = await yt.waitFor(`document.querySelector('#movie_player video.html5-main-video').currentTime > ${seekTo + 0.5}`, 15000, "playback").then(() => true, () => false);
    const inkExpr = `(() => {
      const v = document.querySelector('#movie_player video.html5-main-video');
      const c = document.querySelector('#movie_player canvas.tbh-overlay');
      if (!c) return { error: 'no canvas' };
      const vr = v.getBoundingClientRect(), cr = c.getBoundingClientRect();
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++;
      const s = Math.min(vr.width / v.videoWidth, vr.height / v.videoHeight);
      return { t: +v.currentTime.toFixed(3), ad: document.querySelector('#movie_player').classList.contains('ad-showing'), vis: c.style.visibility,
        video: [vr.left, vr.top, vr.width, vr.height].map(Math.round), canvas: [cr.left, cr.top, cr.width, cr.height].map(Math.round),
        expected: [vr.left + (vr.width - v.videoWidth * s) / 2, vr.top + (vr.height - v.videoHeight * s) / 2, v.videoWidth * s, v.videoHeight * s].map(Math.round),
        inkPixels: n };
    })()`;
    let ink = null;
    let inkFrames = 0;
    for (let i = 0; i < 20; i++) {
      const cur = await yt.eval(inkExpr);
      if (cur.inkPixels > 0) inkFrames++;
      if (!ink || cur.inkPixels > ink.inkPixels) ink = cur;
      await sleep(100);
    }
    ink.inkSamples = `${inkFrames}/20`;
    ink.playbackProgressed = progressed;
    ink.segment = seg;
    const aligned = ink.canvas && ink.expected && ink.canvas.every((x, i) => Math.abs(x - ink.expected[i]) <= 1);
    check("youtube: ring drawn on the real player, aligned to content rect", aligned && ink.inkPixels > 0, JSON.stringify(ink));
    // Pause on a frame with a ring for the screenshot.
    await yt.waitFor(`(() => { const c = document.querySelector('#movie_player canvas.tbh-overlay'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; for (let i = 3; i < d.length; i += 4) if (d[i]) { document.querySelector('#movie_player video.html5-main-video').pause(); return true; } return false; })()`, 8000, "a ring frame").catch(() => {});
    await sleep(300);
    await shot(yt, "youtube-watch.png", { x: ink.video[0], y: ink.video[1], width: ink.video[2], height: ink.video[3] });

    // SPA navigation to an untracked video: prompt appears inside YouTube's player.
    const moved = await yt.eval(`(() => {
      const a = [...document.querySelectorAll('a[href*="/watch?v="]')].find(a => !a.href.includes('YTkyRTsiIaY') && a.offsetParent);
      if (!a) return null; a.click(); return a.href;
    })()`);
    if (moved) {
      await yt.waitFor("!location.href.includes('YTkyRTsiIaY')", 15000, "spa navigation").catch(() => {});
      const offered = await yt.waitFor("(() => { const h = document.querySelector('#movie_player .tbh-prompt-host'); return h && !h.hidden && h.shadowRoot.querySelector('.primary')?.textContent === 'Create tracking'; })()", 15000, "prompt on YouTube").catch(() => false);
      const st = await pop.eval(`chrome.tabs.query({ url: 'https://www.youtube.com/*' }).then(ts => chrome.tabs.sendMessage(ts[0].id, { type: 'tbh:getState' }))`).catch(() => null);
      check("youtube: SPA navigation resets state and offers tracking", offered === true && st?.data?.status === "none" && st?.data?.rows === 0, `${moved} ${JSON.stringify(st?.data)?.slice(0, 160)}`);
      await sleep(500); // let the open animation finish
      const pr = await yt.eval("(() => { const r = document.querySelector('#movie_player').getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; })()");
      await shot(yt, "youtube-prompt.png", pr);
    }
    if (yt.logs.length) console.log("     youtube logs (first 5):", yt.logs.slice(0, 5));
  } finally {
    await browser.close();
  }
}

async function waitForTarget(browser, pred, timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const { targetInfos } = await browser.send("Target.getTargets");
    const t = targetInfos.find(pred);
    if (t) return t;
    await sleep(200);
  }
  return null;
}

(async () => {
  if (run("harness") || run("sim")) {
    const browser = await launch();
    try {
      if (run("harness")) await harnessSuite(browser);
      if (run("sim")) await simSuite(browser);
    } catch (err) {
      check("suite crashed", false, err.stack);
    } finally {
      await browser.close();
    }
  }
  if (run("extension")) {
    try {
      await extensionSuite();
    } catch (err) {
      check("extension suite crashed", false, err.stack);
    }
  }
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
})();
