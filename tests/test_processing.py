import shutil, pytest, openpyxl, xlwt
from app.processing.ingest import read_document, detect_kind
from app.processing.errors import ProcessingError
from app.utils import norm
needs_ocr = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")

def test_digital_pdf_text(mk):
    p, inv = mk("pdf"); pages, _ = read_document(p, detect_kind(p))
    assert pages[0]["source"] == "digital" and inv["invoice_number"] in pages[0]["text"]

def test_multipage_pdf(mk):
    p, inv = mk("pdf", n_items=40); pages, _ = read_document(p, "pdf"); assert len(pages) >= 3 and all(x["source"] == "digital" for x in pages)

@needs_ocr
def test_scanned_pdf_uses_ocr(mk):
    p, inv = mk("pdf_scan"); pages, _ = read_document(p, "pdf")
    assert pages[0]["source"] == "ocr" and pages[0]["ocr_confidence"] > 60 and inv["invoice_number"] in pages[0]["text"].replace(" ", "")

@needs_ocr
@pytest.mark.parametrize("ext", ["png", "jpg"])
def test_image_ocr(mk, ext):
    p, inv = mk(ext); pages, _ = read_document(p, "image"); assert inv["invoice_number"] in pages[0]["text"].replace(" ", "")

@needs_ocr
def test_multiframe_tiff(mk, tmp_path):
    from PIL import Image
    a, _ = mk("png"); b, _ = mk("png"); ims = [Image.open(a), Image.open(b)]; t = tmp_path / "m.tiff"; ims[0].save(t, save_all=True, append_images=ims[1:])
    pages, _ = read_document(t, detect_kind(t)); assert len(pages) == 2

def test_xlsx_multiple_sheets(tmp_path):
    wb = openpyxl.Workbook(); wb.active.append(["Invoice No", "Total"]); wb.active.append(["A1", 5]); wb.create_sheet("Two").append(["x", "y"]); p = tmp_path / "w.xlsx"; wb.save(p)
    pages, tables = read_document(p, "xlsx"); assert len(pages) == 2 and len(tables) == 2 and "A1" in pages[0]["text"]

def test_xls(tmp_path):
    wb = xlwt.Workbook(); w = wb.add_sheet("S"); w.write(0, 0, "hello"); p = tmp_path / "a.xls"; wb.save(str(p))
    pages, _ = read_document(p, "xls"); assert "hello" in pages[0]["text"]

def test_docx_preserves_paragraph_and_table_text(tmp_path):
    from docx import Document
    doc = Document(); doc.add_paragraph("Invoice No: WORD-100")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Supplier"; table.cell(0, 1).text = "Acme Ltd"
    table.cell(1, 0).text = "Total"; table.cell(1, 1).text = "125.00"
    p = tmp_path / "invoice.docx"; doc.save(p)
    assert detect_kind(p) == "docx"
    pages, tables = read_document(p, "docx")
    assert len(pages) == 1 and "Invoice No: WORD-100" in pages[0]["text"]
    assert "Supplier | Acme Ltd" in pages[0]["text"] and "Total | 125.00" in pages[0]["text"]
    assert tables == []

def test_legacy_doc_requires_libreoffice_when_unavailable(tmp_path, monkeypatch):
    from app.processing import word
    p = tmp_path / "legacy.doc"; p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 100)
    monkeypatch.setattr(word, "_find_libreoffice", lambda: None)
    assert detect_kind(p) == "doc"
    with pytest.raises(ProcessingError, match="LibreOffice"):
        read_document(p, "doc")

@pytest.mark.parametrize("enc", ["utf-8", "utf-8-sig", "cp1252"])
def test_csv_encodings_and_delimiters(tmp_path, enc):
    p = tmp_path / "c.csv"; p.write_bytes("Invoice No;Supplier;Total\nA-1;Café Ltd;10,50\n".encode(enc)); pages, tables = read_document(p, "csv")
    assert "A-1" in pages[0]["text"] and tables[0][1].shape[1] == 3

def test_empty_csv_rejected(tmp_path):
    p = tmp_path / "e.csv"; p.write_text("   \n")
    with pytest.raises(ProcessingError): read_document(p, "csv")

@pytest.mark.parametrize("name,data,kind", [("t.pdf", b"%PDF-1.4 truncated", "pdf"), ("g.png", b"\x89PNG\r\n" + b"x" * 500, "image"), ("b.xlsx", b"PK\x03\x04" + b"x" * 500, "xlsx"), ("b.xls", b"\xd0\xcf\x11\xe0" + b"x" * 500, "xls")])
def test_corrupted_files_raise_clean_errors(tmp_path, name, data, kind):
    p = tmp_path / name; p.write_bytes(data)
    with pytest.raises(ProcessingError): read_document(p, detect_kind(p))

def test_encrypted_pdf(mk, tmp_path):
    import pymupdf
    p, _ = mk("pdf"); d = pymupdf.open(str(p)); out = tmp_path / "enc.pdf"; d.save(str(out), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="a", owner_pw="b")
    with pytest.raises(ProcessingError, match="password"): read_document(out, "pdf")

@needs_ocr
def test_blank_image_yields_no_text(tmp_path):
    from PIL import Image
    p = tmp_path / "b.png"; Image.new("RGB", (600, 800), "white").save(p); pages, _ = read_document(p, "image"); assert not pages[0]["text"].strip()

def test_unsupported_type(tmp_path):
    p = tmp_path / "x.exe"; p.write_bytes(b"MZ")
    with pytest.raises(ProcessingError, match="Unsupported"): detect_kind(p)
