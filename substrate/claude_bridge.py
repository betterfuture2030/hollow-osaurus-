"""invoke_claude: a queue where agents request substrate changes.

Agents append well-formed requests to memory/claude_requests.jsonl; the
operator (via Claude Code on the host Mac) appends verdicts to
memory/claude_responses.jsonl. Quality gates stop vague or circular
requests before they ever reach the operator.
"""

import uuid

from .memory import Memory, append_jsonl, now_iso, read_json, read_jsonl, write_json

MIN_DESCRIPTION = 40
MIN_SPEC = 80
MAX_PENDING_PER_AGENT = 3

CIRCULAR_MARKERS = (
    "claude_requests",
    "claude_responses",
    "request queue",
    "response queue",
    "invoke_claude queue",
)


class ClaudeBridge:
    def __init__(self, memory: Memory):
        self.memory = memory
        self.requests_path = memory.dir / "claude_requests.jsonl"
        self.responses_path = memory.dir / "claude_responses.jsonl"
        self.surfaced_path = memory.dir / "claude_surfaced.json"

    def _requests(self):
        return read_jsonl(self.requests_path)

    def _responses(self):
        return read_jsonl(self.responses_path)

    def pending(self, agent: str) -> list:
        answered = {r.get("request_id") for r in self._responses()}
        return [
            r
            for r in self._requests()
            if r["agent_id"] == agent and r["request_id"] not in answered
        ]

    def submit(self, agent: str, description: str, spec: str, request_type: str = "modification"):
        """Returns (ok, message). Enforces the wiki's quality gates."""
        if not agent:
            return False, "agent_id is mandatory"
        description = (description or "").strip()
        spec = (spec or "").strip()
        if len(description) < MIN_DESCRIPTION:
            return False, f"description too vague: needs >= {MIN_DESCRIPTION} chars, got {len(description)}"
        if len(spec) < MIN_SPEC:
            return False, f"spec lacks substance: needs >= {MIN_SPEC} chars, got {len(spec)}"
        combined = (description + " " + spec).lower()
        if any(marker in combined for marker in CIRCULAR_MARKERS):
            return False, "circular request: agents cannot ask Claude to manage the request queue itself"
        if len(self.pending(agent)) >= MAX_PENDING_PER_AGENT:
            return False, f"queue cap reached: {MAX_PENDING_PER_AGENT} pending requests already"

        request = {
            "request_id": uuid.uuid4().hex[:12],
            "agent_id": agent,
            "timestamp": now_iso(),
            "description": description,
            "spec": spec,
            "request_type": request_type,
            "status": "pending",
        }
        append_jsonl(self.requests_path, request)
        return True, request["request_id"]

    def new_responses(self, agent: str) -> list:
        """Responses addressed to this agent that haven't been surfaced yet."""
        surfaced = set(read_json(self.surfaced_path, []))
        by_id = {r["request_id"]: r for r in self._requests()}
        fresh = []
        for resp in self._responses():
            rid = resp.get("request_id")
            req = by_id.get(rid)
            if req and req["agent_id"] == agent and rid not in surfaced:
                fresh.append({**resp, "description": req["description"]})
                surfaced.add(rid)
        if fresh:
            write_json(self.surfaced_path, sorted(surfaced))
        return fresh
