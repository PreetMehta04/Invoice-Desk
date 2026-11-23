# Demo playbook

## Before you record or present (5 minutes before)

1. Start the app (`./run.sh` or open the deployed URL a minute early so a sleeping free instance can wake up).
2. Check the engine badge (bottom left) says **Claude**, **Gemini** or **OpenAI**. If it says *Local OCR + parser*, the key isn't loaded. The demo still works, but the AI story is stronger with a key.
3. **Rules → Reset demo data** (click twice). This gives you a clean dashboard and seeded PO balances.
4. Keep `samples/` open in a file explorer window so you can drag a PDF onto the upload area. Dragging a real file looks more real than clicking a sample card.
5. Optional: set **Live view pacing** to 600 ms if you want more time to talk over each stage.

---

## 5-minute video script

| Time | On screen | Say (roughly) |
|---|---|---|
| 0:00–0:30 | Dashboard (empty) → How it works | "AP teams open hundreds of PDF invoices a month, find the PO, check the numbers and decide. I built a process that does that end to end. One principle: **AI reads the messy document, deterministic rules make the decision**, so every payment decision can be explained and audited." |
| 0:30–1:45 | Drag `01_happy_apex_furniture.pdf` onto the upload area → live run view | Walk down the stages as they light up: "Intake fingerprints the file. The model extracts into a fixed schema, and then I **cross-check key values against the page text** so a hallucinated invoice number can't slip through. Validation does the arithmetic and GST checks. Vendor is matched by GSTIN and the bank account is compared with the verified master. Duplicate check, then line-by-line PO match and 3-way match with the goods receipt." Point at the stamp: "Approved, posted to the ledger, scheduled for payment on the due date to the verified account." |
| 1:45–2:45 | Run **EC3 bank change** | "Same vendor, everything matches the PO to the rupee… but look at vendor verification. The invoice asks us to pay a new account. This is the most expensive AP fraud there is. The process never pays a changed account automatically. It holds the invoice, flags fraud risk and routes it to Treasury to call the vendor on the number we already have." |
| 2:45–3:45 | Run **EC2 split over-billing** → open the PO & receipt matching stage | "Vendors split one PO across several invoices. Each invoice looks fine alone. Here the process knows two invoices already billed ₹1.7 lakh, only ₹85,000 is left, and this one is ₹1.25 lakh, with a deep-clean line that was never ordered. Routed to the buyer." Type a note → **Approve & post**. "The reviewer's decision and note go into the audit trail, and the PO balance updates." |
| 3:45–4:30 | Run **EC4 duplicate** → Vendor email tab | "A paid October invoice comes back as a 'payment reminder' with a new date and the number written with slashes. Invoice numbers are normalised before comparison, so it's caught as a duplicate of BLL-2026-0458, paid on 12 November. Rejected, with a vendor email drafted for a human to send." |
| 4:30–5:00 | Dashboard | "Every run is here with its decision, reason, owner and timing. Straight-through rate, the review queue, and value stopped before payment. Export to CSV. That's the process." |

If you have extra time, use **EC1 scanned / no PO**: "Image-only scan with no PO number. It inferred PO-0104 from the line items at 94% confidence and shows the candidates it rejected."

---

## Live interview runbook

Order: **01 happy path → EC1 scanned/no PO → EC2 split over-billing (then approve it) → EC3 bank change → EC4 duplicate → dashboard**.
Have extra scenarios ready in case they ask: X1 price +10%, X3 short receipt, X2 tax-inclusive, X7 proforma.

Two things you can do live to show the logic is real and not scripted:
- **Re-drop the happy-path PDF you just approved.** It's rejected instantly as a duplicate (same file and same invoice number).
- **Rules → price tolerance 0.5% → re-run 03 (Coastal boxes).** It flips from Approved to Needs review. Reset the tolerance to 2 afterwards.

### Questions to expect

- **Why not let the LLM decide?** Payment approval has to be deterministic, consistent and auditable. The same invoice must get the same answer, with a reason a controller can sign off. The LLM does the part rules can't: reading arbitrary layouts and scans. Its output is then cross-checked (grounding check, arithmetic, GSTIN checksum) before anything depends on it.
- **How do you stop hallucinated extraction?** Schema-constrained output (tool calling) and temperature 0. Key identifiers must appear in the page text or OCR. Arithmetic must reconcile. Anything that doesn't fit becomes a review exception, not a guess.
- **What if the AI is down?** The run automatically falls back to the local OCR and parser engine, with a visible warning on the run.
- **Why is a bank change "review" and not "reject"?** Vendors do legitimately change banks. The right control is to hold and verify through a known channel, not to bounce the invoice. Payment is still blocked to the new account until Treasury confirms.
- **Tolerance design?** The lesser of 2% or ₹5,000 on matched lines, so small invoices can't hide big percentage variances and large invoices can't hide big absolute ones. A variance that's accepted doesn't also count as PO over-billing.
- **Split invoices?** Billing is added up from the AP ledger per PO and per PO line, so quantity and value checks apply across invoices, not within one.
- **Productionising:** a mailbox connector (IMAP or Graph) in place of the watched folder, ERP sync in place of CSVs, SSO reviewer identity, multi-invoice PDFs, credit notes, and reviewer corrections fed back as extraction evaluation data.
