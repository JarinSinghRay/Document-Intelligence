"""Synthetic LABELED invoice dataset generator (deterministic via seed). Produces files + labels.json.
Covers: digital PDFs (3 layouts), multi-page, scanned PDFs & images at 3 quality levels, TIFF, XLSX/XLS/CSV
(tabular, long-format, form layout), injected arithmetic errors, duplicates, missing fields, corrupted files."""
import io, json, random, sys
from datetime import date, timedelta
from pathlib import Path
import pymupdf, openpyxl, xlwt
from PIL import Image, ImageFilter
import numpy as np
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

SUPPLIERS = ["Acme Industrial Supplies Ltd", "Bluewave Logistics", "Delta Office Solutions", "Everest Tech Traders",
             "Greenfield Foods Co", "Horizon Print House", "Karnaphuli Electronics", "Lotus Stationery Mart",
             "Meghna Packaging Ltd", "Northstar Software LLC", "Padma Cleaning Services", "Sundarban Textiles"]
CUSTOMERS = ["Orion Retail Group", "Pioneer Health Care", "Quantum Analytics Inc", "Riverside Hotel & Spa", "Skyline Constructions",
             "Tigris Pharma Ltd", "Union Trading Corp", "Vertex Engineering", "Westbridge School", "Zenith Hardware"]
ITEMS = ["Laptop stand", "A4 paper ream", "Thermal printer roll", "USB-C cable", "Cloud hosting (monthly)", "Safety helmet",
         "Cotton fabric (m)", "Cardboard box 40x30", "Consulting hours", "Network switch 24-port", "Toner cartridge", "Hand sanitizer 5L"]
CUR = {"USD": "$", "EUR": "€", "BDT": "BDT", "GBP": "£"}
DFMT = ["%Y-%m-%d", "%d/%m/%Y", "%B %d, %Y", "%d %b %Y"]


def make_invoice(rng, n, n_items=None, cur=None):
    items = []
    for _ in range(n_items or rng.randint(2, 6)):
        q, u = rng.randint(1, 20), round(rng.uniform(5, 480), 2)
        items.append({"description": rng.choice(ITEMS) + f" {rng.randint(10,99)}", "quantity": float(q), "unit_price": u, "amount": round(q * u, 2)})
    sub = round(sum(i["amount"] for i in items), 2); rate = rng.choice([5, 10, 15]); tax = round(sub * rate / 100, 2)
    d = date(2025, 1, 1) + timedelta(days=rng.randint(0, 600))
    fmt = rng.choice(DFMT)
    if fmt == "%d/%m/%Y": d = d.replace(day=rng.randint(13, 28))
    return {"invoice_number": rng.choice(["INV-2025-%04d", "A-%05d", "2025/%04d"]) % (n + 100), "invoice_date": d.isoformat(),
            "supplier": rng.choice(SUPPLIERS), "customer": rng.choice(CUSTOMERS), "subtotal": sub, "tax": tax, "total": round(sub + tax, 2),
            "currency": cur or rng.choice(list(CUR)), "line_items": items, "_fmt": fmt, "_rate": rate}


def money(v, cur, sym): return f"{CUR[cur]}{v:,.2f}" if sym and CUR[cur] != "BDT" else f"{v:,.2f}"


