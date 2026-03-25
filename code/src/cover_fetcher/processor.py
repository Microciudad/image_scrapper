"""Image processing pipeline driven by a YAML configuration file.

Supported pipeline steps
------------------------
- ``rotate``        – rotate the image by a fixed number of degrees (clockwise).
- ``auto_brightness`` – stretch the image brightness histogram (linear stretch).
- ``auto_saturation`` – stretch the saturation channel histogram (linear stretch).
- ``desaturate``    – reduce color intensity by a configurable amount.
- ``crush_blacks``  – clip shadows below a black-point threshold to pure black.
- ``zoom``          – crop-and-rescale for zoom in/out around a focal point.
- ``resize``        – upscale (or downscale) to a target size using LANCZOS.
- ``watermark``     – overlay a semi-transparent watermark image.
- ``color_eq``      – multiply each RGB channel by an independent gain factor.
- ``jpeg``          – control final JPEG encoding quality/compression settings.

Each step is represented as a mapping in the ``pipeline`` list of the YAML::

    pipeline:
      - step: rotate
        degrees: 90
      - step: auto_brightness
        clip_percent: 2.0
      - step: auto_saturation
        clip_percent: 2.0
            - step: desaturate
                amount: 0.2
"""

from __future__ import annotations

from collections import Counter
import logging
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageEnhance
from PIL.Image import Image as PILImage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

DEFAULT_PIPELINE: list[dict[str, Any]] = [
    {"step": "auto_brightness", "clip_percent": 1.0},
    {"step": "auto_saturation", "clip_percent": 1.0},
]


def load_pipeline(path: str | Path) -> list[dict[str, Any]]:
    """Load and return the pipeline step list from a YAML file.

    Args:
        path: Path to the pipeline YAML file.

    Returns:
        List of step dicts as defined in the YAML ``pipeline`` key.

    Raises:
        FileNotFoundError: If the YAML file does not exist.
        ValueError: If the YAML is malformed or ``pipeline`` key is missing.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Pipeline config not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if not isinstance(config, dict) or "pipeline" not in config:
        raise ValueError(f"Pipeline YAML must contain a top-level 'pipeline' key: {path}")
    steps = config["pipeline"]
    if not isinstance(steps, list):
        raise ValueError(f"'pipeline' must be a list of step mappings in {path}")
    return steps


def apply_pipeline(image_bytes: bytes, steps: list[dict[str, Any]]) -> bytes:
    """Apply all pipeline *steps* to *image_bytes* and return the result.

    Args:
        image_bytes: Raw input image bytes (any format Pillow supports).
        steps: Ordered list of step dicts from :func:`load_pipeline`.

    Returns:
        Processed image encoded as JPEG bytes.
    """
    import io

    img: PILImage = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    jpeg_quality = 95
    jpeg_optimize = False
    jpeg_progressive = False

    for step_cfg in steps:
        step = step_cfg.get("step", "")
        logger.debug("Applying pipeline step: %s", step)
        if step == "stop":
            logger.info("Pipeline stopped early by 'stop' step")
            break
        cfg = _resolve_cfg(step_cfg)
        if step == "rotate":
            img = _rotate(img, cfg)
        elif step == "auto_brightness":
            img = _auto_brightness(img, cfg)
        elif step == "auto_saturation":
            img = _auto_saturation(img, cfg)
        elif step == "desaturate":
            img = _desaturate(img, cfg)
        elif step == "crush_blacks":
            img = _crush_blacks(img, cfg)
        elif step == "color_eq":
            img = _color_eq(img, cfg)
        elif step == "denoise":
            img = _denoise(img, cfg)
        elif step == "distress":
            img = _distress(img, cfg)
        elif step == "zoom":
            img = _zoom(img, cfg)
        elif step == "resize":
            img = _resize(img, cfg)
        elif step == "watermark":
            img = _watermark(img, cfg)
        elif step == "jpeg":
            jpeg_quality = max(1, min(100, int(cfg.get("quality", jpeg_quality))))
            jpeg_optimize = bool(cfg.get("optimize", jpeg_optimize))
            jpeg_progressive = bool(cfg.get("progressive", jpeg_progressive))
        else:
            logger.warning("Unknown pipeline step %r – skipping", step)

    buf = io.BytesIO()
    img.save(
        buf,
        format="JPEG",
        quality=jpeg_quality,
        optimize=jpeg_optimize,
        progressive=jpeg_progressive,
    )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _resolve_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *cfg* with all random-range values resolved.

    A random range is expressed as a mapping with a single ``random`` key
    containing a two-element list ``[min, max]``::

        degrees: {random: [-5, 5]}    # float in [-5, 5]
        clip_percent: {random: [0.1, 0.5]}

    Resolved once per step per image so every image in a batch can get a
    different value.
    """
    import random as _random

    resolved: dict[str, Any] = {}
    for key, value in cfg.items():
        if (
            isinstance(value, dict)
            and list(value.keys()) == ["random"]
            and isinstance(value["random"], list)
            and len(value["random"]) == 2
        ):
            lo, hi = float(value["random"][0]), float(value["random"][1])
            resolved_val = _random.uniform(lo, hi)
            logger.debug("Resolved random %s: [%s, %s] → %.4f", key, lo, hi, resolved_val)
            resolved[key] = resolved_val
        else:
            resolved[key] = value
    return resolved


