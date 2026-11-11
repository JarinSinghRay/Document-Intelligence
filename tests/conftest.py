import os, sys, tempfile, time, random
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="docintel_test_"); os.environ["WORKERS"] = "2"
os.environ["EMBEDDING_BACKEND"] = "hash"
import pytest
from fastapi.testclient import TestClient
from app.main import app
from eval.make_dataset import make_invoice, draw_pdf, render, degrade


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mk(tmp_path):
    """Factory: create sample files; returns (path, invoice truth)."""
    counter = [random.randint(1000, 9000)]
    def make(kind="pdf", template="A", **kw):
        counter[0] += 1
        inv = make_invoice(random.Random(counter[0]), counter[0], **kw)
        pdf = tmp_path / f"i{counter[0]}.pdf"; draw_pdf([inv], pdf, template)
        if kind == "pdf": return pdf, inv
        img = degrade(render(pdf)[0], "clean", random.Random(1))
        p = tmp_path / f"i{counter[0]}.{kind}"
        if kind == "pdf_scan": p = tmp_path / f"s{counter[0]}.pdf"; img.save(p, "PDF", resolution=150)
        else: img.save(p)
        return p, inv
    return make


def wait_done(client, doc_id, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        d = client.get(f"/api/documents/{doc_id}").json()
        if d["status"] in ("completed", "needs_review", "failed", "duplicate"): return d
        time.sleep(0.3)
    raise TimeoutError
