"""TrackNet ball-heatmap network.

Architecture: TrackNet as described in
    Y.-C. Huang, I.-N. Liao, C.-H. Chen, T.-U. Ik, W.-C. Peng,
    "TrackNet: A Deep Learning Network for Tracking High-speed and Tiny Objects
    in Sports Applications", AVSS 2019, arXiv:1907.03698.

Licence note: this module was written for this project from the paper's
description. It is NOT a copy of third-party source code. The pretrained
tennis weights we load come from the unofficial PyTorch implementation at
https://github.com/yastrebksv/TrackNet, which declares no licence (checked
2026-09-17; the GitHub API reports ``"license": null``). Parameter names here are
chosen to line up with that checkpoint's ``state_dict`` layout (``convN.block.{0,2}``)
so it can be loaded. The weights are downloaded at runtime into ``data/weights``
and are never redistributed with this repository. Because they carry no
licence, treat them as personal and research use only.

Input:  (B, 9, 360, 640), three BGR frames [t, t-1, t-2] stacked on channels,
        float in 0..1.
Output: (B, 256, 360, 640) logits. Each pixel is classified into one of 256
        heatmap intensity levels (the paper's depth-coded Gaussian heatmap).
"""

from __future__ import annotations

import torch
from torch import nn

INPUT_W = 640
INPUT_H = 360
N_FRAMES = 3


class ConvBlock(nn.Module):
    """3x3 conv, then ReLU, then BatchNorm (the order used by the checkpoint)."""

    def __init__(self, cin: int, cout: int) -> None:
        super().__init__()
        self.block = nn.Sequential(
            nn.Conv2d(cin, cout, kernel_size=3, padding=1, bias=True),
            nn.ReLU(),
            nn.BatchNorm2d(cout),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.block(x)


# (name, in, out) for the 18 conv layers. "P" is a 2x2 max-pool and "U" a 2x
# nearest-neighbour upsample. This is the VGG-16-style encoder and the mirrored
# decoder from the paper.
_LAYOUT: list[tuple[str, int, int] | str] = [
    ("conv1", 9, 64), ("conv2", 64, 64), "P",
    ("conv3", 64, 128), ("conv4", 128, 128), "P",
    ("conv5", 128, 256), ("conv6", 256, 256), ("conv7", 256, 256), "P",
    ("conv8", 256, 512), ("conv9", 512, 512), ("conv10", 512, 512), "U",
    ("conv11", 512, 256), ("conv12", 256, 256), ("conv13", 256, 256), "U",
    ("conv14", 256, 128), ("conv15", 128, 128), "U",
    ("conv16", 128, 64), ("conv17", 64, 64),
    ("conv18", 64, 256),
]


class TrackNet(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self._ops: list[str] = []
        for item in _LAYOUT:
            if isinstance(item, str):
                self._ops.append(item)
            else:
                name, cin, cout = item
                setattr(self, name, ConvBlock(cin, cout))
                self._ops.append(name)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for op in self._ops:
            if op == "P":
                x = nn.functional.max_pool2d(x, 2, 2)
            elif op == "U":
                x = nn.functional.interpolate(x, scale_factor=2, mode="nearest")
            else:
                x = getattr(self, op)(x)
        return x

    @torch.no_grad()
    def heatmap(self, x: torch.Tensor) -> torch.Tensor:
        """Return (B, H, W) heatmap in 0..1 (argmax class / 255, as trained)."""
        logits = self.forward(x)
        return logits.argmax(dim=1).to(torch.float32) / 255.0


def pick_device(prefer: str | None = None) -> torch.device:
    if prefer:
        return torch.device(prefer)
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def load_tracknet(weights_path, device: torch.device | None = None) -> TrackNet:
    device = device or pick_device()
    model = TrackNet()
    state = torch.load(str(weights_path), map_location="cpu", weights_only=True)
    if isinstance(state, dict) and "model_state" in state:  # tolerate wrapped checkpoints
        state = state["model_state"]
    model.load_state_dict(state, strict=True)
    model.eval().to(device)
    return model