def draw_pdf(invs, path, template):
    c = canvas.Canvas(str(path), pagesize=A4); W, H = A4
    for t, inv in zip(template if isinstance(template, list) else [template] * len(invs), invs):
        d = date.fromisoformat(inv["invoice_date"]).strftime(inv["_fmt"]) if inv["invoice_date"] else None
        pages = [inv["line_items"][i:i + 14] for i in range(0, len(inv["line_items"]), 14)] or [[]]
        for pi, chunk in enumerate(pages):
            y = H - 60
            def line(txt, x=50, dy=18, size=10, bold=False):
                nonlocal y; c.setFont("Helvetica-Bold" if bold else "Helvetica", size); c.drawString(x, y, txt); y -= dy
            if t == "A":
                line("INVOICE", size=20, dy=30, bold=True); line(f"Invoice No: {inv['invoice_number']}")
                if d: line(f"Invoice Date: {d}")
                line(f"From: {inv['supplier']}")
                if inv["customer"]: line(f"Bill To: {inv['customer']}", dy=28)
            elif t == "B":
                line(inv["supplier"], size=16, dy=24, bold=True); line("TAX INVOICE", size=12, bold=True)
                line(f"Invoice # {inv['invoice_number']}")
                if d: line(f"Date: {d}")
                line(f"Currency: {inv['currency']}")
                if inv["customer"]: line("Billed To:", dy=14); line(inv["customer"], dy=28)
            else:
                line("Invoice", size=18, dy=28, bold=True)
                c.setFont("Helvetica", 10); c.drawString(50, y, f"From: {inv['supplier']}")
                if inv["customer"]: c.drawString(330, y, f"Bill To: {inv['customer']}")
                y -= 18; line(f"Invoice Number: {inv['invoice_number']}")
                if d: line(f"Date of Issue: {d}", dy=28)
            sym = t == "A"
            if pi: line(f"(continued, page {pi+1})")
            y -= 6; c.setFont("Helvetica-Bold", 10)
            for x, h in ((50, "Description"), (330, "Qty"), (400, "Unit Price"), (500, "Amount")): c.drawString(x, y, h)
            y -= 18; c.setFont("Helvetica", 10)
            for it in chunk:
                c.drawString(50, y, it["description"]); c.drawString(330, y, f"{it['quantity']:g}")
                c.drawRightString(470, y, money(it["unit_price"], inv["currency"], sym)); c.drawRightString(550, y, money(it["amount"], inv["currency"], sym)); y -= 16
            if pi == len(pages) - 1:
                y -= 10
                labels = {"A": ("Subtotal", f"Tax ({inv['_rate']}%)", "Total"), "B": ("Sub Total", f"VAT {inv['_rate']}%", "Grand Total"),
                          "C": ("Subtotal", f"Tax {inv['_rate']}%", "Total Amount")}[t]
                for lab, key in zip(labels, ("subtotal", "tax", "total")):
                    c.setFont("Helvetica-Bold" if key == "total" else "Helvetica", 10)
                    c.drawString(380, y, lab); c.drawRightString(550, y, money(inv[key], inv["currency"], sym) + (f" {inv['currency']}" if t == "C" and key == "total" else "")); y -= 16
            c.showPage()
    c.save()


def degrade(img, level, rng):
    if level == "clean": return img
    a = np.asarray(img.convert("L"), dtype=np.float32)
    s = 6 if level == "noisy" else 22
    a = np.clip(a + np.random.default_rng(rng.randint(0, 9999)).normal(0, s, a.shape), 0, 255).astype(np.uint8)
    img = Image.fromarray(a).filter(ImageFilter.GaussianBlur(1.0 if level == "noisy" else 1.6))
    img = img.rotate(rng.choice([-1, 1]) * (1.5 if level == "noisy" else 3.0), expand=True, fillcolor=255)
    if level == "poor": img = img.resize((int(img.width * 0.55), int(img.height * 0.55)))
    return img.convert("RGB")


def render(pdf, dpi=150):
    doc = pymupdf.open(str(pdf)); out = []
    for p in doc: out.append(Image.open(io.BytesIO(p.get_pixmap(dpi=dpi).tobytes("png"))).convert("RGB"))
    return out


