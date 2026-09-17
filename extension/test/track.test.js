import { test } from "node:test";
import assert from "node:assert/strict";
import { parseTrack, sampleTrack, createSample, floorIndex, segmentIndex, TrackError, coveredSeconds } from "../src/track.js";

const FIELDS = ["t", "x", "y", "r", "conf", "ring", "flags"];

function doc(frames, { fps = 50, segments = [{ start: 10, end: 20 }], fields = FIELDS } = {}) {
  return {
    schema_version: 1,
    video_id: "YTkyRTsiIaY",
    video: { width: 1280, height: 720, fps },
    segments,
    fields,
    frames,
  };
}

const row = (t, x, y = 0.5, r = 0.01, conf = 0.9, ring = "#101010", flags = 0) => [t, x, y, r, conf, ring, flags];

test("parseTrack: builds columns and reads the example", () => {
  const tr = parseTrack(doc([row(10.02, 0.51234, 0.4312, 0.0041, 0.93, "#101010", 0), row(10.04, 0.51502, 0.4271, 0.0041, 0.88, "#101010", 1)]));
  assert.equal(tr.length, 2);
  assert.equal(tr.fps, 50);
  assert.equal(tr.videoId, "YTkyRTsiIaY");
  assert.ok(tr.t instanceof Float64Array);
  assert.equal(tr.t[1], 10.04);
  assert.ok(Math.abs(tr.x[0] - 0.51234) < 1e-6);
  assert.equal(tr.flags[1], 1);
  assert.deepEqual(tr.segments, [{ start: 10, end: 20 }]);
  assert.equal(coveredSeconds(tr), 10);
});

test("parseTrack: honours a reordered fields list", () => {
  const fields = ["ring", "flags", "conf", "r", "y", "x", "t"];
  const tr = parseTrack(doc([["#F5F5F5", 4, 0.7, 0.02, 0.3, 0.6, 11]], { fields }));
  assert.equal(tr.t[0], 11);
  assert.ok(Math.abs(tr.x[0] - 0.6) < 1e-6);
  assert.ok(Math.abs(tr.y[0] - 0.3) < 1e-6);
  assert.equal(tr.ring[0], "#f5f5f5");
  assert.equal(tr.flags[0], 4);
});

test("parseTrack: rejects bad documents", () => {
  assert.throws(() => parseTrack(null), TrackError);
  assert.throws(() => parseTrack({ ...doc([]), schema_version: 2 }), /schema_version/);
  assert.throws(() => parseTrack({ ...doc([]), video: { fps: 0 } }), /fps/);
  assert.throws(() => parseTrack(doc([], { fields: ["t", "x"] })), /missing "y"/);
  assert.throws(() => parseTrack(doc([[10, "a", 0.5, 0.01, 0.9, "#101010", 0]])), /frames\[0\]\.x/);
  assert.throws(() => parseTrack(doc(["nope"])), /not an array/);
});

test("parseTrack: sorts unsorted rows and segments, sanitises colours", () => {
  const tr = parseTrack(doc([row(12, 0.2), row(11, 0.1, 0.5, 0.01, 0.9, "red")], { segments: [{ start: 30, end: 40 }, { start: 10, end: 20 }, { start: 5, end: 5 }] }));
  assert.deepEqual(Array.from(tr.t), [11, 12]);
  assert.equal(tr.ring[0], "#f5f5f5");
  assert.deepEqual(tr.segments.map((s) => s.start), [10, 30]);
});

test("floorIndex: binary search matches a linear scan", () => {
  const arr = new Float64Array([1, 2, 2.5, 4, 8, 9]);
  const linear = (v) => { let k = -1; for (let i = 0; i < arr.length; i++) if (arr[i] <= v) k = i; return k; };
  for (const v of [0, 1, 1.5, 2, 2.49, 2.5, 3, 4, 7.99, 8, 9, 100]) {
    assert.equal(floorIndex(arr, v), linear(v), `v=${v}`);
    for (let hint = -1; hint < arr.length; hint++) assert.equal(floorIndex(arr, v, hint), linear(v), `v=${v} hint=${hint}`);
  }
  assert.equal(floorIndex(new Float64Array(0), 5), -1);
});

test("floorIndex: random arrays", () => {
  for (let trial = 0; trial < 50; trial++) {
    const n = 1 + Math.floor(Math.random() * 200);
    const arr = Float64Array.from({ length: n }, () => Math.random() * 100).sort();
    for (let k = 0; k < 50; k++) {
      const v = Math.random() * 110 - 5;
      let exp = -1;
      for (let i = 0; i < n; i++) if (arr[i] <= v) exp = i;
      assert.equal(floorIndex(arr, v, Math.floor(Math.random() * n)), exp);
    }
  }
});

test("segmentIndex: inclusive bounds, gaps are outside", () => {
  const tr = parseTrack(doc([], { segments: [{ start: 10, end: 20 }, { start: 30, end: 40 }] }));
  assert.equal(segmentIndex(tr, 9.99), -1);
  assert.equal(segmentIndex(tr, 10), 0);
  assert.equal(segmentIndex(tr, 20), 0);
  assert.equal(segmentIndex(tr, 25), -1);
  assert.equal(segmentIndex(tr, 30), 1);
  assert.equal(segmentIndex(tr, 40.5), -1);
});

