"""Canonical invoice extraction schema — shared by every extraction engine."""

INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "enum": ["tax_invoice", "proforma_invoice", "credit_note", "quotation",
                                                     "purchase_order", "receipt", "other"],
                          "description": "What kind of document this is. A proforma or quotation is NOT a tax invoice."},
        "vendor_name": {"type": ["string", "null"], "description": "Legal name of the supplier issuing the invoice"},
        "vendor_gstin": {"type": ["string", "null"], "description": "Supplier's 15-character GSTIN exactly as printed"},
        "vendor_email": {"type": ["string", "null"]},
        "vendor_address": {"type": ["string", "null"]},
        "buyer_name": {"type": ["string", "null"]},
        "buyer_gstin": {"type": ["string", "null"]},
        "invoice_number": {"type": ["string", "null"], "description": "Invoice / bill number exactly as printed; null if absent"},
        "invoice_date": {"type": ["string", "null"], "description": "Invoice date as YYYY-MM-DD (Indian invoices use day-first dates)"},
        "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD if printed, else null"},
        "payment_terms": {"type": ["string", "null"]},
        "po_number": {"type": ["string", "null"],
                      "description": "Purchase order number exactly as printed (any label: PO No, Buyer's Order No, Work Order). Null if the document only references something that is not a PO number."},
        "other_references": {"type": ["string", "null"], "description": "Any other order reference text, e.g. 'email order dated 20-Oct'"},
        "service_period": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"], "description": "ISO currency code, e.g. INR"},
        "prices_include_tax": {"type": "boolean", "description": "True if unit prices / total are stated inclusive of GST"},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "hsn_sac": {"type": ["string", "null"]},
                    "quantity": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "unit_price": {"type": ["number", "null"], "description": "Unit price as printed (may be tax-inclusive)"},
                    "amount": {"type": ["number", "null"], "description": "Line amount as printed"},
                },
                "required": ["description", "quantity", "unit_price", "amount"],
            },
        },
        "subtotal": {"type": ["number", "null"], "description": "Taxable value before tax, as printed (null if not printed)"},
        "tax_lines": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "type": {"type": "string", "description": "CGST, SGST, IGST, VAT..."},
                "rate": {"type": ["number", "null"], "description": "Percent, e.g. 9"},
                "amount": {"type": ["number", "null"]}}, "required": ["type", "amount"]},
        },
        "tax_total": {"type": ["number", "null"]},
        "total": {"type": ["number", "null"], "description": "Grand total payable exactly as printed"},
        "bank_name": {"type": ["string", "null"]},
        "bank_account_number": {"type": ["string", "null"], "description": "Digits only"},
        "bank_ifsc": {"type": ["string", "null"]},
        "notes": {"type": "array", "items": {"type": "string"},
                  "description": "Important remarks printed on the invoice, verbatim (bank change notices, reminders, disclaimers, extra work explanations)"},
    },
    "required": ["document_type", "vendor_name", "invoice_number", "invoice_date", "po_number", "line_items",
                 "total", "prices_include_tax", "tax_lines", "notes"],
}

EXTRACTION_PROMPT = """You are an accounts-payable data extraction engine. Extract the invoice in the attached document into the
`record_invoice` structure.

Rules:
- Copy identifiers (invoice number, PO number, GSTIN, bank account, IFSC) EXACTLY as printed, preserving separators.
- Never invent or compute values that are not printed. If a field is absent, return null. Do not "fix" arithmetic.
- Dates must be YYYY-MM-DD. Indian invoices are day-first: 12/11/2026 means 12 November 2026.
- Amounts are plain numbers without currency symbols or thousands separators (1,23,456.50 -> 123456.5).
- If unit prices are stated inclusive of GST, keep them as printed and set prices_include_tax=true.
- po_number: only an actual purchase-order identifier. If the vendor writes something like "email order dated ..." put that in
  other_references and set po_number to null.
- document_type: "proforma_invoice" if the document says proforma; "quotation" for quotes; "tax_invoice" for a normal invoice.
- notes: copy any important remarks verbatim (bank account change notices, payment reminders, proforma disclaimers, explanations of extra work).
- The buyer is the company being billed (Kestrel Foods Pvt Ltd); the vendor is the issuer."""
