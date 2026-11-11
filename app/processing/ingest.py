"""File-type detection (by magic bytes, not just extension) and dispatch to the right reader."""
from pathlib import Path
from .errors import ProcessingError
from .pdf import read_pdf
from .images import read_image
from .sheets import read_sheets
from .word import read_word

SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".xlsx", ".xls", ".csv", ".docx", ".doc"}


def detect_kind(path):
    p = Path(path)
    head = p.read_bytes()[:16] if p.exists() else b""
    ext = p.suffix.lower()
    if head.startswith(b"%PDF") or (ext == ".pdf" and b"%PDF" in p.read_bytes()[:1024]): return "pdf"
    if head.startswith(b"\x89PNG") or head[:3] == b"\xff\xd8\xff" or head[:4] in (b"II*\x00", b"MM\x00*"): return "image"
    if ext == ".docx" and head.startswith(b"PK\x03\x04"): return "docx"
    if ext == ".doc" and head.startswith(b"\xd0\xcf\x11\xe0"): return "doc"
    if head.startswith(b"PK\x03\x04") and ext == ".xlsx": return "xlsx"
    if head.startswith(b"\xd0\xcf\x11\xe0") and ext == ".xls": return "xls"
    if ext == ".csv": return "csv"
    if ext in SUPPORTED:  # extension claims a type but the content disagrees -> corrupted/mislabelled
        return {".pdf": "pdf", ".xlsx": "xlsx", ".xls": "xls", ".docx": "docx", ".doc": "doc"}.get(ext, "image")
    raise ProcessingError(f"Unsupported file type '{ext or 'unknown'}'. Allowed: PDF, JPG, PNG, TIFF, XLSX, XLS, CSV, DOCX, DOC")


def read_document(path, kind, on_page=None):
    """Returns (pages, tables). tables is [] except for spreadsheets."""
    if kind == "pdf":
        return read_pdf(path, on_page), []
    if kind == "image":
        return read_image(path, on_page), []
    if kind in ("docx", "doc"):
        return read_word(path, kind, on_page), []
    return read_sheets(path, kind)
