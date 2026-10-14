"""Reader B: free Tesseract OCR, used only to cross-check Reader A (PRD X-2)."""

from __future__ import annotations

import asyncio
import io
import subprocess

from PIL import Image, ImageOps

LANGUAGES = "eng+jpn"


def _prepare(image: bytes) -> list[bytes]:
    """Grayscale, upscale small images, boost contrast; also an inverted copy for light-on-dark graphics."""
    img = Image.open(io.BytesIO(image)).convert("L")
    if img.width < 1600:
        scale = 1600 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    img = ImageOps.autocontrast(img)
    versions = []
    for variant in (img, ImageOps.invert(img)):
        out = io.BytesIO()
        variant.save(out, format="PNG")
        versions.append(out.getvalue())
    return versions


def _tesseract(png: bytes) -> str:
    # psm 11: sparse text, suits schedule graphics with text scattered over artwork.
    result = subprocess.run(["tesseract", "stdin", "stdout", "-l", LANGUAGES, "--psm", "11"],
                            input=png, capture_output=True, timeout=120)
    return result.stdout.decode("utf-8", errors="replace")


def _read(image: bytes) -> str:
    return "\n".join(_tesseract(png) for png in _prepare(image))


async def read_text(image: bytes) -> str:
    return await asyncio.to_thread(_read, image)
