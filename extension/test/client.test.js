import { test } from "node:test";
import assert from "node:assert/strict";
import { createClient, isUnreachable } from "../src/client.js";

function fakeFetch(handler) {
  const calls = [];
  const f = async (url, init) => {
    calls.push({ url, init });
    return handler(url, init);
  };
  f.calls = calls;
  return f;
}

const json = (status, body) => new Response(body === undefined ? "" : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

test("client: success and 404 detail", async () => {
  const f = fakeFetch((url) => (url.endsWith("/v1/videos/abc") ? json(404, { detail: "unknown video" }) : json(200, { ok: true })));
  const c = createClient(f, "http://svc");
  assert.deepEqual(await c.health(), { ok: true, status: 200, data: { ok: true } });
  const r = await c.getVideo("abc");
  assert.equal(r.ok, false);
  assert.equal(r.error, "http");
  assert.equal(r.status, 404);
  assert.equal(r.detail, "unknown video");
  assert.equal(isUnreachable(r), false);
});

test("client: createJob body omits missing range", async () => {
  const f = fakeFetch(() => json(202, { job_id: "j1" }));
  const c = createClient(f, "http://svc");
  await c.createJob("https://www.youtube.com/watch?v=YTkyRTsiIaY");
  await c.createJob("https://www.youtube.com/watch?v=YTkyRTsiIaY", 359, 372);
  assert.equal(f.calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(f.calls[0].init.body), { url: "https://www.youtube.com/watch?v=YTkyRTsiIaY" });
  assert.deepEqual(JSON.parse(f.calls[1].init.body), { url: "https://www.youtube.com/watch?v=YTkyRTsiIaY", start: 359, end: 372 });
  assert.equal(f.calls[1].url, "http://svc/v1/jobs");
});

test("client: network failure is 'unreachable', ids are encoded", async () => {
  const f = fakeFetch(() => { throw new TypeError("Failed to fetch"); });
  const c = createClient(f, "http://svc");
  const r = await c.getJob("a/b");
  assert.equal(r.error, "unreachable");
  assert.ok(isUnreachable(r));
  assert.equal(f.calls[0].url, "http://svc/v1/jobs/a%2Fb");
});

test("client: timeout", async () => {
  const f = (url, init) => new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
  const c = createClient(f, "http://svc", 20);
  const r = await c.health();
  assert.equal(r.error, "timeout");
  assert.ok(isUnreachable(r));
});

test("client: bad JSON on success", async () => {
  const c = createClient(async () => new Response("<html>", { status: 200 }), "http://svc");
  const r = await c.getTrack("x");
  assert.equal(r.error, "bad-json");
});
