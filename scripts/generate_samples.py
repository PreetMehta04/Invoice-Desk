"""Generate realistic, deliberately messy vendor invoice PDFs for the demo.

Five vendor layouts (different labels, number formats, tax presentation, PO reference styles),
one scanned image-only PDF, tax-inclusive pricing, split billing, a duplicate re-send, a bank change, etc.

Run:  python scripts/generate_samples.py
"""
import io
import json
import random
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter, ImageEnhance
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle, Paragraph
from reportlab.lib.styles import ParagraphStyle

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"
OUT.mkdir(exist_ok=True)
W, H = A4

BUYER = ["Kestrel Foods Pvt Ltd", "No. 14, 3rd Cross, Peenya Industrial Area", "Bengaluru, Karnataka 560058",
         "GSTIN: 29AAKCK4821F1ZF"]

VENDORS = {
    "apex": dict(name="Apex Office Solutions Pvt Ltd", addr=["42, Residency Road", "Bengaluru, Karnataka 560025"],
                 gstin="29AAECA4521K1ZN", email="billing@apexoffice.in", phone="+91 80 4112 7788",
                 bank=("HDFC Bank, MG Road Branch", "50200012345678", "HDFC0001234")),
    "brightline": dict(name="Brightline Logistics LLP", addr=["Plot 7, MIDC Turbhe", "Navi Mumbai, Maharashtra 400705"],
                       gstin="27AAJFB7788M1ZK", email="accounts@brightline.co.in", phone="+91 22 2768 4410",
                       bank=("Axis Bank, Vashi", "917020045566771", "UTIB0000412")),
    "coastal": dict(name="Coastal Packaging Industries", addr=["SIDCO Industrial Estate, Ambattur", "Chennai, Tamil Nadu 600098"],
                    gstin="33AABCC9012P1ZL", email="ar@coastalpack.in", phone="+91 44 2625 1180",
                    bank=("Indian Bank, Ambattur", "6754321098", "IDIB000C051")),
    "delta": dict(name="Delta Tech Systems Pvt Ltd", addr=["3rd Floor, Prestige Atrium, Koramangala", "Bengaluru, Karnataka 560034"],
                  gstin="29AADCD3344Q1Z6", email="finance@deltatech.in", phone="+91 80 4920 3300",
                  bank=("ICICI Bank, Koramangala", "000405112233", "ICIC0000004")),
    "evergreen": dict(name="Evergreen Facility Services", addr=["88, 1st Main, HSR Layout Sector 2", "Bengaluru, Karnataka 560102"],
                      gstin="29AAGFE5566R1ZI", email="billing@evergreenfs.in", phone="+91 80 4567 2210",
                      bank=("State Bank of India, HSR Layout", "38812345670", "SBIN0040123")),
    "fusion": dict(name="Fusion Print House", addr=["12, Industrial Suburb, Yeshwanthpur", "Bengaluru, Karnataka 560022"],
                   gstin="29AAHFF7788S1Z2", email="hello@fusionprint.in", phone="+91 80 2337 9001",
                   bank=("Canara Bank, Yeshwanthpur", "0412101009988", "CNRB0000412")),
    "quickparts": dict(name="QuickParts Trading Co.", addr=["5-9-22, Abids Road", "Hyderabad, Telangana 500001"],
                       gstin="36AAQFQ1209T1ZH", email="sales@quickparts.co", phone="+91 40 2475 6612",
                       bank=("Kotak Mahindra Bank, Abids", "7712004589", "KKBK0007481")),
}


# ---------------------------------------------------------------------------------------------------------------
def inr(x, style="indian"):
    """Indian (1,23,456.00) or western (123,456.00) grouping — vendors differ."""
    neg = x < 0
    x = abs(x)
    whole, frac = f"{x:.2f}".split(".")
    if style == "western":
        s = f"{int(whole):,}"
    else:
        if len(whole) > 3:
            head, tail = whole[:-3], whole[-3:]
            groups = []
            while len(head) > 2:
                groups.insert(0, head[-2:])
                head = head[:-2]
            if head:
                groups.insert(0, head)
            s = ",".join(groups) + "," + tail
        else:
            s = whole
    return ("-" if neg else "") + s + "." + frac


ONES = "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen Sixteen " \
       "Seventeen Eighteen Nineteen".split()
TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _words(n):
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " Hundred" + ("" if n % 100 == 0 else " " + _words(n % 100))
    for div, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        if n >= div:
            return _words(n // div) + f" {name}" + ("" if n % div == 0 else " " + _words(n % div))


def amount_words(x):
    rupees = int(round(x))
    return f"Rupees {_words(rupees)} Only"


def compute(inv):
    lines = inv["lines"]
    rate = inv.get("tax_rate", 18)
    for l in lines:
        l["amount"] = round(l["qty"] * l["rate"], 2)
    total_lines = round(sum(l["amount"] for l in lines), 2)
    if inv.get("tax_inclusive"):
        gross = total_lines
        net = round(gross / (1 + rate / 100), 2)
        tax = round(gross - net, 2)
    else:
        net = total_lines
        tax = round(net * rate / 100, 2)
        gross = round(net + tax, 2)
    inv["net"], inv["tax"], inv["gross"] = net, tax, gross
    inv["printed_total"] = inv.get("override_total", gross)
    return inv


def tax_rows(inv, style):
    rate = inv.get("tax_rate", 18)
    if inv["tax_mode"] == "igst":
        return [(f"IGST @ {rate}%", inr(inv["tax"], style))]
    half = round(inv["tax"] / 2, 2)
    return [(f"CGST @ {rate/2:g}%", inr(half, style)), (f"SGST @ {rate/2:g}%", inr(inv["tax"] - half, style))]


def para(text, size=8.5, bold=False, color=colors.black, leading=None):
    st = ParagraphStyle("p", fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size,
                        leading=leading or size * 1.25, textColor=color)
    return Paragraph(text, st)


# ---------------------------------------------------------------------------------------------------------------
# Layout A — classic "TAX INVOICE" with coloured header band and full grid (Apex, Fusion)
def layout_a(c, inv, v, accent=colors.HexColor("#1F4E79")):
    style = "indian"
    c.setFillColor(accent)
    c.rect(0, H - 34 * mm, W, 34 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(18 * mm, H - 16 * mm, v["name"])
    c.setFont("Helvetica", 8.5)
    c.drawString(18 * mm, H - 22 * mm, ", ".join(v["addr"]))
    c.drawString(18 * mm, H - 27 * mm, f"GSTIN: {v['gstin']}   |   {v['email']}   |   {v['phone']}")
    c.setFont("Helvetica-Bold", 15)
    c.drawRightString(W - 18 * mm, H - 16 * mm, inv.get("doc_title", "TAX INVOICE"))

    y = H - 46 * mm
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18 * mm, y, "Bill To")
    c.setFont("Helvetica", 9)
    for i, line in enumerate(BUYER):
        c.drawString(18 * mm, y - (i + 1) * 4.6 * mm, line)
    meta = []
    if inv.get("number"):
        meta.append(("Invoice No.", inv["number"]))
    meta.append(("Invoice Date", inv["date_str"]))
    if inv.get("po_ref"):
        meta.append(("PO Number", inv["po_ref"]))
    meta.append(("Payment Terms", inv.get("terms", "Net 30 days")))
    for i, (k, val) in enumerate(meta):
        c.setFont("Helvetica-Bold", 9)
        c.drawString(120 * mm, y - i * 5 * mm, k)
        c.setFont("Helvetica", 9)
        c.drawString(152 * mm, y - i * 5 * mm, f": {val}")

    data = [["#", "Description", "HSN/SAC", "Qty", "Rate (Rs.)", "Amount (Rs.)"]]
    for i, l in enumerate(inv["lines"], 1):
        data.append([str(i), para(l["desc"]), l.get("hsn", ""), f"{l['qty']:g} {l.get('uom', '')}".strip(),
                     inr(l["rate"], style), inr(l["amount"], style)])
    t = Table(data, colWidths=[8 * mm, 78 * mm, 20 * mm, 20 * mm, 24 * mm, 26 * mm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#8C9BAB")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5), ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    tw, th = t.wrapOn(c, W, H)
    ty = H - 78 * mm - th
    t.drawOn(c, 18 * mm, ty)

    rows = [("Sub Total", inr(inv["net"], style))] + tax_rows(inv, style) + [("Grand Total (Rs.)", inr(inv["printed_total"], style))]
    tt = Table(rows, colWidths=[44 * mm, 26 * mm])
    tt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#8C9BAB")),
                            ("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 9.5),
                            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E8EEF5")), ("ALIGN", (1, 0), (1, -1), "RIGHT")]))
    _, tth = tt.wrapOn(c, W, H)
    tt.drawOn(c, W - 18 * mm - 70 * mm, ty - 4 * mm - tth)
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(18 * mm, ty - tth - 11 * mm, f"Amount in words: {amount_words(inv['printed_total'])}")

    by = ty - tth - 22 * mm
    for note in inv.get("notes", []):
        c.setFillColor(colors.HexColor("#B00020"))
        c.setFont("Helvetica-Bold", 9)
        c.drawString(18 * mm, by, note)
        by -= 5 * mm
        c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18 * mm, by - 3 * mm, "Bank details for payment")
    c.setFont("Helvetica", 9)
    bn, ba, bi = inv.get("bank_override", v["bank"])
    c.drawString(18 * mm, by - 8 * mm, f"Bank: {bn}")
    c.drawString(18 * mm, by - 12.5 * mm, f"Account No: {ba}")
    c.drawString(18 * mm, by - 17 * mm, f"IFSC: {bi}")
    c.drawRightString(W - 18 * mm, by - 12 * mm, f"For {v['name']}")
    c.drawRightString(W - 18 * mm, by - 22 * mm, "Authorised Signatory")
    c.setFont("Helvetica", 7)
    c.setFillColor(colors.grey)
    c.drawCentredString(W / 2, 12 * mm, "This is a computer generated invoice. Subject to Bengaluru jurisdiction.")


# Layout B — modern minimal, big "Invoice" word, rules only, western number format (Brightline)
def layout_b(c, inv, v):
    style = "western"
    ink = colors.HexColor("#222222")
    c.setFillColor(colors.HexColor("#E4572E"))
    c.circle(22 * mm, H - 22 * mm, 5 * mm, fill=1, stroke=0)
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(30 * mm, H - 20 * mm, v["name"])
    c.setFont("Helvetica", 8)
    c.drawString(30 * mm, H - 25 * mm, " · ".join(v["addr"]))
    c.setFont("Helvetica", 34)
    c.drawRightString(W - 18 * mm, H - 26 * mm, inv.get("doc_title", "Invoice"))
    if inv.get("banner"):
        c.setFillColor(colors.HexColor("#FFF1E6"))
        c.rect(18 * mm, H - 40 * mm, W - 36 * mm, 8 * mm, fill=1, stroke=0)
        c.setFillColor(colors.HexColor("#B23A00"))
        c.setFont("Helvetica-Bold", 9)
        c.drawString(21 * mm, H - 37.3 * mm, inv["banner"])
        c.setFillColor(ink)

    y = H - 52 * mm
    cols = [("Invoice #", inv.get("number", "")), ("Issued", inv["date_str"]),
            ("Customer PO", inv.get("po_ref") or "—"), ("Service period", inv.get("period", "—"))]
    for i, (k, val) in enumerate(cols):
        x = 18 * mm + i * 44 * mm
        c.setFont("Helvetica", 7.5)
        c.setFillColor(colors.HexColor("#777777"))
        c.drawString(x, y, k)
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(ink)
        c.drawString(x, y - 5 * mm, val)
    y -= 18 * mm
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#777777"))
    c.drawString(18 * mm, y, "Billed to")
    c.drawString(110 * mm, y, "From")
    c.setFillColor(ink)
    c.setFont("Helvetica", 9)
    for i, line in enumerate(BUYER):
        c.drawString(18 * mm, y - (i + 1) * 4.4 * mm, line)
    for i, line in enumerate([v["name"], f"GSTIN {v['gstin']}", v["email"], v["phone"]]):
        c.drawString(110 * mm, y - (i + 1) * 4.4 * mm, line)

    data = [["Description", "SAC", "Qty", "Unit price", "Line total"]]
    for l in inv["lines"]:
        data.append([para(l["desc"], 9), l.get("hsn", ""), f"{l['qty']:g}", inr(l["rate"], style), inr(l["amount"], style)])
    t = Table(data, colWidths=[86 * mm, 18 * mm, 16 * mm, 26 * mm, 28 * mm])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1, ink), ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                           ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5), ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
                           ("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    _, th = t.wrapOn(c, W, H)
    ty = y - 30 * mm - th
    t.drawOn(c, 18 * mm, ty)
    rows = [("Taxable value", "INR " + inr(inv["net"], style))] + [(k, "INR " + v_) for k, v_ in tax_rows(inv, style)] + \
           [("Total due", "INR " + inr(inv["printed_total"], style))]
    yy = ty - 8 * mm
    for i, (k, val) in enumerate(rows):
        last = i == len(rows) - 1
        c.setFont("Helvetica-Bold" if last else "Helvetica", 12 if last else 9)
        c.drawString(120 * mm, yy, k)
        c.drawRightString(W - 18 * mm, yy, val)
        yy -= 7 * mm if last else 5.5 * mm
    c.setFont("Helvetica", 8.5)
    yy -= 8 * mm
    bn, ba, bi = inv.get("bank_override", v["bank"])
    c.drawString(18 * mm, yy, f"Remit to: {bn} · A/c {ba} · IFSC {bi}")
    c.drawString(18 * mm, yy - 5 * mm, f"Payment due within {inv.get('terms_days', 15)} days of invoice date. Questions: {v['email']}")


# Layout C — boxed, compact, PO without prefix, IGST columns per line (Coastal)
def layout_c(c, inv, v):
    style = "indian"
    c.setLineWidth(0.8)
    c.rect(12 * mm, 12 * mm, W - 24 * mm, H - 24 * mm)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(W / 2, H - 24 * mm, v["name"].upper())
    c.setFont("Helvetica", 8.5)
    c.drawCentredString(W / 2, H - 29 * mm, ", ".join(v["addr"]) + f"  Ph: {v['phone']}")
    c.drawCentredString(W / 2, H - 33.5 * mm, f"GSTIN/UIN: {v['gstin']}    State: Tamil Nadu (33)")
    c.line(12 * mm, H - 37 * mm, W - 12 * mm, H - 37 * mm)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(W / 2, H - 42.5 * mm, inv.get("doc_title", "TAX INVOICE"))
    c.line(12 * mm, H - 45 * mm, W - 12 * mm, H - 45 * mm)
    c.line(W / 2, H - 45 * mm, W / 2, H - 80 * mm)
    c.line(12 * mm, H - 80 * mm, W - 12 * mm, H - 80 * mm)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(15 * mm, H - 50 * mm, "Consignee / Buyer:")
    c.setFont("Helvetica", 8.5)
    for i, line in enumerate(BUYER + ["Place of supply: Karnataka (29)"]):
        c.drawString(15 * mm, H - (55 + i * 4.3) * mm, line)
    meta = [("Bill No", inv.get("number") or ""), ("Dated", inv["date_str"]),
            ("Buyer's Order No", inv.get("po_ref") or ""), ("Mode/Terms of Payment", inv.get("terms", "45 days credit")),
            ("Dispatched through", inv.get("dispatch", "Road - VRL Logistics"))]
    for i, (k, val) in enumerate(meta):
        c.setFont("Helvetica", 8)
        c.drawString(W / 2 + 3 * mm, H - (50 + i * 6) * mm, k)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(W / 2 + 40 * mm, H - (50 + i * 6) * mm, val)

    data = [["Sl", "Description of Goods", "HSN", "Quantity", "Rate", "Per", "Amount"]]
    for i, l in enumerate(inv["lines"], 1):
        data.append([str(i), para(l["desc"], 8.5), l.get("hsn", ""), f"{l['qty']:g}", inr(l["rate"], style), l.get("uom", "Nos"),
                     inr(l["amount"], style)])
    for k, val in [("Taxable Value", inr(inv["net"], style))] + tax_rows(inv, style):
        data.append(["", para(f"<b>{k}</b>", 8.5), "", "", "", "", val])
    data.append(["", para("<b>Total</b>", 9), "", "", "", "", "Rs. " + inr(inv["printed_total"], style)])
    t = Table(data, colWidths=[9 * mm, 70 * mm, 18 * mm, 20 * mm, 20 * mm, 14 * mm, 31 * mm])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black),
                           ("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black),
                           ("INNERGRID", (0, 0), (-1, 0), 0.4, colors.black),
                           ("LINEAFTER", (0, 0), (-2, -1), 0.3, colors.grey),
                           ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
                           ("FONT", (-1, -1), (-1, -1), "Helvetica-Bold", 9),
                           ("ALIGN", (3, 0), (-1, -1), "RIGHT"), ("TOPPADDING", (0, 0), (-1, -1), 3.5)]))
    _, th = t.wrapOn(c, W, H)
    ty = H - 84 * mm - th
    t.drawOn(c, 13 * mm, ty)
    c.setFont("Helvetica", 8.5)
    c.drawString(15 * mm, ty - 6 * mm, f"Amount Chargeable (in words): INR {amount_words(inv['printed_total'])}")
    bn, ba, bi = inv.get("bank_override", v["bank"])
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(15 * mm, ty - 16 * mm, "Company's Bank Details")
    c.setFont("Helvetica", 8.5)
    c.drawString(15 * mm, ty - 21 * mm, f"Bank Name : {bn}")
    c.drawString(15 * mm, ty - 25.5 * mm, f"A/c No. : {ba}")
    c.drawString(15 * mm, ty - 30 * mm, f"Branch & IFS Code : {bi}")
    c.drawRightString(W - 16 * mm, ty - 22 * mm, f"for {v['name']}")
    c.drawRightString(W - 16 * mm, ty - 34 * mm, "Authorised Signatory")
    c.setFont("Helvetica-Oblique", 7.5)
    c.drawString(15 * mm, 16 * mm, "Declaration: We declare that this invoice shows the actual price of the goods described and that all particulars are true and correct.")


