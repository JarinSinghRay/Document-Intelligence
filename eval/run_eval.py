"""End-to-end evaluation through the real REST API + background workers. Writes eval/report.json and docs/EVALUATION.md.
Usage: python eval/run_eval.py [dataset_dir] [--large]"""
import json, os, shutil, statistics as st, sys, tempfile, time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp(prefix="docintel_eval_"); os.environ["DATA_DIR"] = tmp; os.environ.setdefault("WORKERS", "4")
from fastapi.testclient import TestClient            # noqa: E402
from app.main import app                              # noqa: E402
from app.utils import norm                            # noqa: E402

FIELDS = ["invoice_number", "invoice_date", "supplier", "customer", "subtotal", "tax", "total", "currency"]
NUMF = {"subtotal", "tax", "total"}


def same(f, a, b):
    if a is None or b is None: return a is None and b is None
    return abs(a - b) <= 0.01 if f in NUMF else norm(str(a)) == norm(str(b))


def items_f1(pred, truth):
    if not pred and not truth: return 1.0
    key = lambda i: (norm(i.get("description")), round(i.get("amount") or 0, 2))
    p, t = [key(i) for i in pred], [key(i) for i in truth]; tp = 0
    for x in t:
        if x in p: p.remove(x); tp += 1
    pr, rc = tp / max(len(pred), 1), tp / max(len(truth), 1)
    return 0 if pr + rc == 0 else 2 * pr * rc / (pr + rc)


def pct(x, n): return round(100 * x / n, 2) if n else None


def wait(c, ids, timeout=1500):
    t0 = time.time(); terminal = {"completed", "needs_review", "failed", "duplicate"}
    while time.time() - t0 < timeout:
        docs = {d["id"]: d for d in c.get("/api/documents?limit=500").json()["items"]}
        if all(docs[i]["status"] in terminal for i in ids): return docs
        time.sleep(1)
    raise TimeoutError("jobs did not finish")


