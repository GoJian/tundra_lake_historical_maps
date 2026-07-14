"""Lake segmentation with the Segment Anything Model (SAM), constrained to water.

Two strategies:

* ``seeded`` (default): use an MNDWI/NDWI water prior to place point prompts at
  the centroid of each candidate water body, then let SAM refine each boundary.
  Fast and inherently constrained to water (rejects roads, shadows, etc.).
* ``auto``: SAM automatic mask generation over the whole chip, then keep only
  masks that sufficiently overlap the water prior.

Works on both modern satellite composites (via :mod:`utils.imagery`) and warped
historic map COGs (which are RGB, so SAM sees drawn lakes as blobs). Returns a
boolean mask on the input grid, ready for :func:`utils.metrics.lake_metrics`.

Requires ``segment-geospatial`` (installs ``segment-anything`` + torch). Runs on
GPU when available.
"""

from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from scipy import ndimage as ndi

_SAM_CKPTS = {
    "vit_h": ("sam_vit_h_4b8939.pth",
              "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth"),
    "vit_l": ("sam_vit_l_0b3195.pth",
              "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_l_0b3195.pth"),
    "vit_b": ("sam_vit_b_01ec64.pth",
              "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth"),
}

_MODEL_DIR = os.environ.get(
    "TUNDRA_SAM_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "models", "sam"),
)


def _checkpoint(model_type: str) -> str:
    fname, url = _SAM_CKPTS[model_type]
    os.makedirs(_MODEL_DIR, exist_ok=True)
    path = os.path.join(_MODEL_DIR, fname)
    if not os.path.exists(path):
        print(f"[segment] downloading SAM {model_type} checkpoint -> {path}")
        urllib.request.urlretrieve(url, path)
    return path


class _SamCache:
    """Lazily build and cache the SAM model / predictor / mask generator."""
    _predictor = None
    _generator = None
    _model_type = None

    @classmethod
    def get(cls, model_type: str, device: Optional[str] = None):
        import torch
        from segment_anything import (sam_model_registry, SamPredictor,
                                       SamAutomaticMaskGenerator)
        if cls._model_type != model_type or cls._predictor is None:
            dev = device or ("cuda" if torch.cuda.is_available() else "cpu")
            sam = sam_model_registry[model_type](checkpoint=_checkpoint(model_type))
            sam.to(dev)
            cls._predictor = SamPredictor(sam)
            cls._generator = SamAutomaticMaskGenerator(
                sam, points_per_side=32, pred_iou_thresh=0.86,
                stability_score_thresh=0.92, min_mask_region_area=25)
            cls._model_type = model_type
        return cls._predictor, cls._generator


@dataclass
class SegResult:
    mask: np.ndarray              # boolean, input grid
    n_objects: int
    strategy: str


def _water_blobs(water_prior: np.ndarray, min_seed_px: int, connectivity: int = 8):
    """Yield (centroid_col, centroid_row, x0, y0, x1, y1, area_px) per water blob."""
    struct = ndi.generate_binary_structure(2, 2 if connectivity == 8 else 1)
    lbl, n = ndi.label(water_prior, structure=struct)
    if n == 0:
        return []
    out = []
    objs = ndi.find_objects(lbl)
    for i, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        blob = lbl[sl] == i
        area = int(blob.sum())
        if area < min_seed_px:
            continue
        ys, xs = np.where(blob)
        y0, x0 = sl[0].start, sl[1].start
        cy = y0 + ys.mean()
        cx = x0 + xs.mean()
        out.append((cx, cy, x0, y0, sl[1].stop, sl[0].stop, area))
    return out


def segment_water(
    rgb: np.ndarray,
    *,
    water_prior: Optional[np.ndarray] = None,
    strategy: str = "seeded",
    model_type: str = "vit_h",
    device: Optional[str] = None,
    min_seed_px: int = 6,
    water_overlap: float = 0.5,
    box_pad: int = 4,
    max_grow: float = 6.0,
    min_water_frac: float = 0.35,
    multimask: bool = True,
) -> SegResult:
    """Segment lakes in an RGB chip (H,W,3 uint8).

    seeded: needs ``water_prior`` (bool, e.g. MNDWI>0). For each water blob it
    prompts SAM with the blob's bounding box + centroid point, so SAM refines
    that specific lake without running away into surrounding terrain. Each
    returned mask is validated: rejected if it grows more than ``max_grow`` x the
    seed blob, or if less than ``min_water_frac`` of it overlaps the water prior.
    auto: SAM automatic masks filtered by overlap with ``water_prior`` (if given).
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("rgb must be H,W,3 uint8")
    predictor, generator = _SamCache.get(model_type, device)
    H, W = rgb.shape[:2]
    out = np.zeros((H, W), bool)

    if strategy == "seeded":
        if water_prior is None:
            raise ValueError("seeded strategy requires water_prior")
        blobs = _water_blobs(water_prior, min_seed_px)
        if not blobs:
            return SegResult(out, 0, strategy)
        predictor.set_image(rgb)
        n = 0
        for (cx, cy, x0, y0, x1, y1, area) in blobs:
            box = np.array([max(0, x0 - box_pad), max(0, y0 - box_pad),
                            min(W, x1 + box_pad), min(H, y1 + box_pad)], dtype=float)
            masks, scores, _ = predictor.predict(
                point_coords=np.array([[cx, cy]]), point_labels=np.array([1]),
                box=box, multimask_output=multimask)
            m = masks[int(np.argmax(scores))]
            ma = int(m.sum())
            if ma == 0 or m.mean() > 0.5:
                continue
            # reject runaway masks that dwarf their seed or aren't mostly water
            if ma > max_grow * area:
                continue
            if water_prior[m].mean() < min_water_frac:
                continue
            out |= m
            n += 1
        return SegResult(out, n, strategy)

    elif strategy == "auto":
        anns = generator.generate(rgb)
        n = 0
        for a in anns:
            m = a["segmentation"]
            if water_prior is not None:
                inside = water_prior[m]
                if inside.size == 0 or inside.mean() < water_overlap:
                    continue
            out |= m
            n += 1
        return SegResult(out, n, strategy)

    raise ValueError(f"unknown strategy {strategy!r}")
