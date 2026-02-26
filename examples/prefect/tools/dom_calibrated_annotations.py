#!/usr/bin/env python3
from __future__ import annotations

"""High-accuracy screenshot annotations using DOM coordinates + image calibration.

Why this exists
---------------
Manual scaling (for example multiplying all coordinates by 1.2) drifts as soon as
any of these change:
- browser/device pixel ratio
- Playwright screenshot scale mode ("device" vs "css")
- viewport size
- page zoom / OS display scaling
- scroll position when collecting element coordinates

This tool fixes that by:
1. collecting annotation target boxes in DOM/CSS coordinates (via Playwright locators or a JSON spec),
2. injecting color-coded calibration markers at known DOM/CSS coordinates,
3. detecting those markers in the captured screenshot with image processing,
4. fitting an affine transform from DOM/CSS -> image pixels,
5. drawing callouts on the clean screenshot using the fitted transform,
6. validating the fit (RMS/max pixel error) so bad captures fail loudly.

Subcommands
-----------
- self-test
    Synthetic test of the marker detector + affine fit (no browser required).
- render
    Render annotations onto an existing clean screenshot using:
      * a calibration screenshot (same page state + size, but with markers visible),
      * a metadata JSON file describing marker DOM points and callout boxes in DOM/CSS coordinates.
- capture-and-render
    Optional Playwright workflow: navigate a page, apply interactions, capture clean+calibration screenshots,
    fit transform, and emit the final annotated image + metadata.

Notes
-----
- The image-processing path is intentionally independent of Playwright. Once you have a clean screenshot,
  calibration screenshot, and metadata JSON, rendering is deterministic and repeatable.
- Marker detection uses local-window color segmentation around expected positions, which is robust to DPR,
  screenshot scaling, and minor layout changes.
"""

import argparse
import json
import math
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFont


# Eight distinct marker colors chosen to be uncommon in UI screenshots.
MARKER_COLORS: list[tuple[str, tuple[int, int, int]]] = [
    ("tl", (255, 0, 255)),      # magenta
    ("tr", (0, 255, 255)),      # cyan
    ("bl", (255, 255, 0)),      # yellow
    ("br", (255, 128, 0)),      # orange
    ("tc", (0, 255, 0)),        # green
    ("bc", (255, 0, 0)),        # red
    ("lc", (0, 128, 255)),      # azure
    ("rc", (128, 0, 255)),      # violet
]

ACCENT = (57, 136, 255, 255)
FILL = (57, 136, 255, 48)
LABEL_BG = (16, 20, 30, 230)
WHITE = (255, 255, 255, 255)


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float

    def corners(self) -> list[Point]:
        return [
            Point(self.x, self.y),
            Point(self.x + self.w, self.y),
            Point(self.x + self.w, self.y + self.h),
            Point(self.x, self.y + self.h),
        ]


@dataclass
class Affine2D:
    # x' = ax + by + c; y' = dx + ey + f
    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    def transform_point(self, p: Point) -> Point:
        return Point(
            self.a * p.x + self.b * p.y + self.c,
            self.d * p.x + self.e * p.y + self.f,
        )

    def transform_box_polygon(self, box: Box) -> list[Point]:
        return [self.transform_point(p) for p in box.corners()]

    def to_matrix(self) -> list[list[float]]:
        return [[self.a, self.b, self.c], [self.d, self.e, self.f]]


@dataclass
class FitStats:
    rms_error_px: float
    max_error_px: float
    per_point_error_px: dict[str, float]


def load_font(size: int):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


FONT_SMALL = load_font(19)


def _connected_components(mask: np.ndarray) -> list[np.ndarray]:
    """Return connected components as Nx2 arrays of (row, col) indices."""
    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=np.uint8)
    components: list[np.ndarray] = []

    for y in range(h):
        for x in range(w):
            if not mask[y, x] or visited[y, x]:
                continue
            q: deque[tuple[int, int]] = deque([(y, x)])
            visited[y, x] = 1
            pts: list[tuple[int, int]] = []
            while q:
                cy, cx = q.popleft()
                pts.append((cy, cx))
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = 1
                        q.append((ny, nx))
            components.append(np.array(pts, dtype=np.int32))
    return components


