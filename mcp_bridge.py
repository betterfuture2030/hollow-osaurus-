#!/usr/bin/env python3
"""MCP server for the Hollow Claude bridge (stdlib only).

Exposes the agents' substrate change-request queue as MCP tools over
stdio, so a Claude Code session in this repo sees pending requests as
native tools instead of reading jsonl files by hand:

    list_pending_requests   -> all unanswered agent requests
    get_request             -> full detail for one request id
    respond_to_request      -> append an implemented/rejected verdict
    habitat_state           -> on-disk snapshot (works while habitat is down)

Registered via .mcp.json at the repo root. Speaks JSON-RPC 2.0 over
stdin/stdout (MCP protocol version 2024-11-05 shape).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from substrate import AGENT_NAMES  # noqa: E402
from substrate.claude_bridge import ClaudeBridge  # noqa: E402
from substrate.memory import Memory, append_jsonl, read_json, read_jsonl  # noqa: E402

SERVER_INFO = {"name": "hollow-bridge", "version": "1.0.0"}
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "list_pending_requests",
        "description": (
            "List all unanswered substrate change-requests filed by the Hollow "
            "agents via invoke_claude. Answer each by implementing or rejecting "
            "it with respond_to_request."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_request",
        "description": "Fetch the full detail of one agent request by its request_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "respond_to_request",
        "description": (
            "Record the verdict for an agent request: status 'implemented' or "
            "'rejected' plus a response explaining what was done or why not. "
            "The verdict surfaces in the requesting agent's next cycle."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "status": {"type": "string", "enum": ["implemented", "rejected"]},
                "response": {"type": "string", "minLength": 10},
            },
            "required": ["request_id", "status", "response"],
            "additionalProperties": False,
        },
    },
    {
        "name": "habitat_state",
        "description": (
            "Snapshot of each agent's suffering and goals, read from disk — "
            "works whether or not the habitat process is running."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _bridge():
    return ClaudeBridge(Memory(ROOT))


def tool_list_pending_requests(_args):
    bridge = _bridge()
    pending = [r for a in AGENT_NAMES for r in bridge.pending(a)]
    return {"pending": pending, "count": len(pending)}


def tool_get_request(args):
    rid = args.get("request_id", "")
    for r in read_jsonl(ROOT / "memory" / "claude_requests.jsonl"):
        if r.get("request_id") == rid:
            return r
    return {"error": f"no request with id {rid!r}"}


def tool_respond_to_request(args):
    rid = args["request_id"]
    bridge = _bridge()
    known = {r.get("request_id") for r in read_jsonl(bridge.requests_path)}
    if rid not in known:
        return {"error": f"no request with id {rid!r}"}
    answered = {r.get("request_id") for r in read_jsonl(bridge.responses_path)}
    if rid in answered:
        return {"error": f"request {rid} was already answered"}
    append_jsonl(
        bridge.responses_path,
        {"request_id": rid, "status": args["status"], "response": args["response"][:2000]},
    )
    return {"ok": True, "detail": f"verdict recorded; it surfaces in the agent's next cycle"}


def tool_habitat_state(_args):
    out = {}
    for agent in AGENT_NAMES:
        suffering = read_json(ROOT / "memory" / "suffering" / f"{agent}.json", {})
        goals = {}
        for snap in read_jsonl(ROOT / "memory" / "goals" / agent / "registry.jsonl"):
            goals[snap["id"]] = snap
        active = next((g for g in goals.values() if g["status"] == "active"), None)
        out[agent] = {
            "suffering": {k: v.get("severity") for k, v in suffering.items()},
            "active_goal": {
                "title": active["title"],
                "progress": active["progress"],
                "validation_failures": active["validation_failures"],
            } if active else None,
            "goals_completed": sum(1 for g in goals.values() if g["status"] == "completed"),
            "goals_abandoned": sum(1 for g in goals.values() if g["status"] == "abandoned"),
        }
    return out


HANDLERS = {
    "list_pending_requests": tool_list_pending_requests,
    "get_request": tool_get_request,
    "respond_to_request": tool_respond_to_request,
    "habitat_state": tool_habitat_state,
}


def handle(msg):
    method = msg.get("method", "")
    params = msg.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name", "")
        if name not in HANDLERS:
            raise ValueError(f"unknown tool: {name}")
        result = HANDLERS[name](params.get("arguments") or {})
        return {"content": [{"type": "text", "text": json.dumps(result, indent=1)}]}
    if method == "ping":
        return {}
    raise ValueError(f"unknown method: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in msg:  # notification (e.g. notifications/initialized): no reply
            continue
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        try:
            reply["result"] = handle(msg)
        except Exception as e:  # noqa: BLE001 - protocol boundary
            reply["error"] = {"code": -32603, "message": str(e)}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