def main(ds, large=False):
    labels = json.loads((ds / "labels.json").read_text()); rep = {}
    with TestClient(app) as c:
        t0 = time.time(); ids = {}
        for L in labels:
            r = c.post("/api/documents", files=[("files", (L["file"], (ds / L["file"]).read_bytes()))]).json()
            ids[L["file"]] = r["documents"][0]["id"]
        docs = wait(c, list(ids.values())); wall = time.time() - t0
        fa, per_cat = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]); halluc = [0, 0]; full = [0, 0]; f1s = []
        ok_files = [L for L in labels if L["expect"] == "ok"]; fail_files = [L for L in labels if L["expect"] == "fail"]
        succ = sum(docs[ids[L["file"]]]["status"] in ("completed", "needs_review") for L in ok_files)
        caught = sum(docs[ids[L["file"]]]["status"] == "failed" and bool(docs[ids[L["file"]]]["error"]) for L in fail_files)
        times = defaultdict(list); inject_hit = defaultdict(lambda: [0, 0]); clean_flag = [0, 0]; dup_groups = defaultdict(list)
        count_ok = [0, 0]; fails_detail = []; silent = [0, 0, 0]; mism = []; reasons = defaultdict(int)
        for L in labels:
            d = docs[ids[L["file"]]]
            if d["processing_ms"]: times[L["category"].split("_")[0] + ("_" + L["category"].split("_")[1] if L["category"].startswith(("pdf", "image")) else "")].append(d["processing_ms"])
            if L["expect"] == "fail":
                if d["status"] != "failed": fails_detail.append((L["file"], "expected failure but status=" + d["status"]))
                continue
            if d["status"] == "failed": fails_detail.append((L["file"], d["error"])); continue
            recs = [c.get(f"/api/invoices/{r['id']}").json() for r in c.get(f"/api/documents/{d['id']}").json()["records"]]
            truth = L["records"]; count_ok[1] += 1; count_ok[0] += len(recs) == len(truth)
            pairs, used = [], set(); rest = list(range(len(truth)))
            for ti, t in enumerate(truth):
                m = next((k for k, r in enumerate(recs) if k not in used and norm(r["invoice_number"]) == norm(t["invoice_number"])), None)
                if m is not None: used.add(m); pairs.append((t, recs[m]))
            un_t = [t for t in truth if not any(t is p[0] for p in pairs)]; un_r = [r for k, r in enumerate(recs) if k not in used]
            pairs += list(zip(un_t, un_r)); pairs += [(t, None) for t in un_t[len(un_r):]]
            for t, r in pairs:
                ok_all = True; nbad0 = len(mism)
                for f in FIELDS:
                    good = r is not None and same(f, r[f], t[f]); fa[f][0] += good; fa[f][1] += 1
                    if not good: mism.append((L['file'], f, None if r is None else r[f], t[f]))
                    per_cat[L["category"]][0] += good; per_cat[L["category"]][1] += 1; ok_all &= good
                    if t[f] is None: halluc[1] += 1; halluc[0] += (r is not None and r[f] is not None)
                f1 = items_f1(r["line_items"] if r else [], t["line_items"]); f1s.append(f1); ok_all &= f1 == 1.0
                full[0] += ok_all; full[1] += 1
                if not ok_all:
                    silent[0] += 1; flagged = r is None or r['status'] == 'needs_review'; silent[1] += flagged; silent[2] += (not flagged)
            codes = {i["code"] for r in recs for i in r["issues"]}
            if not L["inject"] and not L["category"].startswith(("dup", "inject")):
                clean_flag[1] += 1; clean_flag[0] += d["status"] == "needs_review"
                for r in recs:
                    for i in r["issues"]: reasons[f"{L['category']}:{i['code']}:{i['field']}"] += 1
            for code in L["inject"]:
                if code.startswith("dup_group"): dup_groups[code].append(codes.__contains__("duplicate_invoice"))
                else: inject_hit[code][1] += 1; inject_hit[code][0] += code in codes
        dup_ok = sum(sum(v) == 1 for v in dup_groups.values())
        allf = sum(v[0] for v in fa.values()), sum(v[1] for v in fa.values())
        rep = {"files": len(labels), "labeled_records": sum(len(L["records"]) for L in labels), "wall_clock_s": round(wall, 1),
               "workers": int(os.environ["WORKERS"]), "document_success_rate_pct": pct(succ, len(ok_files)),
               "document_failure_rate_pct_on_valid_files": pct(len(ok_files) - succ, len(ok_files)),
               "corrupt_files_correctly_rejected": f"{caught}/{len(fail_files)}",
               "record_count_correct_pct": pct(*count_ok), "field_accuracy_pct": {f: pct(*fa[f]) for f in FIELDS},
               "overall_field_accuracy_pct": pct(*allf), "full_record_exact_match_pct": pct(*full),
               "line_item_f1_mean": round(sum(f1s) / len(f1s), 4) if f1s else None,
               "records_with_any_error": silent[0], "erroneous_records_routed_to_review_pct": pct(silent[1], silent[0]), "silent_errors_not_flagged": silent[2],
               "fabricated_values_on_absent_fields": f"{halluc[0]}/{halluc[1]}",
               "accuracy_by_category_pct": {k: pct(*v) for k, v in sorted(per_cat.items())},
               "validation_recall": {k: f"{v[0]}/{v[1]}" for k, v in inject_hit.items()} | {"duplicate_pairs_exactly_one_flagged": f"{dup_ok}/{len(dup_groups)}"},
               "clean_docs_flagged_for_review_pct": pct(*clean_flag),
               "processing_ms_by_type": {k: {"n": len(v), "mean": int(st.mean(v)), "median": int(st.median(v)), "p95": int(sorted(v)[int(0.95 * (len(v) - 1))])} for k, v in sorted(times.items())},
               "unexpected_failures": fails_detail, "mismatches": mism, "flag_reasons_on_clean": dict(reasons)}
        if large:
            from eval.make_dataset import make_large_pdf
            p = Path(tmp) / "large.pdf"; truth = make_large_pdf(p, 400); import pymupdf
            n = pymupdf.open(str(p)).page_count; t1 = time.time()
            did = c.post("/api/documents", files=[("files", ("large.pdf", p.read_bytes()))]).json()["documents"][0]["id"]
            d = wait(c, [did], 3000)[did]; recs = c.get(f"/api/invoices?document_id={did}&limit=500").json()
            nums = {norm(r["invoice_number"]) for r in recs["items"]}
            rep["large_pdf"] = {"pages": n, "invoices_in_truth": len(truth), "records_extracted": recs["total"], "wall_s": round(time.time() - t1, 1),
                                "pages_per_s": round(n / (time.time() - t1), 1), "status": d["status"],
                                "invoice_numbers_recovered_pct": pct(sum(norm(t["invoice_number"]) in nums for t in truth), len(truth))}
    (ROOT / "eval/report.json").write_text(json.dumps(rep, indent=1)); print(json.dumps(rep, indent=1))
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    main(Path(a[0] if a else ROOT / "eval/dataset"), "--large" in sys.argv)
