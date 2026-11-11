# DocIntel - AI-Powered Document Intelligence System

Upload invoices/receipts as PDF, JPG, PNG, TIFF, XLSX, XLS, CSV, DOCX or DOC -> text/OCR/spreadsheet/Word parsing -> structured JSON
(invoice no., date, supplier, customer, subtotal, tax, total, currency, line items) -> validation -> searchable API + React UI.
Gemini-assisted extraction is optional; without a key, extraction uses the existing rules. Semantic search uses a persistent local Chroma index with local sentence-transformer embeddings by default. Missing values stay `null`; they are never invented.

## Quick start
Requirements: Python 3.10+, Tesseract OCR (`apt install tesseract-ocr` / `brew install tesseract` / Windows installer), and LibreOffice for legacy `.doc` conversion. `.docx` files need no external office application.
Set the optional Gemini key in PowerShell with `$env:GEMINI_API_KEY = "your-key"`; on macOS/Linux use `export GEMINI_API_KEY=...`.
```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export GEMINI_API_KEY=...                # optional: enables Gemini extraction (rules-only without it)
uvicorn app.main:app --reload            # UI at http://localhost:8000  - API docs at /docs
```
Docker: `docker compose up --build` (PostgreSQL + API).  Single container: `docker build -t docintel . && docker run -p 8000:8000 -v data:/data docintel`

## Configuration (env vars)
`DATABASE_URL` (default SQLite in `./data`), `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-3.5-flash-lite`), `API_KEY` (require `X-API-Key`), `WORKERS` (4), `MAX_UPLOAD_MB` (200), `OCR_DPI` (250), `OCR_LANG` (eng), `CHROMA_DIR` (default `./data/chroma`), `EMBEDDING_BACKEND` (`st` | `hash`; default `st`), `EMBEDDING_MODEL` (default `sentence-transformers/all-MiniLM-L6-v2`), `AMOUNT_TOLERANCE` (0.02), `REVIEW_CONFIDENCE` (0.6), `DATE_DAYFIRST` (1).

Chroma is persisted under `DATA_DIR/chroma` by default. SQL remains the source of truth; on startup, the index is reconciled with existing invoices, including records from before Chroma was added. The default sentence-transformer model downloads once and runs locally; set `EMBEDDING_BACKEND=hash` for a lightweight lexical-vector fallback.

## API (OpenAPI at /docs)
| Method | Path | Purpose |
|---|---|---|
| POST | /api/documents | upload many files (202, background processing) |
| GET | /api/documents, /api/documents/{id} | status, progress, records, pages |
| GET | /api/documents/{id}/pages/{n}, /history, /file | source text, audit trail, original |
| POST | /api/documents/{id}/reprocess | re-run failed (or any) job; `force=true` overwrites corrections |
| GET | /api/invoices | filters: q, supplier, customer, invoice_number, status, currency, date_from/to, min/max_total |
| GET / PATCH | /api/invoices/{id} | result + issues / manual correction (re-validates) |
| GET | /api/search?q=&mode=keyword\|semantic\|hybrid | search |
| GET | /api/stats, /api/health | metrics |

## Tests and evaluation
```bash
pytest tests -q                      # processing, OCR, spreadsheets, Word, extraction (mock LLM), validation, API
python eval/make_dataset.py          # regenerate the labeled synthetic dataset
python eval/run_eval.py --large      # end-to-end metrics -> eval/report.json
```
Results: `docs/EVALUATION.md`, `docs/TEST_RESULTS.txt`. Design: `docs/ARCHITECTURE.md`.

## Layout
`app/processing` readers (pdf, ocr, images, sheets, Word) - `app/extraction` heuristic, Gemini, tables, segment, extractor - `app/validation.py` - `app/pipeline.py` workers - `app/search.py`, `app/embeddings.py`, `app/vector_store.py` - `app/main.py` API - `frontend/index.html` React UI (no build step; React/htm from CDN) - `eval/` dataset + evaluation - `tests/`.

## Honest limitations
Synthetic evaluation data; Gemini path is tested with a mock only and requires a user-provided API key for live use; shared API key, no multi-tenant auth; in-process queue (swap `pipeline.enqueue` for Celery/RQ to scale horizontally).
