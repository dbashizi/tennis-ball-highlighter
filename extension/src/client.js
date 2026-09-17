// HTTP client for the local helper service (docs/api.md). Pure apart from the
// injected `fetch`, so the background worker, the dev harness and the unit
// tests all share it.
//
// Every call resolves (never rejects) to one of:
//   { ok: true,  status, data }
//   { ok: false, error: "unreachable" | "http" | "timeout" | "bad-json", status?, detail? }

export const SERVICE_BASE = "http://127.0.0.1:8765";
const TIMEOUT_MS = 5000;

export function createClient(fetchImpl, base = SERVICE_BASE, timeoutMs = TIMEOUT_MS) {
  async function call(method, path, body) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    let res;
    try {
      res = await fetchImpl(base + path, {
        method,
        headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctrl.signal,
        cache: "no-store",
      });
    } catch (err) {
      clearTimeout(timer);
      return { ok: false, error: ctrl.signal.aborted ? "timeout" : "unreachable", detail: String(err && err.message || err) };
    }
    let data = null;
    try {
      const text = await res.text();
      data = text ? JSON.parse(text) : null;
    } catch (err) {
      clearTimeout(timer);
      if (res.ok) return { ok: false, error: "bad-json", status: res.status, detail: String(err) };
    }
    clearTimeout(timer);
    if (!res.ok) {
      const detail = data && typeof data.detail === "string" ? data.detail : res.statusText;
      return { ok: false, error: "http", status: res.status, detail };
    }
    return { ok: true, status: res.status, data };
  }

  const enc = encodeURIComponent;
  return {
    health: () => call("GET", "/v1/health"),
    getVideo: (id) => call("GET", `/v1/videos/${enc(id)}`),
    getTrack: (id) => call("GET", `/v1/tracks/${enc(id)}`),
    listVideos: () => call("GET", "/v1/videos"),
    getJob: (id) => call("GET", `/v1/jobs/${enc(id)}`),
    deleteVideo: (id) => call("DELETE", `/v1/videos/${enc(id)}`),
    createJob: (url, start, end) => {
      const body = { url };
      if (Number.isFinite(start)) body.start = start;
      if (Number.isFinite(end)) body.end = end;
      return call("POST", "/v1/jobs", body);
    },
  };
}

/** True when a client result means "the service isn't running". */
export function isUnreachable(res) {
  return !res.ok && (res.error === "unreachable" || res.error === "timeout");
}
