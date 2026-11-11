"""User profile photo processing.

Pipeline for uploads to the profile photo field (sibling of app/signature.py;
photos keep colour and are cropped square):
  1. Reject oversized requests early (handled in the route).
  2. Sniff format from bytes; accept PNG, JPEG, HEIC/HEIF.
  3. EXIF auto-orient (phone photos come sideways).
  4. Flatten transparency onto white.
  5. Centre-crop to a square and scale to a fixed edge.
  6. Re-encode as JPEG without any metadata, stepping quality down if needed.
  7. Enforce final size cap.
"""

import hashlib
import io
import logging

from PIL import Image, ImageOps

from app.image_upload import MAX_IMAGE_PIXELS, open_upload_image

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
PHOTO_EDGE_PX = 320
MIN_SOURCE_EDGE_PX = 128
MAX_STORED_BYTES = 64 * 1024
JPEG_QUALITIES = (85, 70, 55)
STORED_MIMETYPE = "image/jpeg"


class ProfilePhotoError(ValueError):
    """Raised when an uploaded profile photo cannot be processed."""


def photo_token(data: bytes) -> str:
    """Short content hash used as presence flag, cache-buster and ETag."""
    return hashlib.sha256(data).hexdigest()[:16]


def process_profile_photo_upload(raw_bytes: bytes) -> bytes:
    """Turn raw uploaded image bytes into a stored-ready square JPEG.

    Raises:
        ProfilePhotoError: If the bytes are not a recognised image, use a
            disallowed format, are too large or too small, or the processed
            output still exceeds `MAX_STORED_BYTES`.
    """
    img = open_upload_image(
        raw_bytes,
        ProfilePhotoError,
        max_pixels=MAX_IMAGE_PIXELS,
        min_edge=MIN_SOURCE_EDGE_PX,
        draft_size=(PHOTO_EDGE_PX, PHOTO_EDGE_PX),
    )

    img = ImageOps.fit(img, (PHOTO_EDGE_PX, PHOTO_EDGE_PX), Image.Resampling.LANCZOS, centering=(0.5, 0.5))

    out = b""
    for quality in JPEG_QUALITIES:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) <= MAX_STORED_BYTES:
            log.info("Processed profile photo: %d bytes -> %d bytes", len(raw_bytes), len(out))
            return out
    raise ProfilePhotoError(
        f"Zpracovaná fotka přesahuje limit {MAX_STORED_BYTES // 1024} kB. Použijte prosím jinou fotku."
    )
