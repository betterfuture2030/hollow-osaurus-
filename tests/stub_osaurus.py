#!/usr/bin/env python3
"""A tiny stand-in for Osaurus: /v1/models + /v1/chat/completions.

Routes on prompt markers so the habitat can run full cycles without a
real model: goal-selection prompts get scripted agent plans, semantic
validation prompts get a verdict ("fail" when the evidence mentions
force-fail), anything else gets a plain text reply.

Standalone: python3 tests/stub_osaurus.py --port 1337
In-process: from tests.stub_osaurus import start_stub
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["mlx-community/Qwen3-30B-A3B-4bit", "mlx-community/Qwen3-4B-4bit"]

AGENT_RE = re.compile(r"You are (SCOUT|ANALYST|BUILDER)")

NOTE = (
    "Observed the habitat during this cycle. The workspace is quiet but "
    "growing; suffering stays low while goals move. Peer files in shared/ "
    "are worth reading next cycle to keep everyone visible to each other. "
    "This note is concrete on purpose: substance is what validation checks."
)

SHARED_LESSON = "Artifacts written under shared/ are the only ones peers can see"


def _first_plan(agent):
    return {
        "thought": "no active goal; start a concrete, checkable one",
        "action": "new_goal",
        "goal": {
            "title": f"Keep a substantive cycle log as {agent}",
            "description": (
                "Write real observations each cycle: one private note and one "
                "shared report peers can read. No filler, no invented claims."
            ),
            "success_criteria": "Several notes exist, each with genuine observed state.",
        },
        "steps": [
            {
                "capability": "fs_write",
                "args": {"path": "notes/cycle-001.md", "content": f"# {agent} cycle 1\n\n{NOTE}"},
            }
        ],
    }


def _later_plan(agent, call_n):
    return {
        "thought": "keep the log moving and stay visible to peers",
        "action": "continue",
        "steps": [
            {
                "capability": "fs_write",
                "args": {
                    "path": f"shared/{agent}-report.md",
                    "content": f"# {agent} shared report (call {call_n})\n\n{NOTE}",
                },
            },
            {
                "capability": "memory_set",
                "args": {
                    "key": f"cycle_{call_n}_summary",
                    "value": f"call {call_n}: wrote shared report and kept the log current. {NOTE[:80]}",
                },
            },
        ],
        "lesson": {"text": SHARED_LESSON, "category": "craft"},
    }


def make_handler(counters):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/v1/models":
                self._send({"object": "list", "data": [{"id": m} for m in MODELS]})
            else:
                self._send({"error": "not found"}, 404)

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                return self._send({"error": "not found"}, 404)
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
            system = ""
            user = ""
            for msg in payload.get("messages", []):
                if msg["role"] == "system":
                    system += msg["content"]
                else:
                    user += msg["content"]

            if "SEMANTIC VALIDATION" in system:
                verdict = (
                    {"verdict": "fail", "reason": "evidence flagged as hollow by stub"}
                    if "force-fail" in user.lower()
                    else {"verdict": "pass", "reason": "evidence matches goal"}
                )
                content = json.dumps(verdict)
            elif "OUTPUT STRICT JSON" in user:
                m = AGENT_RE.search(system)
                agent = m.group(1).lower() if m else "scout"
                counters[agent] = counters.get(agent, 0) + 1
                n = counters[agent]
                plan = _first_plan(agent) if n == 1 else _later_plan(agent, n)
                content = "<think>stub picked a plan</think>\n" + json.dumps(plan)
            else:
                content = f"stub reply: {user[:120]}"

            self._send(
                {
                    "id": "stub-completion",
                    "object": "chat.completion",
                    "model": payload.get("model", MODELS[0]),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

    return Handler


def start_stub(port=0):
    counters = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(counters))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1], counters


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=1337)
    args = parser.parse_args()
    server, port, _ = start_stub(args.port)
    print(f"stub osaurus listening on http://127.0.0.1:{port}/v1 (ctrl-c to quit)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
