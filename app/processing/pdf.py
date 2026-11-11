"""PDF reader: digital text per page, OCR fallback for scanned pages. Page-level fault isolation."""
import io
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
try:
    import pymupdf
except ImportError:  # older PyMuPDF
    import fitz as pymupdf
from PIL import Image
from .errors import ProcessingError
from .ocr import ocr_image
from ..config import settings


def words_to_lines(words):
    """Rebuild visual rows from word boxes so table rows stay on one line (wide gaps -> two spaces)."""
    rows = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        yc = (w[1] + w[3]) / 2
        h = max(w[3] - w[1], 1)
        if rows and abs(rows[-1]["y"] - yc) < h * 0.5:
            rows[-1]["w"].append(w)
        else:
            rows.append({"y": yc, "w": [w]})
    out = []
    for r in rows:
        ws = sorted(r["w"], key=lambda w: w[0])
        s = ws[0][4]
        for a, b in zip(ws, ws[1:]):
            size = max(a[3] - a[1], 1)
            s += ("  " if b[0] - a[2] > size * 1.2 else " ") + b[4]
        out.append(s)
    return "\n".join(out)


def read_pdf(path, on_page=None):
    """Returns list of dicts {page_no, text, source, ocr_confidence, error}."""
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise ProcessingError(f"Corrupted or unreadable PDF: {e}")
    if doc.needs_pass:
        raise ProcessingError("PDF is password-protected")
    n = doc.page_count
    if n == 0:
        raise ProcessingError("PDF has no pages")
    pages = []
    for i in range(n):
        rec = {"page_no": i + 1, "text": "", "source": "digital", "ocr_confidence": None, "error": None}
        try:
            page = doc[i]
            text = words_to_lines(page.get_text("words"))
            if len(text.strip()) < settings.min_digital_chars:   # scanned / image-only page
                pix = page.get_pixmap(dpi=settings.ocr_dpi)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                text, conf = ocr_image(img)
                rec.update(source="ocr", ocr_confidence=round(conf, 1))
            rec["text"] = text
        except Exception as e:  # one bad page must not kill a 1000-page file
            rec.update(source="failed", error=f"{type(e).__name__}: {e}")
        pages.append(rec)
        if on_page:
            on_page(i + 1, n)
    if all(p["source"] == "failed" for p in pages):
        raise ProcessingError("Every page failed to process: " + (pages[0]["error"] or ""))
    return pages
