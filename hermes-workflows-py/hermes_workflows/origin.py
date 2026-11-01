"""Profile-local gateway bindings; never store a process-global delivery router."""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path


def build_origin(source):
    platform = getattr(source, "platform", None)
    platform = getattr(platform, "value", platform)
    chat = getattr(source, "chat_id", None)
    thread = getattr(source, "thread_id", None)
    if not platform or not chat:
        return None
    return f"{platform}:{chat}" + (f":{thread}" if thread else "")


class Origins:
    def __init__(self, host):
        self.host = host
        self._origins = OrderedDict()
        self._lock = threading.RLock()
        self.binding = None

    async def capture(self, event=None, gateway=None, session_store=None, **_kwargs):
        try:
            # Hooks may share a gateway process. Only accept the home actually
            # selected by the host, before entering any adapter override.
            current = Path(self.host.loader("hermes_constants").get_hermes_home()).resolve()
            if current != self.host.home:
                return
            source = getattr(event, "source", None)
            authority = getattr(gateway, "_authorization_home_for_source", None)
            authority_home = authority(source) if callable(authority) else None
            if authority_home is not None and Path(authority_home).resolve() != self.host.home:
                return
            loop = asyncio.get_running_loop()
            origin = build_origin(source)
            with self._lock:
                if gateway is not None:
                    self.binding = (gateway, loop)
                if origin is None:
                    return
                for obj in (gateway, session_store):
                    for method in ("_session_key_for_source", "_generate_session_key"):
                        fn = getattr(obj, method, None)
                        if not callable(fn):
                            continue
                        key = fn(source)
                        if not isinstance(key, str) or not key:
                            continue
                        metadata = {
                            k: str(getattr(source, k))
                            for k in ("thread_id", "chat_type", "scope_id", "parent_chat_id")
                            if getattr(source, k, None)
                        }
                        if (
                            origin.startswith("telegram:")
                            and getattr(source, "thread_id", None)
                            and getattr(source, "chat_type", "dm").lower()
                            in ("dm", "direct", "private")
                        ):
                            metadata["telegram_dm_topic_reply_fallback"] = True
                            if str(source.thread_id) != "1":
                                metadata["direct_messages_topic_id"] = str(source.thread_id)
                            if getattr(source, "message_id", None):
                                metadata["telegram_reply_to_message_id"] = str(source.message_id)
                        routing = {
                            k: str(getattr(source, k))
                            for k in ("user_id", "user_id_alt", "chat_type")
                            if getattr(source, k, None)
                        }
                        routing["delivery_metadata"] = metadata
                        self._origins[key] = {"origin": origin, "routing": routing}
                        self._origins.move_to_end(key)
                        while len(self._origins) > 2048:
                            self._origins.popitem(last=False)
                        return
        except Exception:
            return  # Capture must never interfere with gateway admission.

    def origin_for(self, task_id):
        with self._lock:
            return (
                self._origins.get(task_id, {}).get("origin") if isinstance(task_id, str) else None
            )

    def routing_for(self, task_id):
        with self._lock:
            return (
                dict(self._origins.get(task_id, {}).get("routing", {}))
                if isinstance(task_id, str)
                else {}
            )

    def clear(self):
        with self._lock:
            self._origins.clear()
            self.binding = None


class NativeSender:
    def __init__(self, origins):
        self.origins = origins

    def _binding(self):
        binding = self.origins.binding
        router = getattr(binding[0], "delivery_router", None) if binding else None
        output_dir = getattr(router, "output_dir", None)
        if (
            output_dir is not None
            and Path(output_dir).resolve() != self.origins.host.home / "cron" / "output"
        ):
            return None
        if binding and binding[1].is_running() and callable(getattr(router, "deliver", None)):
            return router, binding[1]
        return None

    def available(self):
        return self._binding() is not None

    async def send(self, target, text, identity, *, metadata=None):
        binding = self._binding()
        if binding is None:
            return False
        # Keep the router we actually validated, including across a gateway
        # rebind/unload while its coroutine waits for the owning event loop.
        router, loop = binding
        host = self.origins.host

        async def deliver():
            with host.scope():
                parsed = host.module("delivery").DeliveryTarget.parse(target)
                if (
                    getattr(parsed, "chat_id", True) is None
                    and not getattr(parsed, "unknown_platform", None)
                    and getattr(parsed.platform, "value", "local") != "local"
                ):
                    home = router.config.get_home_channel(parsed.platform)
                    if home is not None:
                        parsed = replace(
                            parsed, chat_id=home.chat_id, thread_id=getattr(home, "thread_id", None)
                        )
                results = await router.deliver(
                    text, [parsed], job_id="workflow-" + identity, metadata=metadata
                )
                # Native results can wrap a failed SendResult in success=True.
                result = results.get(parsed.to_string(), {})
                inner = result.get("result")
                success = (
                    inner.get("success", True)
                    if isinstance(inner, dict)
                    else getattr(inner, "success", True)
                )
                delivered = (
                    inner.get("delivered", True)
                    if isinstance(inner, dict)
                    else getattr(inner, "delivered", True)
                )
                return (
                    result.get("success") is True
                    and success is not False
                    and delivered is not False
                )

        if asyncio.get_running_loop() is loop:
            return await deliver()
        future = asyncio.run_coroutine_threadsafe(deliver(), loop)
        try:
            return await asyncio.wrap_future(future)
        finally:
            if not future.done():
                future.cancel()
