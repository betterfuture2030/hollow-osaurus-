"""Operator HTTP API on 127.0.0.1:7777 (stdlib only).

GET  /health          -> {"ok": true}
GET  /state           -> per-agent cycle/suffering/goal snapshot
GET  /events?n=50     -> recent event stream entries
GET  /panel           -> the operator panel UI (panel.html)
POST /inject          {"agent": "scout", "message": "..."}
POST /suspend         {"agent": "scout"}
POST /resume          {"agent": "scout"}
POST /stressor        {"agent": "scout", "kind": "stagnation", "severity": 0.4}
POST /nuke            {"confirm": true}   -- wipe ALL runtime state
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import AGENT_NAMES
from .suffering import STRESSOR_TYPES

PANEL_PATH = Path(__file__).resolve().parent.parent / "panel.html"


def make_handler(habitat):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, code, text):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, {"ok": True, "agents": list(AGENT_NAMES)})
            elif parsed.path == "/state":
                self._send(200, habitat.state())
            elif parsed.path == "/events":
                n = int(parse_qs(parsed.query).get("n", ["50"])[0])
                self._send(200, habitat.memory.recent_events(min(n, 500)))
            elif parsed.path in ("/panel", "/"):
                if PANEL_PATH.is_file():
                    self._send_html(200, PANEL_PATH.read_text(encoding="utf-8"))
                else:
                    self._send(404, {"error": "panel.html not found"})
            else:
                self._send(404, {"error": "unknown endpoint"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON body"})

            if self.path == "/nuke":
                if payload.get("confirm") is not True:
                    return self._send(400, {"error": "nuke requires {\"confirm\": true}"})
                habitat.nuke()
                return self._send(200, {"ok": True, "detail": "world reset"})

            agent = payload.get("agent", "")
            if agent not in AGENT_NAMES:
                return self._send(400, {"error": f"unknown agent: {agent!r}"})
            if self.path == "/inject":
                message = str(payload.get("message", "")).strip()
                if not message:
                    return self._send(400, {"error": "message required"})
                habitat.inject(agent, message)
                self._send(200, {"ok": True})
            elif self.path == "/suspend":
                habitat.suspend(agent)
                self._send(200, {"ok": True})
            elif self.path == "/resume":
                habitat.resume(agent)
                self._send(200, {"ok": True})
            elif self.path == "/stressor":
                kind = str(payload.get("kind", ""))
                if kind not in STRESSOR_TYPES:
                    return self._send(400, {"error": f"unknown stressor: {kind!r}"})
                try:
                    severity = float(payload.get("severity", -1))
                except (TypeError, ValueError):
                    return self._send(400, {"error": "severity must be a number"})
                habitat.suffering[agent].set_stressor(kind, severity)
                habitat.memory.event(
                    agent, "control", f"operator set stressor {kind} to {max(0.0, severity):g}"
                )
                self._send(200, {"ok": True, "suffering": habitat.suffering[agent].summary()})
            else:
                self._send(404, {"error": "unknown endpoint"})

    return Handler


def start_server(habitat, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(habitat))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
