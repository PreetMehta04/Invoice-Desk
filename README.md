# Invoice Desk: invoice processing from PDF to decision

**Zamp AI Solutions Analyst case study, PS-1 (Finance / AP)**

Invoice Desk takes a vendor invoice PDF, which can be digital or scanned. It reads the invoice with a multimodal AI model and checks it against procurement data: vendor master, purchase orders, goods receipts and the AP ledger. It then reaches one of three decisions: **Approved**, **Needs review** (routed to the right owner) or **Rejected** (with a vendor email drafted). Every stage runs live on screen. Each run is stored with its full audit trail, and the dashboard shows history, statuses and outputs across runs.

> **Design principle:** AI reads the messy document. Deterministic rules make the decision. The model handles what rules can't, such as different layouts, scanned pages, labels that are worded differently, and tax shown inside or outside the price. The payment decision stays explainable, repeatable and auditable. Every decision comes with the checks that caused it.

---

## 1. Run it locally (about 2 minutes)

You need Python 3.10 or newer.

```bash
# macOS / Linux
./run.sh
# Windows
run.bat
```

Then open **http://localhost:8000**.

You can also start it by hand:

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # then add an API key (see below)
uvicorn app.main:app --port 8000
```

### Turn on AI extraction (recommended for the demo)

Put **one** key in `.env`:

| Provider | Variable | Notes |
|---|---|---|
| Anthropic Claude | `ANTHROPIC_API_KEY` | Default model `claude-sonnet-5-5`. Reads PDFs natively, including scanned pages. |
| Google Gemini | `GEMINI_API_KEY` | Default `gemini-2.5-flash`. Has a free tier. |
| OpenAI | `OPENAI_API_KEY` | Default `gpt-4o-mini` |

The engine currently in use is shown at the bottom left of the UI.

**With no key, the app still works end to end.** It uses a built-in local engine: the PDF text layer or Tesseract OCR, plus a layout-aware parser. All 14 sample invoices reach the correct decision with either engine. If the AI call fails (network, quota, bad key), the run falls back to the local engine automatically and shows a warning. It never fails silently.

For scanned PDFs, the local engine needs Tesseract installed (`brew install tesseract`, `apt install tesseract-ocr`, or the Windows installer from UB-Mannheim). The Docker image includes it. You don't need Tesseract when an AI key is set.

---

## 2. The process

```
 PDF (upload / inbox folder)
   │
   ▼
 1 Intake ──────────── fingerprint (SHA-256), page count, text layer vs scanned image
 2 AI extraction ───── multimodal LLM → fixed schema (vendor, GSTIN, invoice no., dates, PO ref, lines, tax, total, bank, remarks)
                       + grounding check: key values must appear in the page text / OCR
 3 Validation ──────── required fields · lines = subtotal · subtotal + tax = total · tax-inclusive pricing
                       · CGST+SGST vs IGST by place of supply · GSTIN checksum · date sanity · billed-to entity
 4 Vendor ──────────── vendor master by GSTIN (then fuzzy name) · status (blocked?) · bank account = verified account?
 5 Duplicates ──────── same file · normalised invoice no. (BLL/2026/0458 = BLL-2026-0458) · same amount + date/period
 6 PO & receipts ───── explicit or inferred PO · line-level price & qty vs PO with tolerance
                       · cumulative billing across split invoices · 3-way match to goods receipts (goods POs)
 7 Decision ────────── any blocking failure → REJECTED
                       any exception        → NEEDS REVIEW, routed to Treasury / AP / Buyer / Receiving / Controller
                       otherwise            → APPROVED
 8 Post & notify ───── ledger + PO balance updated, payment scheduled to the verified account,
                       review task created, or vendor email drafted
