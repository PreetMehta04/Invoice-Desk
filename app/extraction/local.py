"""Document reading (text layer or OCR) and a layout-aware, rule-based invoice parser.

Used (a) as the extraction engine when no LLM API key is configured, and (b) to produce source text for the
grounding check that verifies LLM-extracted values actually appear on the document."""
import re
from dataclasses import dataclass
from datetime import date, datetime

import pymupdf

from .. import gstin as gst


@dataclass
class Seg:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def yc(self):
        return (self.y0 + self.y1) / 2


# ------------------------------------------------------------------------------------------- reading
def read_document(pdf_bytes: bytes) -> dict:
    """Return page segments + plain text. Uses the text layer when present, otherwise OCR (Tesseract)."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages = len(doc)
    text_chars = sum(len(p.get_text().strip()) for p in doc)
    segs: list[Seg] = []
    method = "text-layer"
    ocr_conf = None
    if text_chars >= 40 * pages:
        yoff = 0.0
        for p in doc:
            for b in p.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    for s in line["spans"]:
                        t = s["text"].strip()
                        if t:
                            x0, y0, x1, y1 = s["bbox"]
                            segs.append(Seg(t, x0, y0 + yoff, x1, y1 + yoff))
            yoff += p.rect.height
    rows = None
    if text_chars < 40 * pages:
        method = "ocr"
        try:
            rows, ocr_conf = _ocr(doc)
        except Exception as e:  # Tesseract not installed / failed — an LLM can still read the image
            method, rows, ocr_conf = "ocr-unavailable", [], None
            print("OCR unavailable:", e)
        segs = [x for r in rows for x in r]
    rows = rows or group_rows(segs)
    text = "\n".join(" ".join(s.text for s in r) for r in rows)
    return {"pages": pages, "method": method, "scanned": text_chars < 40 * pages, "text_chars": text_chars, "segments": segs, "rows": rows,
            "text": text, "ocr_confidence": ocr_conf}


def _ocr(doc):
    import pytesseract
    from PIL import Image
    rows, confs, yoff = [], [], 0.0
    for p in doc:
        pix = p.get_pixmap(dpi=300)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        scale = p.rect.width / pix.width
        d = pytesseract.image_to_data(img, config="--psm 6", output_type=pytesseract.Output.DICT)
        words = {}
        for i, w in enumerate(d["text"]):
            w = w.strip()
            if not w or float(d["conf"][i]) < 0:
                continue
            confs.append(float(d["conf"][i]))
            key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            words.setdefault(key, []).append((d["left"][i] * scale, d["top"][i] * scale + yoff,
                                              (d["left"][i] + d["width"][i]) * scale,
                                              (d["top"][i] + d["height"][i]) * scale + yoff, w))
        for key in sorted(words, key=lambda k: min(w[1] for w in words[k])):
            ws = sorted(words[key])
            height = sorted(w[3] - w[1] for w in ws)[len(ws) // 2]
            row, cur = [], [ws[0]]
            for w in ws[1:]:
                if w[0] - cur[-1][2] > max(6.0, 1.3 * height):  # wide gap -> new column/segment
                    row.append(_merge(cur))
                    cur = [w]
                else:
                    cur.append(w)
            row.append(_merge(cur))
            rows.append(row)
        yoff += p.rect.height
    return rows, round(sum(confs) / len(confs), 1) if confs else None


def _merge(ws):
    return Seg(" ".join(w[4] for w in ws), min(w[0] for w in ws), min(w[1] for w in ws),
               max(w[2] for w in ws), max(w[3] for w in ws))


def group_rows(segs, tol=3.5):
    rows = []
    for s in sorted(segs, key=lambda s: (s.yc, s.x0)):
        if rows and abs(rows[-1][0].yc - s.yc) <= tol:
            rows[-1].append(s)
        else:
            rows.append([s])
    return [sorted(r, key=lambda s: s.x0) for r in rows]


# ------------------------------------------------------------------------------------------- helpers
NUM_RE = re.compile(r"^-?(?:Rs\.?|INR|₹)?\s*\d[\d,]*(?:\.\d+)?$")
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def clean_ocr_numbers(t: str) -> str:
    return re.sub(r"(\d)[,.]\s+(\d{2,3}[,.])", r"\1,\2", t)


def parse_amount(t) -> float | None:
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return float(t)
    t = clean_ocr_numbers(str(t))
    t = re.sub(r"(Rs\.?|INR|₹|/-)", "", t, flags=re.I).strip()
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", t)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def numbers_in(text: str) -> list[float]:
    text = clean_ocr_numbers(text)
    out = []
    for tok in re.findall(r"(?<![\w/-])\d[\d,]*(?:\.\d+)?(?![\w/%-])", text):
        try:
            out.append(float(tok.replace(",", "")))
        except ValueError:
            pass
    return out


def parse_date(t: str | None) -> str | None:
    if not t:
        return None
    t = t.strip().replace(",", " ")
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", t)
        if m:  # Indian convention: day first
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(y, int(m[2]), int(m[1])).isoformat()
        m = re.search(r"\b(\d{1,2})[\s-]+([A-Za-z]{3,9})[\s-]+(\d{4})\b", t)
        if m and m[2][:3].lower() in MONTHS:
            return date(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1])).isoformat()
        m = re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2})\s+(\d{4})\b", t)
        if m and m[1][:3].lower() in MONTHS:
            return date(int(m[3]), MONTHS[m[1][:3].lower()], int(m[2])).isoformat()
    except ValueError:
        return None
    return None


def repair_gstin(raw: str) -> str:
    """Fix the commonest OCR confusions using GSTIN structure (position 14 is always 'Z')."""
    g = re.sub(r"[^0-9A-Z]", "", raw.upper())
    if len(g) != 15:
        return g
    g = list(g)
    if g[13] in "27":
        g[13] = "Z"
    digit_fix = {"O": "0", "I": "1", "S": "5", "B": "8"}
    for i in (0, 1, 7, 8, 9, 10):
        g[i] = digit_fix.get(g[i], g[i])
    g = "".join(g)
    if not gst.validate(g)[0]:
        # try swapping a confusable checksum char
        for a, b in (("Z", "2"), ("2", "Z"), ("O", "0"), ("0", "O"), ("I", "1"), ("1", "I")):
            if g[14] == a and gst.validate(g[:14] + b)[0]:
                return g[:14] + b
    return g


# ------------------------------------------------------------------------------------------- label lookup
class Layout:
    def __init__(self, reading):
        self.rows = reading["rows"]
        self.segs = [s for r in self.rows for s in r]
        self.text = reading["text"]

    def value_for(self, label_re: str, value_re: str | None = None, valid=None) -> str | None:
        """Find a label and return its value: same segment after ':', next segment on the row, or the segment below."""
        lab = re.compile(label_re, re.I)
        vre = re.compile(value_re, re.I) if value_re else None
        for ri, row in enumerate(self.rows):
            for si, s in enumerate(row):
                m = lab.search(s.text)
                if not m:
                    continue
                candidates = []
                rest = s.text[m.end():].strip(" :.#-–\t")
                if rest:
                    candidates.append(rest)
                if si + 1 < len(row) and row[si + 1].x0 - s.x1 < 120:
                    candidates.append(row[si + 1].text.strip(" :"))
                below = [b for r in self.rows[ri + 1:ri + 3] for b in r
                         if b.x0 < s.x1 + 2 and b.x1 > s.x0 - 2 and 0 < b.yc - s.yc < 22]
                if below:
                    candidates.append(min(below, key=lambda b: b.yc).text.strip(" :"))
                for c in candidates:
                    v = (vre.search(c).group(0) if vre.search(c) else None) if vre else c
                    if v and (valid is None or valid(v)):
                        return v
        return None

    def row_amount(self, label_re: str, prefer_last=True) -> float | None:
        lab = re.compile(label_re, re.I)
        for row in self.rows:
            line = " ".join(s.text for s in row)
            if lab.search(line):
                nums = [n for n in numbers_in(line.split(lab.search(line).group(0), 1)[-1])]
                if nums:
                    return nums[-1] if prefer_last else nums[0]
        return None


# ------------------------------------------------------------------------------------------- parser
SUFFIX_RE = re.compile(r"\b(Pvt\.?\s*Ltd|Private Limited|Limited|LLP|Industries|Services|House|Co\.|Company|Trading|Systems)\b", re.I)
TOTAL_WORDS = re.compile(r"(sub\s*total|total|taxable|cgst|sgst|igst|amount in words|round)", re.I)


def parse_invoice(reading: dict, buyer_gstin: str = "") -> dict:
    L = Layout(reading)
    text = clean_ocr_numbers(L.text)
    up = text.upper()

    # document type
    if "PROFORMA" in up:
        dtype = "proforma_invoice"
    elif "CREDIT NOTE" in up:
        dtype = "credit_note"
    elif "QUOTATION" in up and "INVOICE" not in up:
        dtype = "quotation"
    elif "INVOICE" in up or "BILL NO" in up:
        dtype = "tax_invoice"
    else:
        dtype = "other"

    # GSTINs
    gstins = []
    for m in re.finditer(r"\b[0-9OIS]{2}[A-Z]{5}[0-9OISB]{4}[A-Z][0-9A-Z][Z27][0-9A-Z]\b", up):
        g = repair_gstin(m.group(0))
        if g not in gstins:
            gstins.append(g)
    vendor_gstin = next((g for g in gstins if g != buyer_gstin), None)
    buyer = buyer_gstin if buyer_gstin in gstins else None

    # vendor name — signature block "For X" first, else first company-like line that isn't the buyer
    vendor_name = None
    for row in L.rows:
        line = " ".join(s.text for s in row)
        m = re.match(r"^\s*for\s+(.+)$", line, re.I)
        if m and SUFFIX_RE.search(m.group(1)) and "kestrel" not in line.lower():
            vendor_name = m.group(1).strip()
            break
    if not vendor_name:
        for s in L.segs:
            if SUFFIX_RE.search(s.text) and "kestrel" not in s.text.lower() and len(s.text) < 60 \
                    and not re.search(r"(invoice|tax|services for)", s.text, re.I):
                vendor_name = re.sub(r"\s{2,}.*$", "", s.text).strip()
                break
    if vendor_name and vendor_name.isupper():
        vendor_name = vendor_name.title().replace("Pvt Ltd", "Pvt Ltd")

    inv_no = L.value_for(r"(invoice\s*(no\.?|number|#)|bill\s*no\.?|inv\.?\s*no\.?)", r"[A-Z0-9][A-Z0-9/\-_]{2,}",
                         valid=lambda v: bool(re.search(r"\d", v)))
    if inv_no and not re.search(r"\d", inv_no):
        inv_no = None
    raw_date = L.value_for(r"(invoice\s*date|dated|issued|^date)", valid=lambda v: parse_date(v) is not None)
    inv_date = parse_date(raw_date) if raw_date else None
    if not inv_date:
        m = re.search(r"date\s*[:.]?\s*([^\n]{6,22})", text, re.I)
        inv_date = parse_date(m.group(1)) if m else None

    po_raw = L.value_for(r"(p\.?o\.?\s*(number|no\.?|#)|customer\s*po|buyer'?s\s*order\s*no|work\s*order\s*/\s*po|order\s*ref|purchase\s*order)",
                         valid=lambda v: not re.match(r"(service|issued|invoice|date|customer)", v, re.I))
    po_number, other_ref = None, None
    if po_raw:
        m = re.search(r"(PO[-\s]?)?\d{4}[-/]\d{3,5}", po_raw, re.I)
        if m:
            po_number = m.group(0)
        elif not re.search(r"^(n/?a|—|-)$", po_raw.strip(), re.I):
            other_ref = po_raw
    if not po_number:
        m = re.search(r"\bPO[-\s]?\d{4}[-/]\d{3,5}\b", text, re.I)
        po_number = m.group(0) if m else None

    period = L.value_for(r"(service\s*period|billing\s*period)", valid=lambda v: v.strip() not in ("—", "-", "N/A"))

    # line items: rows where qty x rate ≈ amount
    lines = []
    prev_text_row = None
    for row in L.rows:
        line = clean_ocr_numbers(" ".join(s.text for s in row))
        if TOTAL_WORDS.search(line) and not re.search(r"[a-z]{4,}.*\d+\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2}", line, re.I):
            prev_text_row = None
            continue
        nums = numbers_in(line)
        item = None
        if len(nums) >= 3:
            for k in range(len(nums) - 1, 1, -1):
                amt, rate, qty = nums[k], nums[k - 1], nums[k - 2]
                if qty > 0 and rate > 0 and abs(qty * rate - amt) <= max(1.0, amt * 0.001):
                    item = (qty, rate, amt)
                    break
        if not item:
            if not nums and not re.search(r"(description|particulars|item|hsn|qty|amount)", line, re.I):
                prev_text_row = (row, line)
            else:
                prev_text_row = None
            continue
        qty, rate, amt = item
        # description = text segments left of the numbers (drop index numbers / HSN codes)
        desc_parts = []
        for s in row:
            t = s.text.strip()
            if NUM_RE.match(t) or re.match(r"^\d+(\.\d+)?\s*[A-Za-z]{1,5}$", t):
                continue
            desc_parts.append(t)
        desc = " ".join(desc_parts)
        desc = re.sub(r"^\d{1,2}[.)]?\s+", "", desc)
        desc = re.sub(r"\s+\d{4,8}\s*$", "", desc)  # trailing HSN in OCR rows
        desc = re.sub(r"\s+\d[\d,]*(\.\d+)?(\s+[A-Za-z]{2,5})?(\s+\d[\d,.]*)*$", "", desc).strip()
        if prev_text_row and (len(desc) < 18 or desc.startswith("(")) and row[0].yc - prev_text_row[0][0].yc < 16:
            desc = prev_text_row[1] + " " + desc
        hsn = next((x.text for x in row if re.fullmatch(r"\d{4,8}", x.text.strip())
                    and float(x.text) not in (qty, rate, amt)), None)
        unit = None
        for x in row:
            m = re.fullmatch(r"(?:\d+(?:\.\d+)?\s*)?(Nos|EA|Roll|Box|Trip|Month|Pcs|Units?)", x.text.strip(), re.I)
            if m:
                unit = m.group(1)
        desc = re.sub(r"\s+(Nos|EA|Roll|Box|Pcs)$", "", desc, flags=re.I)
        lines.append({"description": desc.strip(" -"), "hsn_sac": hsn, "quantity": qty, "unit": unit,
                      "unit_price": rate, "amount": amt})
        prev_text_row = None

    inclusive = bool(re.search(r"inclusive of (gst|tax)|incl\.?\s*gst|prices are inclusive", text, re.I))

    # totals and taxes
    subtotal = L.row_amount(r"(sub\s*total|total\s*taxable\s*value|taxable\s*value)")
    tax_lines = []
    if inclusive:
        for t in ("CGST", "SGST", "IGST"):
            m = re.search(rf"{t}\s*@?\s*(\d+(?:\.\d+)?)\s*%\s*(?:Rs\.?|INR)?\s*([\d,]+\.\d{{2}})", text, re.I)
            if m:
                tax_lines.append({"type": t, "rate": float(m[1]), "amount": float(m[2].replace(",", ""))})
        m = re.search(r"taxable value\s*[:.]?\s*(?:Rs\.?|INR)?\s*([\d,]+\.\d{2})", text, re.I)
        subtotal = float(m[1].replace(",", "")) if m else subtotal
    else:
        for row in L.rows:
            line = clean_ocr_numbers(" ".join(s.text for s in row))
            m = re.search(r"\b(CGST|SGST|IGST|UTGST)\b\s*@?\s*(\d+(?:\.\d+)?)\s*%", line, re.I)
            if m:
                nums = numbers_in(line[m.end():])
                if nums:
                    tax_lines.append({"type": m[1].upper(), "rate": float(m[2]), "amount": nums[-1]})
    total = L.row_amount(r"(grand\s*total|total\s*due|total\s*payable|invoice\s*total|total\s*\(inclusive[^)]*\)|amount\s*payable)")
    if total is None:
        for row in L.rows:
            line = " ".join(s.text for s in row)
            if re.match(r"^\s*total\b", line, re.I):
                nums = numbers_in(line)
                if nums:
                    total = nums[-1]

    # bank details
    acct = None
    m = re.search(r"(?:a/?c\.?|account)\s*(?:no\.?|number)?\s*[:.]?\s*(\d[\d\s]{8,20}\d)", text, re.I)
    if m:
        acct = re.sub(r"\s", "", m.group(1))
    ifsc = None
    m = re.search(r"\b([A-Z]{4}0[A-Z0-9]{6})\b", up)
    if m:
        ifsc = m.group(1)
    bank_name = None
    m = re.search(r"(?:bank(?:\s*name)?\s*[:.]|remit to[:.]?|kindly remit to)\s*([A-Za-z][A-Za-z .&]*?bank(?:\s*of\s*india)?(?:,\s*[A-Za-z .]+?)?)(?=\s*(?:,\s*account|a/c|·|\||account|\s+for\s|$|\n))", text, re.I | re.M)
    if m:
        bank_name = m.group(1).strip(" ,.")

    notes = []
    for row in L.rows:
        line = " ".join(s.text for s in row)
        if re.search(r"(changed our bank|new account|reminder|overdue|proforma invoice for|not a tax invoice|additional .* carried out|as requested)", line, re.I):
            notes.append(line.strip())

    email = None
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    if m:
        email = m.group(0)

    return {
        "document_type": dtype, "vendor_name": vendor_name, "vendor_gstin": vendor_gstin, "vendor_email": email,
        "vendor_address": None, "buyer_name": "Kestrel Foods Pvt Ltd" if "KESTREL" in up else None, "buyer_gstin": buyer,
        "invoice_number": inv_no, "invoice_date": inv_date, "due_date": None, "payment_terms": None,
        "po_number": po_number, "other_references": other_ref, "service_period": period,
        "currency": "INR" if re.search(r"(INR|Rs\.|Rupees|₹)", text) else None,
        "prices_include_tax": inclusive, "line_items": lines, "subtotal": subtotal, "tax_lines": tax_lines,
        "tax_total": round(sum(t["amount"] or 0 for t in tax_lines), 2) if tax_lines else None, "total": total,
        "bank_name": bank_name, "bank_account_number": acct, "bank_ifsc": ifsc, "notes": notes,
    }


def normalize(raw: dict) -> dict:
    """Coerce any engine's output into the canonical shape with typed values."""
    out = dict(raw or {})
    for k in ("subtotal", "tax_total", "total"):
        out[k] = parse_amount(out.get(k))
    for k in ("invoice_date", "due_date"):
        v = out.get(k)
        if v and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(v)):
            out[k] = parse_date(str(v))
    items = []
    for li in out.get("line_items") or []:
        items.append({"description": (li.get("description") or "").strip(), "hsn_sac": li.get("hsn_sac"),
                      "quantity": parse_amount(li.get("quantity")), "unit": li.get("unit"),
                      "unit_price": parse_amount(li.get("unit_price")), "amount": parse_amount(li.get("amount"))})
    out["line_items"] = items
    out["tax_lines"] = [{"type": (t.get("type") or "").upper(), "rate": parse_amount(t.get("rate")),
                         "amount": parse_amount(t.get("amount"))} for t in (out.get("tax_lines") or [])]
    if out.get("tax_total") is None and out["tax_lines"]:
        out["tax_total"] = round(sum(t["amount"] or 0 for t in out["tax_lines"]), 2)
    if out.get("vendor_gstin"):
        out["vendor_gstin"] = re.sub(r"\s", "", str(out["vendor_gstin"]).upper())
    if out.get("bank_account_number"):
        out["bank_account_number"] = re.sub(r"\D", "", str(out["bank_account_number"]))
    if out.get("bank_ifsc"):
        out["bank_ifsc"] = str(out["bank_ifsc"]).upper().replace(" ", "")
    out["prices_include_tax"] = bool(out.get("prices_include_tax"))
    out["notes"] = [n for n in (out.get("notes") or []) if n]
    out.setdefault("document_type", "tax_invoice")
    for k in ("invoice_number", "po_number", "vendor_name"):
        if isinstance(out.get(k), str):
            out[k] = out[k].strip() or None
    return out
