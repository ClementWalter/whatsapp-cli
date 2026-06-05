"""Inline document-preview thumbnail generation."""

from __future__ import annotations

import io

import pytest

from wa.thumbnail import make_thumbnail


@pytest.fixture
def wide_png(tmp_path):
    # A 1000x1500 image — wider than the 400px cap and taller than the 4:3
    # crop, so both the downscale and the top-crop paths are exercised.
    from PIL import Image

    p = tmp_path / "img.png"
    Image.new("RGB", (1000, 1500), (120, 30, 30)).save(p)
    return str(p)


def test_image_thumbnail_returns_jpeg(wide_png):
    jpg, _w, _h = make_thumbnail(wide_png, "image/png")
    assert jpg[:2] == b"\xff\xd8"  # JPEG SOI marker


def test_image_thumbnail_capped_to_max_width(wide_png):
    _jpg, w, _h = make_thumbnail(wide_png, "image/png")
    assert w == 400


def test_image_thumbnail_cropped_to_aspect(wide_png):
    # Tall source is top-cropped to at most 4:3 (height <= width * 4/3).
    _jpg, w, h = make_thumbnail(wide_png, "image/png")
    assert h <= round(w * 4 / 3)


def test_image_thumbnail_decodes(wide_png):
    from PIL import Image

    jpg, w, h = make_thumbnail(wide_png, "image/png")
    im = Image.open(io.BytesIO(jpg))
    assert im.size == (w, h)


def test_unsupported_type_returns_none(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01\x02")
    assert make_thumbnail(str(p), "application/octet-stream") is None
