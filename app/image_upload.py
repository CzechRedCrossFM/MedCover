"""Shared decoding of user-uploaded images (signature and profile photo).

Opens untrusted bytes safely: HEIC/HEIF support, decompression-bomb guard,
EXIF auto-orientation and transparency flattening. Callers own the cropping,
resizing and re-encoding.
"""

import io
import logging
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

log = logging.getLogger(__name__)

# Register HEIC/HEIF openers with Pillow so Image.open() handles iPhone photos.
register_heif_opener()

# Guardrails against decompression bombs. Comfortably fits any real phone
# photo (12 MP is ~12 M pixels) but rejects pathological inputs. Pillow's own
# Image.MAX_IMAGE_PIXELS only raises at 2x the limit (below that it just emits
# a warning), so callers enforce the exact cap from the lazy-loaded header and
# Pillow's check stays as a second line of defence in case the header lies.
MAX_IMAGE_PIXELS = 25_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# MPO is a multi-frame JPEG container iPhones produce in HDR mode; the primary
# frame is a normal JPEG, which is what Pillow returns from Image.open().
ACCEPTED_FORMATS: Final = frozenset({"PNG", "JPEG", "MPO", "HEIF", "HEIC"})
_JPEG_FORMATS: Final = frozenset({"JPEG", "MPO"})
TOO_LARGE_MSG: Final = "Obrázek je příliš velký (rozlišení)."


def open_upload_image(
    raw_bytes: bytes,
    error_cls: type[ValueError],
    *,
    max_pixels: int,
    min_edge: int | None = None,
    draft_size: tuple[int, int] | None = None,
) -> Image.Image:
    """Decode and validate uploaded bytes, returning an upright RGB image.

    Args:
        raw_bytes: Bytes read from the uploaded file.
        error_cls: ValueError subclass raised with a Czech message on rejection.
        max_pixels: Exact pixel cap, checked from the header before decoding.
        min_edge: Reject images whose shorter edge is below this many pixels.
        draft_size: For JPEG, let the decoder downscale to about this size.

    Raises:
        error_cls: If the bytes are not a decodable image, use a disallowed
            format, or are too large or too small.
    """
    try:
        source = Image.open(io.BytesIO(raw_bytes))
        # Image.open only reads the header, so these checks run before any decode.
        if source.width * source.height > max_pixels:
            log.warning("Rejected oversized image (%dx%d, %d bytes)", source.width, source.height, len(raw_bytes))
            raise error_cls(TOO_LARGE_MSG)
        if source.format not in ACCEPTED_FORMATS:
            raise error_cls("Nepodporovaný formát obrázku. Použijte PNG, JPEG nebo HEIC.")
        if min_edge is not None and min(source.width, source.height) < min_edge:
            raise error_cls(f"Obrázek je příliš malý (minimálně {min_edge} × {min_edge} px).")
        if draft_size is not None and source.format in _JPEG_FORMATS:
            source.draft("RGB", draft_size)
        source.load()  # force decode so format errors surface here
    except error_cls:
        raise
    except Image.DecompressionBombError as exc:
        # Failsafe: header under-reported the pixel count and Pillow tripped
        # its own guard (fires at 2x MAX_IMAGE_PIXELS) during decode.
        log.warning("Rejected oversized image (%d bytes) - possible decompression bomb", len(raw_bytes))
        raise error_cls(TOO_LARGE_MSG) from exc
    except (UnidentifiedImageError, ValueError, SyntaxError, OSError, EOFError) as exc:
        # Truncated or corrupt files surface as ValueError or EOFError (pillow_heif) or
        # SyntaxError (PNG chunks) as well as OSError.
        raise error_cls("Soubor není platný obrázek.") from exc

    try:
        img: Image.Image = ImageOps.exif_transpose(source) or source
    except Exception:  # pylint: disable=broad-exception-caught
        # Malformed EXIF raises assorted errors (struct.error, TypeError, ...);
        # the pixels are fine, so carry on un-rotated.
        log.warning("Ignoring unreadable EXIF orientation", exc_info=True)
        img = source
    return flatten_to_rgb(img)


def flatten_to_rgb(img: Image.Image) -> Image.Image:
    """Composite transparency onto white and return an RGB image."""
    # Palette images (mode "P") can carry transparency via img.info; promote
    # them to RGBA first so the alpha channel exists to composite against.
    if img.mode == "P" and "transparency" in img.info:
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA", "PA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background
    return img.convert("RGB")
