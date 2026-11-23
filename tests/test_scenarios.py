"""End-to-end scenario tests: every sample invoice runs through the real pipeline and must reach its expected decision.

Run:  python -m pytest tests -q        (or: python tests/test_scenarios.py)
Uses an isolated data directory and the extraction engine configured in the environment (local OCR parser if no API key)."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="invoice-test-"))
os.environ["WATCH_INBOX"] = "0"

from app import config, db, pipeline  # noqa: E402

MANIFEST = json.loads((ROOT / "samples" / "manifest.json").read_text())


def setup_module(_=None):
    db.init_db(reset=True)
    config.reset_rules()
    config.save_rules({"stage_delay_ms": 0})


def run(name):
    data = (ROOT / "samples" / name).read_bytes()
    rid = pipeline.create_run(data, name, "test")
    pipeline.execute_run(rid)
    r = db.q1("SELECT * FROM runs WHERE id=?", (rid,))
    return r, json.loads(r["result_json"] or "{}")


def failing(res):
    return {c["id"] for c in res["checks"] if c["status"] in ("warn", "fail") and c["severity"] != "none"}


def test_all_samples_reach_expected_decision():
    results = []
    for m in MANIFEST:
        r, res = run(m["file"])
        results.append((m["file"], m["expected"], r["decision"], r["headline"]))
        print(f"{m['file']:<45} expected={m['expected']:<13} got={r['decision']:<13} {r['duration_ms']:>5}ms  {r['headline'][:90]}")
    bad = [x for x in results if x[1] != x[2]]
    assert not bad, f"Mismatched decisions: {bad}"


def test_specific_reasons():
    setup_module()
    _, res = run("EC1_scanned_no_po_delta_laptops.pdf")
    assert res["matched_po"] == "PO-2026-0104"
    _, res = run("EC2_split_overbilling_evergreen.pdf")
    assert {"po_balance", "line_on_po"} <= failing(res)
    _, res = run("EC3_bank_change_apex_supplies.pdf")
    assert failing(res) == {"bank_match"} and res["fraud_risk"]
    _, res = run("EC4_duplicate_brightline_resent.pdf")
    assert "dup_number" in failing(res)
    _, res = run("X3_short_receipt_coastal_film.pdf")
    assert "grn" in failing(res)
    # re-uploading an approved invoice is caught
    run("01_happy_apex_furniture.pdf")
    r, res = run("01_happy_apex_furniture.pdf")
    assert r["decision"] == "REJECTED" and {"dup_file", "dup_number"} <= failing(res)


def test_review_approval_consumes_po():
    setup_module()
    r, _ = run("EC2_split_overbilling_evergreen.pdf")
    before = db.po_with_lines("PO-2026-0105")["billed_net"]
    pipeline.review(r["id"], "approve", "Deep clean approved by admin head; PO amendment raised")
    after = db.po_with_lines("PO-2026-0105")["billed_net"]
    assert after - before == 125000


if __name__ == "__main__":
    setup_module()
    test_all_samples_reach_expected_decision()
    test_specific_reasons()
    test_review_approval_consumes_po()
    print("ALL TESTS PASSED")
