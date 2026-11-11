"""DOCX parsing and optional legacy DOC conversion through LibreOffice."""
import shutil
import subprocess
import tempfile
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P

from .errors import ProcessingError


def _find_libreoffice():
    for name in ("soffice", "libreoffice"):
        executable = shutil.which(name)
        if executable:
            return executable
    for path in (
        Path("C:/Program Files/LibreOffice/program/soffice.com"),
        Path("C:/Program Files/LibreOffice/program/soffice.exe"),
        Path("C:/Program Files (x86)/LibreOffice/program/soffice.com"),
        Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
    ):
        if path.is_file():
            return str(path)
    return None


def _convert_doc(path):
    executable = _find_libreoffice()
    if not executable:
        raise ProcessingError("Legacy .doc files require LibreOffice. Install LibreOffice and retry this document.")
    try:
        with tempfile.TemporaryDirectory(prefix="docintel_doc_") as tmp:
            workdir = Path(tmp)
            source = workdir / "input.doc"
            shutil.copyfile(path, source)
            profile = (workdir / "profile").as_uri()
            result = subprocess.run(
                [
                    executable, "--headless", "--norestore", "--nodefault", "--nolockcheck",
                    f"-env:UserInstallation={profile}", "--convert-to", "docx",
                    "--outdir", str(workdir), str(source),
                ],
                capture_output=True, text=True, timeout=60, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            converted = workdir / "input.docx"
            if result.returncode != 0 or not converted.is_file():
                detail = (result.stderr or result.stdout).strip()
                raise ProcessingError(f"Could not convert legacy .doc file with LibreOffice: {detail or 'conversion failed'}")
            return _read_docx(converted)
    except subprocess.TimeoutExpired as e:
        raise ProcessingError("LibreOffice timed out while converting the .doc file") from e
    except OSError as e:
        raise ProcessingError(f"Could not run LibreOffice to convert the .doc file: {e}") from e


def _read_docx(path):
    try:
        document = Document(path)
    except Exception as e:
        raise ProcessingError(f"Corrupted or unreadable Word document: {type(e).__name__}: {e}") from e

    blocks = []
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            text = Paragraph(child, document).text.strip()
            if text:
                blocks.append(text)
        elif isinstance(child, CT_Tbl):
            table = Table(child, document)
            for row in table.rows:
                cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                if any(cells):
                    blocks.append(" | ".join(cells))

    text = "\n".join(blocks)
    if not text:
        raise ProcessingError("Word document contains no readable text")
    return text


def read_word(path, kind, on_page=None):
    """Return a one-page text representation of a Word document."""
    text = _convert_doc(path) if kind == "doc" else _read_docx(path)
    if on_page:
        on_page(1, 1)
    return [{"page_no": 1, "text": text, "source": "digital", "ocr_confidence": None, "error": None}]