def _detect_marker_in_window(
    image_rgb: np.ndarray,
    color: tuple[int, int, int],
    expected_px: Point,
    search_radius: int = 120,
    color_tolerance: int = 18,
    min_pixels: int = 12,
) -> Point:
    h, w, _ = image_rgb.shape
    ex, ey = int(round(expected_px.x)), int(round(expected_px.y))
    x0 = max(0, ex - search_radius)
    x1 = min(w, ex + search_radius + 1)
    y0 = max(0, ey - search_radius)
    y1 = min(h, ey + search_radius + 1)
    window = image_rgb[y0:y1, x0:x1, :]

    target = np.array(color, dtype=np.int16)
    diff = np.abs(window.astype(np.int16) - target[None, None, :])
    mask = np.all(diff <= color_tolerance, axis=2)

    if not mask.any():
        raise RuntimeError(f"Marker color {color} not found near expected pixel {expected_px}.")

    components = _connected_components(mask)
    if not components:
        raise RuntimeError(f"No connected component found for marker color {color}.")

    # Choose the largest component (in window); ties broken by distance to expected point.
    candidates = []
    for comp in components:
        if len(comp) < min_pixels:
            continue
        rows = comp[:, 0].astype(np.float64)
        cols = comp[:, 1].astype(np.float64)
        cy = rows.mean() + y0
        cx = cols.mean() + x0
        dist2 = (cx - expected_px.x) ** 2 + (cy - expected_px.y) ** 2
        candidates.append((len(comp), dist2, Point(float(cx), float(cy))))

    if not candidates:
        raise RuntimeError(
            f"Only tiny components found for marker color {color} near {expected_px}; "
            "increase marker size or search radius."
        )

    candidates.sort(key=lambda t: (-t[0], t[1]))
    return candidates[0][2]


def detect_calibration_markers(
    calibration_image_path: Path,
    marker_css_points: dict[str, Point],
    doc_size_css: tuple[float, float],
    marker_colors: dict[str, tuple[int, int, int]],
    search_radius: int = 120,
    color_tolerance: int = 18,
) -> dict[str, Point]:
    """Detect injected markers in screenshot pixels using local-window color segmentation."""
    img = Image.open(calibration_image_path).convert("RGB")
    rgb = np.array(img)
    img_w, img_h = img.size
    doc_w, doc_h = doc_size_css
    if doc_w <= 0 or doc_h <= 0:
        raise ValueError(f"Invalid doc size: {doc_size_css}")

    sx = img_w / doc_w
    sy = img_h / doc_h

    detected: dict[str, Point] = {}
    for marker_id, css_point in marker_css_points.items():
        if marker_id not in marker_colors:
            raise KeyError(f"No color configured for marker {marker_id}")
        expected_px = Point(css_point.x * sx, css_point.y * sy)
        detected[marker_id] = _detect_marker_in_window(
            rgb,
            marker_colors[marker_id],
            expected_px,
            search_radius=search_radius,
            color_tolerance=color_tolerance,
        )
    return detected


def fit_affine(css_points: dict[str, Point], img_points: dict[str, Point]) -> tuple[Affine2D, FitStats]:
    keys = [k for k in css_points.keys() if k in img_points]
    if len(keys) < 4:
        raise ValueError(f"Need at least 4 point correspondences, got {len(keys)}")

    # Build least-squares system.
    # [x y 1 0 0 0] [a b c d e f]^T = x'
    # [0 0 0 x y 1] [...]             = y'
    A = []
    b = []
    for k in keys:
        cp = css_points[k]
        ip = img_points[k]
        A.append([cp.x, cp.y, 1.0, 0.0, 0.0, 0.0])
        A.append([0.0, 0.0, 0.0, cp.x, cp.y, 1.0])
        b.append(ip.x)
        b.append(ip.y)

    A_np = np.array(A, dtype=np.float64)
    b_np = np.array(b, dtype=np.float64)
    params, *_ = np.linalg.lstsq(A_np, b_np, rcond=None)
    aff = Affine2D(*[float(x) for x in params.tolist()])

    errs: dict[str, float] = {}
    sq = []
    max_err = 0.0
    for k in keys:
        pred = aff.transform_point(css_points[k])
        actual = img_points[k]
        err = math.hypot(pred.x - actual.x, pred.y - actual.y)
        errs[k] = err
        sq.append(err * err)
        max_err = max(max_err, err)

    rms = math.sqrt(sum(sq) / len(sq)) if sq else 0.0
    return aff, FitStats(rms_error_px=rms, max_error_px=max_err, per_point_error_px=errs)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))


