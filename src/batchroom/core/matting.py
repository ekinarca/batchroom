"""Edge refinement for mattes: a pure-numpy guided filter.

The guided filter (He et al. 2010) snaps a coarse alpha matte to the real
image edges without the heavyweight dependencies (numba/scipy) full
closed-form matting would pull in — the right cost/benefit for a CPU-first
self-hosted server. Complexity is O(N) via integral images.
"""

from __future__ import annotations

import numpy as np


def _box_sum(x: np.ndarray, r: int) -> np.ndarray:
    """Sum over a (2r+1)² window, windows clipped at the borders. float64 in/out."""
    h, w = x.shape
    # rows
    cs = np.empty((h + 1, w), dtype=np.float64)
    cs[0] = 0.0
    np.cumsum(x, axis=0, out=cs[1:])
    lo = np.clip(np.arange(h) - r, 0, h)
    hi = np.clip(np.arange(h) + r + 1, 0, h)
    x = cs[hi] - cs[lo]
    # cols
    cs = np.empty((h, w + 1), dtype=np.float64)
    cs[:, 0] = 0.0
    np.cumsum(x, axis=1, out=cs[:, 1:])
    lo = np.clip(np.arange(w) - r, 0, w)
    hi = np.clip(np.arange(w) + r + 1, 0, w)
    return cs[:, hi] - cs[:, lo]


def box_mean(x: np.ndarray, r: int) -> np.ndarray:
    """Border-aware sliding-window mean."""
    ones = np.ones_like(x, dtype=np.float64)
    return _box_sum(x.astype(np.float64), r) / _box_sum(ones, r)


def guided_filter(
    guide: np.ndarray, src: np.ndarray, radius: int = 8, eps: float = 1e-4
) -> np.ndarray:
    """Filter ``src`` guided by ``guide``; both float arrays in [0, 1], same shape."""
    guide = guide.astype(np.float64)
    src = src.astype(np.float64)
    mean_i = box_mean(guide, radius)
    mean_p = box_mean(src, radius)
    corr_ip = box_mean(guide * src, radius)
    corr_ii = box_mean(guide * guide, radius)
    var_i = corr_ii - mean_i * mean_i
    cov_ip = corr_ip - mean_i * mean_p
    a = cov_ip / (var_i + eps)
    b = mean_p - a * mean_i
    mean_a = box_mean(a, radius)
    mean_b = box_mean(b, radius)
    return np.clip(mean_a * guide + mean_b, 0.0, 1.0)


def refine_matte(rgb: np.ndarray, matte: np.ndarray, *, radius: int | None = None) -> np.ndarray:
    """Refine a uint8 matte against its uint8 RGB image. Returns uint8 matte.

    Radius scales with resolution so refinement strength is size-independent.
    """
    h, w = matte.shape
    if radius is None:
        radius = max(4, round(min(h, w) / 256) * 4)
    guide = rgb.astype(np.float64).mean(axis=2) / 255.0
    refined = guided_filter(guide, matte.astype(np.float64) / 255.0, radius=radius, eps=1e-4)
    # The filter pulls values slightly off the extremes; restore full
    # opacity/transparency in confident regions to avoid milky subjects.
    refined = np.clip((refined - 0.02) / 0.94, 0.0, 1.0)
    return np.rint(refined * 255.0).astype(np.uint8)