def _rotate(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Rotate image clockwise by ``cfg['degrees']`` (default 0)."""
    degrees = float(cfg.get("degrees", 0))
    if degrees == 0:
        return img
    # Pillow rotates counter-clockwise; negate for clockwise semantics
    expand = bool(cfg.get("expand", True))
    fillcolor = _resolve_rotate_fillcolor(img, cfg)
    return img.rotate(-degrees, expand=expand, fillcolor=fillcolor)


def _resolve_rotate_fillcolor(img: PILImage, cfg: dict[str, Any]) -> tuple[int, int, int] | None:
    """Resolve rotate fill color from config.

    Supported values:
    - omitted / none: keep Pillow default behavior (black for RGB images)
    - border / edge / auto: use dominant outer border color from the source image
    - #RRGGBB: explicit hex color
    - [r, g, b]: explicit RGB sequence
    """
    fill = cfg.get("fill")
    if fill in (None, "", "none"):
        return None

    if isinstance(fill, str):
        lowered = fill.strip().lower()
        if lowered in {"border", "edge", "auto"}:
            return _dominant_border_color(img)
        if lowered.startswith("#") and len(lowered) == 7:
            return tuple(int(lowered[index:index + 2], 16) for index in (1, 3, 5))

    if isinstance(fill, (list, tuple)) and len(fill) == 3:
        return tuple(max(0, min(255, int(value))) for value in fill)

    logger.warning("Unsupported rotate fill value %r; using default fill", fill)
    return None


def _dominant_border_color(img: PILImage) -> tuple[int, int, int]:
    """Return the most common color found on the image perimeter."""
    rgb = img.convert("RGB")
    width, height = rgb.size
    if width <= 0 or height <= 0:
        return (0, 0, 0)

    sample_step = max(1, min(width, height) // 200)
    border_pixels: list[tuple[int, int, int]] = []

    for x in range(0, width, sample_step):
        border_pixels.append(rgb.getpixel((x, 0)))
        border_pixels.append(rgb.getpixel((x, height - 1)))

    for y in range(0, height, sample_step):
        border_pixels.append(rgb.getpixel((0, y)))
        border_pixels.append(rgb.getpixel((width - 1, y)))

    if not border_pixels:
        return (0, 0, 0)

    return Counter(border_pixels).most_common(1)[0][0]


def _auto_brightness(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Auto-stretch image brightness using histogram percentile clipping.

    ``clip_percent`` (default ``1.0``) controls how much of the darkest and
    brightest pixels are clipped before computing the stretch limits.
    """
    clip = float(cfg.get("clip_percent", 1.0))
    return _histogram_stretch_rgb(img, clip)


def _auto_saturation(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Auto-stretch saturation channel in HSV space.

    ``clip_percent`` (default ``1.0``) controls percentile clipping.
    """
    clip = float(cfg.get("clip_percent", 1.0))
    import numpy as np

    # Vectorised RGB→HSV using colorsys-compatible maths (no pixel loops)
    arr = np.array(img, dtype=np.float32) / 255.0  # shape (H, W, 3)

    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    cmax = np.max(arr, axis=2)
    cmin = np.min(arr, axis=2)
    delta = cmax - cmin

    # Saturation
    s_ch = np.divide(delta, cmax, out=np.zeros_like(delta), where=cmax > 0)
    s_ch = _stretch_channel(s_ch, clip)

    # Value
    v_ch = cmax

    # Hue (same formula as colorsys, vectorised)
    hue = np.zeros_like(cmax)
    mask_r = (cmax == r) & (delta > 0)
    mask_g = (cmax == g) & (delta > 0)
    mask_b = (cmax == b) & (delta > 0)
    hue[mask_r] = (60.0 * ((g[mask_r] - b[mask_r]) / delta[mask_r])) % 360.0
    hue[mask_g] = 60.0 * ((b[mask_g] - r[mask_g]) / delta[mask_g]) + 120.0
    hue[mask_b] = 60.0 * ((r[mask_b] - g[mask_b]) / delta[mask_b]) + 240.0

    # HSV → RGB (vectorised)
    h60 = hue / 60.0
    i = np.floor(h60).astype(np.int32) % 6
    f = h60 - np.floor(h60)
    p = v_ch * (1.0 - s_ch)
    q = v_ch * (1.0 - f * s_ch)
    t_ = v_ch * (1.0 - (1.0 - f) * s_ch)

    out = np.zeros_like(arr)
    for idx, (cr, cg, cb) in enumerate([
        (v_ch, t_, p),   # i == 0
        (q, v_ch, p),    # i == 1
        (p, v_ch, t_),   # i == 2
        (p, q, v_ch),    # i == 3
        (t_, p, v_ch),   # i == 4
        (v_ch, p, q),    # i == 5
    ]):
        mask = i == idx
        out[:, :, 0][mask] = cr[mask]
        out[:, :, 1][mask] = cg[mask]
        out[:, :, 2][mask] = cb[mask]

    return Image.fromarray((out * 255.0).clip(0, 255).astype(np.uint8))


def _desaturate(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Reduce image saturation.

    ``amount`` controls the desaturation intensity in ``[0, 1]``:
    - ``0.0``: unchanged image
    - ``1.0``: fully desaturated (grayscale)
    """
    amount = max(0.0, min(1.0, float(cfg.get("amount", 0.25))))
    if amount <= 0.0:
        return img
    factor = 1.0 - amount
    return ImageEnhance.Color(img).enhance(factor)


def _distress(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Overlay analogue-damage artifacts: wavy scratches and dust/dirt spots.

    Args:
        scratches:       Number of scratch lines to draw. Default: 5.
        dust_spots:      Number of dust/dirt specks to draw. Default: 30.
        scratch_opacity: Visibility of scratches in [0, 1]. Default: 0.55.
        dust_opacity:    Visibility of dust spots in [0, 1]. Default: 0.45.
        scratch_width:   Max scratch line width in pixels (1 or 2). Default: 1.
    """
    import random as _random
    from PIL import ImageDraw

    scratch_count   = int(cfg.get("scratches", 5))
    dust_count      = int(cfg.get("dust_spots", 30))
    scratch_opacity = max(0.0, min(1.0, float(cfg.get("scratch_opacity", 0.55))))
    dust_opacity    = max(0.0, min(1.0, float(cfg.get("dust_opacity", 0.45))))
    scratch_width   = max(1, int(cfg.get("scratch_width", 1)))

    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # --- Scratches: wavy segmented lines running roughly top-to-bottom ------
    for _ in range(scratch_count):
        # Random start X anywhere across the image, start Y near the top.
        x0 = _random.randint(0, w - 1)
        y0 = _random.randint(0, int(h * 0.25))
        # End X drifts slightly, end Y near the bottom.
        x1 = max(0, min(w - 1, x0 + _random.randint(-w // 5, w // 5)))
        y1 = _random.randint(int(h * 0.75), h - 1)

        # Light scratches are more common (reflected light on a groove/tear).
        is_light = _random.random() > 0.35
        lum   = _random.randint(190, 255) if is_light else _random.randint(0, 50)
        alpha = int(scratch_opacity * 255)

        # Break into short segments and jitter X slightly for waviness.
        segments = _random.randint(10, 24)
        pts = []
        for i in range(segments + 1):
            t  = i / segments
            px = int(x0 + (x1 - x0) * t + _random.randint(-2, 2))
            py = int(y0 + (y1 - y0) * t)
            pts.append((max(0, min(w - 1, px)), py))

        lw = 1 if _random.random() > 0.25 else min(scratch_width, 2)
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=(lum, lum, lum, alpha), width=lw)

    # --- Dust / dirt spots: tiny filled ellipses scattered randomly ----------
    for _ in range(dust_count):
        x    = _random.randint(0, w - 1)
        y    = _random.randint(0, h - 1)
        size = _random.randint(1, 3)
        is_light = _random.random() > 0.5
        lum   = _random.randint(210, 255) if is_light else _random.randint(0, 35)
        alpha = int(dust_opacity * 255 * _random.uniform(0.5, 1.0))
        draw.ellipse(
            [x - size, y - size, x + size, y + size],
            fill=(lum, lum, lum, alpha),
        )

    # Build a content mask so artifacts never land on black padding.
    # Any pixel whose Rec.709 luma is at or below the threshold is treated as
    # padding and its overlay alpha is zeroed out.
    import numpy as np
    arr  = np.array(img, dtype=np.float32)
    luma = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]
    content_mask = (luma > 8).astype(np.uint8) * 255  # True = real image pixel

    # Apply the mask to the overlay's alpha channel.
    ov_arr        = np.array(overlay, dtype=np.uint8)
    ov_arr[:, :, 3] = (ov_arr[:, :, 3].astype(np.uint16) * content_mask // 255).astype(np.uint8)
    overlay = Image.fromarray(ov_arr, mode="RGBA")

    # Composite the artifact overlay onto the image.
    base   = img.convert("RGBA")
    result = Image.alpha_composite(base, overlay)
    return result.convert("RGB")


def _denoise(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Reduce noise and JPEG compression artifacts.

    Args:
        mode:        'gaussian' (default) blurs block/ringing JPEG artifacts;
                     'median' removes grain and salt-and-pepper noise.
        radius:      Blur radius for gaussian mode. Default: 0.8.
        median_size: Kernel size for median mode (must be odd). Default: 3.
        strength:    Blend factor in [0, 1]. 1.0 = full filter, 0.0 = original.
                     Use values like 0.6-0.8 to soften while retaining detail.
    """
    import numpy as np
    from PIL import ImageFilter

    mode = cfg.get("mode", "gaussian")
    strength = max(0.0, min(1.0, float(cfg.get("strength", 1.0))))

    if mode == "median":
        size = int(cfg.get("median_size", 3))
        size = max(3, size | 1)  # ensure odd and at least 3
        filtered = img.filter(ImageFilter.MedianFilter(size=size))
    elif mode == "gaussian":
        radius = float(cfg.get("radius", 0.8))
        filtered = img.filter(ImageFilter.GaussianBlur(radius=radius))
    else:
        logger.warning("Unknown denoise mode %r – skipping", mode)
        return img

    if strength >= 1.0:
        return filtered
    if strength <= 0.0:
        return img

    # Partial blend: original + strength * (filtered - original)
    orig = np.array(img, dtype=np.float32)
    filt = np.array(filtered, dtype=np.float32)
    blended = orig + strength * (filt - orig)
    return Image.fromarray(blended.clip(0, 255).astype(np.uint8))


def _crush_blacks(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Clip dark tones to pure black using a luminance threshold.

    Args:
        black_point: Normalized threshold in range [0, 1]. Pixels with
            luminance below this value become pure black. Default: 0.16.
    """
    import numpy as np

    black_point = float(cfg.get("black_point", 0.16))
    black_point = max(0.0, min(1.0, black_point))

    arr = np.array(img, dtype=np.float32)

    # Rec.709 luma approximation for robust shadow detection.
    luma = (0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]) / 255.0
    shadow_mask = luma < black_point
    arr[shadow_mask] = 0.0

    return Image.fromarray(arr.clip(0, 255).astype(np.uint8))


def _color_eq(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Adjust individual RGB channel gains (color equalizer).

    Each channel is multiplied by its factor and clamped to [0, 255].
    Values above 1.0 boost the channel; values below 1.0 reduce it.

    Args:
        red:   Red channel multiplier. Default: 1.0 (no change).
        green: Green channel multiplier. Default: 1.0 (no change).
        blue:  Blue channel multiplier. Default: 1.0 (no change).

    Example YAML::

        - step: color_eq
          red: 1.1
          green: 0.95
          blue: 0.9
    """
    import numpy as np

    r_gain = float(cfg.get("red",   1.0))
    g_gain = float(cfg.get("green", 1.0))
    b_gain = float(cfg.get("blue",  1.0))

    if r_gain == 1.0 and g_gain == 1.0 and b_gain == 1.0:
        return img

    arr = np.array(img, dtype=np.float32)
    arr[:, :, 0] *= r_gain
    arr[:, :, 1] *= g_gain
    arr[:, :, 2] *= b_gain

    return Image.fromarray(arr.clip(0, 255).astype(np.uint8))


def _zoom(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Zoom image in or out around a normalized focal point.

    Args:
        factor: Zoom multiplier. ``1.0`` = unchanged, ``> 1.0`` zooms in,
            ``< 1.0`` zooms out. Default: 1.0.
        center_x: Horizontal focal point in normalized coordinates ``[0, 1]``.
            ``0.5`` = center. Default: 0.5.
        center_y: Vertical focal point in normalized coordinates ``[0, 1]``.
            ``0.5`` = center. Default: 0.5.

    Zoom-in is implemented as crop + resize. Zoom-out shrinks the image and
    pads with black so the output dimensions stay unchanged.
    """
    factor = float(cfg.get("factor", 1.0))
    if factor == 1.0:
        return img

    center_x = max(0.0, min(1.0, float(cfg.get("center_x", 0.5))))
    center_y = max(0.0, min(1.0, float(cfg.get("center_y", 0.5))))

    width, height = img.size

    if factor > 1.0:
        crop_w = max(1, round(width / factor))
        crop_h = max(1, round(height / factor))
        focus_x = round(center_x * width)
        focus_y = round(center_y * height)

        left = max(0, min(width - crop_w, focus_x - crop_w // 2))
        top = max(0, min(height - crop_h, focus_y - crop_h // 2))
        cropped = img.crop((left, top, left + crop_w, top + crop_h))
        return cropped.resize((width, height), Image.Resampling.LANCZOS)

    scaled_w = max(1, round(width * factor))
    scaled_h = max(1, round(height * factor))
    scaled = img.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), (0, 0, 0))

    focus_x = round(center_x * width)
    focus_y = round(center_y * height)
    left = max(0, min(width - scaled_w, focus_x - scaled_w // 2))
    top = max(0, min(height - scaled_h, focus_y - scaled_h // 2))
    canvas.paste(scaled, (left, top))
    return canvas


def _resize(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Upscale or downscale to a target size using LANCZOS resampling.

    Args:
        width:  Target width in pixels. Default: 500.
        height: Target height in pixels. Default: same as width.
        keep_aspect: If True (default), fit within width×height while
            preserving aspect ratio and padding with black.
        unsharp_radius:  Radius for post-resize UnsharpMask. Default: 1.5.
        unsharp_percent: Strength of the unsharp mask (0 = off). Default: 120.
        unsharp_threshold: Minimum brightness difference to sharpen. Default: 3.
    """
    from PIL import ImageFilter

    target_w = int(cfg.get("width", cfg.get("target_size", 500)))
    target_h = int(cfg.get("height", target_w))
    keep_aspect = bool(cfg.get("keep_aspect", True))
    unsharp_radius = float(cfg.get("unsharp_radius", 1.5))
    unsharp_percent = int(cfg.get("unsharp_percent", 120))
    unsharp_threshold = int(cfg.get("unsharp_threshold", 3))

    if keep_aspect:
        # Scale to fill as much of the target as possible (upscale or downscale).
        scale = min(target_w / img.width, target_h / img.height)
        new_w = max(1, round(img.width * scale))
        new_h = max(1, round(img.height * scale))
        img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        # Pad to exact target size with black background
        canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
        offset_x = (target_w - new_w) // 2
        offset_y = (target_h - new_h) // 2
        canvas.paste(img_resized, (offset_x, offset_y))
        result = canvas
    else:
        result = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

    if unsharp_percent > 0:
        result = result.filter(
            ImageFilter.UnsharpMask(
                radius=unsharp_radius,
                percent=unsharp_percent,
                threshold=unsharp_threshold,
            )
        )
    return result


def _watermark(img: PILImage, cfg: dict[str, Any]) -> PILImage:
    """Overlay a semi-transparent watermark on the image.

    Args:
        path:     Path to the watermark image (PNG or GIF). Default: 'watermark.gif'.
        opacity:  Watermark opacity in [0, 1]. Default: 0.35.
        position: One of 'bottom-right', 'bottom-left', 'top-right',
                  'top-left', 'center'. Default: 'bottom-right'.
        scale:    Scale watermark relative to shorter image edge [0, 1].
                  Default: 0.25 (25 % of the shorter edge).
        margin:   Pixel margin from the edge for corner positions. Default: 10.
    """
    from pathlib import Path as _Path

    wm_path = _Path(cfg.get("path", "watermark.gif"))
    if not wm_path.exists():
        logger.warning("Watermark file not found: %s – skipping watermark step", wm_path)
        return img

    opacity = max(0.0, min(1.0, float(cfg.get("opacity", 0.35))))
    position = cfg.get("position", "bottom-right")
    scale = max(0.05, min(1.0, float(cfg.get("scale", 0.25))))
    margin = int(cfg.get("margin", 10))

    # Ensure base is RGBA for compositing.
    base = img.convert("RGBA")
    wm = Image.open(wm_path).convert("RGBA")

    # Scale watermark to `scale` fraction of the shorter image edge.
    shorter_edge = min(base.width, base.height)
    wm_max = max(1, int(shorter_edge * scale))
    wm.thumbnail((wm_max, wm_max), Image.Resampling.LANCZOS)

    # Apply opacity to the watermark alpha channel.
    r, g, b, a = wm.split()
    a = a.point(lambda x: int(x * opacity))
    wm = Image.merge("RGBA", (r, g, b, a))

    # Calculate paste position.
    pw, ph = wm.size
    bw, bh = base.size
    positions = {
        "bottom-right": (bw - pw - margin, bh - ph - margin),
        "bottom-left":  (margin, bh - ph - margin),
        "top-right":    (bw - pw - margin, margin),
        "top-left":     (margin, margin),
        "center":       ((bw - pw) // 2, (bh - ph) // 2),
    }
    paste_xy = positions.get(position, positions["bottom-right"])

    base.paste(wm, paste_xy, mask=wm)
    return base.convert("RGB")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _histogram_stretch_rgb(img: PILImage, clip_percent: float) -> PILImage:
    """Apply per-channel linear histogram stretch to an RGB image."""
    import numpy as np

    arr = np.array(img, dtype=np.float32)
    out = np.zeros_like(arr)
    for c in range(3):
        out[:, :, c] = _stretch_channel(arr[:, :, c] / 255.0, clip_percent) * 255.0
    return Image.fromarray(out.clip(0, 255).astype(np.uint8))


def _stretch_channel(channel: "np.ndarray", clip_percent: float) -> "np.ndarray":  # type: ignore[name-defined]
    """Linearly stretch a 2-D float array (0–1 range) using percentile clipping."""
    import numpy as np

    lo = np.percentile(channel, clip_percent)
    hi = np.percentile(channel, 100.0 - clip_percent)
    if hi <= lo:
        return channel
    stretched = (channel - lo) / (hi - lo)
    return np.clip(stretched, 0.0, 1.0)
