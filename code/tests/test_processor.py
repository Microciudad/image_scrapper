"""Tests for cover_fetcher.processor"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from cover_fetcher.processor import (
    DEFAULT_PIPELINE,
    apply_pipeline,
    load_pipeline,
)


def _make_jpeg_bytes(width: int = 50, height: int = 50) -> bytes:
    """Create a minimal solid-colour JPEG for testing."""
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# load_pipeline
# ---------------------------------------------------------------------------


def test_load_pipeline_reads_yaml(tmp_path):
    yaml_content = """
pipeline:
  - step: auto_brightness
    clip_percent: 2.0
  - step: rotate
    degrees: 90
"""
    yaml_path = tmp_path / "pipeline.yaml"
    yaml_path.write_text(yaml_content)
    steps = load_pipeline(yaml_path)
    assert len(steps) == 2
    assert steps[0]["step"] == "auto_brightness"
    assert steps[1]["degrees"] == 90


def test_load_pipeline_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_pipeline(tmp_path / "nonexistent.yaml")


def test_load_pipeline_invalid_yaml(tmp_path):
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("steps:\n  - name: foo\n")  # valid YAML but missing 'pipeline' key
    with pytest.raises(ValueError, match="pipeline"):
        load_pipeline(bad_yaml)


# ---------------------------------------------------------------------------
# apply_pipeline
# ---------------------------------------------------------------------------


def test_apply_pipeline_default_returns_jpeg():
    raw = _make_jpeg_bytes()
    result = apply_pipeline(raw, DEFAULT_PIPELINE)
    # Result must start with JPEG magic bytes
    assert result[:2] == b"\xff\xd8"


def test_apply_pipeline_rotate_90():
    raw = _make_jpeg_bytes(width=100, height=50)
    steps = [{"step": "rotate", "degrees": 90, "expand": True}]
    result = apply_pipeline(raw, steps)
    img = Image.open(io.BytesIO(result))
    # After 90° rotation with expand, width and height swap
    assert img.size == (50, 100)


def test_apply_pipeline_rotate_0_unchanged_size():
    raw = _make_jpeg_bytes(width=60, height=40)
    steps = [{"step": "rotate", "degrees": 0}]
    result = apply_pipeline(raw, steps)
    img = Image.open(io.BytesIO(result))
    assert img.size == (60, 40)


def test_apply_pipeline_rotate_border_fill_uses_edge_color():
    img = Image.new("RGB", (120, 120), color=(255, 255, 255))
    for x in range(20, 100):
        for y in range(20, 100):
            img.putpixel((x, y), (10, 10, 10))

    buf = io.BytesIO()
    img.save(buf, format="PNG")

    result = apply_pipeline(buf.getvalue(), [{"step": "rotate", "degrees": 8, "expand": True, "fill": "border"}])
    rotated = Image.open(io.BytesIO(result)).convert("RGB")
    corner = rotated.getpixel((0, 0))
    assert all(channel >= 240 for channel in corner)


def test_apply_pipeline_rotate_hex_fill_uses_explicit_color():
    raw = _make_jpeg_bytes(width=100, height=100)
    result = apply_pipeline(raw, [{"step": "rotate", "degrees": 8, "expand": True, "fill": "#ffffff"}])
    rotated = Image.open(io.BytesIO(result)).convert("RGB")
    corner = rotated.getpixel((0, 0))
    assert all(channel >= 240 for channel in corner)


def test_apply_pipeline_unknown_step_does_not_raise():
    raw = _make_jpeg_bytes()
    steps = [{"step": "nonexistent_step"}]
    result = apply_pipeline(raw, steps)
    assert result[:2] == b"\xff\xd8"


def test_apply_pipeline_empty_steps():
    raw = _make_jpeg_bytes()
    result = apply_pipeline(raw, [])
    assert result[:2] == b"\xff\xd8"


def test_apply_pipeline_auto_brightness():
    raw = _make_jpeg_bytes()
    steps = [{"step": "auto_brightness", "clip_percent": 1.0}]
    result = apply_pipeline(raw, steps)
    assert result[:2] == b"\xff\xd8"


def test_apply_pipeline_auto_saturation():
    raw = _make_jpeg_bytes()
    steps = [{"step": "auto_saturation", "clip_percent": 1.0}]
    result = apply_pipeline(raw, steps)
    assert result[:2] == b"\xff\xd8"


def test_apply_pipeline_desaturate_full_makes_gray():
    img = Image.new("RGB", (16, 16), color=(220, 40, 40))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")

    result = apply_pipeline(buf.getvalue(), [{"step": "desaturate", "amount": 1.0}])
    out = Image.open(io.BytesIO(result)).convert("RGB")
    r, g, b = out.getpixel((8, 8))
    assert abs(r - g) <= 2 and abs(g - b) <= 2


def test_apply_pipeline_desaturate_zero_keeps_color_relationship():
    img = Image.new("RGB", (16, 16), color=(220, 40, 40))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")

    result = apply_pipeline(buf.getvalue(), [{"step": "desaturate", "amount": 0.0}])
    out = Image.open(io.BytesIO(result)).convert("RGB")
    r, g, b = out.getpixel((8, 8))
    assert r > g and r > b


def test_apply_pipeline_jpeg_quality_controls_output_size():
    raw = _make_jpeg_bytes(width=200, height=200)
    high = apply_pipeline(raw, [{"step": "jpeg", "quality": 95}])
    low = apply_pipeline(raw, [{"step": "jpeg", "quality": 40}])
    assert len(low) <= len(high)


def test_apply_pipeline_jpeg_quality_is_clamped():
    raw = _make_jpeg_bytes(width=100, height=100)
    result = apply_pipeline(raw, [{"step": "jpeg", "quality": 500}])
    assert result[:2] == b"\xff\xd8"