# Layout D — plain, typewriter-ish (Delta Tech). Used for the scanned and the tax-inclusive invoices.
def layout_d(c, inv, v):
    style = "indian"
    c.setFont("Courier-Bold", 15)
    c.drawString(18 * mm, H - 22 * mm, v["name"])
    c.setFont("Courier", 9)
    for i, line in enumerate(v["addr"] + [f"GSTIN: {v['gstin']}", f"Email: {v['email']}"]):
        c.drawString(18 * mm, H - (28 + i * 4.3) * mm, line)
    c.setFont("Courier-Bold", 13)
    c.drawRightString(W - 18 * mm, H - 22 * mm, inv.get("doc_title", "INVOICE"))
    c.setFont("Courier", 9.5)
    meta = [("Invoice No", inv.get("number", "")), ("Date", inv["date_str"])]
    meta.append(("Order Ref", inv.get("po_ref") or inv.get("ref_text", "")))
    for i, (k, val) in enumerate(meta):
        c.drawRightString(W - 18 * mm, H - (30 + i * 5) * mm, f"{k}: {val}")
    c.line(18 * mm, H - 52 * mm, W - 18 * mm, H - 52 * mm)
    c.setFont("Courier-Bold", 9.5)
    c.drawString(18 * mm, H - 58 * mm, "Customer:")
    c.setFont("Courier", 9.5)
    for i, line in enumerate(BUYER):
        c.drawString(18 * mm, H - (63 + i * 4.4) * mm, line)
    y = H - 88 * mm
    c.line(18 * mm, y + 5 * mm, W - 18 * mm, y + 5 * mm)
    c.setFont("Courier-Bold", 9.5)
    rate_hdr = "Rate(incl.GST)" if inv.get("tax_inclusive") else "Rate"
    c.drawString(18 * mm, y, "Item")
    c.drawRightString(128 * mm, y, "Qty")
    c.drawRightString(160 * mm, y, rate_hdr)
    c.drawRightString(W - 18 * mm, y, "Amount")
    c.line(18 * mm, y - 2.5 * mm, W - 18 * mm, y - 2.5 * mm)
    c.setFont("Courier", 9.5)
    y -= 8 * mm
    for l in inv["lines"]:
        c.drawString(18 * mm, y, l["desc"])
        c.drawRightString(128 * mm, y, f"{l['qty']:g}")
        c.drawRightString(160 * mm, y, inr(l["rate"], style))
        c.drawRightString(W - 18 * mm, y, inr(l["amount"], style))
        y -= 6.5 * mm
    c.line(18 * mm, y + 2 * mm, W - 18 * mm, y + 2 * mm)
    y -= 5 * mm
    if inv.get("tax_inclusive"):
        rows = [("Total (inclusive of GST)", inr(inv["printed_total"], style))]
        half = round(inv["tax"] / 2, 2)
        rows_note = [f"Prices are inclusive of GST. Tax included: CGST 9% Rs.{inr(half)}, SGST 9% Rs.{inr(inv['tax'] - half)}",
                     f"Taxable value: Rs.{inr(inv['net'])}"]
    else:
        rows = [("Subtotal", inr(inv["net"], style))] + tax_rows(inv, style) + [("TOTAL PAYABLE", inr(inv["printed_total"], style))]
        rows_note = []
    for k, val in rows:
        c.setFont("Courier-Bold" if k.startswith("TOTAL") or k.startswith("Total") else "Courier", 10)
        c.drawRightString(160 * mm, y, k)
        c.drawRightString(W - 18 * mm, y, val)
        y -= 5.5 * mm
    c.setFont("Courier", 8.5)
    for n in rows_note + inv.get("notes", []):
        y -= 2 * mm
        c.drawString(18 * mm, y, n)
        y -= 3 * mm
    y -= 8 * mm
    bn, ba, bi = inv.get("bank_override", v["bank"])
    c.setFont("Courier", 9)
    c.drawString(18 * mm, y, f"Bank: {bn}  A/C: {ba}  IFSC: {bi}")
    c.drawString(18 * mm, y - 5 * mm, "Payment terms: 30 days. Goods once sold will not be taken back.")


