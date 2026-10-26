"""Edge refinement for mattes: a pure-numpy guided filter.

The guided filter (He et al. 2010) snaps a coarse alpha matte to the real
image edges without the heavyweight dependencies (numba/scipy) full
closed-form matting would pull in — the right cost/benefit for a CPU-first
self-hosted server. Complexity is O(N) via integral images.
"""

from __future__ import annotations

import numpy as np


def _window_counts(n: int, r: int) -> np.ndarray:
    """How many of ``n`` positions each clipped (2r+1) window covers, along one axis."""
    idx = np.arange(n)
    return (np.minimum(idx + r + 1, n) - np.maximum(idx - r, 0)).astype(np.float64)


def _box_sum(x: np.ndarray, r: int) -> np.ndarray:
    """Sum over a (2r+1)² window, windows clipped at the borders. float64 in/out."""
    h, w = x.shape
    # rows
    cs = np.empty((h + 1, w), dtype=np.float64)
    cs[0] = 0.0
    np.cumsum(x, axis=0, out=cs[1:])
    lo = np.clip(np.arange(h) - r, 0, h)
    hi = np.clip(np.arange(h) + r + 1, 0, h)
    x = cs[hi]
    x -= cs[lo]
    # cols
    cs = np.empty((h, w + 1), dtype=np.float64)
    cs[:, 0] = 0.0
    np.cumsum(x, axis=1, out=cs[:, 1:])
    del x
    lo = np.clip(np.arange(w) - r, 0, w)
    hi = np.clip(np.arange(w) + r + 1, 0, w)
    out = cs[:, hi]
    out -= cs[:, lo]
    return out


def box_mean(x: np.ndarray, r: int) -> np.ndarray:
    """Border-aware sliding-window mean."""
    h, w = x.shape
    out = _box_sum(x.astype(np.float64, copy=False), r)
    # the window sizes are separable: no second full-size box sum of ones
    out /= _window_counts(h, r)[:, None] * _window_counts(w, r)[None, :]
    return out


def guided_filter(
    guide: np.ndarray, src: np.ndarray, radius: int = 8, eps: float = 1e-4
) -> np.ndarray:
    """Filter ``src`` guided by ``guide``; both float arrays in [0, 1], same shape.

    Same arithmetic as the textbook form, but in place and freeing each
    intermediate as soon as it is used: a 24 MP photo stays at a few
    full-size float arrays instead of more than ten."""
    guide = guide.astype(np.float64, copy=False)
    src = src.astype(np.float64, copy=False)
    mean_i = box_mean(guide, radius)
    mean_p = box_mean(src, radius)
    cov_ip = box_mean(guide * src, radius)  # corr_ip - mean_i * mean_p
    cov_ip -= mean_i * mean_p
    var_i = box_mean(guide * guide, radius)  # corr_ii - mean_i * mean_i
    var_i -= mean_i * mean_i
    var_i += eps
    a = cov_ip
    a /= var_i
    del var_i
    b = mean_p
    b -= a * mean_i
    del mean_i
    mean_a = box_mean(a, radius)
    del a
    mean_b = box_mean(b, radius)
    del b
    mean_a *= guide
    mean_a += mean_b
    del mean_b
    return np.clip(mean_a, 0.0, 1.0, out=mean_a)


def refine_matte(rgb: np.ndarray, matte: np.ndarray, *, radius: int | None = None) -> np.ndarray:
    """Refine a uint8 matte against its uint8 RGB image. Returns uint8 matte.

    Radius scales with resolution so refinement strength is size-independent.
    """
    h, w = matte.shape
    if radius is None:
        radius = max(4, round(min(h, w) / 256) * 4)
    guide = rgb.mean(axis=2, dtype=np.float64)  # no float copy of all three channels
    guide /= 255.0
    src = matte.astype(np.float64)
    src /= 255.0
    refined = guided_filter(guide, src, radius=radius, eps=1e-4)
    del guide, src
    # The filter pulls values slightly off the extremes; restore full
    # opacity/transparency in confident regions to avoid milky subjects.
    refined -= 0.02
    refined /= 0.94
    np.clip(refined, 0.0, 1.0, out=refined)
    refined *= 255.0
    return np.rint(refined, out=refined).astype(np.uint8)
