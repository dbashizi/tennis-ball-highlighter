import { test } from "node:test";
import assert from "node:assert/strict";
import { contentRect, ringGeometry, parseClock, formatClock, defaultRange, videoIdFromUrl } from "../src/geometry.js";
import { normalizeSettings, DEFAULT_SETTINGS } from "../src/settings.js";

const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test("contentRect: exact fit", () => {
  assert.deepEqual(contentRect(1280, 720, 1920, 1080), { x: 0, y: 0, w: 1280, h: 720 });
});

test("contentRect: pillarbox (4:3 video in a 16:9 box)", () => {
  const r = contentRect(1280, 720, 640, 480);
  near(r.w, 960);
  near(r.h, 720);
  near(r.x, 160);
  near(r.y, 0);
});

test("contentRect: letterbox (16:9 video in a 4:3 box)", () => {
  const r = contentRect(800, 600, 1920, 1080);
  near(r.w, 800);
  near(r.h, 450);
  near(r.x, 0);
  near(r.y, 75);
});

test("contentRect: fill and cover", () => {
  assert.deepEqual(contentRect(800, 600, 1920, 1080, "fill"), { x: 0, y: 0, w: 800, h: 600 });
  const c = contentRect(800, 600, 1920, 1080, "cover");
  near(c.h, 600);
  near(c.w, 1066.6666666666667);
  near(c.x, -133.33333333333334);
});

test("contentRect: unknown sizes", () => {
  assert.equal(contentRect(800, 600, 0, 0), null);
  assert.equal(contentRect(0, 600, 1920, 1080), null);
  assert.equal(contentRect(NaN, 600, 1920, 1080), null);
});

test("ringGeometry: default ratio gives radius 1.1r, width 0.2r", () => {
  const g = ringGeometry(20);
  near(g.radius, 22);
  near(g.width, 4);
  assert.equal(g.clamped, false);
  // inner edge on the ball, outer edge at 1.2r
  near(g.radius - g.width / 2, 20);
  near(g.radius + g.width / 2, 24);
});

test("ringGeometry: clamps thin strokes, keeping the inner edge at r", () => {
  const g = ringGeometry(5); // 0.2 * 5 = 1 px < 1.5
  near(g.width, 1.5);
  near(g.radius, 5.75);
  near(g.radius - g.width / 2, 5);
  assert.equal(g.clamped, true);
});

test("ringGeometry: boundary 7.5 px radius is exactly 1.5 px", () => {
  const g = ringGeometry(7.5);
  near(g.width, 1.5, 1e-9);
  near(g.radius, 8.25, 1e-9);
});

test("ringGeometry: custom ratio and minimum stroke", () => {
  const g = ringGeometry(10, 1.5, 2);
  near(g.width, 5);
  near(g.radius, 12.5);
  const h = ringGeometry(10, 1.1, 3);
  near(h.width, 3);
  near(h.radius, 11.5);
  const z = ringGeometry(0);
  near(z.width, 1.5);
  near(z.radius, 0.75);
});

test("parseClock / formatClock", () => {
  assert.equal(parseClock("5:59"), 359);
  assert.equal(parseClock("0:05"), 5);
  assert.equal(parseClock("1:02:03"), 3723);
  assert.equal(parseClock("  6:12 "), 372);
  assert.equal(parseClock("90"), 90);
  assert.equal(parseClock("12.5"), 12.5);
  assert.ok(Number.isNaN(parseClock("")));
  assert.ok(Number.isNaN(parseClock("5:60")));
  assert.ok(Number.isNaN(parseClock("1:60:00")));
  assert.ok(Number.isNaN(parseClock("abc")));
  assert.ok(Number.isNaN(parseClock("5:")));
  assert.equal(formatClock(359), "5:59");
  assert.equal(formatClock(5), "0:05");
  assert.equal(formatClock(3723), "1:02:03");
  assert.equal(formatClock(-3), "0:00");
  assert.equal(parseClock(formatClock(4321)), 4321);
});

test("defaultRange: current time +/- 30 s, clamped", () => {
  assert.deepEqual(defaultRange(365.4, 1200), { start: 335, end: 396 });
  assert.deepEqual(defaultRange(10, 1200), { start: 0, end: 40 });
  assert.deepEqual(defaultRange(1190, 1200), { start: 1160, end: 1200 });
  assert.deepEqual(defaultRange(100, NaN), { start: 70, end: 130 });
});

test("videoIdFromUrl", () => {
  assert.equal(videoIdFromUrl("https://www.youtube.com/watch?v=YTkyRTsiIaY&t=359s"), "YTkyRTsiIaY");
  assert.equal(videoIdFromUrl("https://www.youtube.com/watch?list=x&v=YTkyRTsiIaY"), "YTkyRTsiIaY");
  assert.equal(videoIdFromUrl("https://www.youtube.com/"), null);
  assert.equal(videoIdFromUrl("https://www.youtube.com/shorts/YTkyRTsiIaY"), null);
  assert.equal(videoIdFromUrl("https://evil.example/watch?v=YTkyRTsiIaY"), null);
  assert.equal(videoIdFromUrl("https://www.youtube.com/watch?v=short"), null);
  assert.equal(videoIdFromUrl("not a url"), null);
});

test("normalizeSettings: defaults and clamping", () => {
  assert.deepEqual(normalizeSettings(undefined), { ...DEFAULT_SETTINGS });
  const s = normalizeSettings({ enabled: false, ratio: 9, minStroke: -1, minConf: "0.7", debug: true, autoOffer: "x" });
  assert.equal(s.enabled, false);
  assert.equal(s.ratio, 1.5);
  assert.equal(s.minStroke, 0.5);
  assert.equal(s.minConf, 0.7);
  assert.equal(s.debug, true);
  assert.equal(s.autoOffer, true);
  assert.equal(normalizeSettings({ ratio: 1.0 }).ratio, 1.1);
  assert.equal(DEFAULT_SETTINGS.ratio, 1.2);
  assert.equal(DEFAULT_SETTINGS.minStroke, 1.5);
  assert.equal(DEFAULT_SETTINGS.minConf, 0.5);
});
