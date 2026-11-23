"""The invoice-to-decision process.

  Intake -> AI extraction -> Validation -> Vendor verification -> Duplicate check -> PO & receipt matching
         -> Decision -> Post & notify

Design principle: AI reads the messy document; deterministic, explainable rules make the decision.
Every check produces a structured result (pass / warn / fail, severity, owner) that is streamed live and stored."""
import hashlib
import json
import re
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone

import pymupdf
from rapidfuzz import fuzz

from . import config, db, events
from . import gstin as gst
from .extraction import local
from .extraction.llm import PROVIDERS, LLMError

PIPE_LOCK = threading.Lock()  # serialises ledger-touching stages so concurrent runs can't double-bill a PO

STAGES = [
    ("intake", "Intake"),
    ("extract", "AI extraction"),
    ("validate", "Validation"),
    ("vendor", "Vendor verification"),
    ("duplicate", "Duplicate check"),
    ("match", "PO & receipt matching"),
    ("decide", "Decision"),
    ("post", "Post & notify"),
]

OWNERS = {
    "treasury": "Treasury (payment security)",
    "ap": "AP team",
    "buyer": "Buyer / requester",
    "receiving": "Warehouse / receiving",
    "controller": "Finance controller",
    "procurement": "Procurement (vendor master)",
    "tax": "Tax team",
}
OWNER_PRIORITY = ["treasury", "ap", "buyer", "receiving", "procurement", "tax", "controller"]

# short, plain-language titles used when a check is the reason for a decision (ordered by importance)
ISSUE_TITLES = {
    "doc_type": "Not a payable tax invoice",
    "vendor_found": "Vendor not onboarded",
    "vendor_status": "Vendor is blocked",
    "dup_file": "Duplicate — file already processed",
    "dup_number": "Duplicate of an invoice already booked",
    "req_invoice_number": "Invoice number missing",
    "req_total": "Total missing",
    "req_vendor": "Supplier not identifiable",
    "req_date": "Invoice date missing",
    "sum_total": "Totals don't add up",
    "sum_lines": "Line items don't add up",
    "buyer_gstin": "Billed to a different company",
    "bank_match": "Bank account differs from vendor master",
    "dup_fuzzy": "Possible duplicate payment",
    "po_balance": "Invoice exceeds remaining PO balance",
    "line_on_po": "Line item not on the purchase order",
    "price": "Price above PO tolerance",
    "qty_po": "Quantity exceeds the PO",
    "grn": "Billed quantity not yet received",
    "po_found": "No matching purchase order",
    "po_vendor": "PO belongs to another vendor",
    "po_open": "PO is not open",
    "vendor_gstin": "GSTIN differs from vendor master",
    "gstin_valid": "Supplier GSTIN invalid",
    "tax_type": "Wrong GST type for place of supply",
    "tax_rate": "Tax amount inconsistent with rate",
    "line_math": "Line arithmetic errors",
    "lines_present": "Invoice not itemised",
    "date_future": "Invoice dated in the future",
    "date_stale": "Invoice unusually old",
    "grounding": "Extraction needs verification",
    "currency": "Foreign currency invoice",
    "approval_limit": "Above auto-approval limit",
}
ISSUE_RANK = {k: i for i, k in enumerate(ISSUE_TITLES)}


def issue_title(c):
    return ISSUE_TITLES.get(c["id"], c["label"])


def inr(x):
    if x is None:
        return "—"
    neg = x < 0
    x = abs(round(x, 2))
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return ("-" if neg else "") + "₹" + whole + ("" if frac == "00" else "." + frac)


def today():
    return date.today()


class Check(dict):
    def __init__(self, id, label, status, detail="", severity="none", owner=None, data=None):
        super().__init__(id=id, label=label, status=status, detail=detail, severity=severity, owner=owner, data=data or {})


