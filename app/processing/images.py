from PIL import Image, ImageSequence, ImageOps
from .errors import ProcessingError
from .ocr import ocr_image

Image.MAX_IMAGE_PIXELS = 200_000_000


def read_image(path, on_page=None):
    """JPG / PNG / (multi-frame) TIFF -> OCR text per frame."""
    try:
        im = Image.open(path)
        im.load()
    except Exception as e:
        raise ProcessingError(f"Corrupted or unreadable image: {e}")
    frames = [f.copy() for f in ImageSequence.Iterator(im)]
    pages = []
    for i, f in enumerate(frames):
        rec = {"page_no": i + 1, "text": "", "source": "ocr", "ocr_confidence": None, "error": None}
        try:
            f = ImageOps.exif_transpose(f).convert("RGB")
            if f.width * f.height > 40_000_000:
                f.thumbnail((6000, 6000))
            text, conf = ocr_image(f)
            rec.update(text=text, ocr_confidence=round(conf, 1))
        except Exception as e:
            rec.update(source="failed", error=f"{type(e).__name__}: {e}")
        pages.append(rec)
        if on_page:
            on_page(i + 1, len(frames))
    if all(p["source"] == "failed" for p in pages):
        raise ProcessingError("OCR failed on every frame")
    return pages
