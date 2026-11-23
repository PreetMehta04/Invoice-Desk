"""Run event bus: every stage transition is persisted (audit trail) and pushed to live viewers."""
import json
import threading
import time
from datetime import datetime, timezone

from . import db

_live: dict[str, dict] = {}
_cv = threading.Condition()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def start(run_id):
    with _cv:
        _live[run_id] = {"events": [], "done": False}


def emit(run_id, stage, status, title, payload=None):
    with _cv:
        st = _live.setdefault(run_id, {"events": [], "done": False})
        ev = {"seq": len(st["events"]) + 1, "ts": now_iso(), "stage": stage, "status": status, "title": title,
              "payload": payload or {}}
        st["events"].append(ev)
        _cv.notify_all()
    db.execute("INSERT INTO run_events (run_id, seq, ts, stage, status, title, payload_json) VALUES (?,?,?,?,?,?,?)",
               (run_id, ev["seq"], ev["ts"], stage, status, title, json.dumps(payload or {}, default=str)))
    return ev


def append_persisted(run_id, stage, status, title, payload=None):
    """Append an event to an already-finished run (e.g. a reviewer action)."""
    last = db.q1("SELECT MAX(seq) AS s FROM run_events WHERE run_id=?", (run_id,))
    seq = (last["s"] or 0) + 1
    ts = now_iso()
    db.execute("INSERT INTO run_events (run_id, seq, ts, stage, status, title, payload_json) VALUES (?,?,?,?,?,?,?)",
               (run_id, seq, ts, stage, status, title, json.dumps(payload or {}, default=str)))


def finish(run_id):
    with _cv:
        if run_id in _live:
            _live[run_id]["done"] = True
        _cv.notify_all()


def persisted(run_id):
    rows = db.q("SELECT seq, ts, stage, status, title, payload_json FROM run_events WHERE run_id=? ORDER BY seq", (run_id,))
    return [{"seq": r["seq"], "ts": r["ts"], "stage": r["stage"], "status": r["status"], "title": r["title"],
             "payload": json.loads(r["payload_json"] or "{}")} for r in rows]


def wait_events(run_id, after_seq, timeout=15.0):
    """Block until there are events after `after_seq` (or the run is done). Returns (events, done)."""
    deadline = time.time() + timeout
    with _cv:
        while True:
            st = _live.get(run_id)
            if st is None:
                return None, True
            new = [e for e in st["events"] if e["seq"] > after_seq]
            if new or st["done"]:
                return new, st["done"]
            remaining = deadline - time.time()
            if remaining <= 0:
                return [], False
            _cv.wait(remaining)


def is_live(run_id):
    with _cv:
        return run_id in _live and not _live[run_id]["done"]


def remove_live(run_id):
    with _cv:
        _live.pop(run_id, None)
        _cv.notify_all()

