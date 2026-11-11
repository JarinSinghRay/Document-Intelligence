"""XLSX / XLS / CSV parsing: every sheet becomes a 'page' of text AND a DataFrame for structured extraction."""
import csv
import io
import pandas as pd
from .errors import ProcessingError

MAX_TEXT_ROWS = 50_000


def _cell(v):
    if v is None or (isinstance(v, float) and v != v):
        return ""
    if hasattr(v, "date") and callable(v.date) and hasattr(v, "hour"):
        return v.date().isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _read_csv(path):
    raw = open(path, "rb").read()
    if not raw.strip():
        raise ProcessingError("CSV file is empty")
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            txt = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(txt[:8192], delimiters=",;\t|")
        sep = dialect.delimiter
    except csv.Error:
        sep = ","
    try:
        return {"Sheet1": pd.read_csv(io.StringIO(txt), sep=sep, header=None, dtype=object, engine="python",
                                      on_bad_lines="skip", skip_blank_lines=True)}
    except Exception as e:
        raise ProcessingError(f"Malformed CSV: {e}")


def read_sheets(path, kind):
    try:
        if kind == "csv":
            sheets = _read_csv(path)
        else:
            sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object,
                                   engine="openpyxl" if kind == "xlsx" else "xlrd")
    except ProcessingError:
        raise
    except Exception as e:
        raise ProcessingError(f"Corrupted or unreadable spreadsheet: {type(e).__name__}: {e}")
    pages, tables = [], []
    for i, (name, df) in enumerate(sheets.items(), 1):
        df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True).map(_cell)
        lines = [" | ".join(c for c in row if c) for row in df.head(MAX_TEXT_ROWS).values.tolist()]
        pages.append({"page_no": i, "text": f"[Sheet: {name}]\n" + "\n".join(l for l in lines if l),
                      "source": "sheet", "ocr_confidence": None, "error": None})
        tables.append((name, df))
    if not pages:
        raise ProcessingError("Workbook contains no sheets")
    return pages, tables