def main(out="eval/dataset", seed=7, scale=1.0):
    rng = random.Random(seed); out = Path(out); out.mkdir(parents=True, exist_ok=True)
    labels, counter = [], [0]
    def inv(**kw): counter[0] += 1; return make_invoice(rng, counter[0], **kw)
    def clean(i): return {k: v for k, v in i.items() if not k.startswith("_")}
    def add(file, cat, recs, expect="ok", inject=None, mismatch=None):
        labels.append({"file": file, "category": cat, "expect": expect, "records": [clean(r) for r in recs], "inject": inject or []})

    def shown(i, t):   # layout A prints no currency for BDT -> truth must not claim one (we never invent values)
        return {**i, "currency": None} if t == "A" and i["currency"] == "BDT" else i
    for k in range(int(24 * scale)):                                  # digital PDFs, single invoice
        i = inv(n_items=rng.choice([3, 4, 5, 20]) if k % 8 == 0 else None); t = "ABC"[k % 3]
        f = f"digital_{k:03d}.pdf"; draw_pdf([i], out / f, t); add(f, "pdf_digital", [shown(i, t)])
    batch = [inv() for _ in range(int(20 * scale))]                    # multi-invoice batch PDF
    tpl = [("ABC"[k % 3]) for k in range(len(batch))]; draw_pdf(batch, out / "batch_digital.pdf", tpl); add("batch_digital.pdf", "pdf_digital_batch", [shown(b, t) for b, t in zip(batch, tpl)])
    for lvl in ("clean", "noisy", "poor"):                             # scanned PDFs + images
        for k in range(int(5 * scale)):
            i = inv(n_items=rng.randint(2, 5)); tmp = out / "_t.pdf"; draw_pdf([i], tmp, "ABC"[k % 3])
            img = degrade(render(tmp)[0], lvl, rng)
            f = f"scan_{lvl}_{k}.pdf"; img.save(out / f, "PDF", resolution=150); add(f, f"pdf_scanned_{lvl}", [shown(i, "ABC"[k % 3])])
            for ext, fmt, kw in (("png", "PNG", {}), ("jpg", "JPEG", {"quality": 35 if lvl == "poor" else 85})):
                if k < max(2, int(3 * scale)):   # each image is a DISTINCT invoice (no accidental duplicates)
                    i2 = inv(n_items=rng.randint(2, 5)); draw_pdf([i2], tmp, "ABC"[(k + 1) % 3]); im2 = degrade(render(tmp)[0], lvl, rng)
                    f = f"img_{lvl}_{k}.{ext}"; im2.save(out / f, fmt, **kw); add(f, f"image_{lvl}", [shown(i2, "ABC"[(k + 1) % 3])])
        tmp.unlink(missing_ok=True)
    a, b = inv(n_items=3), inv(n_items=3); tmp = out / "_t.pdf"; draw_pdf([a, b], tmp, "A"); a, b = shown(a, "A"), shown(b, "A")      # multi-page TIFF
    ims = [degrade(x, "noisy", rng) for x in render(tmp)]; ims[0].save(out / "multipage.tiff", save_all=True, append_images=ims[1:]); add("multipage.tiff", "image_tiff", [a, b]); tmp.unlink()
    # ---- spreadsheets
    def sheet_rows(invs, long=False):
        hdr = ["Invoice No", "Invoice Date", "Supplier", "Customer", "Subtotal", "Tax", "Total", "Currency"]
        return hdr, [[r["invoice_number"], r["invoice_date"], r["supplier"], r["customer"], r["subtotal"], r["tax"], r["total"], r["currency"]] for r in invs]
    X = [inv() for _ in range(int(25 * scale))]; wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Invoices"
    ws.append(["Monthly invoice register"]); ws.append([]); h, rows = sheet_rows(X); ws.append(h); [ws.append(r) for r in rows]
    wb.create_sheet("Notes").append(["Prepared by finance team", "do not distribute"]); wb.save(out / "register.xlsx"); add("register.xlsx", "xlsx_tabular", [{**x, "line_items": []} for x in X])
    L = [inv(n_items=3) for _ in range(5)]; wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Invoice No", "Date", "Vendor", "Client", "Description", "Qty", "Unit Price", "Amount", "Subtotal", "Tax", "Total", "Currency"])
    for r in L:
        for n, it in enumerate(r["line_items"]):
            ws.append([r["invoice_number"], r["invoice_date"], r["supplier"], r["customer"], it["description"], it["quantity"], it["unit_price"], it["amount"]] + ([r["subtotal"], r["tax"], r["total"], r["currency"]] if n == 0 else [None] * 4))
    wb.save(out / "long_format.xlsx"); add("long_format.xlsx", "xlsx_long", L)
    C = [inv() for _ in range(10)]; h, rows = sheet_rows(C)
    (out / "invoices_semicolon.csv").write_text("\n".join(";".join(map(str, r)) for r in [h] + rows), encoding="utf-8"); add("invoices_semicolon.csv", "csv", [{**x, "line_items": []} for x in C])
    S = [inv() for _ in range(8)]; h, rows = sheet_rows(S); wbx = xlwt.Workbook(); w = wbx.add_sheet("Data")
    for ri, r in enumerate([h] + rows):
        for ci, v in enumerate(r): w.write(ri, ci, v)
    wbx.save(str(out / "legacy.xls")); add("legacy.xls", "xls", [{**x, "line_items": []} for x in S])
    for k in range(int(4 * scale)):                                    # form-layout single-invoice sheet
        i = inv(n_items=3); wb = openpyxl.Workbook(); ws = wb.active
        for lab, v in (("Invoice No", i["invoice_number"]), ("Invoice Date", i["invoice_date"]), ("Supplier", i["supplier"]), ("Bill To", i["customer"])): ws.append([lab, v])
        ws.append([]); ws.append(["Description", "Qty", "Unit Price", "Amount"])
        for it in i["line_items"]: ws.append([it["description"], it["quantity"], f"{it['unit_price']:.2f}", f"{it['amount']:.2f}"])
        ws.append([]); ws.append(["Subtotal", f"{i['subtotal']:.2f}"]); ws.append(["Tax", f"{i['tax']:.2f}"]); ws.append(["Total", f"{i['total']:.2f}"]); ws.append(["Currency", i["currency"]])
        f = f"form_{k}.xlsx"; wb.save(out / f); add(f, "xlsx_form", [i])
    # ---- validation scenarios (digital PDFs with injected problems)
    for k in range(4):   # wrong total
        i = inv(); i["total"] = round(i["total"] + rng.choice([10, 25.5, 100]), 2); f = f"bad_total_{k}.pdf"; draw_pdf([i], out / f, "ABC"[k % 3]); add(f, "inject_total_mismatch", [shown(i, "ABC"[k % 3])], inject=["total_mismatch"])
    for k in range(3):   # line math wrong
        i = inv(n_items=3); i["line_items"][1]["amount"] = round(i["line_items"][1]["amount"] + 7.0, 2); f = f"bad_line_{k}.pdf"; draw_pdf([i], out / f, "A"); add(f, "inject_line_math", [shown(i, "A")], inject=["line_item_math"])
    for k in range(3):   # duplicates (same supplier+number, different layout)
        i = inv(); f1, f2 = f"dup_{k}_a.pdf", f"dup_{k}_b.pdf"; draw_pdf([i], out / f1, "A"); draw_pdf([i], out / f2, "C")
        add(f1, "dup_pair", [shown(i, "A")], inject=[f"dup_group_{k}"]); add(f2, "dup_pair", [i], inject=[f"dup_group_{k}"])
    for k in range(3):   # missing date + customer (must NOT be invented)
        i = inv(); i["invoice_date"] = None; i["customer"] = None; f = f"missing_{k}.pdf"; draw_pdf([i], out / f, "ABC"[k]); add(f, "inject_missing", [shown(i, "ABC"[k])], inject=["missing_field"])
    # ---- corrupted / unreadable
    good = (out / "digital_000.pdf").read_bytes()
    (out / "corrupt_truncated.pdf").write_bytes(good[:300]); add("corrupt_truncated.pdf", "corrupt", [], "fail")
    (out / "corrupt_garbage.png").write_bytes(bytes(rng.getrandbits(8) for _ in range(4000))); add("corrupt_garbage.png", "corrupt", [], "fail")
    (out / "corrupt_book.xlsx").write_bytes(b"PK\x03\x04" + bytes(rng.getrandbits(8) for _ in range(3000))); add("corrupt_book.xlsx", "corrupt", [], "fail")
    Image.new("RGB", (800, 1100), "white").save(out / "blank_page.png"); add("blank_page.png", "corrupt", [], "fail")
    d = pymupdf.open(str(out / "digital_001.pdf")); d.save(str(out / "encrypted.pdf"), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="o"); add("encrypted.pdf", "corrupt", [], "fail")
    (out / "labels.json").write_text(json.dumps(labels, indent=1)); print(f"{len(labels)} files, {sum(len(l['records']) for l in labels)} labeled records -> {out}")


def make_large_pdf(path, n_invoices=400):
    """~1000-page digital PDF stress test (invoices of 1-5 pages, some with extra-long item lists)."""
    rng = random.Random(1); invs = [make_invoice(rng, 5000 + k, n_items=rng.choice([3, 6, 30, 45])) for k in range(n_invoices)]
    draw_pdf(invs, path, [("ABC"[k % 3]) for k in range(len(invs))]); return invs


if __name__ == "__main__":
    main(*(sys.argv[1:2] or ["eval/dataset"]))
