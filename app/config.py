"""Central configuration (all overridable through environment variables)."""
import os
from pathlib import Path


class Settings:
    def __init__(self):
        self.data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        self.upload_dir = self.data_dir / "uploads"
        self.chroma_dir = Path(os.getenv("CHROMA_DIR", str(self.data_dir / "chroma"))).resolve()
        self.database_url = os.getenv("DATABASE_URL", f"sqlite:///{self.data_dir}/docintel.db")
        self.api_key = os.getenv("API_KEY", "")  # empty = auth disabled (dev)
        self.max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "200"))
        self.workers = int(os.getenv("WORKERS", "4"))
        self.ocr_dpi = int(os.getenv("OCR_DPI", "250"))
        self.ocr_lang = os.getenv("OCR_LANG", "eng")
        self.min_digital_chars = int(os.getenv("MIN_DIGITAL_CHARS", "25"))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "")
        self.llm_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        self.embedding_backend = os.getenv("EMBEDDING_BACKEND", "st")  # st | hash
        self.embedding_model = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        self.amount_tolerance = float(os.getenv("AMOUNT_TOLERANCE", "0.02"))
        self.review_confidence = float(os.getenv("REVIEW_CONFIDENCE", "0.6"))
        self.dayfirst = os.getenv("DATE_DAYFIRST", "1") == "1"

    def ensure_dirs(self):
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