# ======================================================================================================== run entry
def create_run(file_bytes: bytes, filename: str, source: str = "upload") -> str:
    run_id = datetime.now().strftime("R%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4].upper()
    path = config.UPLOAD_DIR / f"{run_id}.pdf"
    path.write_bytes(file_bytes)
    db.execute("""INSERT INTO runs (id, filename, file_path, file_hash, source, status, created_at, currency)
                  VALUES (?,?,?,?,?,?,?,?)""",
               (run_id, filename, str(path), hashlib.sha256(file_bytes).hexdigest(), source, "queued",
                events.now_iso(), "INR"))
    events.start(run_id)
    return run_id


def start_run(run_id: str):
    t = threading.Thread(target=execute_run, args=(run_id,), daemon=True)
    t.start()
    return t


def execute_run(run_id: str):
    run = db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
    rules = config.load_rules()
    ctx = {"run_id": run_id, "rules": rules, "filename": run["filename"], "file_hash": run["file_hash"],
           "pdf": open(run["file_path"], "rb").read(), "checks": [], "t0": time.time()}
    db.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
    events.emit(run_id, "run", "running", "Run started", {"filename": run["filename"], "source": run["source"],
                                                          "stages": [{"key": k, "label": l} for k, l in STAGES]})
    try:
        stage(ctx, "intake", stage_intake)
        stage(ctx, "extract", stage_extract)
        stage(ctx, "validate", stage_validate)
        with PIPE_LOCK:
            stage(ctx, "vendor", stage_vendor)
            stage(ctx, "duplicate", stage_duplicate)
            stage(ctx, "match", stage_match)
            stage(ctx, "decide", stage_decide)
            stage(ctx, "post", stage_post)
        dur = int((time.time() - ctx["t0"]) * 1000)
        d = ctx["decision"]
        ex = ctx.get("extracted", {})
        db.execute("""UPDATE runs SET status='completed', decision=?, final_decision=?, headline=?, route_to=?, engine=?,
                      vendor_id=?, vendor_name=?, invoice_number=?, po_number=?, gross_amount=?, extracted_json=?,
                      result_json=?, completed_at=?, duration_ms=?, review_status=? WHERE id=?""",
                   (d["decision"], d["decision"], d["headline"], d.get("route_to"), ctx.get("engine_label"),
                    (ctx.get("vendor") or {}).get("vendor_id"),
                    (ctx.get("vendor") or {}).get("name") or ex.get("vendor_name"),
                    ex.get("invoice_number"), (ctx.get("po") or {}).get("po_number") or ex.get("po_number"),
                    ex.get("total"), json.dumps(ex, default=str), json.dumps(build_output(ctx), default=str),
                    events.now_iso(), dur, "pending" if d["decision"] == "NEEDS_REVIEW" else None, run_id))
        events.emit(run_id, "run", "completed", f"Decision: {d['decision']}", {"decision": d["decision"],
                                                                              "duration_ms": dur})
    except Exception as e:  # unexpected failure: surface it, never leave a run hanging
        traceback.print_exc()
        dur = int((time.time() - ctx["t0"]) * 1000)
        db.execute("UPDATE runs SET status='error', error=?, completed_at=?, duration_ms=?, decision='ERROR', "
                   "final_decision='ERROR', headline=? WHERE id=?",
                   (str(e), events.now_iso(), dur, "Processing error — needs manual handling", run_id))
        events.emit(run_id, "run", "error", "Run failed", {"error": str(e)})
    finally:
        events.finish(run_id)


def stage(ctx, key, fn):
    label = dict(STAGES)[key]
    events.emit(ctx["run_id"], key, "running", label, {})
    t = time.time()
    n_before = len(ctx["checks"])
    summary, data = fn(ctx)
    checks = ctx["checks"][n_before:]
    status = "pass"
    if any(c["status"] == "fail" for c in checks):
        status = "fail"
    elif any(c["status"] == "warn" for c in checks):
        status = "warn"
    if key == "decide":
        status = {"APPROVED": "pass", "NEEDS_REVIEW": "warn", "REJECTED": "fail"}[ctx["decision"]["decision"]]
    delay = ctx["rules"].get("stage_delay_ms", 0) / 1000
    elapsed = time.time() - t
    if delay > elapsed:
        time.sleep(delay - elapsed)
    events.emit(ctx["run_id"], key, status, label, {"summary": summary, "checks": checks, "data": data,
                                                    "duration_ms": int(elapsed * 1000)})


# ======================================================================================================== stages
def stage_intake(ctx):
    pdf = ctx["pdf"]
    add = ctx["checks"].append
    try:
        doc = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception:
        add(Check("file_type", "Readable PDF", "fail", "File could not be opened as a PDF.", "block", "ap"))
        raise RuntimeError("Unreadable or non-PDF file")
    if doc.needs_pass:
        add(Check("encrypted", "Not password protected", "fail", "PDF is password protected.", "block", "ap"))
        raise RuntimeError("Encrypted PDF")
    reading = local.read_document(pdf)
    ctx["reading"] = reading
    scanned = reading["scanned"]
    if reading["method"] == "ocr-unavailable" and config.resolve_provider() == "local":
        add(Check("ocr", "Scanned page readable", "fail",
                  "This is a scanned image and no reader is available. Configure an AI API key or install Tesseract OCR.", "block", "ap"))
        raise RuntimeError("Scanned PDF but neither an AI engine nor Tesseract OCR is available")
    add(Check("file_type", "Readable PDF", "pass", f"{reading['pages']} page(s), {len(pdf) / 1024:.0f} KB"))
    add(Check("text_layer", "Text layer", "info" if scanned else "pass",
              "No text layer — scanned image, will be read visually / via OCR" if scanned
              else f"Machine-readable text ({reading['text_chars']} characters)"))
    data = {"sha256": ctx["file_hash"], "pages": reading["pages"], "size_kb": round(len(pdf) / 1024, 1),
            "document_kind": "Scanned image" if scanned else "Digital PDF"}
    if reading.get("ocr_confidence") is not None:
        data["ocr_confidence"] = reading["ocr_confidence"]
    return ("Scanned image PDF received" if scanned else "Digital PDF received"), data


def stage_extract(ctx):
    add = ctx["checks"].append
    provider = config.resolve_provider()
    rules = ctx["rules"]
    raw, usage, engine = None, None, provider
    if provider in PROVIDERS:
        try:
            raw, usage = PROVIDERS[provider](ctx["pdf"])
        except (LLMError, Exception) as e:
            add(Check("ai_engine", "AI engine available", "warn",
                      f"{config.provider_label(provider)} failed ({str(e)[:160]}). Fell back to local OCR parser.", "none"))
            engine = "local"
    if raw is None:
        engine = "local"
        raw = local.parse_invoice(ctx["reading"], rules["buyer_gstin"])
    ex = local.normalize(raw)
    ctx["extracted"] = ex
    ctx["engine"] = engine
    if engine == "local" and provider in PROVIDERS:
        ctx["engine_label"] = "Local OCR + parser (AI fallback)"
    else:
        ctx["engine_label"] = config.provider_label(engine)
    if engine == "local" and ctx["reading"]["method"] == "ocr":
        ctx["engine_label"] += " · OCR"

    # document type
    dt = ex.get("document_type") or "tax_invoice"
    if dt in ("proforma_invoice", "quotation", "purchase_order", "receipt", "other"):
        nice = dt.replace("_", " ")
        add(Check("doc_type", "Payable tax invoice", "fail",
                  f"Document is a {nice}, not a tax invoice — it cannot be booked for payment.", "block", "ap"))
    elif dt == "credit_note":
        add(Check("doc_type", "Payable tax invoice", "warn", "Document is a credit note — route to AP to apply against open invoices.",
                  "review", "ap"))
    else:
        add(Check("doc_type", "Payable tax invoice", "pass", "Document identified as a tax invoice"))

    # grounding: verify AI-extracted identifiers actually appear on the page (guards against hallucination)
    if engine != "local" and not ctx["reading"]["text"].strip():
        add(Check("grounding", "Values found on document", "info", "No machine-readable text to cross-check against (OCR unavailable)."))
    elif engine != "local":
        src = re.sub(r"[^A-Z0-9]", "", ctx["reading"]["text"].upper())
        src_nums = set(round(n, 2) for n in local.numbers_in(ctx["reading"]["text"]))
        missing, checked = [], 0
        for field in ("invoice_number", "vendor_gstin", "po_number", "bank_account_number"):
            v = ex.get(field)
            if v:
                checked += 1
                vv = re.sub(r"[^A-Z0-9]", "", str(v).upper())
                if vv not in src and fuzz.partial_ratio(vv, src) < 85:
                    missing.append(field)
        if ex.get("total") is not None:
            checked += 1
            if round(ex["total"], 2) not in src_nums and ctx["reading"]["method"] != "ocr":
                missing.append("total")
        if missing:
            add(Check("grounding", "Values found on document", "warn",
                      f"Could not locate {', '.join(missing)} in the document text — verify extraction.", "review", "ap"))
        else:
            add(Check("grounding", "Values found on document", "pass",
                      f"{checked} key values cross-checked against the {'OCR' if ctx['reading']['method'] == 'ocr' else 'PDF'} text"))

    fields = ["vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_number", "total", "bank_account_number"]
    found = sum(1 for f in fields if ex.get(f) not in (None, ""))
    data = {"engine": ctx["engine_label"], "fields_found": f"{found}/{len(fields)}", "line_items": len(ex["line_items"]),
            "extracted": ex}
    if usage:
        data["tokens"] = usage
    summary = f"{len(ex['line_items'])} line item(s), total {inr(ex.get('total'))} · {ctx['engine_label']}"
    return summary, data


def stage_validate(ctx):
    ex, rules, add = ctx["extracted"], ctx["rules"], ctx["checks"].append
    tol = rules["arithmetic_tolerance_abs"]

    # required fields
    if not ex.get("invoice_number"):
        add(Check("req_invoice_number", "Invoice number present", "fail",
                  "No invoice number — the invoice can't be posted or protected against duplicate payment.", "block", "ap"))
    else:
        add(Check("req_invoice_number", "Invoice number present", "pass", ex["invoice_number"]))
    if not ex.get("total"):
        add(Check("req_total", "Invoice total present", "fail", "No total amount found on the invoice.", "block", "ap"))
    if not ex.get("vendor_name") and not ex.get("vendor_gstin"):
        add(Check("req_vendor", "Supplier identified", "fail", "No supplier name or GSTIN on the invoice.", "block", "ap"))

    # dates
    inv_date = None
    if ex.get("invoice_date"):
        inv_date = date.fromisoformat(ex["invoice_date"])
        age = (today() - inv_date).days
        if inv_date > today():
            add(Check("date_future", "Invoice date plausible", "warn", f"Invoice is dated in the future ({inv_date:%d %b %Y}).",
                      "review", "ap"))
        elif age > rules["max_invoice_age_days"]:
            add(Check("date_stale", "Invoice date plausible", "warn",
                      f"Invoice is {age} days old (limit {rules['max_invoice_age_days']}) — confirm it hasn't been paid.", "review", "ap"))
        else:
            add(Check("date_ok", "Invoice date plausible", "pass", f"{inv_date:%d %b %Y} ({age} days ago)"))
    else:
        add(Check("req_date", "Invoice date present", "fail", "No invoice date found.", "block", "ap"))
    ctx["invoice_date"] = inv_date

    # arithmetic
    lines = ex["line_items"]
    bad_lines = [l for l in lines if None not in (l["quantity"], l["unit_price"], l["amount"])
                 and abs(l["quantity"] * l["unit_price"] - l["amount"]) > max(tol, l["amount"] * 0.001)]
    if bad_lines:
        add(Check("line_math", "Line items multiply correctly", "warn",
                  f"{len(bad_lines)} line(s) where quantity × rate ≠ amount: " + "; ".join(l["description"][:40] for l in bad_lines),
                  "review", "ap"))
    lines_sum = round(sum(l["amount"] or 0 for l in lines), 2)
    tax_total = ex.get("tax_total") or 0.0
    total = ex.get("total")
    inclusive = ex.get("prices_include_tax")
    subtotal = ex.get("subtotal")
    if inclusive:
        net = round((total or lines_sum) - tax_total, 2) if tax_total else round((total or lines_sum) / 1.18, 2)
        if total is not None and lines and abs(lines_sum - total) > tol:
            add(Check("sum_lines", "Lines add up to total", "fail",
                      f"Line items sum to {inr(lines_sum)} but the tax-inclusive total is {inr(total)}.", "block", "ap"))
        elif total is not None:
            add(Check("sum_lines", "Lines add up to total", "pass",
                      f"Tax-inclusive pricing: {inr(total)} = taxable value {inr(net)} + GST {inr(tax_total)}"))
    else:
        net = subtotal if subtotal is not None else lines_sum
        if lines and subtotal is not None and abs(lines_sum - subtotal) > tol:
            add(Check("sum_lines", "Lines add up to subtotal", "fail",
                      f"Line items sum to {inr(lines_sum)} but the subtotal printed is {inr(subtotal)}.", "block", "ap"))
        elif lines:
            add(Check("sum_lines", "Lines add up to subtotal", "pass", f"{len(lines)} line(s) = {inr(lines_sum)}"))
        if total is not None:
            expected = round(net + tax_total, 2)
            if abs(expected - total) > tol:
                add(Check("sum_total", "Subtotal + tax = total", "fail",
                          f"Printed total {inr(total)} ≠ taxable value {inr(net)} + tax {inr(tax_total)} = {inr(expected)} "
                          f"(difference {inr(total - expected)}).", "block", "ap"))
            else:
                add(Check("sum_total", "Subtotal + tax = total", "pass", f"{inr(net)} + {inr(tax_total)} = {inr(total)}"))
    if not lines:
        add(Check("lines_present", "Itemised lines", "warn", "No line items — invoice is bundled; matching at header level only.",
                  "review", "ap"))
    ctx["net"] = round(net or 0, 2)

    # tax structure: intra-state -> CGST+SGST, inter-state -> IGST
    types = {t["type"] for t in ex["tax_lines"]}
    vstate, bstate = gst.state_of(ex.get("vendor_gstin")), gst.state_of(rules["buyer_gstin"])
    if types and vstate:
        intra = vstate == bstate
        ok = (types <= {"CGST", "SGST"} and len(types) == 2) if intra else (types == {"IGST"})
        exp = "CGST + SGST" if intra else "IGST"
        place = f"{gst.STATES.get(vstate, vstate)} → {gst.STATES.get(bstate, bstate)}"
        if ok:
            add(Check("tax_type", "Correct GST type", "pass", f"{exp} for {place} supply"))
        else:
            add(Check("tax_type", "Correct GST type", "warn", f"Expected {exp} for {place} supply, invoice charges {' + '.join(sorted(types))}.",
                      "review", "tax"))
        rates = {t["rate"] for t in ex["tax_lines"] if t["rate"]}
        eff = round(tax_total / net * 100, 2) if net else 0
        if net and abs(eff - sum(t["rate"] or 0 for t in ex["tax_lines"])) > 0.2:
            add(Check("tax_rate", "Tax amount matches rate", "warn", f"Tax charged is {eff}% of taxable value; stated rates {sorted(rates)}.",
                      "review", "tax"))
    elif not types and not inclusive:
        add(Check("tax_type", "GST charged", "info", "No GST lines found on invoice."))

    # GSTINs
    vg = ex.get("vendor_gstin")
    ok, why = gst.validate(vg)
    if vg and ok:
        add(Check("gstin_valid", "Supplier GSTIN valid", "pass", f"{vg} — format and checksum valid"))
    elif vg:
        add(Check("gstin_valid", "Supplier GSTIN valid", "warn", f"{vg}: {why}.", "review", "tax"))
    else:
        add(Check("gstin_valid", "Supplier GSTIN valid", "warn", "No supplier GSTIN — input tax credit can't be claimed.", "review", "tax"))
    bg = ex.get("buyer_gstin")
    if bg and bg.replace(" ", "").upper() != rules["buyer_gstin"]:
        add(Check("buyer_gstin", "Billed to our entity", "fail", f"Invoice is addressed to GSTIN {bg}, not {rules['buyer_gstin']}.",
                  "block", "ap"))
    if ex.get("currency") and ex["currency"].upper() not in ("INR",):
        add(Check("currency", "Currency", "warn", f"Invoice currency {ex['currency']} — FX handling required.", "review", "ap"))

    fails = sum(1 for c in ctx["checks"] if c["status"] == "fail")
    summary = "Invoice is complete and arithmetically consistent" if not fails else f"{fails} integrity problem(s) found"
    return summary, {"taxable_value": ctx["net"], "tax_total": tax_total, "total": total,
                     "pricing": "Tax-inclusive" if inclusive else "Tax-exclusive"}


def stage_vendor(ctx):
    ex, add = ctx["extracted"], ctx["checks"].append
    vendors = db.vendors()
    vendor, how, score = None, None, 0
    vg = (ex.get("vendor_gstin") or "").upper()
    for v in vendors:
        if vg and v["gstin"] == vg:
            vendor, how, score = v, "GSTIN exact match", 100
            break
    if not vendor and ex.get("vendor_name"):
        best = None
        for v in vendors:
            names = [v["name"]] + [a for a in (v["aliases"] or "").split("|") if a]
            s = max(fuzz.token_set_ratio(ex["vendor_name"].lower(), n.lower()) for n in names)
            if not best or s > best[1]:
                best = (v, s)
        if best and best[1] >= 88:
            vendor, how, score = best[0], f"name match ({best[1]:.0f}% similar)", best[1]
    ctx["vendor"] = vendor
    if not vendor:
        add(Check("vendor_found", "Vendor in master", "fail",
                  f"'{ex.get('vendor_name') or vg or 'Unknown'}' is not an onboarded vendor. Payments are only made to vendors in the master.",
                  "block", "procurement"))
        return "Supplier not found in vendor master", {"invoice_vendor": ex.get("vendor_name"), "invoice_gstin": vg or None}

    add(Check("vendor_found", "Vendor in master", "pass", f"{vendor['vendor_id']} · {vendor['name']} — {how}"))
    if vg and vendor["gstin"] != vg:
        add(Check("vendor_gstin", "GSTIN matches master", "warn", f"Invoice GSTIN {vg} differs from master {vendor['gstin']}.",
                  "review", "procurement"))
    if vendor["status"] == "blocked":
        add(Check("vendor_status", "Vendor active", "fail", f"Vendor is BLOCKED: {vendor['status_reason']}", "block", "procurement"))
    elif vendor["status"] != "approved":
        add(Check("vendor_status", "Vendor active", "warn", f"Vendor status is '{vendor['status']}'.", "review", "procurement"))
    else:
        add(Check("vendor_status", "Vendor active", "pass", "Approved vendor"))

    # bank details — the payment-fraud control
    acct, ifsc = ex.get("bank_account_number"), ex.get("bank_ifsc")
    bank_flag = False
    if acct or ifsc:
        acct_ok = (not acct) or acct.lstrip("0") == vendor["bank_account"].lstrip("0")
        ifsc_ok = (not ifsc) or ifsc == vendor["ifsc"]
        if acct_ok and ifsc_ok:
            add(Check("bank_match", "Bank details match master", "pass",
                      f"A/c ••••{vendor['bank_account'][-4:]} · {vendor['ifsc']} matches verified master record"))
        else:
            bank_flag = True
            change_notice = any(re.search(r"(chang|new (bank|account))", n, re.I) for n in ex.get("notes", []))
            add(Check("bank_match", "Bank details match master", "fail",
                      f"Invoice asks for payment to {ex.get('bank_name') or 'a bank'} a/c {acct or '?'} ({ifsc or '?'}), "
                      f"but the verified account on file is {vendor['bank_name']} a/c ••••{vendor['bank_account'][-4:]} ({vendor['ifsc']})."
                      + (" The invoice carries a 'we have changed our bankers' notice — a common payment-fraud pattern." if change_notice else "")
                      + " Hold payment and verify by calling the vendor on the number in the master record.",
                      "review", "treasury", {"fraud_risk": True}))
    else:
        add(Check("bank_match", "Bank details match master", "info", "No bank details on invoice — payment will go to the master account."))
    ctx["bank_flag"] = bank_flag
    return f"{vendor['name']} ({vendor['vendor_id']}) — {vendor['status']}", {
        "vendor_id": vendor["vendor_id"], "vendor": vendor["name"], "match": how, "status": vendor["status"],
        "payment_terms_days": vendor["payment_terms_days"],
        "master_bank": f"{vendor['bank_name']} ••••{vendor['bank_account'][-4:]} / {vendor['ifsc']}"}


def stage_duplicate(ctx):
    ex, rules, add = ctx["extracted"], ctx["rules"], ctx["checks"].append
    vendor = ctx.get("vendor")
    hits = []
    same_file = db.q1("SELECT * FROM ap_ledger WHERE file_hash=? AND status IN ('paid','approved','in_review')", (ctx["file_hash"],))
    if same_file:
        add(Check("dup_file", "File not seen before", "fail",
                  f"This exact file was already processed as {same_file['invoice_number']} (status: {same_file['status']}).",
                  "block", "ap", {"match": same_file}))
        hits.append(same_file)
    else:
        add(Check("dup_file", "File not seen before", "pass", "No identical file in the AP ledger"))
    if not vendor:
        add(Check("dup_number", "Invoice number unique for vendor", "info", "Skipped — vendor unknown."))
        return "Duplicate check limited (unknown vendor)", {}
    norm = db.norm_invoice_number(ex.get("invoice_number"))
    prior = db.q("SELECT * FROM ap_ledger WHERE vendor_id=? AND status IN ('paid','approved','in_review')", (vendor["vendor_id"],))
    exact = [p for p in prior if norm and p["invoice_number_norm"] == norm]
    if exact:
        p = exact[0]
        same_amt = ex.get("total") and abs((p["gross_amount"] or 0) - ex["total"]) < 1
        when = f"paid on {p['paid_date']}" if p["status"] == "paid" and p["paid_date"] else f"status {p['status']}"
        label = "the same number" if p["invoice_number"] == ex.get("invoice_number") else f"the same number in a different format ('{p['invoice_number']}')"
        add(Check("dup_number", "Invoice number unique for vendor", "fail",
                  f"Invoice {ex.get('invoice_number')} is {label}, already booked on {p['invoice_date']} for {inr(p['gross_amount'])} ({when})."
                  + ("" if same_amt else f" Amount now differs: {inr(ex.get('total'))}."), "block", "ap", {"match": p}))
        hits.append(p)
    elif norm:
        add(Check("dup_number", "Invoice number unique for vendor", "pass", f"No prior invoice '{ex['invoice_number']}' from this vendor"))

    # near-duplicates: same amount close in time / same service period / very similar number
    if not exact and ex.get("total"):
        win = rules["duplicate_window_days"]
        for p in prior:
            if abs((p["gross_amount"] or 0) - ex["total"]) > max(1.0, ex["total"] * 0.005):
                continue
            reasons = []
            if ctx.get("invoice_date") and p["invoice_date"]:
                gap = abs((ctx["invoice_date"] - date.fromisoformat(p["invoice_date"])).days)
                if gap <= win:
                    reasons.append(f"dated {gap} day(s) apart")
            if ex.get("service_period") and p.get("service_period") and \
                    fuzz.token_set_ratio(ex["service_period"].lower(), p["service_period"].lower()) >= 90:
                reasons.append(f"same service period ({p['service_period']})")
            if norm and fuzz.ratio(norm, p["invoice_number_norm"] or "") >= 85:
                reasons.append("near-identical invoice number")
            if reasons:
                add(Check("dup_fuzzy", "No near-duplicate", "warn",
                          f"Same amount {inr(p['gross_amount'])} as {p['invoice_number']} ({', '.join(reasons)}) — possible duplicate.",
                          "review", "ap", {"match": p}))
                hits.append(p)
                break
        else:
            add(Check("dup_fuzzy", "No near-duplicate", "pass", f"Checked {len(prior)} prior invoice(s) from this vendor"))
    ctx["duplicates"] = hits
    return ("Possible duplicate payment detected" if hits else "No duplicates"), {
        "prior_invoices_checked": len(prior), "normalised_number": norm or None}


# -------------------------------------------------------------------------------------------------------- matching
def norm_po(s):
    if not s:
        return None
    m = re.search(r"(\d{4})\s*[-/]\s*(\d{3,5})", s)
    return f"PO-{m.group(1)}-{int(m.group(2)):04d}" if m else None


def unit_net(line, ex):
    p = line.get("unit_price")
    if p is None and line.get("amount") is not None and line.get("quantity"):
        p = line["amount"] / line["quantity"]
    if p is not None and ex.get("prices_include_tax"):
        rate = sum(t["rate"] or 0 for t in ex["tax_lines"]) or 18
        p = p / (1 + rate / 100)
    return round(p, 4) if p is not None else None


def match_lines(ex, po):
    """Greedy best-match of invoice lines to PO lines on description similarity, tie-broken by price closeness."""
    pairs, used = [], set()
    for i, li in enumerate(ex["line_items"]):
        best = None
        for pl in po["lines"]:
            if pl["line_no"] in used:
                continue
            s = fuzz.token_set_ratio(li["description"].lower(), pl["description"].lower())
            up = unit_net(li, ex)
            if up and pl["unit_price"]:
                s += max(0, 10 - abs(up - pl["unit_price"]) / pl["unit_price"] * 50)
            if not best or s > best[1]:
                best = (pl, s)
        if best and best[1] >= 60:
            used.add(best[0]["line_no"])
            pairs.append((li, best[0], best[1]))
        else:
            pairs.append((li, None, best[1] if best else 0))
    return pairs


def score_po(ex, po, net):
    pairs = match_lines(ex, po)
    if ex["line_items"]:
        line_score = sum(min(s, 100) for _, pl, s in pairs if pl) / (100 * len(pairs))
    else:
        line_score = 0.5
    rem = po["remaining_net"]
    amount_score = 1.0 if net <= rem + 1 else max(0.0, 1 - (net - rem) / max(net, 1))
    return round(0.7 * line_score + 0.3 * amount_score, 3), pairs


def stage_match(ctx):
    ex, rules, add = ctx["extracted"], ctx["rules"], ctx["checks"].append
    vendor = ctx.get("vendor")
    net = ctx["net"]
    ctx["po"] = None
    if not vendor:
        add(Check("po_found", "Purchase order identified", "info", "Skipped — vendor unknown."))
        return "Matching skipped", {}

    printed = ex.get("po_number")
    po_no = norm_po(printed)
    po, how, candidates = None, None, []
    if po_no:
        po = db.po_with_lines(po_no)
        if po and po["vendor_id"] != vendor["vendor_id"]:
            add(Check("po_vendor", "PO belongs to this vendor", "warn",
                      f"{po_no} was raised on {po['vendor_id']}, not {vendor['vendor_id']}.", "review", "buyer"))
        if po:
            how = "explicit" if printed.upper().startswith("PO") else f"explicit (printed as '{printed}', normalised)"
        else:
            add(Check("po_found", "Purchase order identified", "warn", f"PO reference '{printed}' does not exist in the procurement system — attempting to infer.",
                      "review", "buyer"))
    if not po:
        # infer from the vendor's open POs
        open_pos = [db.po_with_lines(p["po_number"]) for p in
                    db.q("SELECT po_number FROM purchase_orders WHERE vendor_id=? AND status='open'", (vendor["vendor_id"],))]
        for cand in open_pos:
            s, _ = score_po(ex, cand, net)
            candidates.append({"po_number": cand["po_number"], "description": cand["description"], "score": s,
                               "remaining": cand["remaining_net"]})
        candidates.sort(key=lambda c: -c["score"])
        top = candidates[0] if candidates else None
        second = candidates[1]["score"] if len(candidates) > 1 else 0
        if top and top["score"] >= rules["po_inference_min_confidence"] and top["score"] - second >= 0.15:
            po = db.po_with_lines(top["po_number"])
            how = f"inferred ({top['score'] * 100:.0f}% confidence)"
            ref = ex.get("other_references")
            add(Check("po_found", "Purchase order identified", "pass",
                      f"No PO number on invoice{f' (vendor wrote: “{ref}”)' if ref else ''}. Matched to {po['po_number']} — "
                      f"{po['description']} — by line items and amount; {len(candidates)} open PO(s) considered.",
                      data={"candidates": candidates}))
        else:
            detail = "No PO number on the invoice and no open PO for this vendor." if not candidates else \
                "No PO number on the invoice, and no open PO matches confidently: " + \
                ", ".join(f"{c['po_number']} ({c['score'] * 100:.0f}%)" for c in candidates[:3])
            add(Check("po_found", "Purchase order identified", "fail" if not candidates else "warn", detail,
                      "block" if not candidates else "review", "buyer", {"candidates": candidates}))
            return "No purchase order could be matched", {"candidates": candidates}
    elif how:
        add(Check("po_found", "Purchase order identified", "pass", f"{po['po_number']} — {po['description']} ({how})"))
    ctx["po"] = po

    if po["status"] != "open":
        add(Check("po_open", "PO is open", "warn", f"{po['po_number']} is {po['status']}.", "review", "buyer"))

    # ---- line-level match: price + quantity (+ goods receipt)
    pairs = match_lines(ex, po)
    line_results, allocations = [], []
    price_var_total, matched_po_amount = 0.0, 0.0
    for li, pl, s in pairs:
        r = {"invoice_line": li["description"], "qty": li["quantity"], "invoice_unit_net": unit_net(li, ex)}
        if not pl:
            r["status"] = "unmatched"
            add(Check("line_on_po", "Every line is on the PO", "warn",
                      f"'{li['description']}' ({inr(li['amount'])}) is not on {po['po_number']} — work or goods not ordered.",
                      "review", "buyer"))
            line_results.append(r)
            continue
        up = unit_net(li, ex) or 0
        qty = li["quantity"] or 0
        var = round((up - pl["unit_price"]) * qty, 2)
        price_var_total += var
        matched_po_amount += pl["unit_price"] * qty
        cum = pl["qty_billed"] + qty
        r.update({"po_line": pl["line_no"], "po_description": pl["description"], "po_unit_price": pl["unit_price"],
                  "price_variance": var, "price_variance_pct": round((up - pl["unit_price"]) / pl["unit_price"] * 100, 2),
                  "po_qty": pl["qty"], "qty_billed_before": pl["qty_billed"], "qty_received": pl["qty_received"],
                  "similarity": round(min(s, 100))})
        if cum > pl["qty"] + 1e-6:
            r["status"] = "over_qty"
            add(Check("qty_po", "Quantity within PO", "warn",
                      f"Line {pl['line_no']} ({pl['description']}): {pl['qty_billed']:g} already billed + {qty:g} now = {cum:g} > {pl['qty']:g} ordered.",
                      "review", "buyer"))
        if po["po_type"] == "goods" and rules["require_grn_for_goods"]:
            if pl["qty_received"] <= 0:
                r["status"] = "no_grn"
                add(Check("grn", "Goods received (3-way match)", "warn",
                          f"No goods receipt recorded for line {pl['line_no']} ({pl['description']}).", "review", "receiving"))
            elif cum > pl["qty_received"] + 1e-6:
                r["status"] = "short_receipt"
                short = cum - pl["qty_received"]
                add(Check("grn", "Goods received (3-way match)", "warn",
                          f"Billed {cum:g} {pl['uom']} of {pl['description']} but only {pl['qty_received']:g} received "
                          f"({pl['grn_numbers']}). {short:g} unit(s) — {inr(short * up)} — not yet delivered.",
                          "review", "receiving"))
        r.setdefault("status", "matched")
        allocations.append({"po_line_no": pl["line_no"], "qty": qty, "unit_net": up})
        line_results.append(r)

    # price tolerance (aggregate over matched lines)
    if matched_po_amount:
        pct_limit = matched_po_amount * rules["price_tolerance_pct"] / 100
        limit = min(pct_limit, rules["price_tolerance_abs"])
        pct = price_var_total / matched_po_amount * 100
        worst = max((r for r in line_results if r.get("po_line")), key=lambda r: abs(r["price_variance"]), default=None)
        if abs(price_var_total) < 0.5:
            add(Check("price", "Prices match PO", "pass", "All unit prices equal the PO price"))
        elif abs(price_var_total) <= limit:
            add(Check("price", "Prices match PO", "pass",
                      f"Price variance {inr(price_var_total)} ({pct:+.2f}%) is within tolerance (lesser of "
                      f"{rules['price_tolerance_pct']}% or {inr(rules['price_tolerance_abs'])} = {inr(limit)}). "
                      f"Largest: {worst['po_description']} at {inr(worst['invoice_unit_net'])} vs {inr(worst['po_unit_price'])}.",
                      data={"variance": price_var_total}))
        else:
            add(Check("price", "Prices match PO", "warn",
                      f"Price variance {inr(price_var_total)} ({pct:+.2f}%) exceeds tolerance of {inr(limit)}. "
                      f"{worst['po_description']}: invoiced {inr(worst['invoice_unit_net'])} vs PO {inr(worst['po_unit_price'])} "
                      f"({worst['price_variance_pct']:+.1f}%).", "review", "buyer", {"variance": price_var_total}))

    # quantity / receipt confirmations when clean
    if not any(c["id"] in ("qty_po",) for c in ctx["checks"]) and pairs:
        add(Check("qty_po", "Quantity within PO", "pass", "Cumulative billed quantities within ordered quantities"))
    if po["po_type"] == "goods" and not any(c["id"] == "grn" for c in ctx["checks"]):
        grns = sorted({l["grn_numbers"] for l in po["lines"] if l["grn_numbers"]})
        add(Check("grn", "Goods received (3-way match)", "pass", f"Billed quantities received in full ({', '.join(grns)})"))
    if po["po_type"] == "service":
        add(Check("grn", "Goods received (3-way match)", "info", "Service PO — 2-way match (no goods receipt required)"))

    # PO balance — handles vendors splitting one PO across several invoices
    prior_n = len(po["invoices"])
    after = po["billed_net"] + net
    # value consumed at PO prices: a price variance is judged by the price check, not double-counted as over-billing
    consumed = net - price_var_total if price_var_total > 0 else net
    if consumed > po["remaining_net"] + rules["po_balance_tolerance_abs"]:
        add(Check("po_balance", "Within remaining PO value", "warn",
                  f"{po['po_number']} is worth {inr(po['total_amount'])}; {prior_n} earlier invoice(s) already billed {inr(po['billed_net'])}, "
                  f"leaving {inr(po['remaining_net'])}. This invoice ({inr(net)} taxable) would over-bill the PO by {inr(consumed - po['remaining_net'])}.",
                  "review", "buyer", {"prior_invoices": po["invoices"]}))
    else:
        txt = f"{inr(net)} of {inr(po['remaining_net'])} remaining" + \
            (f" at PO prices (price variance {inr(price_var_total)} assessed separately)" if consumed != net else "")
        if prior_n:
            txt = f"Split billing: invoice {prior_n + 1} against this PO. " + txt + f" → PO {after / po['total_amount'] * 100:.0f}% billed after posting"
        add(Check("po_balance", "Within remaining PO value", "pass", txt))

    ctx["allocations"] = allocations
    ctx["price_variance"] = round(price_var_total, 2)
    po_summary = {k: po[k] for k in ("po_number", "description", "po_type", "requester", "department", "total_amount",
                                      "billed_net", "remaining_net", "status")}
    po_summary["prior_invoices"] = [{k: i[k] for k in ("invoice_number", "invoice_date", "net_amount", "status")} for i in po["invoices"]]
    issues = sum(1 for c in ctx["checks"] if c["id"] in ("line_on_po", "qty_po", "grn", "price", "po_balance", "po_vendor") and c["status"] in ("warn", "fail"))
    summary = f"Matched to {po['po_number']}, {how}" + (f" · {issues} exception(s)" if issues else " · all lines reconcile")
    return summary, {"po": po_summary, "match_method": how, "lines": line_results, "candidates": candidates}


# -------------------------------------------------------------------------------------------------------- decision
def stage_decide(ctx):
    rules, ex = ctx["rules"], ctx["extracted"]
    checks = ctx["checks"]
    if ex.get("total") and ex["total"] > rules["auto_approve_limit"] and not any(c["severity"] == "block" for c in checks):
        checks.append(Check("approval_limit", "Within auto-approval limit", "warn",
                            f"{inr(ex['total'])} exceeds the straight-through limit of {inr(rules['auto_approve_limit'])} — needs controller sign-off.",
                            "review", "controller"))
    blocks = [c for c in checks if c["severity"] == "block" and c["status"] == "fail"]
    reviews = [c for c in checks if c["severity"] == "review" and c["status"] in ("warn", "fail")]
    vendor, po = ctx.get("vendor"), ctx.get("po")
    who = (vendor or {}).get("name") or ex.get("vendor_name") or "Unknown supplier"
    inv = ex.get("invoice_number") or "(no number)"

    rank = lambda c: ISSUE_RANK.get(c["id"], 99)
    if blocks:
        decision = "REJECTED"
        blocks.sort(key=rank)
        headline = issue_title(blocks[0]) + (f" (+{len(blocks) - 1} more)" if len(blocks) > 1 else "")
        reasons = [c["detail"] for c in blocks]
        route = "ap"
        action = "Return to vendor with the reasons below; nothing is posted."
    elif reviews:
        decision = "NEEDS_REVIEW"
        owners = sorted({c["owner"] or "ap" for c in reviews}, key=OWNER_PRIORITY.index)
        route = owners[0]
        reviews.sort(key=lambda c: (OWNER_PRIORITY.index(c["owner"] or "ap"), rank(c)))
        headline = issue_title(reviews[0]) + (f" (+{len(reviews) - 1} more)" if len(reviews) > 1 else "")
        reasons = [c["detail"] for c in reviews]
        action = f"Held — routed to {OWNERS[route]}" + (f" (also needs: {', '.join(OWNERS[o] for o in owners[1:])})" if len(owners) > 1 else "") + "."
    else:
        decision = "APPROVED"
        route = None
        reasons = [c["detail"] for c in checks if c["status"] == "pass" and c["id"] in
                   ("po_found", "price", "grn", "po_balance", "bank_match", "dup_number", "sum_total")]
        headline = f"Matches {po['po_number']} — approved for payment"
        action = "Posted to AP ledger and scheduled for payment."

    terms = (vendor or {}).get("payment_terms_days") or 30
    due = None
    if ex.get("due_date"):
        due = ex["due_date"]
    elif ctx.get("invoice_date"):
        due = (ctx["invoice_date"] + timedelta(days=terms)).isoformat()
    exceptions = len(blocks) + len(reviews)
    ctx["decision"] = {
        "decision": decision, "headline": headline, "reasons": reasons, "route_to": route,
        "route_to_label": OWNERS.get(route) if route else None, "next_action": action, "due_date": due,
        "payment_terms_days": terms, "exceptions": exceptions,
        "checks_passed": sum(1 for c in checks if c["status"] == "pass"), "checks_total": len(checks),
        "fraud_risk": any(c.get("data", {}).get("fraud_risk") for c in checks),
        "issues": [{"title": issue_title(c), "detail": c["detail"], "owner": OWNERS.get(c["owner"] or "ap"),
                    "severity": c["severity"]} for c in (blocks or reviews)],
    }
    word = {"APPROVED": "Approved", "NEEDS_REVIEW": "Needs review", "REJECTED": "Rejected"}[decision]
    return f"{word} — {headline}", ctx["decision"]


def vendor_email(ctx, reasons, decision):
    ex, vendor = ctx["extracted"], ctx.get("vendor") or {}
    to = vendor.get("email") or ex.get("vendor_email") or "(vendor email unknown)"
    inv = ex.get("invoice_number") or "without an invoice number"
    dt = ex.get("invoice_date") or "undated"
    amt = inr(ex.get("total"))
    bullets = "\n".join(f"  • {r}" for r in reasons)
    if decision == "REJECTED":
        subject = f"Invoice {ex.get('invoice_number') or ''} returned — action needed".replace("  ", " ")
        body = (f"Dear {vendor.get('name') or ex.get('vendor_name') or 'Supplier'},\n\n"
                f"We have received your invoice {inv} dated {dt} for {amt}. We are unable to process it for the following reason(s):\n\n"
                f"{bullets}\n\nPlease review and, where applicable, send a corrected invoice referencing a valid Kestrel Foods purchase order "
                f"to ap@kestrelfoods.in. No payment will be made against this document.\n\nRegards,\nAccounts Payable, Kestrel Foods Pvt Ltd")
    else:
        subject = f"Invoice {ex.get('invoice_number')} on hold — clarification needed"
        body = (f"Dear {vendor.get('name') or 'Supplier'},\n\nYour invoice {inv} dated {dt} for {amt} is on hold pending internal review:\n\n"
                f"{bullets}\n\nWe will be in touch if anything further is needed from you.\n\nRegards,\nAccounts Payable, Kestrel Foods Pvt Ltd")
    return {"to": to, "subject": subject, "body": body}


def stage_post(ctx):
    d, ex = ctx["decision"], ctx["extracted"]
    vendor, po = ctx.get("vendor"), ctx.get("po")
    add = ctx["checks"].append
    actions = []
    status_map = {"APPROVED": "approved", "NEEDS_REVIEW": "in_review"}
    if d["decision"] in status_map and vendor:
        db.execute("""INSERT INTO ap_ledger (invoice_number, invoice_number_norm, vendor_id, invoice_date, po_number, net_amount,
                      tax_amount, gross_amount, status, service_period, source, run_id, file_hash, lines_json)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (ex.get("invoice_number"), db.norm_invoice_number(ex.get("invoice_number")), vendor["vendor_id"],
                    ex.get("invoice_date"), (po or {}).get("po_number"), ctx["net"], ex.get("tax_total") or 0, ex.get("total"),
                    status_map[d["decision"]], ex.get("service_period"), "run", ctx["run_id"], ctx["file_hash"],
                    json.dumps(ctx.get("allocations", []))))
    if d["decision"] == "APPROVED":
        actions.append({"type": "ledger", "text": f"Posted to AP ledger against {po['po_number']} — taxable {inr(ctx['net'])}, "
                                                  f"GST {inr(ex.get('tax_total'))}"})
        actions.append({"type": "payment", "text": f"Scheduled in payment run on {d['due_date']} (Net {d['payment_terms_days']}) to verified "
                                                   f"account {vendor['bank_name']} ••••{vendor['bank_account'][-4:]}"})
        p2 = db.po_with_lines(po["po_number"])
        actions.append({"type": "po", "text": f"{po['po_number']} now {p2['billed_net'] / p2['total_amount'] * 100:.0f}% billed "
                                              f"({inr(p2['remaining_net'])} remaining)"})
        add(Check("posted", "Posted", "pass", "Ledger updated"))
    elif d["decision"] == "NEEDS_REVIEW":
        actions.append({"type": "task", "text": f"Review task created for {d['route_to_label']}: {d['headline']}"})
        if ctx.get("bank_flag"):
            actions.append({"type": "security", "text": "Payment blocked to the new account. Treasury to call vendor on the master-record phone number "
                                                        "before any bank change is accepted."})
        actions.append({"type": "ledger", "text": "Recorded in AP ledger as 'in review' — protects against re-submission, does not consume PO balance"})
        add(Check("posted", "Posted", "info", "Held for review"))
    else:
        actions.append({"type": "email", "text": "Rejection notice drafted to vendor"})
        add(Check("posted", "Posted", "info", "Not posted"))
    email = None
    if d["decision"] == "REJECTED":
        email = vendor_email(ctx, d["reasons"], "REJECTED")
    elif d["decision"] == "NEEDS_REVIEW" and any(c["owner"] in ("buyer", "receiving") for c in ctx["checks"]
                                                  if c["severity"] == "review" and c["status"] in ("warn", "fail")):
        email = vendor_email(ctx, [c["detail"] for c in ctx["checks"] if c["severity"] == "review" and c["status"] in ("warn", "fail")
                                   and c["owner"] in ("buyer", "receiving")][:3], "NEEDS_REVIEW")
    ctx["actions"], ctx["email"] = actions, email
    return {"APPROVED": "Posted and scheduled for payment", "NEEDS_REVIEW": "Held and routed for review",
            "REJECTED": "Rejected — vendor notice drafted"}[d["decision"]], {"actions": actions, "email": email}


def build_output(ctx):
    d, ex = ctx["decision"], ctx["extracted"]
    return {
        "run_id": ctx["run_id"],
        "decision": d["decision"],
        "headline": d["headline"],
        "reasons": d["reasons"],
        "route_to": d.get("route_to_label"),
        "next_action": d["next_action"],
        "fraud_risk": d["fraud_risk"],
        "invoice": {k: ex.get(k) for k in ("vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_number",
                                           "subtotal", "tax_total", "total", "currency")},
        "taxable_value": ctx.get("net"),
        "vendor_id": (ctx.get("vendor") or {}).get("vendor_id"),
        "matched_po": (ctx.get("po") or {}).get("po_number"),
        "price_variance": ctx.get("price_variance"),
        "payment_due": d.get("due_date"),
        "engine": ctx.get("engine_label"),
        "checks": [{k: c[k] for k in ("id", "label", "status", "severity", "owner", "detail")} for c in ctx["checks"]],
        "actions": ctx.get("actions", []),
        "vendor_email": ctx.get("email"),
        "processed_at": events.now_iso(),
    }


# ======================================================================================================== review
def review(run_id, action, note, reviewer="AP reviewer"):
    run = db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run or run["decision"] != "NEEDS_REVIEW" or run["review_status"] != "pending":
        raise ValueError("Run is not awaiting review")
    out = json.loads(run["result_json"])
    if action == "approve":
        db.execute("UPDATE ap_ledger SET status='approved' WHERE run_id=?", (run_id,))
        final = "APPROVED"
        msg = f"Approved by {reviewer}" + (f": {note}" if note else "")
    elif action == "reject":
        db.execute("UPDATE ap_ledger SET status='rejected' WHERE run_id=?", (run_id,))
        final = "REJECTED"
        msg = f"Rejected by {reviewer}" + (f": {note}" if note else "")
    else:
        raise ValueError("action must be approve or reject")
    out.setdefault("review", {})
    out["review"] = {"action": action, "note": note, "reviewer": reviewer, "at": events.now_iso(), "final_decision": final}
    db.execute("UPDATE runs SET review_status=?, final_decision=?, reviewer_note=?, result_json=? WHERE id=?",
               (action + "d", final, note, json.dumps(out, default=str), run_id))
    events.append_persisted(run_id, "review", "pass" if action == "approve" else "fail", msg,
                            {"note": note, "final_decision": final, "reviewer": reviewer})
    return final
