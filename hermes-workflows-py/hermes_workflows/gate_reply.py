"""Consume scoped operator replies without borrowing the ordinary agent turn.

Selection is rechecked under the same SQLite write lock as Engine.review. The
review event is also the durable inbound receipt; no second store or scheduler
is needed. A failed confirmation never replays a decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from datetime import datetime
from pathlib import Path

from .db import ConflictError
from .origin import build_origin
from .specs import REVIEWS

log = logging.getLogger(__name__)
MARKER = re.compile(r"\[workflow-review:([0-9a-f]{32})\]")


def interpret_reply(text):
    stripped = text.strip()
    if not stripped:
        return None, None
    parts = stripped.split(None, 1)
    decision = parts[0].lower()
    if decision in REVIEWS:
        return decision, parts[1].strip() or None if len(parts) > 1 else None
    return "approved", stripped


def handled(identity):
    return {"action": "skip", "reason": f"workflow review reply {identity}"}


def consumed(con, identity):
    return (
        con.execute(
            "SELECT 1 FROM py_events WHERE json_extract(event,'$.chat_reply_id')=? LIMIT 1",
            (identity,),
        ).fetchone()
        is not None
    )


def candidates(con, reply):
    found = []
    for row in con.execute("SELECT state FROM py_runs WHERE status='waiting'"):
        run = json.loads(row[0])
        context = run.get("notification_context", {})
        routing = context.get("routing", {})
        if (
            run.get("origin") != reply["origin"]
            or Path(context.get("home", "")).resolve() != reply["home"]
            or routing.get("session_key") not in (None, reply["session_key"])
        ):
            continue
        if routing.get("delivery_metadata", {}).get("scope_id") != reply["scope_id"]:
            continue
        for name, node in run["nodes"].items():
            if (
                node["status"] == "waiting_for_review"
                and node.get("review_decision") is None
                and node.get("attempt_id")
            ):
                found.append((run, name))
    return found


class AlreadyConsumed(ConflictError):
    pass


class SelectionChanged(ConflictError):
    pass


def guard_reply(con, run, node_id, reply):
    if consumed(con, reply["identity"]):
        raise AlreadyConsumed("reply already consumed")
    waiting = candidates(con, reply)
    if len(waiting) != 1 or (waiting[0][0]["run_id"], waiting[0][1]) != (run["run_id"], node_id):
        raise SelectionChanged("review selection changed")
    node = run["nodes"][node_id]
    if reply["timestamp"] < node.get("started_at", math.inf):
        raise ConflictError("reply predates this review")
    if reply["quoted"] and reply["marker"] != node["attempt_id"]:
        raise ConflictError("reply does not reference this review attempt")


class ChatReviews:
    def __init__(self, runtime):
        self.runtime = runtime

    def _context(self, event, gateway, session_store):
        text = getattr(event, "text", "")
        if not isinstance(text, str) or not text.strip() or text.lstrip().startswith("/"):
            return None
        is_command = getattr(event, "is_command", None)
        if callable(is_command) and is_command():
            return None
        if (
            getattr(event, "internal", False)
            or not getattr(event, "allow_gateway_control", True)
            or getattr(event, "prompt_response", None) is not None
            or getattr(event, "edit_revision", None) is not None
        ):
            return None
        source = getattr(event, "source", None)
        origin = build_origin(source)
        if (
            not origin
            or getattr(source, "is_bot", False)
            or getattr(source, "profile_route_rejected", False)
        ):
            return None
        host = self.runtime.origins.host
        # This hook is BEFORE native authorization. Never infer permission from
        # origin membership, a captured cache entry, or a public message field.
        if Path(host.loader("hermes_constants").get_hermes_home()).resolve() != host.home:
            return None
        resolve = getattr(gateway, "_resolve_profile_home_for_source", None)
        if not callable(resolve) or Path(resolve(source)).resolve() != host.home:
            return None
        authority = getattr(gateway, "_authorization_home_for_source", None)
        authority_home = authority(source) if callable(authority) else None
        if authority_home is not None and Path(authority_home).resolve() != host.home:
            return None
        authorize = getattr(gateway, "_is_user_authorized_for_source", None)
        if not callable(authorize) or authorize(source) is not True:
            return None
        binding = self.runtime.origins.binding
        if not binding or binding[0] is not gateway:
            return None
        key_fn = getattr(gateway, "_session_key_for_source", None)
        if not callable(key_fn):
            key_fn = getattr(session_store, "_generate_session_key", None)
        key = key_fn(source) if callable(key_fn) else None
        if not isinstance(key, str) or not key:
            return None
        if self.runtime.origins.origin_for(key) != origin:
            return None
        metadata = self.runtime.origins.routing_for(key).get("delivery_metadata")
        # Pending native controls own their replies, even if a workflow happens
        # to be waiting in this chat. Missing host APIs degrade to manual review.
        if host.loader("tools.approval").has_blocking_approval(key):
            return None
        if host.loader("tools.slash_confirm").get_pending(key):
            return None
        if host.loader("tools.clarify_gateway").get_pending_for_session(
            key, include_choice_prompts=True
        ):
            return None
        if gateway._is_session_running(key):
            return None
        state = gateway._peek_session_state(key)
        if state is not None and state.persistent.update_prompt_pending:
            return None
        stamp = getattr(event, "timestamp", None)
        if not isinstance(stamp, datetime):
            return None
        timestamp = stamp.timestamp()
        if not math.isfinite(timestamp) or timestamp > time.time() + 5:
            return None
        message_id = getattr(event, "message_id", None) or getattr(source, "message_id", None)
        identity = hashlib.sha256(
            json.dumps([origin, key, str(message_id) if message_id else stamp.isoformat()]).encode()
        ).hexdigest()
        quote = getattr(event, "reply_to_text", None) or ""
        markers = MARKER.findall(quote)
        return {
            "identity": identity,
            "origin": origin,
            "session_key": key,
            "home": host.home,
            "scope_id": str(source.scope_id) if getattr(source, "scope_id", None) else None,
            "timestamp": timestamp,
            "quoted": bool(getattr(event, "reply_to_message_id", None) or quote),
            "marker": markers[0] if len(markers) == 1 else None,
            "metadata": metadata,
        }

    def resolve(self, text, reply):
        with self.runtime.db.connect() as con:
            if consumed(con, reply["identity"]):
                return handled(reply["identity"]), None
            waiting = candidates(con, reply)
        if len(waiting) != 1:
            return None, None
        run, node_id = waiting[0]
        if run["notification_context"].get("routing", {}).get("session_key") is None:
            return None, None  # Older runs remain available through manual review.
        node = run["nodes"][node_id]
        decision, note = interpret_reply(text)
        if decision is None:
            return None, None
        try:
            self.runtime.engine.review(
                run["run_id"],
                node_id,
                decision,
                note,
                attempt_id=node["attempt_id"],
                _chat_reply=reply,
            )
        except AlreadyConsumed:
            return handled(reply["identity"]), None
        except SelectionChanged:
            return None, None
        except Exception:
            with self.runtime.db.connect() as con:
                if consumed(con, reply["identity"]):
                    return handled(reply["identity"]), None
            # Never return raw exception text: native errors can contain secrets.
            message = (
                f"Could not resolve gate '{node_id}' of run {run['run_id']}. "
                "It may have changed, the reply may be stale, or that decision may not be allowed. "
                "Check the current review in the dashboard or use /workflow review."
            )
        else:
            message = f"Gate '{node_id}' of run {run['run_id']} resolved as {decision}."
        return handled(reply["identity"]), message

    async def route(self, event=None, gateway=None, session_store=None):
        try:
            reply = self._context(event, gateway, session_store)
            if reply is None:
                return None
            sender = self.runtime.notifications.sender
            snapshot = getattr(sender, "snapshot", None)
            send = snapshot() if callable(snapshot) else sender.send
            # SQLite work must not hold the gateway event loop while waiting on
            # another workflow host's write lock.
            result, message = await asyncio.to_thread(self.resolve, event.text, reply)
        except Exception:
            log.debug("workflow chat review unavailable")
            return None
        if message:
            try:
                sent = await asyncio.wait_for(
                    send(
                        reply["origin"],
                        message,
                        "review-" + reply["identity"],
                        metadata=reply["metadata"],
                    ),
                    timeout=5,
                )
                if not sent:
                    log.warning("workflow review confirmation was not acknowledged")
            except Exception:
                log.warning("workflow review confirmation unavailable; decision is not replayed")
        return result