def _rect_intersection_area(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return float((ix1 - ix0) * (iy1 - iy0))


def _compute_edge_strength(rgb_img: Image.Image) -> np.ndarray:
    arr = np.asarray(rgb_img.convert("RGB"), dtype=np.float32)
    gray = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    return np.hypot(gx, gy)


def _stripe_mean(edge_map: np.ndarray, orientation: str, pos: int, start: int, end: int, half_thickness: int = 2) -> float:
    h, w = edge_map.shape
    if orientation == "v":
        x0 = max(0, pos - half_thickness)
        x1 = min(w, pos + half_thickness + 1)
        y0 = max(0, start)
        y1 = min(h, end)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        return float(edge_map[y0:y1, x0:x1].mean())
    y0 = max(0, pos - half_thickness)
    y1 = min(h, pos + half_thickness + 1)
    x0 = max(0, start)
    x1 = min(w, end)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float(edge_map[y0:y1, x0:x1].mean())


def _refine_rect_to_edges(
    edge_map: np.ndarray,
    rect: tuple[float, float, float, float],
    max_shift_px: int = 28,
    stripe_px: int = 2,
    penalty_per_px: float = 0.35,
) -> tuple[float, float, float, float]:
    x0f, y0f, x1f, y1f = rect
    h, w = edge_map.shape
    x0 = int(round(_clamp(x0f, 0, w - 2)))
    y0 = int(round(_clamp(y0f, 0, h - 2)))
    x1 = int(round(_clamp(x1f, x0 + 1, w - 1)))
    y1 = int(round(_clamp(y1f, y0 + 1, h - 1)))
    if (x1 - x0) < 12 or (y1 - y0) < 12:
        return rect

    min_w = max(12, int((x1 - x0) * 0.5))
    min_h = max(12, int((y1 - y0) * 0.5))

    def best_pos(orientation: str, base_pos: int, start: int, end: int) -> int:
        lo = max(0 if orientation == "v" else 0, base_pos - max_shift_px)
        hi = min((w - 1) if orientation == "v" else (h - 1), base_pos + max_shift_px)
        best = (float("-inf"), base_pos)
        for p in range(lo, hi + 1):
            score = _stripe_mean(edge_map, orientation, p, start, end, half_thickness=stripe_px)
            score -= penalty_per_px * abs(p - base_pos)
            if score > best[0]:
                best = (score, p)
        return int(best[1])

    # A short interior margin reduces attraction to unrelated outer elements.
    y_margin = max(4, int((y1 - y0) * 0.08))
    x_margin = max(4, int((x1 - x0) * 0.08))
    vy0, vy1 = y0 + y_margin, y1 - y_margin
    hx0, hx1 = x0 + x_margin, x1 - x_margin

    rx0 = best_pos("v", x0, vy0, vy1)
    rx1 = best_pos("v", x1, vy0, vy1)
    if rx1 - rx0 < min_w:
        mid = (rx0 + rx1) // 2
        rx0 = max(0, mid - min_w // 2)
        rx1 = min(w - 1, rx0 + min_w)

    ry0 = best_pos("h", y0, hx0, hx1)
    ry1 = best_pos("h", y1, hx0, hx1)
    if ry1 - ry0 < min_h:
        mid = (ry0 + ry1) // 2
        ry0 = max(0, mid - min_h // 2)
        ry1 = min(h - 1, ry0 + min_h)

    return (float(rx0), float(ry0), float(rx1), float(ry1))


def _choose_label_position(
    preferred: tuple[float, float],
    label_size: tuple[float, float],
    target_rect: tuple[float, float, float, float],
    placed_labels: list[tuple[float, float, float, float]],
    img_size: tuple[int, int],
) -> tuple[float, float]:
    img_w, img_h = img_size
    tw, th = label_size
    tx_pref, ty_pref = preferred
    x0, y0, x1, y1 = target_rect

    # Candidate positions around the target rect, plus the preferred point first.
    candidates = [
        (tx_pref, ty_pref),
        (x0 + 8.0, y0 - (th + 10.0)),
        (x1 - tw - 8.0, y0 - (th + 10.0)),
        (((x0 + x1) / 2.0) - (tw / 2.0), y0 - (th + 12.0)),
        (x1 + 10.0, y0 + 4.0),
        (x1 + 10.0, ((y0 + y1) / 2.0) - (th / 2.0)),
        (x0 - tw - 10.0, y0 + 4.0),
        (x0 - tw - 10.0, ((y0 + y1) / 2.0) - (th / 2.0)),
        (x0 + 8.0, y1 + 8.0),
        (((x0 + x1) / 2.0) - (tw / 2.0), y1 + 8.0),
    ]

    best = (float("inf"), (_clamp(tx_pref, 4.0, img_w - tw - 4.0), _clamp(ty_pref, 4.0, img_h - th - 4.0)))
    target_rect_xyxy = target_rect
    for cand_x, cand_y in candidates:
        tx = _clamp(cand_x, 4.0, img_w - tw - 4.0)
        ty = _clamp(cand_y, 4.0, img_h - th - 4.0)
        label_rect = (tx, ty, tx + tw, ty + th)
        overlap_area = sum(_rect_intersection_area(label_rect, other) for other in placed_labels)
        target_overlap = _rect_intersection_area(label_rect, target_rect_xyxy)
        dist_penalty = math.hypot(tx - tx_pref, ty - ty_pref) * 0.05
        label_center = ((label_rect[0] + label_rect[2]) / 2.0, (label_rect[1] + label_rect[3]) / 2.0)
        target_center = ((target_rect_xyxy[0] + target_rect_xyxy[2]) / 2.0, (target_rect_xyxy[1] + target_rect_xyxy[3]) / 2.0)
        # Prefer placements that produce short connector lines between label and target.
        line_penalty = math.hypot(label_center[0] - target_center[0], label_center[1] - target_center[1]) * 0.01
        score = overlap_area * 100.0 + target_overlap * 2.0 + dist_penalty + line_penalty
        if score < best[0]:
            best = (score, (tx, ty))
    return best[1]


def _rect_center(rect: tuple[float, float, float, float]) -> tuple[float, float]:
    x0, y0, x1, y1 = rect
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _nearest_point_on_rect_perimeter(
    rect: tuple[float, float, float, float],
    point: tuple[float, float],
) -> tuple[float, float]:
    x0, y0, x1, y1 = rect
    px, py = point
    if x1 <= x0 or y1 <= y0:
        return (x0, y0)
    cx = _clamp(px, x0, x1)
    cy = _clamp(py, y0, y1)
    candidates = [
        (cx, y0),  # top
        (cx, y1),  # bottom
        (x0, cy),  # left
        (x1, cy),  # right
    ]
    return min(candidates, key=lambda p: (p[0] - px) ** 2 + (p[1] - py) ** 2)


def _target_anchor_point(rect: tuple[float, float, float, float], mode: str) -> tuple[float, float]:
    x0, y0, x1, y1 = rect
    if mode == "center":
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    if mode == "top-center":
        return ((x0 + x1) / 2.0, y0)
    if mode == "bottom-center":
        return ((x0 + x1) / 2.0, y1)
    if mode == "left-center":
        return (x0, (y0 + y1) / 2.0)
    if mode == "right-center":
        return (x1, (y0 + y1) / 2.0)
    if mode == "top-left":
        return (x0, y0)
    # Backward-compatible default: slightly inset from top-left.
    return (
        x0 + min(24.0, max(8.0, (x1 - x0) / 6.0)),
        y0 + 8.0,
    )


def draw_annotations(
    clean_image_path: Path,
    output_path: Path,
    affine: Affine2D,
    callouts: list[dict[str, Any]],
) -> None:
    img = Image.open(clean_image_path).convert("RGBA")
    edge_map = _compute_edge_strength(img.convert("RGB"))
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    placed_label_rects: list[tuple[float, float, float, float]] = []
    prepared: list[dict[str, Any]] = []

    for idx, callout in enumerate(callouts, start=1):
        box_vals = callout["box_css"]
        box = Box(float(box_vals[0]), float(box_vals[1]), float(box_vals[2]), float(box_vals[3]))
        poly = affine.transform_box_polygon(box)

        # Axis-aligned bounds for default label placement and line anchor.
        min_x = min(p.x for p in poly)
        min_y = min(p.y for p in poly)
        max_x = max(p.x for p in poly)
        max_y = max(p.y for p in poly)
        rect_xyxy = (min_x, min_y, max_x, max_y)

        auto_refine = ((max_x - min_x) >= 60.0 and (max_y - min_y) >= 50.0 and (max_x - min_x) * (max_y - min_y) >= 18000.0)
        do_refine = bool(callout.get("edge_refine", auto_refine))
        if do_refine:
            rect_xyxy = _refine_rect_to_edges(
                edge_map,
                rect_xyxy,
                max_shift_px=int(callout.get("edge_refine_max_shift_px", 28)),
                stripe_px=int(callout.get("edge_refine_stripe_px", 2)),
            )
            min_x, min_y, max_x, max_y = rect_xyxy

        poly_xy = [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)]

        prepared.append(
            {
                "idx": idx,
                "callout": callout,
                "rect_xyxy": (min_x, min_y, max_x, max_y),
                "poly_xy": poly_xy,
                "area": max(0.0, (max_x - min_x) * (max_y - min_y)),
            }
        )

    # Draw larger regions first so nested/smaller callouts remain visible on top.
    for item in sorted(prepared, key=lambda it: it["area"], reverse=True):
        poly_xy = item["poly_xy"]
        draw.polygon(poly_xy, fill=FILL)
        draw.line(poly_xy + [poly_xy[0]], fill=ACCENT, width=4, joint="curve")

    # Draw labels + connectors in the authored order (preserves numbering narrative).
    for item in sorted(prepared, key=lambda it: int(it["idx"])):
        idx = int(item["idx"])
        callout = dict(item["callout"])
        min_x, min_y, max_x, max_y = item["rect_xyxy"]
        label_css = callout.get("label_css")
        if label_css is not None:
            label_pt = affine.transform_point(Point(float(label_css[0]), float(label_css[1])))
            preferred_tx, preferred_ty = label_pt.x, label_pt.y
        else:
            preferred_tx, preferred_ty = min_x + 8.0, max(8.0, min_y - 38.0)

        text = f"{idx}. {callout['text']}"
        tw = float(draw.textlength(text, font=FONT_SMALL))
        th = 36.0
        label_w = tw + 18.0
        label_h = th + 8.0
        tx, ty = _choose_label_position(
            (preferred_tx, preferred_ty),
            (label_w, label_h),
            (min_x, min_y, max_x, max_y),
            placed_label_rects,
            (img.width, img.height),
        )

        draw.rounded_rectangle(
            [tx, ty, tx + label_w, ty + label_h],
            radius=8,
            fill=LABEL_BG,
            outline=ACCENT,
            width=2,
        )
        draw.text((tx + 9.0, ty + 7.0), text, font=FONT_SMALL, fill=WHITE)

        label_rect = (tx, ty, tx + label_w, ty + label_h)
        anchor_mode = callout.get("line_anchor", "top-left")
        target_pref = _target_anchor_point((min_x, min_y, max_x, max_y), str(anchor_mode))
        if callout.get("line_anchor_perimeter", True):
            ax, ay = _nearest_point_on_rect_perimeter((min_x, min_y, max_x, max_y), _rect_center(label_rect))
        else:
            ax, ay = target_pref
        lx, ly = _nearest_point_on_rect_perimeter(label_rect, (ax, ay))
        draw.line([(lx, ly), (ax, ay)], fill=ACCENT, width=3)
        placed_label_rects.append(label_rect)

    out = Image.alpha_composite(img, overlay).convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(output_path, quality=95)


def build_default_markers(doc_w: float, doc_h: float, margin: float = 22.0) -> dict[str, Point]:
    m = margin
    return {
        "tl": Point(m, m),
        "tr": Point(doc_w - m, m),
        "bl": Point(m, doc_h - m),
        "br": Point(doc_w - m, doc_h - m),
        "tc": Point(doc_w / 2.0, m),
        "bc": Point(doc_w / 2.0, doc_h - m),
        "lc": Point(m, doc_h / 2.0),
        "rc": Point(doc_w - m, doc_h / 2.0),
    }


def inject_markers_js(marker_css_points: dict[str, Point], marker_size_css: int = 18) -> str:
    marker_defs = []
    color_map = dict(MARKER_COLORS)
    for marker_id, point in marker_css_points.items():
        rgb = color_map[marker_id]
        marker_defs.append(
            {
                "id": marker_id,
                "x": point.x,
                "y": point.y,
                "size": marker_size_css,
                "rgb": list(rgb),
            }
        )
    marker_json = json.dumps(marker_defs)
    return f"""
(() => {{
  const existing = document.getElementById('__codex_calibration_markers__');
  if (existing) existing.remove();
  const container = document.createElement('div');
  container.id = '__codex_calibration_markers__';
  container.style.position = 'absolute';
  container.style.left = '0px';
  container.style.top = '0px';
  container.style.width = '0px';
  container.style.height = '0px';
  container.style.pointerEvents = 'none';
  container.style.zIndex = '2147483647';
  container.setAttribute('aria-hidden', 'true');
  const defs = {marker_json};
  for (const d of defs) {{
    const outer = document.createElement('div');
    outer.dataset.codexMarkerId = d.id;
    outer.style.position = 'absolute';
    outer.style.left = `${{d.x - d.size / 2}}px`;
    outer.style.top = `${{d.y - d.size / 2}}px`;
    outer.style.width = `${{d.size}}px`;
    outer.style.height = `${{d.size}}px`;
    outer.style.background = `rgb(${{d.rgb[0]}}, ${{d.rgb[1]}}, ${{d.rgb[2]}})`;
    outer.style.border = '2px solid black';
    outer.style.boxSizing = 'border-box';
    outer.style.borderRadius = '2px';
    container.appendChild(outer);
  }}
  (document.body || document.documentElement).appendChild(container);
}})();
"""


def remove_markers_js() -> str:
    return "document.getElementById('__codex_calibration_markers__')?.remove();"


def _prefect_close_modal(page) -> None:
    for sel in ["button:has-text('Skip')", "text=Skip", "button[aria-label='Close']"]:
        try:
            page.locator(sel).first.click(timeout=1200)
            page.wait_for_timeout(300)
            return
        except Exception:
            pass


def _capture_callout_boxes_via_playwright(page, callouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _resolve_locator(spec: Any, default_timeout_ms: int):
        if isinstance(spec, str):
            sel = spec
            nth = None
            timeout_ms = default_timeout_ms
        elif isinstance(spec, dict):
            sel = spec["selector"]
            nth = spec.get("nth")
            within = spec.get("within")
            timeout_ms_val = spec.get("timeout_ms", default_timeout_ms)
            timeout_ms = int(default_timeout_ms if timeout_ms_val is None else timeout_ms_val)
        else:
            raise ValueError(f"Unsupported selector spec type: {type(spec)!r}")
        if isinstance(spec, dict) and within:
            loc = page.locator(within).first.locator(sel)
        else:
            loc = page.locator(sel)
        if nth is not None:
            loc = loc.nth(int(nth))
        else:
            loc = loc.first
        return loc, timeout_ms

    resolved = []
    for c in callouts:
        out = dict(c)
        if "box_css" in c:
            resolved.append(out)
            continue
        selector = c.get("selector")
        selectors = c.get("selectors")
        if not selector and not selectors:
            raise ValueError(f"Callout missing box_css/selector/selectors: {c}")

        bbs = []
        default_timeout_ms = int(c.get("timeout_ms", 20000))
        selector_specs = selectors if selectors else [{"selector": selector, "nth": c.get("nth"), "timeout_ms": c.get("timeout_ms")}]
        for spec in selector_specs:
            loc, timeout_ms = _resolve_locator(spec, default_timeout_ms)
            loc.wait_for(timeout=timeout_ms)
            bb = loc.bounding_box()
            if bb is None:
                raise RuntimeError(f"No bounding box for selector spec {spec!r}")
            bbs.append(bb)

        bb = {
            "x": min(float(b["x"]) for b in bbs),
            "y": min(float(b["y"]) for b in bbs),
            "width": max(float(b["x"]) + float(b["width"]) for b in bbs) - min(float(b["x"]) for b in bbs),
            "height": max(float(b["y"]) + float(b["height"]) for b in bbs) - min(float(b["y"]) for b in bbs),
        }
        scroll = page.evaluate("() => ({x: window.scrollX, y: window.scrollY})")
        pad = c.get("pad_css", [0, 0, 0, 0])
        if len(pad) == 1:
            l = t = r = b = float(pad[0])
        elif len(pad) == 4:
            l, t, r, b = [float(v) for v in pad]
        else:
            raise ValueError(f"pad_css must have length 1 or 4, got {pad}")
        out["box_css"] = [
            float(bb["x"]) + float(scroll["x"]) - l,
            float(bb["y"]) + float(scroll["y"]) - t,
            float(bb["width"]) + l + r,
            float(bb["height"]) + t + b,
        ]
        # Allow label offsets relative to resolved DOM box top-left.
        if "label_offset_css" in c and "label_css" not in c:
            dx, dy = c["label_offset_css"]
            out["label_css"] = [out["box_css"][0] + float(dx), out["box_css"][1] + float(dy)]
        resolved.append(out)
    return resolved


def _playwright_capture_and_render(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - optional dependency path
        print(
            "Playwright is required for capture-and-render. Run with something like:\n"
            "  uv run --no-project --with playwright --with pillow --with numpy python ... capture-and-render ...\n"
            f"Import error: {exc}",
            file=sys.stderr,
        )
        return 2

    spec_path = Path(args.spec)
    spec = json.loads(spec_path.read_text())

    clean_path = Path(args.clean_screenshot)
    annotated_path = Path(args.annotated_out)
    metadata_out = Path(args.metadata_out) if args.metadata_out else None
    calibration_path = Path(args.calibration_screenshot) if args.calibration_screenshot else None
    if calibration_path is None:
        calibration_path = Path(tempfile.gettempdir()) / f"codex-calibration-{spec_path.stem}.png"

    viewport = spec.get("viewport", {"width": 1470, "height": 980})
    full_page = bool(spec.get("full_page", True))
    url = spec["url"]
    callouts = spec["callouts"]
    actions = spec.get("actions", [])
    screenshot_scale = spec.get("screenshot_scale", "device")
    device_scale_factor = float(spec.get("device_scale_factor", 1.0))
    wait_for_ms = int(spec.get("wait_for_ms", 1500))
    close_prefect_modal = bool(spec.get("close_prefect_modal", False))
    chromepath = spec.get("chromium_executable_path")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=chromepath)
        ctx = browser.new_context(
            viewport={"width": int(viewport["width"]), "height": int(viewport["height"])} ,
            device_scale_factor=device_scale_factor,
        )
        page = ctx.new_page()

        for rr in spec.get("route_rewrites", []):
            src_prefix = rr["source_prefix"]
            dst_prefix = rr["dest_prefix"]

            def make_rewriter(src_prefix=src_prefix, dst_prefix=dst_prefix):
                def _rewrite(route):
                    u = route.request.url
                    if u.startswith(src_prefix):
                        route.continue_(url=dst_prefix + u[len(src_prefix):])
                    else:
                        route.continue_()
                return _rewrite

            page.route(src_prefix + "**", make_rewriter())

        page.goto(url, wait_until=spec.get("wait_until", "domcontentloaded"), timeout=int(spec.get("timeout_ms", 60000)))
        if wait_for_ms:
            page.wait_for_timeout(wait_for_ms)
        if close_prefect_modal:
            _prefect_close_modal(page)

        if wait_selector := spec.get("wait_for_selector"):
            page.locator(wait_selector).first.wait_for(timeout=int(spec.get("wait_selector_timeout_ms", 30000)))

        for action in actions:
            kind = action["type"]
            sel = action.get("selector")
            timeout_ms = int(action.get("timeout_ms", 10000))
            if kind == "click":
                page.locator(sel).first.click(timeout=timeout_ms)
            elif kind == "wait":
                page.wait_for_timeout(int(action.get("ms", 500)))
            elif kind == "wait_for":
                page.locator(sel).first.wait_for(timeout=timeout_ms)
            elif kind == "eval":
                page.evaluate(action["script"])
            else:
                raise ValueError(f"Unsupported action type: {kind}")

        # Resolve callouts to DOM/CSS boxes after interactions.
        resolved_callouts = _capture_callout_boxes_via_playwright(page, callouts)

        metrics = page.evaluate(
            """() => ({
              scrollX: window.scrollX,
              scrollY: window.scrollY,
              innerWidth: window.innerWidth,
              innerHeight: window.innerHeight,
              devicePixelRatio: window.devicePixelRatio,
              docWidth: Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0, document.documentElement.clientWidth),
              docHeight: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0, document.documentElement.clientHeight),
            })"""
        )

        doc_w = float(metrics["docWidth"])
        doc_h = float(metrics["docHeight"])
        marker_css_points = build_default_markers(doc_w, doc_h, margin=float(spec.get("marker_margin_css", 22)))
        page.evaluate(inject_markers_js(marker_css_points, marker_size_css=int(spec.get("marker_size_css", 18))))
        page.wait_for_timeout(int(spec.get("marker_settle_ms", 150)))
        page.screenshot(path=str(calibration_path), full_page=full_page, scale=screenshot_scale)

        page.evaluate(remove_markers_js())
        page.wait_for_timeout(int(spec.get("marker_settle_ms", 150)))
        page.screenshot(path=str(clean_path), full_page=full_page, scale=screenshot_scale)
        browser.close()

    marker_color_map = {k: rgb for k, rgb in MARKER_COLORS}
    detected_markers = detect_calibration_markers(
        calibration_path,
        marker_css_points=marker_css_points,
        doc_size_css=(doc_w, doc_h),
        marker_colors=marker_color_map,
        search_radius=int(spec.get("marker_search_radius_px", 120)),
        color_tolerance=int(spec.get("marker_color_tolerance", 18)),
    )
    affine, fit = fit_affine(marker_css_points, detected_markers)

    max_err = float(spec.get("max_fit_error_px", 1.25))
    if fit.max_error_px > max_err:
        raise RuntimeError(
            f"Calibration fit too loose (max={fit.max_error_px:.3f}px, rms={fit.rms_error_px:.3f}px > allowed max={max_err}px)."
        )

    draw_annotations(clean_path, annotated_path, affine, resolved_callouts)

    if metadata_out:
        metadata = {
            "url": url,
            "full_page": full_page,
            "viewport": viewport,
            "metrics": metrics,
            "screenshot_scale": screenshot_scale,
            "device_scale_factor": device_scale_factor,
            "calibration_screenshot": str(calibration_path),
            "clean_screenshot": str(clean_path),
            "annotated_out": str(annotated_path),
            "marker_css_points": {k: [v.x, v.y] for k, v in marker_css_points.items()},
            "marker_img_points": {k: [v.x, v.y] for k, v in detected_markers.items()},
            "affine_matrix": affine.to_matrix(),
            "fit": {
                "rms_error_px": fit.rms_error_px,
                "max_error_px": fit.max_error_px,
                "per_point_error_px": fit.per_point_error_px,
            },
            "callouts": resolved_callouts,
        }
        metadata_out.parent.mkdir(parents=True, exist_ok=True)
        metadata_out.write_text(json.dumps(metadata, indent=2))

    print(f"Clean screenshot: {clean_path}")
    print(f"Calibration screenshot: {calibration_path}")
    print(f"Annotated screenshot: {annotated_path}")
    print(f"Calibration fit: rms={fit.rms_error_px:.3f}px max={fit.max_error_px:.3f}px")
    if metadata_out:
        print(f"Metadata: {metadata_out}")
    return 0


def _render_from_metadata(args: argparse.Namespace) -> int:
    meta = json.loads(Path(args.metadata).read_text())
    clean = Path(args.clean_screenshot or meta["clean_screenshot"])
    calibration = Path(args.calibration_screenshot or meta["calibration_screenshot"])
    out = Path(args.annotated_out or meta["annotated_out"])
    marker_css_points = {k: Point(*v) for k, v in meta["marker_css_points"].items()}
    marker_colors = {k: rgb for k, rgb in MARKER_COLORS}
    metrics = meta["metrics"]
    detected = detect_calibration_markers(
        calibration,
        marker_css_points,
        (float(metrics["docWidth"]), float(metrics["docHeight"])),
        marker_colors,
        search_radius=int(args.marker_search_radius_px),
        color_tolerance=int(args.marker_color_tolerance),
    )
    affine, fit = fit_affine(marker_css_points, detected)
    if fit.max_error_px > float(args.max_fit_error_px):
        raise RuntimeError(
            f"Calibration fit too loose (max={fit.max_error_px:.3f}px, rms={fit.rms_error_px:.3f}px)"
        )
    draw_annotations(clean, out, affine, meta["callouts"])
    print(f"Annotated screenshot: {out}")
    print(f"Calibration fit: rms={fit.rms_error_px:.3f}px max={fit.max_error_px:.3f}px")
    return 0


def _self_test(args: argparse.Namespace) -> int:
    # Synthetic document space and a known affine transform.
    doc_w, doc_h = 1470.0, 980.0
    marker_css_points = build_default_markers(doc_w, doc_h, margin=22)
    true_affine = Affine2D(
        a=1.1843,
        b=0.0017,
        c=3.2,
        d=-0.0009,
        e=1.1836,
        f=2.1,
    )

    img_w = int(round(true_affine.a * doc_w + true_affine.b * doc_h + true_affine.c + 8))
    img_h = int(round(true_affine.e * doc_h + true_affine.d * doc_w + true_affine.f + 8))
    img = Image.new("RGB", (img_w, img_h), (30, 34, 42))
    draw = ImageDraw.Draw(img)

    color_map = dict(MARKER_COLORS)
    # Draw synthetic markers (including black border like browser injection)
    marker_size = 18
    for marker_id, css_p in marker_css_points.items():
        ip = true_affine.transform_point(css_p)
        x0 = int(round(ip.x - marker_size / 2))
        y0 = int(round(ip.y - marker_size / 2))
        x1 = x0 + marker_size
        y1 = y0 + marker_size
        draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))
        draw.rectangle([x0 + 2, y0 + 2, x1 - 2, y1 - 2], fill=color_map[marker_id])

    # Add some random UI-ish rectangles/noise to prove local-window matching stays stable.
    rng = np.random.default_rng(7)
    for _ in range(120):
        x0 = int(rng.integers(0, img_w - 20))
        y0 = int(rng.integers(0, img_h - 20))
        x1 = x0 + int(rng.integers(6, 60))
        y1 = y0 + int(rng.integers(4, 30))
        color = tuple(int(v) for v in rng.integers(0, 255, size=3))
        draw.rectangle([x0, y0, min(x1, img_w - 1), min(y1, img_h - 1)], outline=color)

    with tempfile.TemporaryDirectory() as td:
        cal_path = Path(td) / "synthetic_cal.png"
        clean_path = Path(td) / "synthetic_clean.png"
        out_path = Path(td) / "synthetic_annotated.png"
        img.save(cal_path)
        img.save(clean_path)

        detected = detect_calibration_markers(
            cal_path,
            marker_css_points,
            (doc_w, doc_h),
            color_map,
            search_radius=90,
            color_tolerance=8,
        )
        fit_aff, fit = fit_affine(marker_css_points, detected)

        # Validate that we recover the transform to sub-pixel accuracy.
        if fit.max_error_px > float(args.max_allowed_error_px):
            raise RuntimeError(
                f"Self-test failed: max error {fit.max_error_px:.4f}px > allowed {args.max_allowed_error_px}px"
            )

        callouts = [
            {
                "text": "Synthetic panel",
                "box_css": [120, 100, 300, 160],
                "label_css": [450, 70],
            },
            {
                "text": "Synthetic footer",
                "box_css": [200, 800, 600, 120],
                "label_css": [830, 760],
            },
        ]
        draw_annotations(clean_path, out_path, fit_aff, callouts)
        print(f"Self-test OK: rms={fit.rms_error_px:.4f}px max={fit.max_error_px:.4f}px")
        print(f"Synthetic annotated preview: {out_path}")
    return 0


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_self = sub.add_parser("self-test", help="Run synthetic calibration/fit test (no browser required)")
    p_self.add_argument("--max-allowed-error-px", type=float, default=0.75)
    p_self.set_defaults(func=_self_test)

    p_render = sub.add_parser("render", help="Render annotations from clean+calibration screenshots and metadata JSON")
    p_render.add_argument("--metadata", required=True)
    p_render.add_argument("--clean-screenshot")
    p_render.add_argument("--calibration-screenshot")
    p_render.add_argument("--annotated-out")
    p_render.add_argument("--marker-search-radius-px", type=int, default=120)
    p_render.add_argument("--marker-color-tolerance", type=int, default=18)
    p_render.add_argument("--max-fit-error-px", type=float, default=1.25)
    p_render.set_defaults(func=_render_from_metadata)

    p_cap = sub.add_parser("capture-and-render", help="Capture page with Playwright, calibrate, and render annotations")
    p_cap.add_argument("--spec", required=True, help="JSON spec containing url, interactions, and callouts")
    p_cap.add_argument("--clean-screenshot", required=True)
    p_cap.add_argument("--annotated-out", required=True)
    p_cap.add_argument("--metadata-out")
    p_cap.add_argument("--calibration-screenshot", help="Optional explicit path for calibration screenshot")
    p_cap.set_defaults(func=_playwright_capture_and_render)

    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
