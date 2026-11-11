"""Database schema. SQLite by default, PostgreSQL by setting DATABASE_URL."""
from datetime import datetime
from sqlalchemy import (Column, Integer, String, Float, Text, DateTime, Boolean, ForeignKey,
                        JSON, Index, UniqueConstraint)
from sqlalchemy.orm import relationship
from .db import Base


def now():
    return datetime.utcnow()


class Document(Base):
    """One uploaded file (may contain many pages / sheets / invoices)."""
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    filename = Column(String(512), nullable=False)
    stored_path = Column(String(1024), nullable=False)
    sha256 = Column(String(64), index=True, nullable=False)
    kind = Column(String(16))                       # pdf | image | xlsx | xls | csv
    size_bytes = Column(Integer, default=0)
    status = Column(String(20), default="queued", index=True)  # queued|processing|completed|needs_review|failed
    stage = Column(String(40), default="queued")
    progress = Column(Float, default=0.0)           # 0..100
    page_count = Column(Integer, default=0)
    error = Column(Text)
    attempts = Column(Integer, default=0)
    duplicate_of = Column(Integer, ForeignKey("documents.id"), nullable=True)
    processing_ms = Column(Integer)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)
    pages = relationship("Page", cascade="all, delete-orphan", back_populates="document")
    invoices = relationship("Invoice", cascade="all, delete-orphan", back_populates="document")
    history = relationship("History", cascade="all, delete-orphan", back_populates="document")


class Page(Base):
    """Extracted text per PDF page / image frame / sheet."""
    __tablename__ = "pages"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_no = Column(Integer)
    text = Column(Text, default="")
    source = Column(String(16))                     # digital | ocr | sheet | failed
    ocr_confidence = Column(Float)
    error = Column(Text)
    document = relationship("Document", back_populates="pages")
    __table_args__ = (UniqueConstraint("document_id", "page_no"),)


class Invoice(Base):
    """One structured record (a document can hold several invoices)."""
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    segment_index = Column(Integer, default=0)
    page_start = Column(Integer); page_end = Column(Integer)
    invoice_number = Column(String(128), index=True)
    invoice_date = Column(String(10), index=True)   # ISO yyyy-mm-dd
    supplier = Column(String(256), index=True)
    customer = Column(String(256))
    subtotal = Column(Float); tax = Column(Float); total = Column(Float, index=True)
    currency = Column(String(8))
    line_items = Column(JSON, default=list)
    confidence = Column(JSON, default=dict)         # per-field 0..1
    method = Column(String(32))                     # heuristic | llm+heuristic | spreadsheet | manual
    status = Column(String(16), default="ok", index=True)  # ok | needs_review | corrected
    corrected = Column(Boolean, default=False)
    extras = Column(JSON, default=dict)             # {'notes': [...], 'invalid': {field: raw}, 'dropped': [...]}
    source_text = Column(Text)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)
    document = relationship("Document", back_populates="invoices")
    issues = relationship("Issue", cascade="all, delete-orphan", back_populates="invoice")
    __table_args__ = (Index("ix_supplier_invno", "supplier", "invoice_number"),)


class Issue(Base):
    __tablename__ = "validation_issues"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    code = Column(String(48)); severity = Column(String(8))  # error | warning
    field = Column(String(48)); message = Column(Text)
    invoice = relationship("Invoice", back_populates="issues")


class History(Base):
    """Audit trail / processing history."""
    __tablename__ = "history"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    invoice_id = Column(Integer, nullable=True)
    ts = Column(DateTime, default=now)
    event = Column(String(48)); detail = Column(JSON)
    document = relationship("Document", back_populates="history")
