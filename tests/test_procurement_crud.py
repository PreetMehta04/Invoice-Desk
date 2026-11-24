"""Integration tests for Procurement Data CRUD: Vendors, POs, GRNs, Ledger."""
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="invoice-proc-test-"))
os.environ["WATCH_INBOX"] = "0"

from app import auth, config, db
from app.main import app


@pytest.fixture(autouse=True)
def setup_db():
    db.init_db(reset=True)
    auth.seed_default_user()
    config.reset_rules()


def test_procurement_crud():
    client = TestClient(app)

    # 1. Sign up user & get auth token
    r_signup = client.post("/api/auth/signup", json={
        "name": "Procurement Manager", "email": "proc@company.com", "password": "Password@123", "confirm_password": "Password@123"
    })
    token = r_signup.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. Test Vendors CRUD
    # CREATE
    new_vendor = {
        "vendor_id": "VEND-TEST-1",
        "name": "Test Industrial Supplies",
        "aliases": "TIS, Test Industrial",
        "gstin": "29AABCT1234F1Z5",
        "status": "approved",
        "bank_name": "HDFC Bank",
        "bank_account": "123456789012",
        "ifsc": "HDFC0001234",
        "payment_terms_days": 45,
        "email": "contact@testindustrial.com",
        "category": "Industrial Goods"
    }
    r = client.post("/api/procurement/vendors", json=new_vendor, headers=headers)
    assert r.status_code == 200
    assert r.json()["vendor_id"] == "VEND-TEST-1"

    # READ
    proc = client.get("/api/procurement", headers=headers).json()
    assert any(v["vendor_id"] == "VEND-TEST-1" for v in proc["vendors"])

    # UPDATE
    r_up = client.put("/api/procurement/vendors/VEND-TEST-1", json={
        "name": "Test Industrial Supplies Updated",
        "aliases": "TIS",
        "gstin": "29AABCT1234F1Z5",
        "status": "blocked",
        "status_reason": "Audit issue",
        "bank_name": "HDFC Bank",
        "bank_account": "123456789012",
        "ifsc": "HDFC0001234",
        "payment_terms_days": 30,
        "email": "contact@testindustrial.com",
        "category": "Industrial Goods"
    }, headers=headers)
    assert r_up.status_code == 200
    v_updated = db.q1("SELECT * FROM vendors WHERE vendor_id='VEND-TEST-1'")
    assert v_updated["name"] == "Test Industrial Supplies Updated"
    assert v_updated["status"] == "blocked"

    # 3. Test Purchase Orders CRUD
    new_po = {
        "po_number": "PO-TEST-001",
        "vendor_id": "VEND-TEST-1",
        "po_date": "2026-02-01",
        "po_type": "goods",
        "description": "Factory safety helmets and boots",
        "requester": "Rohan Sharma",
        "department": "Operations",
        "status": "open",
        "lines": [
            {"line_no": 1, "item_code": "HELM-01", "description": "Safety Helmets", "qty": 100, "uom": "pcs", "unit_price": 450.0},
            {"line_no": 2, "item_code": "BOOT-02", "description": "Safety Boots", "qty": 50, "uom": "pairs", "unit_price": 1200.0}
        ]
    }
    r_po = client.post("/api/procurement/pos", json=new_po, headers=headers)
    assert r_po.status_code == 200
    assert r_po.json()["po_number"] == "PO-TEST-001"

    po_lines = db.q("SELECT * FROM po_lines WHERE po_number='PO-TEST-001'")
    assert len(po_lines) == 2

    # 4. Test Goods Receipts (GRN) CRUD
    new_grn = {
        "grn_number": "GRN-TEST-99",
        "po_number": "PO-TEST-001",
        "line_no": 1,
        "qty_received": 100.0,
        "received_date": "2026-02-05",
        "received_by": "Warehouse Gate 2"
    }
    r_grn = client.post("/api/procurement/grns", json=new_grn, headers=headers)
    assert r_grn.status_code == 200
    grn_id = r_grn.json()["id"]

    # 5. Test AP Ledger CRUD
    new_ledger = {
        "invoice_number": "INV-HIST-999",
        "vendor_id": "VEND-TEST-1",
        "invoice_date": "2026-02-06",
        "po_number": "PO-TEST-001",
        "net_amount": 45000.0,
        "tax_amount": 8100.0,
        "gross_amount": 53100.0,
        "status": "paid",
        "paid_date": "2026-02-10",
        "service_period": "Batch 1"
    }
    r_led = client.post("/api/procurement/ledger", json=new_ledger, headers=headers)
    assert r_led.status_code == 200
    led_id = r_led.json()["id"]

    # 6. Test Deletes
    # Delete GRN
    r_del_grn = client.delete(f"/api/procurement/grns/{grn_id}", headers=headers)
    assert r_del_grn.status_code == 200
    assert db.q1("SELECT * FROM goods_receipts WHERE rowid=?", (grn_id,)) is None

    # Delete Ledger
    r_del_led = client.delete(f"/api/procurement/ledger/{led_id}", headers=headers)
    assert r_del_led.status_code == 200
    assert db.q1("SELECT * FROM ap_ledger WHERE id=?", (led_id,)) is None

    # Delete PO
    r_del_po = client.delete("/api/procurement/pos/PO-TEST-001", headers=headers)
    assert r_del_po.status_code == 200
    assert db.q1("SELECT * FROM purchase_orders WHERE po_number='PO-TEST-001'") is None
    assert db.q("SELECT * FROM po_lines WHERE po_number='PO-TEST-001'") == []

    # Delete Vendor
    r_del_v = client.delete("/api/procurement/vendors/VEND-TEST-1", headers=headers)
    assert r_del_v.status_code == 200
    assert db.q1("SELECT * FROM vendors WHERE vendor_id='VEND-TEST-1'") is None
