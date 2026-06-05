"""Generate an inline JPEG preview for an outbound document.

WhatsApp renders a document's ``jpegThumbnail`` (an inline JPEG embedded in
the encrypted ``DocumentMessage``) as the preview in the chat bubble. It only
auto-previews known types server-side (PDF, images, Office) — never HTML — so
for an HTML article we must rasterise it ourselves and ship the thumbnail.

Rasterisation per type:
  * image/*          → the image itself (Pillow).
  * text/html        → ``wkhtmltoimage`` if present, else headless Chrome.
  * application/pdf  → ``pdftoppm`` (poppler) if present, else macOS ``sips``.

Everything is best-effort: any failure (no renderer, bad file) returns
``None`` and the caller simply sends the document without a preview.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger(__name__)

# The CDN thumbnail is shown crisp by both clients; the inline one is only a
# placeholder until it loads, so it stays small (a few KB inside the message).
_HIRES_WIDTH = 800
_INLINE_WIDTH = 280
_MAX_ASPECT = 4 / 3  # crop tall pages (articles) to a portrait preview


def make_thumbnail(path: str, mimetype: str) -> tuple[bytes, bytes, int, int] | None:
    """Return ``(hires_jpeg, inline_jpeg, width, height)`` for *path*, or ``None``.

    *hires_jpeg* (≈800px) is uploaded to the CDN as the document's thumbnail;
    *inline_jpeg* (≈280px) is embedded in the message as the placeholder.
    *width*/*height* describe the hi-res image. Never raises — any failure
    (no renderer, bad file) degrades to ``None`` and the caller sends no preview.
    """
    try:
        raw = _rasterise(path, mimetype)
        if raw is None:
            return None
        return _encode_thumbnails(raw)
    except Exception as e:  # best-effort: a preview is never worth failing a send
        log.debug("thumbnail generation failed for %s: %s", path, e)
        return None


def _encode_thumbnails(raw_image: bytes) -> tuple[bytes, bytes, int, int]:
    """Downscale + top-crop a rendered image into hi-res and inline JPEGs."""
    from PIL import Image

    im = Image.open(io.BytesIO(raw_image)).convert("RGB")
    if im.width > _HIRES_WIDTH:
        h = round(im.height * _HIRES_WIDTH / im.width)
        im = im.resize((_HIRES_WIDTH, h))
    max_h = round(im.width * _MAX_ASPECT)
    if im.height > max_h:
        im = im.crop((0, 0, im.width, max_h))  # keep the top (title + lead)

    hires = io.BytesIO()
    im.save(hires, format="JPEG", quality=82, optimize=True)

    inline_im = im
    if im.width > _INLINE_WIDTH:
        h = round(im.height * _INLINE_WIDTH / im.width)
        inline_im = im.resize((_INLINE_WIDTH, h))
    inline = io.BytesIO()
    inline_im.save(inline, format="JPEG", quality=60, optimize=True)

    return hires.getvalue(), inline.getvalue(), im.width, im.height


def _rasterise(path: str, mimetype: str) -> bytes | None:
    """Render the document's first screen/page to raw image bytes."""
    if mimetype.startswith("image/"):
        with open(path, "rb") as f:
            return f.read()
    if mimetype == "text/html":
        return _render_html(path)
    if mimetype == "application/pdf":
        return _render_pdf(path)
    return None


def _render_html(path: str) -> bytes | None:
    """HTML → image via wkhtmltoimage, falling back to headless Chrome."""
    if exe := shutil.which("wkhtmltoimage"):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
            out = fh.name
        try:
            res = subprocess.run(
                # Render wide so the downscaled thumbnail stays crisp on a
                # desktop preview (we only ever scale down from here).
                [exe, "--quiet", "--format", "jpeg", "--width", "800",
                 "--quality", "90", path, out],
                capture_output=True,
                timeout=60,
            )
            if res.returncode == 0 and os.path.getsize(out) > 0:
                with open(out, "rb") as f:
                    return f.read()
            log.debug("wkhtmltoimage failed: %s", res.stderr.decode(errors="replace")[:200])
        finally:
            _unlink(out)

    if chrome := _find_chrome():
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            out = fh.name
        try:
            res = subprocess.run(
                [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                 "--window-size=800,1067", f"--screenshot={out}",
                 f"file://{os.path.abspath(path)}"],
                capture_output=True,
                timeout=60,
            )
            if res.returncode == 0 and os.path.getsize(out) > 0:
                with open(out, "rb") as f:
                    return f.read()
            log.debug("chrome screenshot failed: %s", res.stderr.decode(errors="replace")[:200])
        finally:
            _unlink(out)
    return None


def _render_pdf(path: str) -> bytes | None:
    """PDF first page → image via pdftoppm, falling back to macOS sips."""
    if exe := shutil.which("pdftoppm"):
        with tempfile.TemporaryDirectory() as d:
            prefix = os.path.join(d, "page")
            res = subprocess.run(
                [exe, "-jpeg", "-f", "1", "-l", "1", "-singlefile",
                 "-scale-to", "1000", path, prefix],
                capture_output=True,
                timeout=60,
            )
            jpg = prefix + ".jpg"
            if res.returncode == 0 and os.path.exists(jpg):
                with open(jpg, "rb") as f:
                    return f.read()
    if exe := shutil.which("sips"):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
            out = fh.name
        try:
            res = subprocess.run(
                [exe, "-s", "format", "jpeg", path, "--out", out],
                capture_output=True,
                timeout=60,
            )
            if res.returncode == 0 and os.path.getsize(out) > 0:
                with open(out, "rb") as f:
                    return f.read()
        finally:
            _unlink(out)
    return None


def _find_chrome() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        if exe := shutil.which(name):
            return exe
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return mac if os.path.exists(mac) else None


def _unlink(p: str) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass
