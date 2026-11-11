import io, shutil, subprocess, time, pytest, openpyxl
from pathlib import Path
from tests.conftest import wait_done
from app.config import settings
from app import search, vector_store

def up(client, path, name=None):
    r = client.post("/api/documents", files=[("files", (name or path.name, path.read_bytes()))]); assert r.status_code == 202, r.text; return r.json()["documents"][0]

def test_upload_process_and_results(client, mk):
    p, inv = mk("pdf"); d = wait_done(client, up(client, p)["id"])
    assert d["status"] in ("completed", "needs_review") and d["progress"] == 100 and d["page_count"] == 1
    rec = client.get(f"/api/invoices/{d['records'][0]['id']}").json()
    assert rec["invoice_number"] == inv["invoice_number"] and rec["total"] == inv["total"] and rec["supplier"] == inv["supplier"]
    assert len(rec["line_items"]) == len(inv["line_items"]) and set(rec["confidence"]) >= {"total", "supplier"}

def test_multi_file_upload_and_rejection(client, mk, tmp_path):
    a, _ = mk("pdf"); b, _ = mk("pdf"); bad = tmp_path / "x.exe"; bad.write_bytes(b"zz"); empty = tmp_path / "e.pdf"; empty.write_bytes(b"")
    r = client.post("/api/documents", files=[("files", (n.name, n.read_bytes())) for n in (a, b, bad, empty)]).json()
    assert len(r["documents"]) == 2 and {x["filename"] for x in r["rejected"]} == {"x.exe", "e.pdf"}
    for d in r["documents"]: assert wait_done(client, d["id"])["status"] != "failed"

def test_progress_endpoint_during_processing(client, mk):
    p, _ = mk("pdf", n_items=40); d = up(client, p); assert d["status"] in ("queued", "processing", "completed", "needs_review"); wait_done(client, d["id"])

def test_duplicate_file_not_reprocessed(client, mk):
    p, _ = mk("pdf"); first = up(client, p); wait_done(client, first["id"]); second = up(client, p, "copy.pdf")
    assert second["status"] == "duplicate" and second["duplicate_of"] == first["id"]

def test_delete_document_removes_record_and_upload(client, mk):
    p, _ = mk("pdf"); d = wait_done(client, up(client, p)["id"])
    from app.db import SessionLocal
    from app.models import Document, Invoice
    with SessionLocal() as db:
        path = db.get(Document, d["id"]).stored_path
        invoice_ids = [row[0] for row in db.query(Invoice.id).filter(Invoice.document_id == d["id"]).all()]
    response = client.delete(f"/api/documents/{d['id']}")
    assert response.status_code == 200 and response.json() == {"deleted": d["id"]}
    assert client.get(f"/api/documents/{d['id']}").status_code == 404
    assert not Path(path).exists()
    assert all(client.get(f"/api/invoices/{invoice_id}").status_code == 404 for invoice_id in invoice_ids)

def test_delete_processing_document_conflicts(client, mk):
    p, _ = mk("pdf"); d = wait_done(client, up(client, p)["id"])
    from app.db import SessionLocal
    from app.models import Document
    with SessionLocal() as db:
        document = db.get(Document, d["id"])
        document.status = "processing"
        db.commit()
    response = client.delete(f"/api/documents/{d['id']}")
    assert response.status_code == 409
    assert client.get(f"/api/documents/{d['id']}").status_code == 200

def test_delete_duplicate_reprocesses_dependent(client, mk):
    p, _ = mk("pdf"); original = up(client, p); wait_done(client, original["id"])
    duplicate = up(client, p, "copy.pdf")
    response = client.delete(f"/api/documents/{original['id']}")
    assert response.status_code == 200
    processed_duplicate = wait_done(client, duplicate["id"])
    assert processed_duplicate["status"] in ("completed", "needs_review")

def test_duplicate_invoice_detected_across_files(client, mk):
    p1, inv = mk("pdf", template="A"); wait_done(client, up(client, p1)["id"])
    from eval.make_dataset import draw_pdf
    p2 = p1.parent / "again.pdf"; draw_pdf([inv], p2, "C"); d = wait_done(client, up(client, p2)["id"])
    codes = {i["code"] for r in d["records"] for i in client.get(f"/api/invoices/{r['id']}").json()["issues"]}; assert "duplicate_invoice" in codes and d["status"] == "needs_review"

def test_corrupted_file_fails_cleanly_then_reprocess(client, tmp_path):
    p = tmp_path / "broken.pdf"; p.write_bytes(b"%PDF-1.4\n garbage"); d = wait_done(client, up(client, p)["id"])
    assert d["status"] == "failed" and d["error"]
    r = client.post(f"/api/documents/{d['id']}/reprocess"); assert r.status_code == 202
    d2 = wait_done(client, d["id"]); assert d2["status"] == "failed" and d2["attempts"] == 2
    ev = [h["event"] for h in client.get(f"/api/documents/{d['id']}/history").json()]; assert ev.count("failed") == 2 and "reprocess_requested" in ev

def test_reprocess_recovers_after_fix(client, mk):
    p, _ = mk("pdf"); d = up(client, p); wait_done(client, d["id"])
    from app.db import SessionLocal; from app.models import Document
    with SessionLocal() as db: x = db.get(Document, d["id"]); x.status, x.error = "failed", "simulated crash"; db.commit()
    assert client.post(f"/api/documents/{d['id']}/reprocess").status_code == 202; assert wait_done(client, d["id"])["status"] in ("completed", "needs_review")

def test_correction_revalidates_and_audits(client, mk):
    p, inv = mk("pdf"); d = wait_done(client, up(client, p)["id"]); rid = d["records"][0]["id"]
    r = client.patch(f"/api/invoices/{rid}", json={"total": inv["total"] + 50}).json()
    assert "total_mismatch" in {i["code"] for i in r["issues"]} and r["status"] == "corrected" and r["corrected"]
    assert client.get(f"/api/documents/{d['id']}").json()["status"] == "completed"
    r = client.patch(f"/api/invoices/{rid}", json={"total": inv["total"]}).json()
    assert "total_mismatch" not in {i["code"] for i in r["issues"]} and r["confidence"]["total"] == 1.0 and r["status"] == "corrected"
    from app.db import SessionLocal
    from app.models import Invoice
    with SessionLocal() as db:
        db.get(Invoice, rid).status = "needs_review"
        db.commit()
    reviewed = client.post(f"/api/invoices/{rid}/review").json()
    assert reviewed["status"] == "corrected"
    ev = client.get(f"/api/documents/{d['id']}/history").json(); assert sum(h["event"] == "corrected" for h in ev) == 2
    assert any(h["event"] == "reviewed" and h["invoice_id"] == rid for h in ev)
    assert client.post(f"/api/documents/{d['id']}/reprocess").status_code == 409     # protects manual work


def test_review_changes_status_without_dismissing_issues(client, tmp_path):
    from reportlab.pdfgen import canvas
    p = tmp_path / "vague-review.pdf"; c = canvas.Canvas(str(p)); c.drawString(50, 750, "Thank you for your business."); c.save()
    d = wait_done(client, up(client, p)["id"]); rid = d["records"][0]["id"]
    before = client.get(f"/api/invoices/{rid}").json()
    assert before["status"] == "needs_review" and before["issues"]
    reviewed = client.post(f"/api/invoices/{rid}/review").json()
    assert reviewed["status"] == "ok" and reviewed["issues"] == before["issues"]
    assert client.get(f"/api/invoices/{rid}").json()["status"] == "ok"
    assert client.get(f"/api/documents/{d['id']}").json()["status"] == "completed"
    history = client.get(f"/api/documents/{d['id']}/history").json()
    assert any(h["event"] == "reviewed" and h["invoice_id"] == rid for h in history)


@pytest.mark.parametrize("body", [{"invoice_date": "05/03/2025"}, {"currency": "DOLLARS"}, {"total": "abc"}])
def test_correction_input_validation(client, mk, body):
    p, _ = mk("pdf"); rid = wait_done(client, up(client, p)["id"])["records"][0]["id"]; assert client.patch(f"/api/invoices/{rid}", json=body).status_code == 422

def test_missing_values_are_null_not_invented(client, tmp_path):
    from reportlab.pdfgen import canvas
    p = tmp_path / "vague.pdf"; c = canvas.Canvas(str(p)); c.drawString(50, 750, "Thank you for your business. Payment received."); c.save()
    d = wait_done(client, up(client, p)["id"]); rec = client.get(f"/api/invoices/{d['records'][0]['id']}").json()
    assert d["status"] == "needs_review" and rec["invoice_number"] is None and rec["total"] is None and any(i["code"] == "missing_field" for i in rec["issues"])

