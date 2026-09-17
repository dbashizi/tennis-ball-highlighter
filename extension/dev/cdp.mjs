// Minimal Chrome DevTools Protocol driver (Node >= 22: global WebSocket). No deps.
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, existsSync, readdirSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";

export function findChrome() {
  if (process.env.CHROME) return process.env.CHROME;
  const pw = join(homedir(), "Library/Caches/ms-playwright");
  if (existsSync(pw)) {
    const dirs = readdirSync(pw).filter((d) => /^chromium-\d+$/.test(d)).sort().reverse();
    for (const d of dirs) {
      const p = join(pw, d, "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing");
      if (existsSync(p)) return p;
      const lin = join(pw, d, "chrome-linux/chrome");
      if (existsSync(lin)) return lin;
    }
  }
  // Branded Chrome ignores --load-extension since 137, so it only works for page tests.
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  return existsSync(mac) ? mac : null;
}

export async function launch({ extension, headless = true, width = 1280, height = 860 } = {}) {
  const chrome = findChrome();
  if (!chrome) throw new Error("No Chrome found; set CHROME=/path/to/chrome");
  const userDir = mkdtempSync(join(tmpdir(), "tbh-chrome-"));
  const args = [
    `--user-data-dir=${userDir}`,
    "--remote-debugging-port=0",
    "--no-first-run",
    "--no-default-browser-check",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    `--window-size=${width},${height}`,
    "--hide-scrollbars",
    "--mute-audio",
  ];
  if (headless) args.push("--headless=new");
  if (extension) args.push(`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, "--enable-unsafe-extension-debugging");
  args.push("about:blank");
  const proc = spawn(chrome, args, { stdio: ["ignore", "ignore", "pipe"] });
  const wsUrl = await new Promise((resolve, reject) => {
    let buf = "";
    const timer = setTimeout(() => reject(new Error(`Chrome didn't start:\n${buf}`)), 20000);
    proc.stderr.on("data", (d) => {
      buf += d;
      const m = /DevTools listening on (ws:\/\/\S+)/.exec(buf);
      if (m) { clearTimeout(timer); resolve(m[1]); }
    });
    proc.on("exit", (code) => reject(new Error(`Chrome exited ${code}:\n${buf}`)));
  });
  const browser = await connect(wsUrl);
  browser.proc = proc;
  browser.close = async () => {
    try { await browser.send("Browser.close"); } catch { /* ignore */ }
    proc.kill();
    try { rmSync(userDir, { recursive: true, force: true }); } catch { /* ignore */ }
  };
  return browser;
}

export async function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let nextId = 1;
  const pending = new Map();
  const listeners = new Set();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(`${msg.error.message} ${msg.error.data || ""}`));
      else resolve(msg.result);
    } else if (msg.method) {
      for (const l of listeners) l(msg);
    }
  };
  const send = (method, params = {}, sessionId) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  const on = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
  return { ws, send, on };
}

/** Attach to a target and return a page helper bound to its session. */
export async function page(browser, targetId, { width = 1280, height = 860 } = {}) {
  const { sessionId } = await browser.send("Target.attachToTarget", { targetId, flatten: true });
  const s = (m, p) => browser.send(m, p, sessionId);
  const logs = [];
  const contexts = new Map(); // name -> id, for isolated worlds (content scripts)
  browser.on((msg) => {
    if (msg.sessionId !== sessionId) return;
    if (msg.method === "Runtime.executionContextCreated") {
      const c = msg.params.context;
      if (c.auxData?.type === "isolated") contexts.set(c.name, c.id);
    }
    if (msg.method === "Runtime.executionContextsCleared") contexts.clear();
    if (msg.method === "Runtime.consoleAPICalled") logs.push(`${msg.params.type}: ${msg.params.args.map((a) => a.value ?? a.description).join(" ")}`);
    if (msg.method === "Runtime.exceptionThrown") logs.push(`exception: ${msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text}`);
  });
  await s("Runtime.enable");
  await s("Page.enable").catch(() => {});
  await s("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false }).catch(() => {});
  const api = {
    sessionId,
    logs,
    send: s,
    async eval(expression) {
      const r = await s("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
      if (r.exceptionDetails) throw new Error(`eval failed: ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}\n  in: ${expression.slice(0, 200)}`);
      return r.result.value;
    },
    /** Evaluate in an extension's isolated world (content script), by extension name. */
    async evalIsolated(name, expression) {
      const id = contexts.get(name);
      if (!id) throw new Error(`no isolated world named ${name} (have: ${[...contexts.keys()]})`);
      const r = await s("Runtime.evaluate", { expression, contextId: id, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(`eval failed: ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}`);
      return r.result.value;
    },
    async goto(url, waitMs = 0) {
      const loaded = new Promise((res) => {
        const off = browser.on((m) => { if (m.sessionId === sessionId && m.method === "Page.loadEventFired") { off(); res(); } });
        setTimeout(res, 15000);
      });
      await s("Page.navigate", { url });
      await loaded;
      if (waitMs) await sleep(waitMs);
    },
    async waitFor(expression, timeoutMs = 10000, what = expression) {
      const t0 = Date.now();
      for (;;) {
        const v = await api.eval(expression).catch(() => undefined);
        if (v) return v;
        if (Date.now() - t0 > timeoutMs) throw new Error(`timed out waiting for ${what}`);
        await sleep(100);
      }
    },
    async screenshot(path, clip) {
      const { writeFileSync } = await import("node:fs");
      const r = await s("Page.captureScreenshot", { format: "png", ...(clip ? { clip: { ...clip, scale: 1 } } : {}) });
      writeFileSync(path, Buffer.from(r.data, "base64"));
      return path;
    },
    async key(key, text) {
      const code = key.length === 1 ? `Key${key.toUpperCase()}` : key;
      await s("Input.dispatchKeyEvent", { type: "keyDown", key, code, text: text ?? (key.length === 1 ? key : undefined) });
      await s("Input.dispatchKeyEvent", { type: "keyUp", key, code });
    },
    async mouseMove(x, y) {
      await s("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    },
  };
  return api;
}

export async function newPage(browser, url = "about:blank", opts) {
  const { targetId } = await browser.send("Target.createTarget", { url: "about:blank" });
  const p = await page(browser, targetId, opts);
  p.targetId = targetId;
  if (url !== "about:blank") await p.goto(url);
  return p;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
