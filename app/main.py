"""FastAPI app: REST + Server-Sent Events for the live run view, serves the single-page UI."""
import asyncio
import csv
import io
import json
import shutil
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, Security, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from . import auth, config, db, events, pipeline

app = FastAPI(title="Invoice Desk — AI invoice processing", version="1.0")
MAX_BYTES = 15 * 1024 * 1024
bearer_scheme = HTTPBearer(auto_error=False)


@app.on_event("startup")
def _startup():
    db.init_db()
    auth.seed_default_user()
    config.load_rules()
    # runs interrupted by a restart can never finish — mark them
    db.execute("UPDATE runs SET status='error', decision='ERROR', final_decision='ERROR', error='Interrupted by server restart', "
               "headline='Interrupted by server restart' WHERE status IN ('queued','running')")
    if config.WATCH_INBOX:
        threading.Thread(target=_watch_inbox, daemon=True).start()


def _watch_inbox():
    """Simulates the AP mailbox: any PDF dropped into data/inbox is picked up and processed automatically."""
    done = config.INBOX_DIR / "processed"
    done.mkdir(exist_ok=True)
    while True:
        try:
            for f in sorted(config.INBOX_DIR.glob("*.pdf")):
                if time.time() - f.stat().st_mtime < 1.0:
                    continue  # still being written
                data = f.read_bytes()
                run_id = pipeline.create_run(data, f.name, "inbox")
                shutil.move(str(f), done / f"{run_id}__{f.name}")
                pipeline.start_run(run_id)
        except Exception as e:  # keep watching
            print("inbox watcher:", e)
        time.sleep(2)


# ------------------------------------------------------------------------------------------------ auth
async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> dict:
    token = None
    if creds and creds.credentials:
        token = creds.credentials
    elif "session_token" in request.cookies:
        token = request.cookies.get("session_token")
    elif "token" in request.query_params:
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    user = auth.get_user_by_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


@app.post("/api/auth/signup")
async def signup(request: Request, response: Response):
    body = await request.json()
    name = body.get("name")
    email = body.get("email")
    password = body.get("password")
    confirm_password = body.get("confirm_password")
    try:
        user = auth.create_user(name, email, password, confirm_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token = auth.create_session(user["id"])
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )
    return {"token": token, "user": user}


@app.post("/api/auth/login")
async def login(request: Request, response: Response):
    body = await request.json()
    email = body.get("email")
    password = body.get("password")
    user = auth.authenticate_user(email, password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = auth.create_session(user["id"])
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )
    return {"token": token, "user": user}