def test_spreadsheet_end_to_end(client, tmp_path):
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(["Invoice No", "Date", "Vendor", "Customer", "Subtotal", "Tax", "Total", "Currency"])
    ws.append(["Q-1", "2025-05-10", "Zed Ltd", "Yan Inc", 100, 10, 110, "USD"]); ws.append(["Q-2", "2025-05-11", "Zed Ltd", "Yan Inc", 100, 10, 999, "USD"])
    p = tmp_path / "reg.xlsx"; wb.save(p); d = wait_done(client, up(client, p)["id"]); assert len(d["records"]) == 2
    recs = {r["invoice_number"]: client.get(f"/api/invoices/{r['id']}").json() for r in d["records"]}
    assert recs["Q-1"]["invoice_date"] == "2025-05-10" and recs["Q-1"]["status"] == "ok" and recs["Q-2"]["status"] == "needs_review"

def test_docx_upload_and_processing(client, tmp_path):
    from docx import Document
    doc = Document()
    doc.add_paragraph("Invoice No: DOC-100")
    doc.add_paragraph("Date: 2025-01-02")
    doc.add_paragraph("Supplier: Acme Ltd")
    doc.add_paragraph("Total: $125.00")
    path = tmp_path / "invoice.docx"; doc.save(path)
    processed = wait_done(client, up(client, path)["id"])
    assert processed["kind"] == "docx" and processed["status"] in ("completed", "needs_review")
    page = client.get(f"/api/documents/{processed['id']}/pages/1").json()
    assert "Invoice No: DOC-100" in page["text"] and "Supplier: Acme Ltd" in page["text"]

def test_legacy_doc_upload_and_processing(client, tmp_path):
    from docx import Document
    from app.processing.word import _find_libreoffice
    executable = _find_libreoffice()
    if not executable:
        pytest.skip("LibreOffice not installed")
    doc = Document(); doc.add_paragraph("Invoice No: LEGACY-100")
    source = tmp_path / "legacy.docx"; doc.save(source)
    profile = (tmp_path / "lo-profile").as_uri()
    result = subprocess.run(
        [executable, "--headless", "--norestore", "--nodefault", "--nolockcheck",
         f"-env:UserInstallation={profile}", "--convert-to", "doc", "--outdir", str(tmp_path), str(source)],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    path = tmp_path / "legacy.doc"
    assert path.is_file(), result.stderr or result.stdout
    processed = wait_done(client, up(client, path)["id"])
    assert processed["kind"] == "doc" and processed["status"] in ("completed", "needs_review")
    page = client.get(f"/api/documents/{processed['id']}/pages/1").json()
    assert "Invoice No: LEGACY-100" in page["text"]

def test_search_keyword_filters_and_semantic(client, mk):
    p, inv = mk("pdf"); wait_done(client, up(client, p)["id"])
    r = client.get("/api/invoices", params={"supplier": inv["supplier"][:8], "min_total": inv["total"] - 1, "max_total": inv["total"] + 1}).json(); assert any(i["invoice_number"] == inv["invoice_number"] for i in r["items"])
    for mode in ("keyword", "semantic", "hybrid"):
        s = client.get("/api/search", params={"q": inv["supplier"], "mode": mode}).json(); assert s["results"], mode
    invoice_id = next(i["id"] for i in r["items"] if i["invoice_number"] == inv["invoice_number"])
    vector_store.delete([invoice_id])
    assert search.sync_vectors()["added_or_updated"] >= 1
    assert any(i["id"] == invoice_id for i in client.get("/api/search", params={"q": inv["supplier"], "mode": "semantic"}).json()["results"])
    assert client.get("/api/search", params={"q": inv["supplier"], "mode": "bogus"}).status_code == 422

def test_404s_and_stats(client):
    assert client.get("/api/documents/999999").status_code == 404 and client.get("/api/invoices/999999").status_code == 404
    s = client.get("/api/stats").json(); assert s["total_documents"] > 0 and 0 <= s["success_rate"] <= 1

def test_api_key_enforced(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret")
    assert client.get("/api/stats").status_code == 401 and client.get("/api/stats", headers={"X-API-Key": "s3cret"}).status_code == 200 and client.get("/api/health").status_code == 200
