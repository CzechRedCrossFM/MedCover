"""User signature image processing.

Pipeline for uploads to the profile signature field:
  1. Reject oversized requests early (handled in the route).
  2. Sniff format from bytes; accept PNG, JPEG, HEIC/HEIF.
  3. EXIF auto-orient (phone photos come sideways).
  4. Flatten transparency onto white; keep colour.
  5. Auto-crop white borders (with a safety valve for false positives).
  6. Resize to a fixed target height, preserving aspect ratio.
  7. Quantise to a 64-colour adaptive palette and re-encode as PNG.
  8. Enforce final size cap.

The stored blob is always a PNG; the mimetype column is kept for future
flexibility but currently always holds "image/png".
"""

import io
import logging

from PIL import Image, ImageOps

from app.image_upload import MAX_IMAGE_PIXELS, open_upload_image

log = logging.getLogger(__name__)

# Public constants; used by the route and template.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MiB — request-body cap before decode
MAX_STORED_BYTES = 50 * 1024  # 50 KiB — hard cap on the persisted PNG
TARGET_HEIGHT_PX = 200  # signature strip height in the final PNG
# Aspect-ratio cap: rejects pathological inputs (e.g. 1xN strips) whose
# height-normalised width would blow up allocation during resize. A real
# signature strip is at most ~6:1; 20:1 leaves headroom for odd scans.
MAX_ASPECT_RATIO = 20


class SignatureError(ValueError):
    """Raised when an uploaded signature image cannot be processed."""


def process_signature_upload(raw_bytes: bytes) -> bytes:
    """Turn raw uploaded image bytes into a stored-ready PNG.

    Args:
        raw_bytes: Bytes read from the uploaded file.

    Returns:
        PNG-encoded bytes ready to persist to the DB (adaptive-palette PNG,
        height `TARGET_HEIGHT_PX`, size at most `MAX_STORED_BYTES`).

    Raises:
        SignatureError: If the bytes are not a recognised image, use a
            disallowed format, or the processed output still exceeds
            `MAX_STORED_BYTES`.
    """
    img = open_upload_image(raw_bytes, SignatureError, max_pixels=MAX_IMAGE_PIXELS)

    # Auto-crop white borders. Threshold at ~200 so photo backgrounds (which
    # are rarely pure white) still get trimmed. Safety valve: if the trim
    # would remove more than 70% of the area, assume it's a false positive
    # (e.g. a dark photo of a signature) and keep the original bounds.
    # Border detection runs on a grayscale copy; the stored image stays RGB.
    orig_w, orig_h = img.size
    gray_for_bbox = img.convert("L")
    inverted = ImageOps.invert(gray_for_bbox).point(lambda p: 255 if p > (255 - 200) else 0)
    bbox = inverted.getbbox()
    if bbox is not None:
        crop_w = bbox[2] - bbox[0]
        crop_h = bbox[3] - bbox[1]
        if (crop_w * crop_h) / max(1, orig_w * orig_h) >= 0.30:
            pad = max(4, min(orig_w, orig_h) // 100)
            padded = (
                max(0, bbox[0] - pad),
                max(0, bbox[1] - pad),
                min(orig_w, bbox[2] + pad),
                min(orig_h, bbox[3] + pad),
            )
            img = img.crop(padded)

    # Resize to target height, preserve aspect ratio. Reject extreme aspect
    # ratios up-front so a huge new_w can't allocate a giant output buffer
    # before the MAX_STORED_BYTES check ever runs.
    if img.width > img.height * MAX_ASPECT_RATIO:
        raise SignatureError("Obrázek má neobvyklý poměr stran. Ořízněte jej blíže k podpisu.")
    if img.height != TARGET_HEIGHT_PX:
        new_w = max(1, round(img.width * TARGET_HEIGHT_PX / img.height))
        img = img.resize((new_w, TARGET_HEIGHT_PX), Image.Resampling.LANCZOS)

    # Quantise to an adaptive 64-colour palette before encoding. A signature
    # is ink + paper + antialiasing, so 64 colours is visually indistinguishable
    # from the full RGB image while shrinking a photo-sourced PNG by ~2.5×.
    quantised = img.quantize(colors=64, method=Image.Quantize.FASTOCTREE)
    buf = io.BytesIO()
    quantised.save(buf, format="PNG", optimize=True)
    out = buf.getvalue()

    if len(out) > MAX_STORED_BYTES:
        raise SignatureError(
            f"Zpracovaný obrázek přesahuje limit " f"{MAX_STORED_BYTES // 1024} kB. Použijte prosím jiný sken."
        )
    log.info("Processed signature: %d bytes -> %d bytes", len(raw_bytes), len(out))
    return out
