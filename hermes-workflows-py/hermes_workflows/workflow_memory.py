"""Provider-neutral lifecycle records and an explicitly driven effect outbox.

No provider discovery, configuration, secret access, or background tasks live
here. Optional activation is owned by MemoryService and its public host adapter.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from .db import ACTIVE, NODE_TERMINAL, TERMINAL, ConflictError
from .effects import EffectStore
from .redact import redact

KIND = "workflow_memory"
REASONS = frozenset(
    {
        "capability_unavailable",
        "not_consented",
        "no_external_provider",
        "scheduled_records_disabled",
        "provider_unavailable",
        "destination_unsupported",
        "writer_busy",
        "config_unavailable",
        "deadline_exceeded",
        "provider_call_unconfirmed",
        "invalid_response",
        "wrong_profile",
        "destination_changed",
        "selection_changed",
        "deadline_before_acceptance",
        "lookup_unsupported",
        "policy_disabled", "unsupported_provider", "invalid_policy", "strict_mode_unsupported",
    }
)


class MemoryUnavailable(RuntimeError):
    def __init__(self, reason="capability_unavailable"):
        self.reason = (
            reason if isinstance(reason, str) and reason in REASONS else "provider_unavailable"
        )
        super().__init__(self.reason)


@dataclass(frozen=True)
class Destination:
    home: str
    provider: str
    binding: str  # Non-secret SHA-256 identity supplied by the host adapter.

    def __post_init__(self):
        if not isinstance(self.home, str) or not Path(self.home).is_absolute():
            raise ValueError("memory destination requires an absolute profile home")
        if not isinstance(self.provider, str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,63}", self.provider
        ):
            raise ValueError("invalid memory provider identity")
        if not isinstance(self.binding, str) or not re.fullmatch(r"[0-9a-f]{64}", self.binding):
            raise ValueError("memory binding must be a non-secret SHA-256 identity")
        object.__setattr__(self, "home", str(Path(self.home).resolve()))


@dataclass(frozen=True)
class MemoryPolicy:
    destination: Destination
    enabled: bool = False
    write_run_summaries: bool = True
    write_node_failures: bool = True
    write_node_events: bool = False
    writer_context: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)

    def snapshot(self, home, **_kwargs):
        for key in ("enabled", "write_run_summaries", "write_node_failures", "write_node_events"):
            if type(getattr(self, key)) is not bool:
                raise ValueError("memory policy flags must be booleans")
        if not self.enabled:
            return None
        if Path(home).resolve() != Path(self.destination.home):
            raise ValueError("memory destination must belong to the run-owner profile")
        return {"version": 1, **asdict(self)}


@dataclass(frozen=True)
class Receipt:
    status: str
    receipt_id: str | None = None
    reason: str | None = None

    def __post_init__(self):
        if self.status not in ("queued", "acknowledged", "failed", "unknown", "unavailable"):
            raise ValueError("invalid memory receipt status")
        if self.receipt_id is not None and (
            not isinstance(self.receipt_id, str)
            or not self.receipt_id
            or len(self.receipt_id) > 256
        ):
            raise ValueError("invalid memory receipt identity")
        if self.reason is not None and self.reason not in REASONS:
            raise ValueError("invalid memory receipt reason")


class MemorySink(Protocol):
    """Internal seam, not an existing Hermes API.

    Adapters must enforce deadlines and atomically validate expected_destination
    at write acceptance. lookup is read-only; neither method may change routing.
    acknowledged means a durable receipt, never inferred search visibility.
    """

    async def describe(self) -> Destination | None: ...

    async def write(
        self, record: dict, *, effect_id: str, expected_destination: Destination, timeout: float
    ) -> Receipt: ...

    async def lookup(
        self, *, effect_id: str, expected_destination: Destination, timeout: float
    ) -> Receipt: ...


def _record(run, kind, *, node=None, revision=None):
    # Deliberately no prompts, inputs, outputs, errors, notes, paths or transcripts.
    record = {
        "schema_version": 1,
        "kind": kind,
        "run_id": run["run_id"],
        "workflow_id": run["workflow_id"],
        "workflow_version": run.get("workflow_version", 1),
        "execution_revision": run.get("execution_revision", 0) if revision is None else revision,
    }
    if node is not None:
        record.update(
            {
                key: node[key]
                for key in (
                    "node_id",
                    "node_type",
                    "status",
                    "outcome",
                    "iteration",
                    "attempt",
                    "attempt_id",
                )
                if key in node
            }
        )
    else:
        record["status"] = run["status"]
        if kind in ("run_completed", "retrospective"):
            record["node_counts"] = dict(
                sorted(Counter(node["status"] for node in run["nodes"].values()).items())
            )
        if kind == "retrospective":
            counts = ", ".join(f"{key}: {value}" for key, value in record["node_counts"].items())
            record["markdown"] = f"Workflow run {run['status']}.\n\nNode states: {counts}."
    # Clip identifiers only after redaction, so truncation cannot expose part of a secret.
    record = redact(record)
    return {key: value[:256] if isinstance(value, str) else value for key, value in record.items()}


def capture(con, before, run):
    """Commit immutable intents with a run transition, under its SQLite transaction."""
    context = run.get("memory_export")
    if not context or context.get("enabled") is not True or context.get("version") != 1:
        return
    destination = Destination(**context["destination"])
    revision = run.get("execution_revision", 0)

    def emit(record, attempt_row=None):
        attempt_id = attempt_row["attempt_id"] if attempt_row is not None else None
        identity = json.dumps(
            [
                "workflow-memory-v1",
                run["run_id"],
                record["execution_revision"],
                record["kind"],
                *(
                    [attempt_row[k] for k in ("node_id", "iteration", "attempt", "attempt_id")]
                    if attempt_row is not None
                    else [None, None, None, None]
                ),
            ],
            separators=(",", ":"),
        )
        # Keep canonical identities local; exported identifiers may have been redacted.
        effect_id = uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        payload = json.dumps(
            {
                "destination": asdict(destination),
                "record": record,
                "writer_context": context.get("writer_context", {}),
            },
            sort_keys=True,
        )
        now = time.time()
        con.execute(
            "INSERT OR IGNORE INTO py_effects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                effect_id,
                run["run_id"],
                identity,
                record["execution_revision"],
                attempt_row["node_id"] if attempt_row is not None else None,
                attempt_id,
                KIND,
                payload,
                "prepared",
                None,
                None,
                None,
                now,
                now,
            ),
        )

    if context["write_node_events"] and (
        before is None
        or before.get("execution_revision", 0) != revision
        and run["status"] in ACTIVE
    ):
        emit(_record(run, "run_started"))
    # Include archived attempts from a loop's immediate reset/automatic retry.
    # Inserting the same stable key is harmless and never updates an older intent.
    if context["write_node_failures"] or context["write_node_events"]:
        for row in con.execute("SELECT * FROM py_attempts WHERE run_id=?", (run["run_id"],)):
            node = json.loads(row["data"])
            node.update(
                node_id=row["node_id"],
                attempt_id=row["attempt_id"],
                iteration=row["iteration"],
                attempt=row["attempt"],
            )
            if node.get("status") in NODE_TERMINAL:
                if context["write_node_failures"] and node.get("outcome") == "failure":
                    emit(_record(run, "node_failed", node=node, revision=row["revision"]), row)
                if context["write_node_events"]:
                    emit(_record(run, "node_completed", node=node, revision=row["revision"]), row)
            elif context["write_node_events"] and node.get("status") in ("running", "scheduled"):
                # A scheduled handle must not create a second start record.
                node["status"] = "running"
                emit(_record(run, "node_started", node=node, revision=row["revision"]), row)
    if context["write_run_summaries"] and run["status"] in TERMINAL:
        emit(_record(run, "run_completed"))
        emit(_record(run, "retrospective"))


class MemoryOutbox:
    def __init__(self, db):
        self.db = db
        self.effects = EffectStore(db)

    def entries(self, run_id):
        with self.db.connect() as con:
            return [
                self.effects._decode(row)
                for row in con.execute(
                    "SELECT * FROM py_effects WHERE run_id=? AND kind=? ORDER BY created_at,effect_id",
                    (run_id, KIND),
                )
            ]

    def _read(self, effect_id):
        effect = self.effects.get(effect_id)
        if effect["kind"] != KIND:
            raise ValueError("not a workflow memory effect")
        destination = Destination(**effect["payload"]["destination"])
        if Path(destination.home) != self.db.path.parent.parent.resolve():
            raise ConflictError("memory effect belongs to another profile")
        return effect, destination

    def _settle(self, effect_id, owner, status, *, receipt=None, reason=None):
        # Never copy exception/remote text into this durable status record.
        result = {"reason": reason} if reason else redact(asdict(receipt))
        with self.db.connect(write=True) as con:
            return (
                con.execute(
                    "UPDATE py_effects SET status=?,result=?,updated_at=? "
                    "WHERE effect_id=? AND kind=? AND status='in_flight' AND driver_owner=?",
                    (status, json.dumps(result), time.time(), effect_id, KIND, owner),
                ).rowcount
                == 1
            )

    @staticmethod
    def _timeout(timeout):
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 0 < timeout <= 30
        ):
            raise ValueError("memory timeout must be positive and at most 30 seconds")
        return timeout

    def recover_expired(self):
        """Expired in-flight work is unknown; it is never reset for automatic resend."""
        with self.db.connect(write=True) as con:
            return con.execute(
                "UPDATE py_effects SET status='unknown',result=?,updated_at=? "
                "WHERE kind=? AND status='in_flight' AND json_extract(handle,'$.deadline')<=?",
                (json.dumps({"reason": "delivery_interrupted"}), time.time(), KIND, time.time()),
            ).rowcount

    async def deliver(self, effect_id, sink: MemorySink, *, timeout=5):
        timeout = self._timeout(timeout)
        effect, destination = self._read(effect_id)
        if effect["status"] != "prepared":
            return self.effects.get(effect_id)
        try:
            current = await asyncio.wait_for(sink.describe(), timeout)
            reason = None if current is not None else "capability_unavailable"
        except MemoryUnavailable as exc:
            current, reason = None, exc.reason
        except Exception:
            current, reason = None, "provider_unavailable"
        owner = uuid.uuid4().hex
        with self.db.connect(write=True) as con:
            state = self.db._owned_row(con, effect["run_id"])
            context = json.loads(state["state"]).get("memory_export", {})
            status = (
                "stale"
                if state["revision"] != effect["revision"]
                or current not in (None, destination)
                or context.get("enabled") is not True
                or context.get("destination") != asdict(destination)
                else "unavailable"
                if current is None
                else "in_flight"
            )
            con.execute(
                "UPDATE py_effects SET status=?,driver_owner=?,handle=?,result=?,updated_at=? "
                "WHERE effect_id=? AND kind=? AND status='prepared'",
                (
                    status,
                    owner,
                    json.dumps({"deadline": time.time() + timeout}),
                    json.dumps({"reason": reason}) if status == "unavailable" else None,
                    time.time(),
                    effect_id,
                    KIND,
                ),
            )
            selected = con.execute(
                "SELECT status,driver_owner FROM py_effects WHERE effect_id=?", (effect_id,)
            ).fetchone()
        if tuple(selected) != ("in_flight", owner):
            return self.effects.get(effect_id)
        try:
            receipt = await asyncio.wait_for(
                sink.write(
                    redact(effect["payload"]["record"]),
                    effect_id=effect_id,
                    expected_destination=destination,
                    timeout=timeout,
                ),
                timeout,
            )
            if not isinstance(receipt, Receipt):
                raise ValueError("malformed memory receipt")
            self._settle(effect_id, owner, receipt.status, receipt=receipt)
        except BaseException as exc:
            self._settle(effect_id, owner, "unknown", reason="delivery_unconfirmed")
            if not isinstance(exc, Exception):
                raise
        return self.effects.get(effect_id)

    async def reconcile(self, effect_id, sink: MemorySink, *, timeout=5):
        """Explicit read-only receipt lookup; never invokes write or changes destination."""
        timeout = self._timeout(timeout)
        effect, destination = self._read(effect_id)
        if effect["status"] not in ("unknown", "queued"):
            return effect
        try:
            if await asyncio.wait_for(sink.describe(), timeout) != destination:
                return effect
            receipt = await asyncio.wait_for(
                sink.lookup(
                    effect_id=effect_id,
                    expected_destination=destination,
                    timeout=timeout,
                ),
                timeout,
            )
            if not isinstance(receipt, Receipt):
                return effect
            if receipt.status in ("unknown", "unavailable"):
                return effect
        except Exception:
            return effect
        with self.db.connect(write=True) as con:
            con.execute(
                "UPDATE py_effects SET status=?,result=?,updated_at=? WHERE effect_id=? "
                "AND kind=? AND status=? AND updated_at=?",
                (
                    receipt.status,
                    json.dumps(redact(asdict(receipt))),
                    time.time(),
                    effect_id,
                    KIND,
                    effect["status"],
                    effect["updated_at"],
                ),
            )
        return self.effects.get(effect_id)
