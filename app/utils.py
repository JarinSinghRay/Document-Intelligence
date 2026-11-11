import re
from datetime import datetime
from dateutil import parser as dparser

_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
DATE_RE = re.compile(
    rf"\b(\d{{4}}[-/.]\d{{1,2}}[-/.]\d{{1,2}}|\d{{1,2}}[-/.]\d{{1,2}}[-/.]\d{{2,4}}|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?[\s-]+{_MONTHS}\.?,?[\s-]+\d{{2,4}}|{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})\b", re.I)


def parse_amount(s):
    """'$1,234.50' -> 1234.5 ; '1.234,50' -> 1234.5 ; '(12.00)' -> -12.0 ; garbage -> None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return None if s != s else round(float(s), 2)
    t = str(s).strip()
    neg = (t.startswith("(") and t.endswith(")")) or t.startswith("-") or t.endswith("-")
    t = re.sub(r"[^\d.,]", "", t)
    if not re.search(r"\d", t):
        return None
    if "," in t and "." in t:
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".") if re.search(r",\d{1,2}$", t) and t.count(",") == 1 else t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return round(-v if neg else v, 2)


def to_iso_date(s, dayfirst=True):
    """Returns (iso|None, ambiguous: bool)."""
    if not s:
        return None, False
    s = str(s).strip()
    amb = False
    if re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", s):   # ISO-like: year first, never day-first
        try:
            return dparser.parse(s, yearfirst=True, dayfirst=False, default=datetime(1900, 1, 1)).date().isoformat(), False
        except (ValueError, OverflowError):
            return None, False
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12: dayfirst = True
        elif b > 12: dayfirst = False
        else: amb = a != b
    try:
        d = dparser.parse(s, dayfirst=dayfirst, default=datetime(1900, 1, 1))
        if d.year == 1900:
            return None, False
        return d.date().isoformat(), amb
    except (ValueError, OverflowError):
        return None, False


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
