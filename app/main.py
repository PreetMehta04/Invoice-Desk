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
def _start(data: bytes, filename: str, source: str):
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File larger than 15 MB")
    if not data[:5] == b"%PDF-":
        raise HTTPException(415, f"{filename} is not a PDF")
    run_id = pipeline.create_run(data, filename, source)
    pipeline.start_run(run_id)
    return run_id


@app.post("/api/runs")
async def upload(files: list[UploadFile] = File(...), current_user: dict = Depends(get_current_user)):
    ids, errors = [], []
    for f in files:
        try:
            ids.append(_start(await f.read(), f.filename or "invoice.pdf", "upload"))
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
    return {"run_id": _start((config.SAMPLES_DIR / name).read_bytes(), name, "sample")}


RUN_COLS = ("id, filename, source, status, decision, final_decision, headline, route_to, engine, vendor_id, vendor_name, "
            "invoice_number, po_number, gross_amount, currency, review_status, created_at, completed_at, duration_ms")


@app.get("/api/runs")
def list_runs(decision: str | None = None, limit: int = 200, current_user: dict = Depends(get_current_user)):
    sql = f"SELECT {RUN_COLS} FROM runs"
    params = []
    if decision:
        sql += " WHERE final_decision=?" if decision != "PENDING_REVIEW" else " WHERE review_status='pending'"
        if decision != "PENDING_REVIEW":
            params.append(decision)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return db.q(sql, params)


@app.get("/api/runs.csv")
def export_csv(current_user: dict = Depends(get_current_user)):
    rows = db.q(f"SELECT {RUN_COLS}, reviewer_note FROM runs ORDER BY created_at DESC")
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=invoice_runs.csv"})


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, current_user: dict = Depends(get_current_user)):
    run = db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
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
    run = db.q1("SELECT file_path, filename FROM runs WHERE id=?", (run_id,))
    if not run or not Path(run["file_path"]).exists():
        raise HTTPException(404)
    return FileResponse(run["file_path"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{run["filename"]}"'})


@app.get("/api/runs/{run_id}/output.json")
def run_output(run_id: str, current_user: dict = Depends(get_current_user)):
    run = db.q1("SELECT result_json FROM runs WHERE id=?", (run_id,))
    if not run or not run["result_json"]:
        raise HTTPException(404, "No output yet")
    return Response(run["result_json"], media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename={run_id}_decision.json"})


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    """SSE stream. Replays persisted events, then pushes new ones until the run completes."""
    if not db.q1("SELECT id FROM runs WHERE id=?", (run_id,)):
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
    body = await request.json()
    try:
        reviewer_name = body.get("reviewer") or f"{current_user['name']} ({current_user['role']})"
        final = pipeline.review(run_id, body.get("action"), (body.get("note") or "").strip(), reviewer_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"final_decision": final}


# ------------------------------------------------------------------------------------------------ dashboard data
@app.get("/api/stats")
def stats(current_user: dict = Depends(get_current_user)):
    runs = db.q("SELECT final_decision, decision, gross_amount, duration_ms, review_status, result_json, status FROM runs")
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
    return {
        "vendors": db.vendors(),
        "pos": db.all_pos(),
        "grns": db.q("SELECT * FROM goods_receipts ORDER BY grn_number, line_no"),
        "ledger": db.q("SELECT * FROM ap_ledger ORDER BY created_at DESC, invoice_date DESC"),
    }


@app.get("/api/rules")
def get_rules(current_user: dict = Depends(get_current_user)):
    return config.load_rules()


@app.put("/api/rules")
async def put_rules(request: Request, current_user: dict = Depends(get_current_user)):
    return config.save_rules(await request.json())


@app.post("/api/reset")
def reset(current_user: dict = Depends(get_current_user)):
    """Restore procurement data, AP ledger and rules to the seeded state and clear run history (for demos)."""
    if any(events.is_live(r["id"]) for r in db.q("SELECT id FROM runs WHERE status IN ('queued','running')")):
        raise HTTPException(409, "Wait for running invoices to finish")
    db.init_db(reset=True)
    auth.seed_default_user()
    config.reset_rules()
    for f in config.UPLOAD_DIR.glob("*.pdf"):
        f.unlink(missing_ok=True)
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
