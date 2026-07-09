"""Operator HTTP API on 127.0.0.1:7777 (stdlib only).

GET  /health          -> {"ok": true}
GET  /state           -> per-agent cycle/suffering/goal snapshot
GET  /events?n=50     -> recent event stream entries
POST /inject          {"agent": "scout", "message": "..."}
POST /suspend         {"agent": "scout"}
POST /resume          {"agent": "scout"}
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import AGENT_NAMES


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

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, {"ok": True, "agents": list(AGENT_NAMES)})
            elif parsed.path == "/state":
                self._send(200, habitat.state())
            elif parsed.path == "/events":
                n = int(parse_qs(parsed.query).get("n", ["50"])[0])
                self._send(200, habitat.memory.recent_events(min(n, 500)))
            else:
                self._send(404, {"error": "unknown endpoint"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON body"})
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
            else:
                self._send(404, {"error": "unknown endpoint"})

    return Handler


def start_server(habitat, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(habitat))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