@app.get("/api/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    return {"user": current_user}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "session_token" in request.cookies:
        token = request.cookies.get("session_token")
    elif "token" in request.query_params:
        token = request.query_params.get("token")

    if token:
        auth.revoke_session(token)
    response.delete_cookie(key="session_token", path="/")
    return {"ok": True}


# ------------------------------------------------------------------------------------------------ meta
@app.get("/api/config")
def get_config():
    p = config.resolve_provider()
    return {"engine": config.provider_label(p), "provider": p, "ai_enabled": p != "local",
            "stages": [{"key": k, "label": l} for k, l in pipeline.STAGES], "rules": config.load_rules(),
            "inbox_dir": str(config.INBOX_DIR), "watch_inbox": config.WATCH_INBOX}


@app.get("/api/health")
def health():
    return {"ok": True, "engine": config.provider_label()}


# ------------------------------------------------------------------------------------------------ runs
def _start(data: bytes, filename: str, source: str, user_id: str | None = None):
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File larger than 15 MB")
    if not data[:5] == b"%PDF-":
        raise HTTPException(415, f"{filename} is not a PDF")
    run_id = pipeline.create_run(data, filename, source, user_id=user_id)
    pipeline.start_run(run_id)
    return run_id


@app.post("/api/runs")
async def upload(files: list[UploadFile] = File(...), current_user: dict = Depends(get_current_user)):
    ids, errors = [], []
    for f in files:
        try:
            ids.append(_start(await f.read(), f.filename or "invoice.pdf", "upload", user_id=current_user["id"]))
        except HTTPException as e:
            errors.append({"file": f.filename, "error": e.detail})
    if not ids:
        raise HTTPException(400, errors[0]["error"] if errors else "No files")
    return {"run_ids": ids, "errors": errors}


def _manifest():
    p = config.SAMPLES_DIR / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else []


@app.get("/api/samples")
def samples(current_user: dict = Depends(get_current_user)):
    return _manifest()


@app.get("/api/samples/{name}")
def sample_file(name: str, current_user: dict = Depends(get_current_user)):
    if name not in {m["file"] for m in _manifest()}:
        raise HTTPException(404)
    return FileResponse(config.SAMPLES_DIR / name, media_type="application/pdf")


@app.post("/api/samples/{name}/run")
def run_sample(name: str, current_user: dict = Depends(get_current_user)):
    if name not in {m["file"] for m in _manifest()}:
        raise HTTPException(404, "Unknown sample")
    return {"run_id": _start((config.SAMPLES_DIR / name).read_bytes(), name, "sample", user_id=current_user["id"])}


RUN_COLS = ("id, filename, source, status, decision, final_decision, headline, route_to, engine, vendor_id, vendor_name, "
            "invoice_number, po_number, gross_amount, currency, review_status, created_at, completed_at, duration_ms, user_id")


@app.get("/api/runs")
def list_runs(decision: str | None = None, limit: int = 200, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    sql = f"SELECT {RUN_COLS} FROM runs WHERE user_id=?"
    params = [user_id]
    if decision:
        sql += " AND final_decision=?" if decision != "PENDING_REVIEW" else " AND review_status='pending'"
        if decision != "PENDING_REVIEW":
            params.append(decision)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return db.q(sql, params)


@app.get("/api/runs.csv")
def export_csv(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    rows = db.q(f"SELECT {RUN_COLS}, reviewer_note FROM runs WHERE user_id=? ORDER BY created_at DESC", (user_id,))
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=invoice_runs.csv"})


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, current_user: dict = Depends(get_current_user)):
    run = db.q1("SELECT * FROM runs WHERE id=? AND (user_id=? OR user_id IS NULL)", (run_id, current_user["id"]))
    if not run:
        raise HTTPException(404, "Run not found")
    run["extracted"] = json.loads(run.pop("extracted_json") or "null")
    run["result"] = json.loads(run.pop("result_json") or "null")
    run["events"] = events.persisted(run_id)
    run["live"] = events.is_live(run_id)
    run.pop("file_path", None)
    return run


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str, current_user: dict = Depends(get_current_user)):
    run = db.q1("SELECT file_path, filename FROM runs WHERE id=? AND (user_id=? OR user_id IS NULL)", (run_id, current_user["id"]))
    if not run or not Path(run["file_path"]).exists():
        raise HTTPException(404)
    return FileResponse(run["file_path"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{run["filename"]}"'})


@app.get("/api/runs/{run_id}/output.json")
def run_output(run_id: str, current_user: dict = Depends(get_current_user)):
    run = db.q1("SELECT result_json FROM runs WHERE id=? AND (user_id=? OR user_id IS NULL)", (run_id, current_user["id"]))
    if not run or not run["result_json"]:
        raise HTTPException(404, "No output yet")
    return Response(run["result_json"], media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename={run_id}_decision.json"})


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    """SSE stream. Replays persisted events, then pushes new ones until the run completes."""
    if not db.q1("SELECT id FROM runs WHERE id=? AND (user_id=? OR user_id IS NULL)", (run_id, current_user["id"])):
        raise HTTPException(404)

    async def gen():
        sent = 0
        for ev in events.persisted(run_id):
            sent = ev["seq"]
            yield f"data: {json.dumps(ev, default=str)}\n\n"
        while events.is_live(run_id) or sent == 0:
            if await request.is_disconnected():
                return
            new, done = await asyncio.to_thread(events.wait_events, run_id, sent, 10.0)
            if new is None:
                break
            for ev in new:
                sent = ev["seq"]
                yield f"data: {json.dumps(ev, default=str)}\n\n"
            if not new:
                yield ": keepalive\n\n"
            if done:
                break
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/review")
async def review(run_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    run = db.q1("SELECT id FROM runs WHERE id=? AND (user_id=? OR user_id IS NULL)", (run_id, current_user["id"]))
    if not run:
        raise HTTPException(404, "Run not found")
    body = await request.json()
    try:
        reviewer_name = body.get("reviewer") or f"{current_user['name']} ({current_user['role']})"
        final = pipeline.review(run_id, body.get("action"), (body.get("note") or "").strip(), reviewer_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"final_decision": final}


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    run = db.q1("SELECT id, file_path FROM runs WHERE id=? AND (user_id=? OR user_id IS NULL)", (run_id, user_id))
    if not run:
        raise HTTPException(404, "Run not found")

    if run.get("file_path"):
        try:
            fp = Path(run["file_path"])
            if fp.exists() and config.UPLOAD_DIR in fp.resolve().parents:
                fp.unlink(missing_ok=True)
        except Exception:
            pass

    events.remove_live(run_id)

    db.execute("DELETE FROM run_events WHERE run_id=?", (run_id,))
    db.execute("DELETE FROM ap_ledger WHERE run_id=?", (run_id,))
    db.execute("DELETE FROM runs WHERE id=?", (run_id,))

    return {"ok": True, "deleted_run_id": run_id}



# ------------------------------------------------------------------------------------------------ dashboard data
@app.get("/api/stats")
def stats(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    runs = db.q("SELECT final_decision, decision, gross_amount, duration_ms, review_status, result_json, status FROM runs WHERE user_id=?", (user_id,))
    done = [r for r in runs if r["status"] == "completed"]
    by = {k: sum(1 for r in done if r["final_decision"] == k) for k in ("APPROVED", "NEEDS_REVIEW", "REJECTED")}
    stp = sum(1 for r in done if r["decision"] == "APPROVED")
    protected = 0.0
    fraud = 0
    for r in done:
        if r["final_decision"] == "REJECTED" or (r["decision"] == "NEEDS_REVIEW" and r["review_status"] == "pending"):
            res = json.loads(r["result_json"] or "{}")
            ids = {c["id"] for c in res.get("checks", []) if c["status"] in ("warn", "fail") and c["severity"] != "none"}
            if ids & {"dup_number", "dup_file", "dup_fuzzy", "bank_match", "price", "po_balance", "grn", "vendor_status", "vendor_found",
                      "sum_total", "line_on_po"}:
                protected += r["gross_amount"] or 0
            if res.get("fraud_risk"):
                fraud += 1
    durs = [r["duration_ms"] for r in done if r["duration_ms"]]
    return {
        "total": len(runs), "completed": len(done), "running": sum(1 for r in runs if r["status"] in ("queued", "running")),
        "errors": sum(1 for r in runs if r["status"] == "error"),
        "by_decision": by, "pending_review": sum(1 for r in done if r["review_status"] == "pending"),
        "straight_through_rate": round(stp / len(done) * 100) if done else 0,
        "value_processed": round(sum(r["gross_amount"] or 0 for r in done), 2),
        "value_approved": round(sum(r["gross_amount"] or 0 for r in done if r["final_decision"] == "APPROVED"), 2),
        "value_protected": round(protected, 2), "fraud_flags": fraud,
        "avg_duration_ms": int(sum(durs) / len(durs)) if durs else 0,
    }


@app.get("/api/procurement")
def procurement(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    return {
        "vendors": db.vendors(),
        "pos": db.all_pos(user_id=user_id),
        "grns": db.q("SELECT rowid AS id, * FROM goods_receipts ORDER BY grn_number, line_no"),
        "ledger": db.q("SELECT * FROM ap_ledger WHERE (user_id=? OR user_id IS NULL) ORDER BY created_at DESC, invoice_date DESC", (user_id,)),
    }


# ------------------------------------------------------------------------------------------------ procurement CRUD
@app.post("/api/procurement/vendors")
async def create_vendor(request: Request, current_user: dict = Depends(get_current_user)):
    b = await request.json()
    vid = (b.get("vendor_id") or "").strip().upper()
    if not vid:
        raise HTTPException(400, "Vendor ID is required")
    if db.q1("SELECT vendor_id FROM vendors WHERE vendor_id=?", (vid,)):
        raise HTTPException(400, f"Vendor {vid} already exists")
    db.execute(
        """INSERT INTO vendors (vendor_id, name, aliases, gstin, status, status_reason, bank_name, bank_account, ifsc, payment_terms_days, email, category)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (vid, b.get("name") or "", b.get("aliases") or "", (b.get("gstin") or "").strip().upper(),
         b.get("status") or "approved", b.get("status_reason") or "", b.get("bank_name") or "",
         b.get("bank_account") or "", (b.get("ifsc") or "").strip().upper(),
         int(b.get("payment_terms_days") or 30), b.get("email") or "", b.get("category") or "")
    )
    return {"ok": True, "vendor_id": vid}


@app.put("/api/procurement/vendors/{vendor_id}")
async def update_vendor(vendor_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    b = await request.json()
    if not db.q1("SELECT vendor_id FROM vendors WHERE vendor_id=?", (vendor_id,)):
        raise HTTPException(404, "Vendor not found")
    db.execute(
        """UPDATE vendors SET name=?, aliases=?, gstin=?, status=?, status_reason=?, bank_name=?, bank_account=?, ifsc=?, payment_terms_days=?, email=?, category=?
           WHERE vendor_id=?""",
        (b.get("name") or "", b.get("aliases") or "", (b.get("gstin") or "").strip().upper(),
         b.get("status") or "approved", b.get("status_reason") or "", b.get("bank_name") or "",
         b.get("bank_account") or "", (b.get("ifsc") or "").strip().upper(),
         int(b.get("payment_terms_days") or 30), b.get("email") or "", b.get("category") or "", vendor_id)
    )
    return {"ok": True, "vendor_id": vendor_id}


@app.delete("/api/procurement/vendors/{vendor_id}")
def delete_vendor(vendor_id: str, current_user: dict = Depends(get_current_user)):
    if not db.q1("SELECT vendor_id FROM vendors WHERE vendor_id=?", (vendor_id,)):
        raise HTTPException(404, "Vendor not found")
    db.execute("DELETE FROM vendors WHERE vendor_id=?", (vendor_id,))
    return {"ok": True, "vendor_id": vendor_id}


@app.post("/api/procurement/pos")
async def create_po(request: Request, current_user: dict = Depends(get_current_user)):
    b = await request.json()
    pno = (b.get("po_number") or "").strip().upper()
    if not pno:
        raise HTTPException(400, "PO number is required")
    if db.q1("SELECT po_number FROM purchase_orders WHERE po_number=?", (pno,)):
        raise HTTPException(400, f"Purchase order {pno} already exists")
    db.execute(
        """INSERT INTO purchase_orders (po_number, vendor_id, po_date, po_type, description, requester, department, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (pno, b.get("vendor_id") or "", b.get("po_date") or "", b.get("po_type") or "goods",
         b.get("description") or "", b.get("requester") or "", b.get("department") or "", b.get("status") or "open")
    )
    lines = b.get("lines") or []
    for idx, l in enumerate(lines, start=1):
        db.execute(
            """INSERT INTO po_lines (po_number, line_no, item_code, description, qty, uom, unit_price)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (pno, int(l.get("line_no") or idx), l.get("item_code") or "", l.get("description") or "",
             float(l.get("qty") or 1.0), l.get("uom") or "pcs", float(l.get("unit_price") or 0.0))
        )
    return {"ok": True, "po_number": pno}


@app.put("/api/procurement/pos/{po_number}")
async def update_po(po_number: str, request: Request, current_user: dict = Depends(get_current_user)):
    b = await request.json()
    if not db.q1("SELECT po_number FROM purchase_orders WHERE po_number=?", (po_number,)):
        raise HTTPException(404, "Purchase order not found")
    db.execute(
        """UPDATE purchase_orders SET vendor_id=?, po_date=?, po_type=?, description=?, requester=?, department=?, status=?
           WHERE po_number=?""",
        (b.get("vendor_id") or "", b.get("po_date") or "", b.get("po_type") or "goods",
         b.get("description") or "", b.get("requester") or "", b.get("department") or "", b.get("status") or "open", po_number)
    )
    if "lines" in b:
        db.execute("DELETE FROM po_lines WHERE po_number=?", (po_number,))
        for idx, l in enumerate(b.get("lines") or [], start=1):
            db.execute(
                """INSERT INTO po_lines (po_number, line_no, item_code, description, qty, uom, unit_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (po_number, int(l.get("line_no") or idx), l.get("item_code") or "", l.get("description") or "",
                 float(l.get("qty") or 1.0), l.get("uom") or "pcs", float(l.get("unit_price") or 0.0))
            )
    return {"ok": True, "po_number": po_number}


@app.delete("/api/procurement/pos/{po_number}")
def delete_po(po_number: str, current_user: dict = Depends(get_current_user)):
    if not db.q1("SELECT po_number FROM purchase_orders WHERE po_number=?", (po_number,)):
        raise HTTPException(404, "Purchase order not found")
    db.execute("DELETE FROM po_lines WHERE po_number=?", (po_number,))
    db.execute("DELETE FROM purchase_orders WHERE po_number=?", (po_number,))
    return {"ok": True, "po_number": po_number}


@app.post("/api/procurement/grns")
async def create_grn(request: Request, current_user: dict = Depends(get_current_user)):
    b = await request.json()
    gno = (b.get("grn_number") or "").strip().upper()
    pno = (b.get("po_number") or "").strip().upper()
    if not gno or not pno:
        raise HTTPException(400, "GRN number and PO number are required")
    rowid = db.execute(
        """INSERT INTO goods_receipts (grn_number, po_number, line_no, qty_received, received_date, received_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (gno, pno, int(b.get("line_no") or 1), float(b.get("qty_received") or 0.0),
         b.get("received_date") or "", b.get("received_by") or "")
    )
    return {"ok": True, "id": rowid}


@app.put("/api/procurement/grns/{grn_id}")
async def update_grn(grn_id: int, request: Request, current_user: dict = Depends(get_current_user)):
    b = await request.json()
    db.execute(
        """UPDATE goods_receipts SET grn_number=?, po_number=?, line_no=?, qty_received=?, received_date=?, received_by=?
           WHERE rowid=?""",
        ((b.get("grn_number") or "").strip().upper(), (b.get("po_number") or "").strip().upper(),
         int(b.get("line_no") or 1), float(b.get("qty_received") or 0.0),
         b.get("received_date") or "", b.get("received_by") or "", grn_id)
    )
    return {"ok": True, "id": grn_id}


@app.delete("/api/procurement/grns/{grn_id}")
def delete_grn(grn_id: int, current_user: dict = Depends(get_current_user)):
    db.execute("DELETE FROM goods_receipts WHERE rowid=?", (grn_id,))
    return {"ok": True, "id": grn_id}


@app.post("/api/procurement/ledger")
async def create_ledger(request: Request, current_user: dict = Depends(get_current_user)):
    b = await request.json()
    inv_no = (b.get("invoice_number") or "").strip()
    if not inv_no:
        raise HTTPException(400, "Invoice number is required")
    rowid = db.execute(
        """INSERT INTO ap_ledger (invoice_number, invoice_number_norm, vendor_id, invoice_date, po_number,
                                 net_amount, tax_amount, gross_amount, status, paid_date, service_period, source, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (inv_no, db.norm_invoice_number(inv_no), b.get("vendor_id") or "", b.get("invoice_date") or "",
         (b.get("po_number") or "").strip().upper() or None, float(b.get("net_amount") or 0.0),
         float(b.get("tax_amount") or 0.0), float(b.get("gross_amount") or 0.0),
         b.get("status") or "approved", b.get("paid_date") or None, b.get("service_period") or None,
         "manual", current_user["id"])
    )
    return {"ok": True, "id": rowid}


@app.put("/api/procurement/ledger/{ledger_id}")
async def update_ledger(ledger_id: int, request: Request, current_user: dict = Depends(get_current_user)):
    b = await request.json()
    inv_no = (b.get("invoice_number") or "").strip()
    db.execute(
        """UPDATE ap_ledger SET invoice_number=?, invoice_number_norm=?, vendor_id=?, invoice_date=?, po_number=?,
                                net_amount=?, tax_amount=?, gross_amount=?, status=?, paid_date=?, service_period=?
           WHERE id=?""",
        (inv_no, db.norm_invoice_number(inv_no), b.get("vendor_id") or "", b.get("invoice_date") or "",
         (b.get("po_number") or "").strip().upper() or None, float(b.get("net_amount") or 0.0),
         float(b.get("tax_amount") or 0.0), float(b.get("gross_amount") or 0.0),
         b.get("status") or "approved", b.get("paid_date") or None, b.get("service_period") or None, ledger_id)
    )
    return {"ok": True, "id": ledger_id}


@app.delete("/api/procurement/ledger/{ledger_id}")
def delete_ledger(ledger_id: int, current_user: dict = Depends(get_current_user)):
    db.execute("DELETE FROM ap_ledger WHERE id=?", (ledger_id,))
    return {"ok": True, "id": ledger_id}



@app.get("/api/rules")
def get_rules(current_user: dict = Depends(get_current_user)):
    return config.load_rules()


@app.put("/api/rules")
async def put_rules(request: Request, current_user: dict = Depends(get_current_user)):
    return config.save_rules(await request.json())


@app.post("/api/reset")
def reset(current_user: dict = Depends(get_current_user)):
    """Reset current user's runs and ledger entries back to clean 0 state."""
    user_id = current_user["id"]
    running = db.q("SELECT id FROM runs WHERE status IN ('queued','running') AND user_id=?", (user_id,))
    if any(events.is_live(r["id"]) for r in running):
        raise HTTPException(409, "Wait for running invoices to finish")
    user_runs = db.q("SELECT file_path FROM runs WHERE user_id=?", (user_id,))
    for r in user_runs:
        if r.get("file_path"):
            Path(r["file_path"]).unlink(missing_ok=True)
    db.execute("DELETE FROM runs WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM ap_ledger WHERE user_id=?", (user_id,))
    return {"ok": True}
    return {"ok": True}


# ------------------------------------------------------------------------------------------------ static UI
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": getattr(exc, "detail", "Not found")}, status_code=404)
    return FileResponse(config.STATIC_DIR / "index.html")
