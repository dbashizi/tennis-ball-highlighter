"""Per-frame ball candidate detection.

Two detectors share one interface. ``detect_batch(stacks)`` takes a list of
(current, other1, other2) frame triples and returns candidate lists:

* ``TrackNetDetector``: the pretrained TrackNet heatmap. We read the soft map
  ``1 - p(class 0)`` rather than the argmax, which recovers weak responses.
  Peaks are extracted with connected components.
* ``ClassicalDetector``: the fallback. It uses 3-frame differencing, a colour
  and brightness prior, and a small-blob filter inside an optional court ROI.

``run_detection`` streams frames from ffmpeg and detects camera cuts
(histogram difference) on the fly. Frames near a cut or at the clip start get
their temporal context from the *following* frames instead.
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .video import VideoInfo, iter_frames

TRACKNET_GDRIVE_ID = "1XEYZ4myUN7QT-NeBYJI0xteLsvs-ZAOl"
TRACKNET_FILENAME = "tracknet_yastrebksv.pt"
TRACKNET_SHA256 = "c735bc1a1b13a35f179c6492f778ef4ebb9bffd512a96f4d970b32e076653076"

Candidate = tuple[float, float, float]  # (x_px, y_px, score) in analysis-frame pixels


@dataclass
class DetectionResult:
    candidates: list[list[Candidate]]
    cuts: list[int]  # analysis-frame indices that start a new shot (0 excluded)
    width: int
    height: int
    detector: str
    fps_processed: float
    seconds: float


def default_weights_dir() -> Path:
    env = os.environ.get("TBH_WEIGHTS_DIR")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[3]
    if (repo / "pyproject.toml").exists():
        return repo / "data" / "weights"
    home = Path(os.environ.get("TBH_HOME", Path.home() / ".tbh"))
    return home / "weights"


def ensure_tracknet_weights(path: Path | None = None, download: bool = True) -> Path | None:
    path = Path(path) if path else default_weights_dir() / TRACKNET_FILENAME
    if path.exists() and path.stat().st_size > 1_000_000:
        return path
    if not download:
        return None
    try:
        import gdown

        path.parent.mkdir(parents=True, exist_ok=True)
        gdown.download(id=TRACKNET_GDRIVE_ID, output=str(path), quiet=True)
    except Exception:  # network or quota problems: the caller falls back
        return None
    return path if path.exists() and path.stat().st_size > 1_000_000 else None


# --------------------------------------------------------------------------- peaks

def heatmap_peaks(heat: np.ndarray, scale_x: float, scale_y: float, thresh: float = 0.1,
                  max_peaks: int = 4) -> list[Candidate]:
    """Connected-component peaks of a 0..1 heatmap, in analysis-frame pixels."""
    mask = (heat > thresh).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for k in range(1, n):
        x, y, w, h, _a = stats[k]
        sub = heat[y:y + h, x:x + w]
        m = lab[y:y + h, x:x + w] == k
        peak = float(sub[m].max())
        wts = np.where(m & (sub >= 0.5 * peak), sub, 0.0)
        ys, xs = np.mgrid[0:h, 0:w]
        s = wts.sum()
        cx = (xs * wts).sum() / s + x
        cy = (ys * wts).sum() / s + y
        # Pixel-centre convention: pixel i covers [i, i+1).
        out.append(((cx + 0.5) * scale_x, (cy + 0.5) * scale_y, peak))
    out.sort(key=lambda c: -c[2])
    return out[:max_peaks]


# --------------------------------------------------------------------------- TrackNet

def soft_heatmap(torch, logits):
    """``1 - softmax(logits)[:, 0]`` without materialising the 256-way softmax everywhere.

    It equals ``sigmoid(logsumexp(l[1:]) - l[0])``. The dense logsumexp is 3x
    slower on MPS than the network itself, so we use the lower bound
    ``sigmoid(max(l[1:]) - l[0])`` everywhere and compute the exact value only
    where that bound is not negligible. (Measured max error: < 0.01.)
    """
    d = logits[:, 1:].amax(1) - logits[:, 0]
    out = torch.sigmoid(d).float()
    idx = (d > -7).nonzero(as_tuple=True)
    if idx[0].numel():
        sel = logits.permute(0, 2, 3, 1)[idx].float()
        out[idx] = torch.sigmoid(torch.logsumexp(sel[:, 1:], 1) - sel[:, 0])
    return out


class TrackNetDetector:
    name = "tracknet-v2"
    input_size = (640, 360)

    def __init__(self, weights: Path, device: str | None = None, batch_size: int = 4,
                 thresh: float = 0.1) -> None:
        import torch

        from .models.tracknet import load_tracknet, pick_device

        self.torch = torch
        self.device = pick_device(device)
        try:
            self.model = load_tracknet(weights, self.device)
        except Exception:
            if self.device.type == "cpu":
                raise
            self.device = torch.device("cpu")
            self.model = load_tracknet(weights, self.device)
        self.batch_size = batch_size
        self.thresh = thresh

    def detect_batch(self, stacks: list[tuple[np.ndarray, np.ndarray, np.ndarray]],
                     full_size: tuple[int, int]) -> list[list[Candidate]]:
        torch = self.torch
        arr = np.stack([np.concatenate(s, axis=2) for s in stacks])  # B,H,W,9 uint8
        x = torch.from_numpy(arr).to(self.device)
        x = x.permute(0, 3, 1, 2).float().div_(255.0)
        with torch.no_grad():
            heat = soft_heatmap(torch, self.model(x)).cpu().numpy()
        sx = full_size[0] / self.input_size[0]
        sy = full_size[1] / self.input_size[1]
        return [heatmap_peaks(h, sx, sy, self.thresh) for h in heat]


# --------------------------------------------------------------------------- classical

class ClassicalDetector:
    """Classical fallback: 3-frame differencing, a colour/brightness prior, and a small-blob filter."""

    name = "classical-diff"

    def __init__(self, work_width: int = 960, roi: tuple[float, float, float, float] | None = None,
                 diff_thresh: int = 18, batch_size: int = 16) -> None:
        self.work_width = work_width
        self.roi = roi  # normalised (x0, y0, x1, y1)
        self.diff_thresh = diff_thresh
        self.batch_size = batch_size
        self.input_size: tuple[int, int] | None = None  # set by run_detection
        # Differencing uses the frames at t-step and t+step (symmetric 3-frame difference).
        self.stack_mode = "symmetric"
        self.step = 1

    def configure(self, width: int, height: int, fps: float = 25.0) -> None:
        self.step = 2 if fps > 37.5 else 1
        w = min(self.work_width, width)
        h = int(round(height * w / width / 2)) * 2
        self.input_size = (w, h)

    def _one(self, cur, a, b, full_size) -> list[Candidate]:
        w, h = self.input_size
        d1 = cv2.absdiff(cur, a).max(axis=2)
        d2 = cv2.absdiff(cur, b).max(axis=2)
        motion = np.minimum(d1, d2)
        mask = (motion > self.diff_thresh).astype(np.uint8)
        hsv = cv2.cvtColor(cur, cv2.COLOR_BGR2HSV)
        hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        yellow = (hue >= 18) & (hue <= 45) & (sat >= 70) & (val >= 110)
        whiteish = (val >= 170) & (sat <= 60)
        prior = (yellow | whiteish).astype(np.uint8)
        mask &= prior
        if self.roi:
            x0, y0, x1, y1 = self.roi
            roi = np.zeros_like(mask)
            roi[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = 1
            mask &= roi
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        n, lab, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        # Plausible ball diameter: 0.25%..2% of width, with streaks up to 4x as long.
        dmin, dmax = 0.0025 * w, 0.03 * w  # plausible diameter range (deinterlacing ghosts widen blobs)
        out = []
        for k in range(1, n):
            bw, bh, area = stats[k, 2], stats[k, 3], stats[k, 4]
            major, minor = max(bw, bh), max(1, min(bw, bh))
            if area < max(2, 0.5 * dmin * dmin) or minor > dmax or major > 4 * dmax or major / minor > 4.5:
                continue
            fill = area / float(bw * bh)
            if fill < 0.3:
                continue
            ys, xs = np.nonzero(lab == k)
            strength = float(np.mean(motion[ys, xs])) / 80.0
            score = float(np.clip(0.35 + 0.5 * min(1.0, strength) + 0.15 * fill, 0, 1))
            cx, cy = cents[k]
            out.append(((cx + 0.5) * full_size[0] / w, (cy + 0.5) * full_size[1] / h, score))
        out.sort(key=lambda c: -c[2])
        return out[:4]

    def detect_batch(self, stacks, full_size):
        return [self._one(*s, full_size) for s in stacks]


# --------------------------------------------------------------------------- cuts

class CutDetector:
    def __init__(self, bhatt_thresh: float = 0.35, gray_thresh: float = 22.0) -> None:
        self.prev_hist = None
        self.prev_small = None
        self.bhatt_thresh = bhatt_thresh
        self.gray_thresh = gray_thresh

    def is_cut(self, frame: np.ndarray) -> bool:
        small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 1.0, 0, cv2.NORM_L1)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.int16)
        cut = False
        if self.prev_hist is not None:
            d = cv2.compareHist(self.prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
            g = float(np.mean(np.abs(gray - self.prev_small)))
            cut = d > self.bhatt_thresh and g > self.gray_thresh
        self.prev_hist, self.prev_small = hist, gray
        return cut


# --------------------------------------------------------------------------- driver

def make_detector(kind: str = "auto", weights: Path | None = None, device: str | None = None,
                  batch_size: int = 4):
    """kind: 'tracknet', 'classical' or 'auto' (TrackNet if its weights are available)."""
    if kind in ("auto", "tracknet"):
        w = ensure_tracknet_weights(weights, download=True)
        if w is not None:
            return TrackNetDetector(w, device=device, batch_size=batch_size)
        if kind == "tracknet":
            raise RuntimeError("TrackNet weights unavailable (download failed)")
    return ClassicalDetector()


def run_detection(info: VideoInfo, detector, progress: Callable[[str, float, str], None] | None = None,
                  start_index: int = 0, end_index: int | None = None) -> DetectionResult:
    if isinstance(detector, ClassicalDetector) and detector.input_size is None:
        detector.configure(info.width, info.height, info.analysis_fps)
    symmetric = getattr(detector, "stack_mode", "") == "symmetric"
    step = getattr(detector, "step", 1)
    size = detector.input_size
    full = (info.width, info.height)
    total = (end_index if end_index is not None else len(info.pts)) - start_index
    cut_det = CutDetector()
    cuts: list[int] = []
    results: dict[int, list[Candidate]] = {}
    buf: deque[tuple[int, np.ndarray, int]] = deque()  # (idx, frame, shot_start)
    history: deque[tuple[int, np.ndarray, int]] = deque(maxlen=2)
    pending: list = []
    pending_idx: list[int] = []
    shot_start = start_index
    t0 = time.time()
    done = 0

    def flush():
        nonlocal done
        if not pending:
            return
        outs = detector.detect_batch(pending, full)
        for i, o in zip(pending_idx, outs):
            results[i] = o
        done += len(pending)
        pending.clear()
        pending_idx.clear()
        if progress:
            el = time.time() - t0
            progress("detect", min(1.0, done / max(1, total)),
                     f"frame {done}/{total} ({done / max(el, 1e-6):.1f} fps)")

    def emit(item, future: list):
        idx, fr, ss = item
        past = [h for h in history if h[2] == ss]
        fut = [f for f in future if f[2] == ss]
        if symmetric:
            # (cur, t-step, t+step), falling back to the nearest available frames.
            before = past[-step][1] if len(past) >= step else (past[-1][1] if past else None)
            after = fut[step - 1][1] if len(fut) >= step else (fut[-1][1] if fut else None)
            if before is None:
                before = fut[min(len(fut) - 1, step)][1] if fut else fr
            if after is None:
                after = past[max(0, len(past) - 1 - step)][1] if past else fr
            stack = (fr, before, after)
        elif len(past) == 2:
            stack = (fr, past[1][1], past[0][1])  # history is oldest->newest
        elif len(fut) >= 2:
            stack = (fr, fut[0][1], fut[1][1])
        elif len(past) == 1 and len(fut) == 1:
            stack = (fr, past[0][1], fut[0][1])
        else:
            others = [p[1] for p in past] + [f[1] for f in fut] + [fr, fr]
            stack = (fr, others[0], others[1])
        pending.append(stack)
        pending_idx.append(idx)
        history.append(item)
        if len(pending) >= detector.batch_size:
            flush()

    for idx, fr in iter_frames(info, size=size, start_index=start_index, end_index=end_index):
        fr = np.ascontiguousarray(fr)
        if idx > start_index and cut_det.is_cut(fr):
            cuts.append(idx)
            shot_start = idx
        elif idx == start_index:
            cut_det.is_cut(fr)
        buf.append((idx, fr, shot_start))
        if len(buf) == 3:
            emit(buf.popleft(), list(buf))
    while buf:
        emit(buf.popleft(), list(buf))
    flush()
    el = time.time() - t0
    n = done
    cands = [results.get(i, []) for i in range(start_index, start_index + n)]
    return DetectionResult(candidates=cands, cuts=cuts, width=info.width, height=info.height,
                           detector=detector.name, fps_processed=n / max(el, 1e-6), seconds=el)