# Layout E — letter-style services invoice (Evergreen)
def layout_e(c, inv, v):
    style = "indian"
    green = colors.HexColor("#2E6B3F")
    c.setStrokeColor(green)
    c.setLineWidth(3)
    c.line(18 * mm, H - 14 * mm, W - 18 * mm, H - 14 * mm)
    c.setFillColor(green)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(18 * mm, H - 26 * mm, "Evergreen")
    c.setFont("Helvetica", 10)
    c.drawString(18 * mm, H - 31 * mm, "Facility Services")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 8.5)
    for i, line in enumerate(v["addr"] + [f"GSTIN {v['gstin']}"]):
        c.drawRightString(W - 18 * mm, H - (22 + i * 4.3) * mm, line)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(18 * mm, H - 48 * mm, "Tax Invoice for Services")
    c.setFont("Helvetica", 9.5)
    lines = [f"Invoice Number: {inv.get('number', '')}", f"Invoice Date: {inv['date_str']}",
             f"Against Work Order / PO: {inv.get('po_ref') or 'N/A'}", f"Billing Period: {inv.get('period', '')}"]
    for i, l in enumerate(lines):
        c.drawString(18 * mm, H - (56 + i * 5) * mm, l)
    c.drawString(120 * mm, H - 56 * mm, "To,")
    for i, l in enumerate(BUYER):
        c.drawString(120 * mm, H - (61 + i * 4.6) * mm, l)
    data = [["S.No", "Particulars", "SAC", "Units", "Rate", "Amount (INR)"]]
    for i, l in enumerate(inv["lines"], 1):
        data.append([str(i), para(l["desc"], 9), l.get("hsn", "998533"), f"{l['qty']:g}", inr(l["rate"], style), inr(l["amount"], style)])
    data.append(["", "Total taxable value", "", "", "", inr(inv["net"], style)])
    for k, val in tax_rows(inv, style):
        data.append(["", k, "", "", "", val])
    data.append(["", "Invoice Total", "", "", "", inr(inv["printed_total"], style)])
    t = Table(data, colWidths=[12 * mm, 82 * mm, 18 * mm, 14 * mm, 22 * mm, 26 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), green), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5), ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
                           ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 9.5),
                           ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#BBBBBB")),
                           ("ALIGN", (3, 0), (-1, -1), "RIGHT"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    _, th = t.wrapOn(c, W, H)
    ty = H - 92 * mm - th
    t.drawOn(c, 18 * mm, ty)
    c.setFont("Helvetica", 9)
    y = ty - 10 * mm
    for n in inv.get("notes", []):
        c.drawString(18 * mm, y, n)
        y -= 5 * mm
    bn, ba, bi = inv.get("bank_override", v["bank"])
    c.drawString(18 * mm, y - 4 * mm, f"Kindly remit to {bn}, Account {ba}, IFSC {bi}.")
    c.drawString(18 * mm, y - 9 * mm, "Payment due within 15 days. Thank you for your business.")
    c.drawString(18 * mm, y - 24 * mm, "For Evergreen Facility Services")
    c.drawString(18 * mm, y - 34 * mm, "Accounts Manager")


LAYOUTS = {"A": layout_a, "B": layout_b, "C": layout_c, "D": layout_d, "E": layout_e}


def render(inv):
    compute(inv)
    v = VENDORS[inv["vendor"]]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Invoice {inv.get('number', '')}")
    c.setAuthor(v["name"])
    LAYOUTS[inv["layout"]](c, inv, v)
    c.showPage()
    c.save()
    pdf = buf.getvalue()
    if inv.get("scanned"):
        pdf = scanify(pdf)
    (OUT / inv["file"]).write_bytes(pdf)
    return inv


def scanify(pdf_bytes):
    """Turn a clean PDF into an image-only 'scan': grayscale, slight skew, blur, noise, no text layer."""
    random.seed(7)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=200)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    img = img.rotate(-0.9, resample=Image.BICUBIC, expand=False, fillcolor=255)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    px = img.load()
    for _ in range(int(img.width * img.height * 0.004)):
        x, y = random.randrange(img.width), random.randrange(img.height)
        px[x, y] = random.choice((90, 140, 200))
    img = ImageEnhance.Contrast(img).enhance(0.85)
    img = ImageEnhance.Brightness(img).enhance(0.96)
    jb = io.BytesIO()
    img.save(jb, format="JPEG", quality=60)
    out = pymupdf.open()
    page = out.new_page(width=A4[0], height=A4[1])
    page.insert_image(page.rect, stream=jb.getvalue())
    return out.tobytes(deflate=True)


