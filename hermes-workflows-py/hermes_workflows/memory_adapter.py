"""The supported Hermes record API; no provider-specific implementation lives here."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from dataclasses import asdict
from pathlib import Path

from .workflow_memory import REASONS, Destination, MemoryUnavailable, Receipt

METHODS = {
    "status": "memory_record_status",
    "describe": "describe_memory_writer",
    "write": "write_memory_record",
    "lookup": "lookup_memory_record",
}


class HermesMemorySink:
    def __init__(
        self, home, writer_context, *, context=None, loader=importlib.import_module, timeout=5
    ):
        self.home = Path(home).resolve()
        self.writer_context = json.loads(json.dumps(writer_context))
        self.context = context
        self.loader = loader
        self.timeout = timeout
        self.capabilities = {}
        self._client = None

    def _call(self, operation, *args, **kwargs):
        if self.context is not None:
            method = getattr(self.context, METHODS[operation], None)
        else:
            if self._client is None:
                try:
                    client = self.loader("hermes_cli.plugin_memory_records").MemoryRecordClient
                except (ImportError, AttributeError):
                    raise MemoryUnavailable() from None
                self._client = client(self.home, "hermes-workflows")
            method = getattr(self._client, operation, None)
        if not callable(method):
            raise MemoryUnavailable()
        return method(*args, **kwargs)

    def status(self):
        """Configuration/capability metadata only; never bind or initialize a provider."""
        try:
            response = self._call("status")
        except (MemoryUnavailable, OSError, RuntimeError):
            return {"available": False, "reason": "capability_unavailable", "provider": None}
        if not isinstance(response, dict) or response.get("api_version") != 1:
            return {"available": False, "reason": "invalid_response", "provider": None}
        provider = response.get("provider")
        if provider is not None and (
            not isinstance(provider, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", provider)
        ):
            return {"available": False, "reason": "invalid_response", "provider": None}
        reason = response.get("reason")
        return {
            "available": response.get("status") != "unavailable",
            "provider": provider,
            "consented": response.get("consented") is True,
            "provider_support": "unprobed",
            **({"reason": reason} if isinstance(reason, str) and reason in REASONS else {}),
        }

    def bind(self):
        response = self._call("describe", self.writer_context, timeout=self.timeout)
        if not isinstance(response, dict):
            raise MemoryUnavailable("invalid_response")
        if response.get("status") != "ready":
            raise MemoryUnavailable(response.get("reason"))
        if response.get("api_version") != 1:
            raise MemoryUnavailable("capability_unavailable")
        try:
            destination = Destination(**response["destination"])
        except (ValueError, TypeError, KeyError):
            raise MemoryUnavailable("invalid_response") from None
        if Path(destination.home) != self.home:
            raise MemoryUnavailable("wrong_profile")
        capabilities = response.get("capabilities", {})
        if not isinstance(capabilities, dict) or capabilities.get("write") is not True:
            raise MemoryUnavailable("capability_unavailable")
        self.capabilities = {
            key: capabilities.get(key) is True for key in ("write", "lookup", "idempotency")
        }
        resolved = response.get("context")
        if resolved is not None:
            allowed = {"session_id", "cwd", "platform", "user_id", "user_id_alt", "parent_session_id", "scheduled"}
            if not isinstance(resolved, dict) or set(resolved) - allowed or any(
                type(value) is not bool if key == "scheduled" else not isinstance(value, str)
                for key, value in resolved.items()
            ):
                raise MemoryUnavailable("invalid_response")
            if any(resolved.get(key) != value for key, value in self.writer_context.items()
                   if key != "cwd" or value):
                raise MemoryUnavailable("invalid_response")
            self.writer_context = json.loads(json.dumps(resolved))
        return destination

    async def describe(self):
        return await asyncio.to_thread(self.bind)

    @staticmethod
    def _receipt(response):
        if not isinstance(response, dict):
            raise ValueError("invalid memory receipt")
        reason = response.get("reason")
        return Receipt(
            response.get("status"),
            response.get("receipt_id"),
            reason if isinstance(reason, str) and reason in REASONS else None,
        )

    def _destination(self, destination):
        if not isinstance(destination, Destination) or Path(destination.home) != self.home:
            raise MemoryUnavailable("wrong_profile")
        return asdict(destination)

    async def write(self, record, *, effect_id, expected_destination, timeout):
        destination = self._destination(expected_destination)
        response = await asyncio.to_thread(
            self._call,
            "write",
            record,
            context=self.writer_context,
            effect_id=effect_id,
            expected_destination=destination,
            timeout=timeout,
        )
        return self._receipt(response)

    async def lookup(self, *, effect_id, expected_destination, timeout):
        destination = self._destination(expected_destination)
        response = await asyncio.to_thread(
            self._call,
            "lookup",
            context=self.writer_context,
            effect_id=effect_id,
            expected_destination=destination,
            timeout=timeout,
        )
        if isinstance(response, dict) and (
            response.get("status") == "unavailable"
            or response.get("reason")
            in ("destination_changed", "selection_changed", "deadline_before_acceptance")
        ):
            return Receipt("unknown")
        return self._receipt(response)
