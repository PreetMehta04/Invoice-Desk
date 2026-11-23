"""SQLite persistence: procurement master data (seeded from CSV), AP ledger, runs, events."""
import csv
import json
import re
import sqlite3
import threading
from contextlib import contextmanager

from .config import DB_PATH, SEED_DIR

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
  vendor_id TEXT PRIMARY KEY, name TEXT, aliases TEXT, gstin TEXT, status TEXT, status_reason TEXT,
  bank_name TEXT, bank_account TEXT, ifsc TEXT, payment_terms_days INTEGER, email TEXT, category TEXT);
CREATE TABLE IF NOT EXISTS purchase_orders (
  po_number TEXT PRIMARY KEY, vendor_id TEXT, po_date TEXT, po_type TEXT, description TEXT,
  requester TEXT, department TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS po_lines (
  po_number TEXT, line_no INTEGER, item_code TEXT, description TEXT, qty REAL, uom TEXT, unit_price REAL,
  PRIMARY KEY (po_number, line_no));
CREATE TABLE IF NOT EXISTS goods_receipts (
  grn_number TEXT, po_number TEXT, line_no INTEGER, qty_received REAL, received_date TEXT, received_by TEXT);
CREATE TABLE IF NOT EXISTS ap_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_number TEXT, invoice_number_norm TEXT, vendor_id TEXT,
  invoice_date TEXT, po_number TEXT, net_amount REAL, tax_amount REAL, gross_amount REAL, status TEXT,
  paid_date TEXT, service_period TEXT, source TEXT, run_id TEXT, file_hash TEXT, lines_json TEXT,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, filename TEXT, file_path TEXT, file_hash TEXT, source TEXT, status TEXT,
  decision TEXT, final_decision TEXT, headline TEXT, route_to TEXT, engine TEXT,
  vendor_id TEXT, vendor_name TEXT, invoice_number TEXT, po_number TEXT, gross_amount REAL, currency TEXT,
  extracted_json TEXT, result_json TEXT, error TEXT, review_status TEXT, reviewer_note TEXT,
  created_at TEXT, completed_at TEXT, duration_ms INTEGER);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, seq INTEGER, ts TEXT, stage TEXT, status TEXT,
  title TEXT, payload_json TEXT);
CREATE INDEX IF NOT EXISTS ix_events_run ON run_events(run_id, seq);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id));
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def tx():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def q(sql, params=()):
    with tx() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def q1(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    with tx() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def norm_invoice_number(s: str | None) -> str:
    """Normalise an invoice number so 'BLL/2026/0458', 'BLL-2026-0458' and 'bll 2026 0458' compare equal."""
    if not s:
        return ""
    s = re.sub(r"[^A-Za-z0-9]", "", s.upper())
    s = re.sub(r"^(INVOICE|INV|NO)+(?=[A-Z0-9])", "", s) if len(s) > 6 else s
    return s


def _read_csv(name):
    with open(SEED_DIR / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def init_db(reset: bool = False):
    with tx() as c:
        if reset:
            # Never drop users or sessions on reset — only procurement / run data
            for t in ("vendors", "purchase_orders", "po_lines", "goods_receipts", "ap_ledger", "runs", "run_events"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 0:
            _seed(c)


def _seed(c):
    for r in _read_csv("vendors.csv"):
        c.execute("INSERT INTO vendors VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            r["vendor_id"], r["name"], r["aliases"], r["gstin"], r["status"], r["status_reason"], r["bank_name"],
            r["bank_account"], r["ifsc"], int(r["payment_terms_days"]), r["email"], r["category"]))
    for r in _read_csv("purchase_orders.csv"):
        c.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?)", tuple(r[k] for k in (
            "po_number", "vendor_id", "po_date", "po_type", "description", "requester", "department", "status")))
    for r in _read_csv("po_lines.csv"):
        c.execute("INSERT INTO po_lines VALUES (?,?,?,?,?,?,?)", (
            r["po_number"], int(r["line_no"]), r["item_code"], r["description"], float(r["qty"]), r["uom"],
            float(r["unit_price"])))
    for r in _read_csv("goods_receipts.csv"):
        c.execute("INSERT INTO goods_receipts VALUES (?,?,?,?,?,?)", (
            r["grn_number"], r["po_number"], int(r["line_no"]), float(r["qty_received"]), r["received_date"],
            r["received_by"]))
    for r in _read_csv("ap_invoice_history.csv"):
        c.execute("""INSERT INTO ap_ledger (invoice_number, invoice_number_norm, vendor_id, invoice_date, po_number,
                     net_amount, tax_amount, gross_amount, status, paid_date, service_period, source, lines_json)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            r["invoice_number"], norm_invoice_number(r["invoice_number"]), r["vendor_id"], r["invoice_date"],
            r["po_number"] or None, float(r["net_amount"]), float(r["tax_amount"]), float(r["gross_amount"]),
            r["status"], r["paid_date"] or None, r["service_period"] or None, "history",
            json.dumps([{"po_line_no": int(a.split(":")[0]), "qty": float(a.split(":")[1])}
                        for a in (r.get("po_line_qty") or "").split("|") if a])))


# --- convenience readers -----------------------------------------------------
def vendors():
    return q("SELECT * FROM vendors ORDER BY vendor_id")


def po_with_lines(po_number):
    po = q1("SELECT * FROM purchase_orders WHERE po_number=?", (po_number,))
    if not po:
        return None
    po["lines"] = q("SELECT * FROM po_lines WHERE po_number=? ORDER BY line_no", (po_number,))
    po["total_amount"] = round(sum(l["qty"] * l["unit_price"] for l in po["lines"]), 2)
    billed = q("""SELECT invoice_number, invoice_date, net_amount, gross_amount, status, service_period, lines_json
                  FROM ap_ledger WHERE po_number=? AND status IN ('paid','approved') ORDER BY invoice_date""",
               (po_number,))
    po["invoices"] = billed
    po["billed_net"] = round(sum(b["net_amount"] for b in billed), 2)
    po["remaining_net"] = round(po["total_amount"] - po["billed_net"], 2)
    # quantity billed per PO line, from structured line allocations on previously posted invoices
    billed_qty = {}
    for b in billed:
        for a in json.loads(b.get("lines_json") or "[]"):
            if a.get("po_line_no") is not None:
                billed_qty[a["po_line_no"]] = billed_qty.get(a["po_line_no"], 0) + (a.get("qty") or 0)
    grn = q("SELECT line_no, SUM(qty_received) AS qty, GROUP_CONCAT(DISTINCT grn_number) AS grns, "
            "MAX(received_date) AS last_date FROM goods_receipts WHERE po_number=? GROUP BY line_no", (po_number,))
    grn_map = {g["line_no"]: g for g in grn}
    for l in po["lines"]:
        l["amount"] = round(l["qty"] * l["unit_price"], 2)
        g = grn_map.get(l["line_no"])
        l["qty_received"] = g["qty"] if g else 0
        l["grn_numbers"] = g["grns"] if g else None
        l["qty_billed"] = billed_qty.get(l["line_no"], 0)
    return po


def all_pos():
    return [po_with_lines(p["po_number"]) for p in q("SELECT po_number FROM purchase_orders ORDER BY po_number")]