```

**Human in the loop.** A reviewer opens a held invoice and approves or rejects it with a note. Approval posts the invoice to the ledger and uses up PO balance. Every action is added to the run's audit trail.

**Inputs the process checks against** (`data/seed/*.csv`, editable):
- `vendors.csv`: 6 vendors with GSTIN, status, verified bank account and payment terms. One is blocked.
- `purchase_orders.csv`, `po_lines.csv`: 10 POs covering goods and services, including blanket POs.
- `goods_receipts.csv`: GRNs per PO line, including one short receipt.
- `ap_invoice_history.csv`: invoices booked before go-live. These feed duplicate detection and split-billing balances.

Business thresholds are editable on the **Rules** page: price tolerance, auto-approval limit, PO-inference confidence, duplicate window and the others.

---

## 3. Edge cases

There are four featured edge cases, plus seven more scenarios the rules handle. All 14 PDFs are in `samples/` and run with one click from **Process invoices**.

| # | Scenario | Why it's hard | What the process does |
|---|---|---|---|
| EC1 | **Scanned image with no PO number.** Delta Tech writes "Your email order dt 20-Oct-2026". | No text layer, skewed and noisy. The PO has to be inferred, and Delta has two open POs. | Reads the image. Scores each of the vendor's open POs on line-description similarity and amount fit, picks PO-2026-0104 at 94% confidence and shows the candidates. Then runs the full 3-way match → **Approved**. If confidence is low, it **holds** and lists the candidates. |
| EC2 | **Third split invoice goes over the PO.** Evergreen's 3-month housekeeping PO already has two invoices. The third adds an unplanned ₹40,000 deep clean. | Each invoice looks fine on its own. The problem only shows up when billing is added up across invoices. | Tracks billed value per PO from the ledger. Shows "₹1,70,000 already billed across 2 invoices, ₹85,000 left, this would over-bill by ₹40,000" and flags the line that isn't on the PO → **Needs review → Buyer**. If approved, the PO balance updates. |
| EC3 | **Perfect match, but the bank account has changed.** "We have changed our bankers. Kindly remit to the new account." | Every financial check passes. This is the classic business-email-compromise fraud pattern. | Compares the remittance details with the verified vendor master. Holds the invoice and raises a fraud flag. Payment can never go to the new account automatically, and Treasury must verify through the contact on the master record → **Needs review → Treasury**. |
| EC4 | **An already-paid invoice re-sent in a new format.** BLL/2026/0458 arrives as a "payment reminder" with a new date. The original BLL-2026-0458 was paid on 12 Nov. | Different file, date and number formatting, so a naive exact-match check misses it. | Normalises invoice numbers before comparing and cites the original booking and payment date → **Rejected**, with a vendor email drafted. |

**More scenarios:** unit price 10% above the PO (held for Buyer) · tax included in unit prices (taxable value backed out, then approved) · billed 200 units but only 150 received (held for Receiving) · missing invoice number and a total ₹2,000 too high (rejected) · blocked vendor (rejected) · vendor not onboarded and no PO (rejected) · proforma invoice sent to AP (rejected). The happy paths also cover a small price variance inside tolerance and partial billing against a blanket service PO with no GRN needed (2-way match).

Things the process also catches: re-uploading an invoice that was already approved (same file and same number → rejected), wrong GST type for the place of supply, an invalid GSTIN checksum, invoices addressed to a different entity, and invoices above the auto-approval limit (controller sign-off).

---

## 4. Testing

```bash
python -m pytest tests -q
```

`tests/test_scenarios.py` runs every sample PDF through the real pipeline against an isolated database. It checks each sample's expected decision and the specific reason codes: the PO inferred for EC1, the over-balance flag for EC2, the fraud flag for EC3, the duplicate for EC4, the GRN shortfall, and that a re-upload gets caught. It also checks that a reviewer's approval uses up PO balance.

Manual checks:
- **Process invoices → Run all 14 samples**, then look at the dashboard (on a fresh reset: 5 approved, 4 for review, 5 rejected).
- Drop your own PDF on the upload area, or copy a PDF into `data/inbox/`. The inbox watcher picks it up within about 2 seconds (simulated AP mailbox).
- **Rules → change the price tolerance to 0.5%** and re-run `03_tolerance_coastal_boxes.pdf`. It moves from Approved to Needs review.
- **Rules → Reset demo data** restores the seeded state between rehearsals.

To regenerate the sample PDFs: `python scripts/generate_samples.py`.

---

## 5. Deploy (live link for the submission)

**Render (free):** push this folder to a GitHub repo. In Render, choose **New + → Blueprint** and pick the repo (it uses `render.yaml` and the `Dockerfile`), then add your API key under *Environment*. The free plan sleeps when idle, so open the URL about a minute before a demo. Data resets on restart, which is fine for a demo.

**Docker anywhere:** `docker compose up --build`, then open http://localhost:8000.

**Hugging Face Spaces:** create a *Docker* Space, upload the folder, set the secret `ANTHROPIC_API_KEY` and the variable `PORT=7860`.

---

## 6. Project layout

```
app/
  main.py            FastAPI: REST API, Server-Sent Events live stream, inbox watcher, serves the UI
  pipeline.py        the 8-stage process, decision rules, routing, ledger posting, reviewer actions
  extraction/
    schema.py        canonical invoice schema + extraction prompt
    llm.py           Claude / Gemini / OpenAI multimodal extraction (PDF sent as-is)
    local.py         text-layer / Tesseract OCR reader + layout-aware parser; normalisation
  db.py              SQLite: master data seeded from CSV, AP ledger, runs, events (audit trail)
  events.py          live event bus (persisted + pushed to viewers)
  gstin.py           GSTIN format + checksum validation
  config.py          env config, editable rules
static/              single-page UI (no build step): dashboard, live run view, review queue, data, rules
data/seed/           procurement data (CSV) · data/rules.default.json
samples/             14 generated invoice PDFs + manifest.json
scripts/generate_samples.py
tests/test_scenarios.py
```

## 7. Assumptions and limits

- The buyer is Kestrel Foods Pvt Ltd (Bengaluru, GSTIN 29AAKCK4821F1ZF). Amounts are in INR. PO values exclude tax, so matching uses taxable value.
- Price tolerance is the **lesser** of 2% or ₹5,000 on the matched lines. An accepted variance doesn't count as over-billing the PO.
- Only *paid* or *approved* invoices use up PO balance. Invoices *in review* are recorded so a resubmission gets caught, but they don't use up balance.
- Goods POs need a 3-way match (PO, invoice, GRN). Service POs need a 2-way match.
- Vendor emails are **drafted, not sent**, so a person stays in the loop for anything outbound.
- Next steps for production: an IMAP or Graph mailbox connector in place of the watched folder, ERP integration (SAP or NetSuite) in place of the CSVs, multi-page and multi-invoice PDFs, credit-note matching, a reviewer identity (SSO), and a feedback loop where reviewer corrections become extraction evaluation data.
