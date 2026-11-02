"""Durable external-effect intent and reconciliation; never auto-repeat a send."""

from __future__ import annotations

import json
import time
import uuid

from .db import ConflictError, Database


class EffectStore:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _decode(row):
        if row is None:
            raise KeyError("effect not found")
        return {
            **dict(row),
            **{
                key: json.loads(row[key]) if row[key] is not None else None
                for key in ("payload", "handle", "result")
            },
        }

    def get(self, effect_id):
        with self.db.connect() as con:
            return self._decode(
                con.execute("SELECT * FROM py_effects WHERE effect_id=?", (effect_id,)).fetchone()
            )

    def prepare(
        self, run_id, key, kind, payload, *, owner, revision, node_id=None, attempt_id=None
    ):
        """Commit a stable intent BEFORE invoking an external service."""
        if not isinstance(key, str) or not key or not isinstance(kind, str) or not kind:
            raise ValueError("effect key and kind must be non-empty strings")
        if (node_id is None) != (attempt_id is None):
            raise ValueError("node and attempt identities must be supplied together")
        encoded = json.dumps(payload, sort_keys=True)
        identity = uuid.uuid5(uuid.NAMESPACE_URL, f"hermes-workflows:{run_id}:{key}").hex
        now = time.time()
        with self.db.connect(write=True) as con:
            self.db._owned_row(
                con, run_id, owner, revision, (node_id, attempt_id) if node_id else None
            )
            row = con.execute(
                "SELECT * FROM py_effects WHERE run_id=? AND effect_key=?", (run_id, key)
            ).fetchone()
            if row is not None:
                if (row["kind"], row["payload"], row["revision"], row["attempt_id"]) != (
                    kind,
                    encoded,
                    revision,
                    attempt_id,
                ):
                    raise ConflictError("effect key already describes a different operation")
                return self._decode(row)
            con.execute(
                "INSERT INTO py_effects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    run_id,
                    key,
                    revision,
                    node_id,
                    attempt_id,
                    kind,
                    encoded,
                    "prepared",
                    owner,
                    None,
                    None,
                    now,
                    now,
                ),
            )
        return self.get(identity)

    def _owned_effect(self, con, effect_id, owner, revision, *, reconcile=False):
        effect = self._decode(
            con.execute("SELECT * FROM py_effects WHERE effect_id=?", (effect_id,)).fetchone()
        )
        attempt = (effect["node_id"], effect["attempt_id"]) if effect["node_id"] else None
        self.db._owned_row(con, effect["run_id"], owner, revision, None if reconcile else attempt)
        if not reconcile and effect["revision"] != revision:
            raise ConflictError("effect belongs to an earlier execution")
        return effect

    def begin(self, effect_id, *, owner, revision):
        """An in-flight record is ambiguous after a crash, even without a handle."""
        with self.db.connect(write=True) as con:
            effect = self._owned_effect(con, effect_id, owner, revision)
            if effect["status"] != "prepared":
                raise ConflictError(
                    "effect is already started; reconcile it instead of repeating it"
                )
            con.execute(
                "UPDATE py_effects SET status='in_flight',driver_owner=?,updated_at=? WHERE effect_id=?",
                (owner, time.time(), effect_id),
            )
        return self.get(effect_id)

    def attach_handle(self, effect_id, handle, *, owner, revision):
        with self.db.connect(write=True) as con:
            effect = self._owned_effect(con, effect_id, owner, revision)
            if effect["status"] != "in_flight":
                raise ConflictError("effect is not in flight")
            if effect["handle"] is not None and effect["handle"] != handle:
                raise ConflictError("effect already has a different external handle")
            con.execute(
                "UPDATE py_effects SET handle=?,updated_at=? WHERE effect_id=?",
                (json.dumps(handle), time.time(), effect_id),
            )

    def complete(self, effect_id, result, *, owner, revision):
        with self.db.connect(write=True) as con:
            effect = self._owned_effect(con, effect_id, owner, revision)
            if effect["status"] != "in_flight" or effect["driver_owner"] != owner:
                raise ConflictError("effect must be reconciled by its current owner")
            con.execute(
                "UPDATE py_effects SET status='completed',result=?,updated_at=? WHERE effect_id=?",
                (json.dumps(result), time.time(), effect_id),
            )

    def recover(self, run_id, *, owner, revision):
        with self.db.connect(write=True) as con:
            self.db._owned_row(con, run_id, owner, revision)
            con.execute(
                "UPDATE py_effects SET status='unknown',updated_at=? WHERE run_id=? "
                "AND status='in_flight' AND driver_owner!=? AND kind!='workflow_memory'",
                (time.time(), run_id, owner),
            )
            return [
                self._decode(row)
                for row in con.execute(
                    "SELECT * FROM py_effects WHERE run_id=? AND status='unknown' "
                    "AND kind!='workflow_memory'", (run_id,)
                )
            ]

    def resolve(self, effect_id, result, *, completed, owner, revision):
        """Record an explicit reconciliation result, including for an old revision."""
        with self.db.connect(write=True) as con:
            effect = self._owned_effect(con, effect_id, owner, revision, reconcile=True)
            if effect["status"] != "unknown":
                raise ConflictError("only an unknown effect needs reconciliation")
            con.execute(
                "UPDATE py_effects SET status=?,result=?,updated_at=? WHERE effect_id=?",
                (
                    "completed" if completed else "failed",
                    json.dumps(result),
                    time.time(),
                    effect_id,
                ),
            )
