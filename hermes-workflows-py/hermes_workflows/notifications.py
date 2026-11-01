"""Transactional lifecycle outbox; sends are never blindly replayed after a crash."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


def resolve_target(origin, default, deliver=None):
    return deliver if deliver and deliver != "origin" else origin or default


def parse_origin(origin):
    if not isinstance(origin, str):
        return None
    parts = origin.split(":", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1], parts[2] if len(parts) == 3 and parts[2] else None


def result_output(run):
    best, seq = None, -1
    for node in run["nodes"].values():
        if node["status"] == "completed" and node.get("output") and (node.get("seq") or 0) >= seq:
            best, seq = node["output"], node.get("seq") or 0
    return best


def notice_text(run, event, node_id=None, deliver=None):
    if event == "completed" and deliver and result_output(run):
        return result_output(run)
    prefix = f"Workflow {run['workflow_id']} run {run['run_id']}"
    if event == "waiting":
        context = run.get("notification_context", {})
        attempt = run.get("nodes", {}).get(node_id, {}).get("attempt_id")
        chat_hint = ""
        if (
            attempt
            and run.get("origin")
            and context.get("routing", {}).get("session_key")
            and resolve_target(run["origin"], context.get("default"), deliver) == run["origin"]
        ):
            chat_hint = (
                " When this is the only waiting gate here, reply with approved, rejected, "
                "or needs_changes plus an optional note. Other text approves and becomes the note. "
                f"[workflow-review:{attempt}]"
            )
        return (
            f"ACTION NEEDED - {prefix}: review gate '{node_id}' is waiting for your decision. "
            "Resolve it in the dashboard or with: "
            f"/workflow review {run['run_id']} {node_id} <approved|rejected|needs_changes> [note]."
            + chat_hint
        )
    if event == "blocked":
        return f"ATTENTION - {prefix}: the card for node '{node_id}' is blocked. Inspect and unblock it on its board; the run resumes automatically."
    if event == "stuck":
        return f"ATTENTION - {prefix}: node '{node_id}' failed after repeated card dispatch failures. Check its profile and reviewer skills on the board."
    return f"{prefix}: {event}."


def enqueue(con, run, spec, revision):
    """Called in the SAME transaction as state settlement, including terminal runs.

    Old runs without a captured context never acquire surprise retrospective sends.
    Review keys use attempt identity so a new loop iteration gets its own notice.
    """
    context = run.get("notification_context")
    if not context:
        return
    deliver = spec.get("deliver")
    target = resolve_target(run.get("origin"), context.get("default"), deliver)
    events = []
    if run["status"] in ("completed", "failed"):
        events.append((run["status"], None, run["status"]))
    for name, node in run["nodes"].items():
        attempt = node.get("attempt_id", name)
        if node["status"] == "waiting_for_review":
            events.append(("waiting", name, f"waiting:{attempt}"))
        active = node["status"] in ("running", "scheduled") and run["status"] not in (
            "completed",
            "failed",
            "cancelled",
        )
        for card in node.get("adoption", {}).get("blocked", []) if active else []:
            events.append(("blocked", name, f"blocked:{attempt}:{card}"))
        if active and node.get("native_status") == "blocked":
            events.append(("blocked", name, f"blocked:{attempt}:{node.get('task_id')}"))
        if (node.get("error") or "").startswith("adopt stuck:"):
            events.append(("stuck", name, f"stuck:{attempt}"))
    now = time.time()
    for event, name, key in events:
        key = f"r{revision}:{key}"
        identity = uuid.uuid5(uuid.NAMESPACE_URL, f"workflow-notice:{run['run_id']}:{key}").hex
        text = notice_text(run, event, name, deliver)
        status = "suppressed" if not target or "[SILENT]" in text else "pending"
        payload = {
            "target": target,
            "text": text,
            "home": context["home"],
            "event": event,
            "node_id": name,
            "metadata": context.get("routing", {}).get("delivery_metadata")
            if target == run.get("origin")
            else None,
        }
        con.execute(
            "INSERT OR IGNORE INTO py_notifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                run["run_id"],
                revision,
                key,
                json.dumps(payload),
                status,
                None,
                0,
                None,
                0,
                now,
                now,
            ),
        )
    # Retry/cancel cannot release old pending messages into a new execution.
    con.execute(
        "UPDATE py_notifications SET status='superseded',updated_at=? WHERE run_id=? AND status='pending' AND (revision!=? OR ?='cancelled')",
        (now, run["run_id"], revision, run["status"]),
    )
    relevant = {f"r{revision}:{key}" for _, _, key in events}
    for row in con.execute(
        "SELECT notice_id,notice_key FROM py_notifications WHERE run_id=? AND status='pending'",
        (run["run_id"],),
    ).fetchall():
        if row["notice_key"] not in relevant:
            con.execute(
                "UPDATE py_notifications SET status='superseded',updated_at=? WHERE notice_id=?",
                (now, row["notice_id"]),
            )


class Notifications:
    def __init__(self, db, home, sender=None):
        self.db, self.home, self.sender = db, Path(home).resolve(), sender

    def records(self, run_id):
        self.db.get_run(run_id)
        with self.db.connect() as con:
            return [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in con.execute(
                    "SELECT * FROM py_notifications WHERE run_id=? ORDER BY created_at,notice_id",
                    (run_id,),
                )
            ]

    async def dispatch(self, limit=16):
        now = time.time()
        with self.db.connect(write=True) as con:
            con.execute(
                "UPDATE py_notifications SET status='unknown',error='sender_lost_acknowledgement',updated_at=? WHERE status='in_flight' AND lease_until<?",
                (now, now),
            )
        if self.sender is None or not self.sender.available():
            return
        for _ in range(limit):
            owner = uuid.uuid4().hex
            with self.db.connect(write=True) as con:
                row = con.execute(
                    "SELECT n.* FROM py_notifications n JOIN py_runs r USING(run_id) WHERE n.status='pending' AND n.revision=r.revision AND r.status!='cancelled' ORDER BY n.created_at,n.notice_id LIMIT 1"
                ).fetchone()
                if row is None:
                    return
                payload = json.loads(row["payload"])
                if Path(payload["home"]).resolve() != self.home:
                    con.execute(
                        "UPDATE py_notifications SET status='failed',error='profile_home_mismatch' WHERE notice_id=?",
                        (row["notice_id"],),
                    )
                    continue
                con.execute(
                    "UPDATE py_notifications SET status='in_flight',owner=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE notice_id=?",
                    (owner, time.time() + 60, time.time(), row["notice_id"]),
                )
            # The durable intent commits before calling the router. A timeout,
            # exception, cancellation or ambiguous result is NEVER auto-retried.
            try:
                async with asyncio.timeout(25):
                    result = await self.sender.send(
                        payload["target"],
                        payload["text"],
                        row["notice_id"],
                        metadata=payload.get("metadata"),
                    )
                status, error = (
                    ("sent", None) if result is True else ("unknown", "delivery_not_confirmed")
                )
            except asyncio.CancelledError:
                self._settle(row["notice_id"], owner, "unknown", "sender_cancelled")
                raise
            except Exception as exc:
                status, error = "unknown", f"sender_error:{type(exc).__name__}"
            self._settle(row["notice_id"], owner, status, error)

    def _settle(self, identity, owner, status, error):
        with self.db.connect(write=True) as con:
            changed = con.execute(
                "UPDATE py_notifications SET status=?,error=?,updated_at=? WHERE notice_id=? AND owner=? AND status='in_flight'",
                (status, error, time.time(), identity, owner),
            ).rowcount
            if changed:
                self._trace_delivery(con, identity, status)

    @staticmethod
    def _trace_delivery(con, identity, status):
        from .trace import safe_emit

        row = con.execute(
            "SELECT r.state,n.payload,n.revision FROM py_notifications n JOIN py_runs r USING(run_id) WHERE n.notice_id=?",
            (identity,),
        ).fetchone()
        if row:
            payload = json.loads(row["payload"])
            safe_emit(
                con,
                json.loads(row["state"]),
                "marker" if status == "sent" else "notification_status",
                marker=payload["event"],
                node_id=payload.get("node_id"),
                notice_id=identity,
                notice_revision=row["revision"],
                status=status,
            )

    def reconcile(self, identity, *, delivered):
        """Record a human-verified outcome; never performs another send."""
        if type(delivered) is not bool:
            raise ValueError("delivered must be boolean")
        with self.db.connect(write=True) as con:
            changed = con.execute(
                "UPDATE py_notifications SET status=?,error=NULL,updated_at=? WHERE notice_id=? AND status='unknown'",
                ("sent" if delivered else "failed", time.time(), identity),
            ).rowcount
            if changed != 1:
                raise ValueError("only an unknown notification can be reconciled")
            self._trace_delivery(con, identity, "sent" if delivered else "failed")
