"""Standard-library HTTP sidecar for hosts without third-party plugin routers."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .api import API, MAX_BODY, APIError, decode_body


def make_server(host="127.0.0.1", port=9123, *, api: API | None = None, token: str | None = None):
    api = api or API()
    dashboard = Path(__file__).resolve().parent.parent / "dashboard"

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            if token and not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.respond(401, {"detail": "unauthorized"})
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise APIError(400, "chunked request bodies are not supported")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise APIError(400, "invalid Content-Length") from exc
                if length < 0 or length > MAX_BODY:
                    raise APIError(413, "request body too large")
                if self.command in ("GET", "HEAD") and urlsplit(self.path).path.startswith(
                    "/dashboard/"
                ):
                    relative = unquote(urlsplit(self.path).path)[len("/dashboard/") :]
                    target = (dashboard / relative).resolve()
                    if (
                        not target.is_relative_to(dashboard.resolve())
                        or not target.is_file()
                        or target.suffix not in (".json", ".js", ".css")
                    ):
                        raise APIError(404, "asset not found")
                    data = target.read_bytes()
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        mimetypes.guess_type(str(target))[0] or "application/octet-stream",
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(data)
                    return
                self.connection.settimeout(15)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise APIError(400, "incomplete body")
                status, data = api.handle(self.command, self.path, decode_body(raw))
                self.respond(status, data)
            except APIError as exc:
                self.respond(exc.status, {"detail": exc.detail})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:
                logging.getLogger(__name__).exception("workflow API request failed")
                self.respond(500, {"detail": "internal server error"})

        def respond(self, status, data):
            encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(encoded)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_request

        def log_message(self, fmt, *args):
            logging.getLogger(__name__).info(fmt, *args)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    return Server((host, port), Handler)