// fps 50: interpolate if rows <= 0.05 s apart; otherwise nearest within 0.02 s.
test("sampleTrack: interpolates x, y, r between close rows", () => {
  const tr = parseTrack(doc([row(10.0, 0.2, 0.4, 0.010), row(10.04, 0.4, 0.6, 0.020, 0.9, "#f5f5f5")]));
  const s = createSample();
  assert.equal(sampleTrack(tr, 10.01, s), true);
  assert.equal(s.mode, "interp");
  assert.ok(Math.abs(s.x - 0.25) < 1e-6);
  assert.ok(Math.abs(s.y - 0.45) < 1e-6);
  assert.ok(Math.abs(s.r - 0.0125) < 1e-6);
  assert.equal(s.ring, "#101010", "ring comes from the nearest row");
  sampleTrack(tr, 10.03, s);
  assert.equal(s.ring, "#f5f5f5");
});

test("sampleTrack: gap exactly 2.5/fps still interpolates", () => {
  const tr = parseTrack(doc([row(10.0, 0.2), row(10.05, 0.3)]));
  const s = createSample();
  assert.equal(sampleTrack(tr, 10.025, s), true);
  assert.equal(s.mode, "interp");
  assert.ok(Math.abs(s.x - 0.25) < 1e-6);
});

test("sampleTrack: gap above 2.5/fps uses nearest within 1/fps, else nothing", () => {
  const tr = parseTrack(doc([row(10.0, 0.2), row(10.06, 0.8)]));
  const s = createSample();
  assert.equal(sampleTrack(tr, 10.015, s), true);
  assert.equal(s.mode, "nearest");
  assert.equal(s.row, 0);
  assert.ok(Math.abs(s.x - 0.2) < 1e-6);
  assert.equal(sampleTrack(tr, 10.02, s), true, "exactly 1/fps away is still drawn");
  assert.equal(sampleTrack(tr, 10.045, s), true);
  assert.equal(s.row, 1);
  assert.ok(Math.abs(s.x - 0.8) < 1e-6);
  assert.equal(sampleTrack(tr, 10.03, s), false, "0.03 s from both rows");
  assert.equal(s.mode, "none");
});

test("sampleTrack: before the first / after the last row", () => {
  const tr = parseTrack(doc([row(11.0, 0.5), row(11.02, 0.6)]));
  const s = createSample();
  assert.equal(sampleTrack(tr, 10.99, s), true);
  assert.equal(s.mode, "nearest");
  assert.equal(sampleTrack(tr, 10.97, s), false);
  assert.equal(sampleTrack(tr, 11.035, s), true);
  assert.equal(s.row, 1);
  assert.equal(sampleTrack(tr, 11.05, s), false);
});

test("sampleTrack: exact row time", () => {
  const tr = parseTrack(doc([row(11.0, 0.5), row(11.02, 0.6)]));
  const s = createSample();
  assert.equal(sampleTrack(tr, 11.02, s), true);
  assert.ok(Math.abs(s.x - 0.6) < 1e-6);
});

test("sampleTrack: nothing outside segments even near a row", () => {
  const tr = parseTrack(doc([row(20.0, 0.5)], { segments: [{ start: 10, end: 20 }] }));
  const s = createSample();
  assert.equal(sampleTrack(tr, 20.0, s), true);
  assert.equal(sampleTrack(tr, 20.01, s), false);
  assert.equal(s.mode, "outside");
});

test("sampleTrack: confidence threshold", () => {
  const tr = parseTrack(doc([row(10.0, 0.5, 0.5, 0.01, 0.4), row(10.02, 0.5, 0.5, 0.01, 0.8)]));
  const s = createSample();
  assert.equal(sampleTrack(tr, 10.0, s, 0.5), false);
  assert.equal(s.mode, "lowconf");
  assert.equal(sampleTrack(tr, 10.0, s, 0.3), true);
  // Interpolated conf 0.6 at the midpoint.
  assert.equal(sampleTrack(tr, 10.01, s, 0.5), true);
  assert.ok(Math.abs(s.conf - 0.6) < 1e-6);
  assert.equal(sampleTrack(tr, 10.01, s, 0.65), false);
});

test("sampleTrack: uses fps from the file (25 fps widens the windows)", () => {
  const tr = parseTrack(doc([row(10.0, 0.2), row(10.1, 0.4)], { fps: 25 }));
  const s = createSample();
  assert.equal(sampleTrack(tr, 10.05, s), true);
  assert.equal(s.mode, "interp");
  assert.ok(Math.abs(s.x - 0.3) < 1e-6);
});

test("sampleTrack: sequential playback with reused sample matches fresh samples", () => {
  const frames = [];
  for (let i = 0; i < 500; i++) if (i % 7 !== 3) frames.push(row(10 + i * 0.02, (i % 100) / 100, 0.5, 0.01, 0.5 + ((i * 37) % 50) / 100));
  const tr = parseTrack(doc(frames));
  const seq = createSample();
  for (let t = 9.9; t < 20.1; t += 0.0137) {
    const fresh = createSample();
    const a = sampleTrack(tr, t, seq);
    const b = sampleTrack(tr, t, fresh);
    assert.equal(a, b);
    assert.equal(seq.mode, fresh.mode);
    if (a) assert.equal(seq.x, fresh.x);
  }
});