# ---------------------------------------------------------------------------------------------------------------
SAMPLES = [
    # ---------------- happy paths
    dict(file="01_happy_apex_furniture.pdf", group="happy", title="Clean invoice, full 3-way match",
         blurb="Apex Office Solutions bills PO-2026-0101 for furniture. Machine-readable PDF, goods fully received.",
         expected="APPROVED", vendor="apex", layout="A", number="APX-INV-2026-1187", date_str="14-Nov-2026",
         po_ref="PO-2026-0101", tax_mode="cgst_sgst",
         lines=[dict(desc="Ergonomic Office Chair - Mesh Back (Model EC-210)", hsn="9401", qty=20, uom="Nos", rate=6500),
                dict(desc="Height Adjustable Standing Desk 1400mm", hsn="9403", qty=10, uom="Nos", rate=18000),
                dict(desc="Dual Monitor Arm (gas spring)", hsn="8473", qty=20, uom="Nos", rate=2200)]),
    dict(file="02_happy_brightline_freight.pdf", group="happy", title="Partial billing on a blanket service PO",
         blurb="Brightline bills 5 of 12 freight trips on blanket PO-2026-0102 (4 already billed). Services need no goods receipt.",
         expected="APPROVED", vendor="brightline", layout="B", number="BLL-2026-0512", date_str="Nov 16, 2026",
         po_ref="PO-2026-0102", period="01–15 Nov 2026", tax_mode="igst", terms_days=15,
         lines=[dict(desc="FTL freight Bengaluru → Chennai, 32ft MXL container (trips 5–9)".replace("→", "to"), hsn="996511", qty=5, rate=42000)]),
    dict(file="03_tolerance_coastal_boxes.pdf", group="happy", title="Small price variance within tolerance",
         blurb="Coastal Packaging charges ₹18.20 vs ₹18.00 on the PO for boxes. PO written without its prefix.",
         expected="APPROVED", vendor="coastal", layout="C", number="CPI/1061/26-27", date_str="10.11.2026",
         po_ref="2026-0103", tax_mode="igst",
         lines=[dict(desc="Corrugated Box 3 Ply 12x10x8 inch", hsn="4819", qty=5000, uom="Nos", rate=18.20),
                dict(desc="Air Bubble Wrap Roll 1m x 100m", hsn="3923", qty=100, uom="Roll", rate=450)]),
    # ---------------- featured edge cases
    dict(file="EC1_scanned_no_po_delta_laptops.pdf", group="edge", title="Scanned image, no PO number",
         blurb="Image-only scan with skew and noise. Vendor references an 'email order' instead of a PO — the process must infer which of Delta's open POs it belongs to.",
         expected="APPROVED", vendor="delta", layout="D", number="DTS/26-27/0342", date_str="12/11/2026", po_ref=None,
         ref_text="Your email order dt 20-Oct-2026", tax_mode="cgst_sgst", scanned=True,
         lines=[dict(desc="Dell Latitude 5450 Laptop i7 16GB 512GB", qty=8, rate=72000),
                dict(desc="Dell WD19S Docking Station", qty=8, rate=9500)]),
    dict(file="EC2_split_overbilling_evergreen.pdf", group="edge", title="Third split invoice exceeds PO balance",
         blurb="Evergreen's 3rd invoice on a 3-month PO adds an unplanned ₹40,000 deep-clean. Cumulative billing would exceed the PO.",
         expected="NEEDS_REVIEW", vendor="evergreen", layout="E", number="EFS/2026/1120", date_str="20 November 2026",
         po_ref="PO-2026-0105", period="November 2026", tax_mode="cgst_sgst",
         notes=["Additional deep cleaning carried out on 28-29 Oct as requested by the admin team (Diwali)."],
         lines=[dict(desc="Housekeeping services - HQ (monthly, Nov 2026)", qty=1, rate=85000),
                dict(desc="Additional deep cleaning - festive (one time)", qty=1, rate=40000)]),
    dict(file="EC3_bank_change_apex_supplies.pdf", group="edge", title="Perfect match, but bank account changed",
         blurb="Everything matches PO-2026-0107 — but the remittance account differs from the vendor master. Classic payment-fraud pattern.",
         expected="NEEDS_REVIEW", vendor="apex", layout="A", number="APX-INV-2026-1203", date_str="17-Nov-2026",
         po_ref="PO-2026-0107", tax_mode="cgst_sgst",
         bank_override=("ICICI Bank, Indiranagar", "091805001122", "ICIC0000918"),
         notes=["IMPORTANT: We have changed our bankers. Kindly remit to the new account below."],
         lines=[dict(desc="Toner Cartridge HP 85A Compatible", hsn="8443", qty=30, uom="Nos", rate=3800),
                dict(desc="A4 Copier Paper 75 GSM (Box of 5 reams)", hsn="4802", qty=100, uom="Box", rate=1450)]),
    dict(file="EC4_duplicate_brightline_resent.pdf", group="edge", title="Already-paid invoice re-sent in a new format",
         blurb="Brightline re-sends October's invoice as a 'payment reminder' with a new date and the number written BLL/2026/0458.",
         expected="REJECTED", vendor="brightline", layout="B", number="BLL/2026/0458", date_str="Nov 18, 2026",
         po_ref="PO-2026-0102", period="Oct 2026", tax_mode="igst", banner="PAYMENT REMINDER — this invoice is overdue, please process at the earliest",
         lines=[dict(desc="FTL freight Bengaluru to Chennai, 32ft MXL container (trips 1–4)", hsn="996511", qty=4, rate=42000)]),
    # ---------------- more scenarios
    dict(file="X1_price_over_tolerance_apex_keyboards.pdf", group="more", title="Unit price 10% above PO",
         blurb="Apex bills keyboards at ₹2,650 against ₹2,400 on PO-2026-0108.",
         expected="NEEDS_REVIEW", vendor="apex", layout="A", number="APX-INV-2026-1211", date_str="18-Nov-2026",
         po_ref="PO-2026-0108", tax_mode="cgst_sgst",
         lines=[dict(desc="Ergonomic Wireless Keyboard", hsn="8471", qty=25, uom="Nos", rate=2650),
                dict(desc="Ergonomic Wireless Mouse", hsn="8471", qty=25, uom="Nos", rate=1100)]),
    dict(file="X2_tax_inclusive_delta_switches.pdf", group="more", title="Tax embedded in unit prices",
         blurb="Delta quotes GST-inclusive rates. The process must back out the taxable value before comparing with the PO.",
         expected="APPROVED", vendor="delta", layout="D", number="DTS/26-27/0351", date_str="16/11/2026",
         po_ref="PO-2026-0109", tax_mode="cgst_sgst", tax_inclusive=True,
         lines=[dict(desc="Cisco CBS350-24T Managed Switch", qty=4, rate=44840)]),
    dict(file="X3_short_receipt_coastal_film.pdf", group="more", title="Billed for more than was received",
         blurb="Coastal bills 200 rolls of stretch film; the warehouse has only received 150 (3-way match).",
         expected="NEEDS_REVIEW", vendor="coastal", layout="C", number="CPI/1088/26-27", date_str="19.11.2026",
         po_ref="2026-0106", tax_mode="igst",
         lines=[dict(desc="Stretch Film Roll 500mm x 23 Micron", hsn="3920", qty=200, uom="Roll", rate=650)]),
    dict(file="X4_incomplete_math_error_coastal.pdf", group="more", title="Missing invoice number, total doesn't add up",
         blurb="No bill number and a grand total ₹2,000 higher than the lines plus tax.",
         expected="REJECTED", vendor="coastal", layout="C", number=None, date_str="21.11.2026",
         po_ref="2026-0103", tax_mode="igst", override_total=162480.00,
         lines=[dict(desc="Corrugated Box 3 Ply 12x10x8 inch", hsn="4819", qty=5000, uom="Nos", rate=18.20),
                dict(desc="Air Bubble Wrap Roll 1m x 100m", hsn="3923", qty=100, uom="Roll", rate=450)]),
    dict(file="X5_blocked_vendor_fusion.pdf", group="more", title="Vendor blocked in procurement",
         blurb="Fusion Print House has a valid PO, but was blocked after failing compliance review.",
         expected="REJECTED", vendor="fusion", layout="A", number="FPH/2026/882", date_str="15-Nov-2026",
         po_ref="PO-2026-0110", tax_mode="cgst_sgst",
         lines=[dict(desc="Product Brochure A4 Tri-fold 170 GSM, 4 colour", hsn="4911", qty=10000, uom="Nos", rate=9)]),
    dict(file="X6_unknown_vendor_quickparts.pdf", group="more", title="Vendor not in the master, no PO",
         blurb="An invoice from a company procurement has never onboarded, with no PO reference.",
         expected="REJECTED", vendor="quickparts", layout="B", number="QPT-5531", date_str="Nov 19, 2026",
         po_ref=None, tax_mode="igst",
         lines=[dict(desc="Industrial safety gloves (pair)", hsn="4015", qty=400, rate=145),
                dict(desc="Safety goggles anti-fog", hsn="9004", qty=100, rate=320)]),
    dict(file="X7_proforma_delta.pdf", group="more", title="Proforma invoice sent to AP",
         blurb="A proforma (advance quote) is not a payable tax invoice, even though it looks like one.",
         expected="REJECTED", vendor="delta", layout="D", doc_title="PROFORMA INVOICE", number="DTS/PI/26-27/0088",
         date_str="19/11/2026", po_ref=None, ref_text="Quotation for upcoming requirement", tax_mode="cgst_sgst",
         notes=["This is a proforma invoice for advance payment and is not a tax invoice."],
         lines=[dict(desc="Dell Latitude 5450 Laptop i7 16GB 512GB", qty=4, rate=72000)]),
]


def main():
    manifest = []
    for s in SAMPLES:
        inv = render(dict(s, lines=[dict(l) for l in s["lines"]]))
        manifest.append({k: inv[k] for k in ("file", "group", "title", "blurb", "expected")} |
                        {"vendor": VENDORS[inv["vendor"]]["name"], "gross": inv["printed_total"]})
        print(f"  {inv['file']:<45} {inv['expected']:<13} gross={inv['printed_total']:>12,.2f}")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"Wrote {len(manifest)} invoices to {OUT}")


if __name__ == "__main__":
    sys.exit(main())
