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

# Target geometry for the embedded preview. Kept small — the thumbnail travels
# inline inside the encrypted message, so it must stay a few KB, not hundreds.
_MAX_WIDTH = 400
_MAX_ASPECT = 4 / 3  # crop tall pages (articles) to a portrait preview


def make_thumbnail(path: str, mimetype: str) -> tuple[bytes, int, int] | None:
    """Return ``(jpeg_bytes, width, height)`` for *path*, or ``None``.

    Never raises — rasterisation and encoding failures degrade to ``None``.
    """
    try:
        raw = _rasterise(path, mimetype)
        if raw is None:
            return None
        return _to_jpeg_thumbnail(raw)
    except Exception as e:  # best-effort: a preview is never worth failing a send
        log.debug("thumbnail generation failed for %s: %s", path, e)
        return None


def _to_jpeg_thumbnail(raw_image: bytes) -> tuple[bytes, int, int]:
    """Downscale + top-crop a rendered image into a small JPEG."""
    from PIL import Image

    im = Image.open(io.BytesIO(raw_image)).convert("RGB")
    if im.width > _MAX_WIDTH:
        h = round(im.height * _MAX_WIDTH / im.width)
        im = im.resize((_MAX_WIDTH, h))
    max_h = round(im.width * _MAX_ASPECT)
    if im.height > max_h:
        im = im.crop((0, 0, im.width, max_h))  # keep the top (title + lead)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=72, optimize=True)
    return buf.getvalue(), im.width, im.height


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
                [exe, "--quiet", "--format", "jpeg", "--width", "480",
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
                 "--window-size=480,640", f"--screenshot={out}",
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
                 "-scale-to", "600", path, prefix],
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
