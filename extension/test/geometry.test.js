import { test } from "node:test";
import assert from "node:assert/strict";
import { contentRect, ringGeometry, ringShape, traceRing, STADIUM_MIN_RATIO, parseClock, formatClock, defaultRange, videoIdFromUrl } from "../src/geometry.js";
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
  assert.equal(s.shape, "stadium");
  assert.equal(normalizeSettings({ shape: "circle" }).shape, "circle");
  assert.equal(normalizeSettings({ shape: "hexagon" }).shape, "stadium");
  assert.equal(normalizeSettings({ ratio: 1.0 }).ratio, 1.1);
  assert.equal(DEFAULT_SETTINGS.ratio, 1.2);
  assert.equal(DEFAULT_SETTINGS.minStroke, 1.5);
  assert.equal(DEFAULT_SETTINGS.minConf, 0.5);
});

// ---- stadium ring -------------------------------------------------------------

/** Records a path and flattens it into points (arcs sampled every ~1 degree). */
function recorder() {
  const ops = [];
  return {
    ops,
    beginPath: () => ops.push(["begin"]),
    arc: (x, y, r, a0, a1) => ops.push(["arc", x, y, r, a0, a1]),
    lineTo: (x, y) => ops.push(["line", x, y]),
    closePath: () => ops.push(["close"]),
    points() {
      const pts = [];
      let start = null;
      let last = null;
      const add = (p) => { if (!start) start = p; if (last) segment(last, p); last = p; };
      const segment = (p, q) => { for (let i = 1; i <= 20; i++) pts.push([p[0] + (q[0] - p[0]) * i / 20, p[1] + (q[1] - p[1]) * i / 20]); };
      for (const op of ops) {
        if (op[0] === "arc") {
          const [, x, y, r, a0, a1] = op;
          const n = Math.max(2, Math.ceil(Math.abs(a1 - a0) / (Math.PI / 180)));
          for (let i = 0; i <= n; i++) {
            const a = a0 + (a1 - a0) * i / n;
            const p = [x + r * Math.cos(a), y + r * Math.sin(a)];
            if (i === 0 && last) segment(last, p); // canvas joins with a line
            if (!start) start = p;
            pts.push(p);
            last = p;
          }
        } else if (op[0] === "line") add([op[1], op[2]]);
        else if (op[0] === "close" && last && start) segment(last, start);
      }
      return pts;
    },
  };
}

function distToSegment(p, a, b) {
  const vx = b[0] - a[0], vy = b[1] - a[1];
  const L2 = vx * vx + vy * vy;
  const t = L2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L2)) : 0;
  return Math.hypot(p[0] - a[0] - t * vx, p[1] - a[1] - t * vy);
}

test("ringShape: threshold sl >= 0.5 r picks the stadium", () => {
  assert.equal(STADIUM_MIN_RATIO, 0.5);
  assert.equal(ringShape(10, 4.99, 0).kind, "circle");
  assert.equal(ringShape(10, 4.99, 0).half, 0);
  assert.equal(ringShape(10, 5, 0).kind, "stadium");
  assert.equal(ringShape(10, 5, 0).half, 5);
  assert.equal(ringShape(10, 0, 0).kind, "circle");
  assert.equal(ringShape(0, 0, 0).kind, "circle");
  assert.equal(ringShape(10, 40, 1, 1.2, 1.5, "circle").kind, "circle", "circle-only setting");
});

test("ringShape: same width/clamp maths as the circle, inner edge on the streak", () => {
  const g = ringShape(20, 60, 0.3);
  near(g.width, 4);
  near(g.radius, 22);
  near(g.radius - g.width / 2, 20);
  const c = ringShape(5, 20, 0.3); // 0.2 * 5 = 1 px -> clamped to 1.5
  near(c.width, 1.5);
  near(c.radius, 5.75);
  near(c.radius - c.width / 2, 5);
  assert.equal(c.clamped, true);
  const r = ringShape(10, 30, 0, 1.5, 2);
  near(r.width, 5);
  near(r.radius, 12.5);
});

test("traceRing: circle is one full arc", () => {
  const ctx = recorder();
  traceRing(ctx, 50, 60, ringShape(10, 2, 1));
  assert.deepEqual(ctx.ops[0], ["begin"]);
  assert.equal(ctx.ops.length, 2);
  assert.deepEqual(ctx.ops[1].slice(0, 4), ["arc", 50, 60, 11]);
  near(ctx.ops[1][5] - ctx.ops[1][4], Math.PI * 2);
});

for (const sa of [0, Math.PI / 2, Math.PI / 6, -2, 3.0, Math.PI]) {
  test(`traceRing: stadium outline at sa=${sa.toFixed(3)} is the streak offset by the ring radius`, () => {
    const rPx = 8, slPx = 30, cx = 100, cy = 70;
    const g = ringShape(rPx, slPx, sa);
    const ctx = recorder();
    traceRing(ctx, cx, cy, g);
    const kinds = ctx.ops.map((o) => o[0]);
    assert.deepEqual(kinds, ["begin", "arc", "line", "arc", "close"], "two arcs and two lines in one path");
    // Cap centres sit on the axis, rotated by sa.
    const A = [cx - slPx * Math.cos(sa), cy - slPx * Math.sin(sa)];
    const B = [cx + slPx * Math.cos(sa), cy + slPx * Math.sin(sa)];
    near(ctx.ops[1][1], B[0], 1e-9);
    near(ctx.ops[1][2], B[1], 1e-9);
    near(ctx.ops[3][1], A[0], 1e-9);
    near(ctx.ops[3][2], A[1], 1e-9);
    // Each cap sweeps exactly a half turn.
    near(ctx.ops[1][5] - ctx.ops[1][4], Math.PI);
    near(ctx.ops[3][5] - ctx.ops[3][4], Math.PI);
    // Every point of the path is at distance R from the streak's axis segment.
    const pts = ctx.points();
    assert.ok(pts.length > 300);
    for (const p of pts) near(distToSegment(p, A, B), g.radius, 1e-6);
    // The path reaches both tips (axis ends pushed out by R) and both sides.
    const tip = (s) => [cx + s * (slPx + g.radius) * Math.cos(sa), cy + s * (slPx + g.radius) * Math.sin(sa)];
    const side = (s) => [cx - s * g.radius * Math.sin(sa), cy + s * g.radius * Math.cos(sa)];
    for (const q of [tip(1), tip(-1), side(1), side(-1)]) {
      const best = Math.min(...pts.map((p) => Math.hypot(p[0] - q[0], p[1] - q[1])));
      assert.ok(best < 0.2, `path passes near ${q.map((v) => v.toFixed(2))} (closest ${best.toFixed(3)})`);
    }
    // The first arc starts where closePath ends the second side (continuity).
    const a0 = ctx.ops[1][4];
    const start = [B[0] + g.radius * Math.cos(a0), B[1] + g.radius * Math.sin(a0)];
    const a1 = ctx.ops[3][5];
    const end = [A[0] + g.radius * Math.cos(a1), A[1] + g.radius * Math.sin(a1)];
    near(distToSegment(start, A, B), g.radius, 1e-9);
    near(Math.hypot(start[0] - end[0] - (B[0] - A[0]), start[1] - end[1] - (B[1] - A[1])), 0, 1e-9);
    // The explicit lineTo lands on the start of the second arc.
    const a2 = ctx.ops[3][4];
    near(ctx.ops[2][1], A[0] + g.radius * Math.cos(a2), 1e-9);
    near(ctx.ops[2][2], A[1] + g.radius * Math.sin(a2), 1e-9);
  });
}
