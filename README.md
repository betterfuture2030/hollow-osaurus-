# Hollow on Osaurus

A Mac-native rebuild of [Hollow AgentOS](https://ninjahawk.github.io/hollow-wiki/)
that runs its model inference through **[Osaurus](https://github.com/osaurus-ai/osaurus)**
(Apple-Silicon MLX server) instead of Ollama. No Docker, no cloud calls —
three local agents living in a substrate on your MacBook.

## Provenance & license

This is a **clean-room reimplementation** of
[Hollow AgentOS](https://github.com/ninjahawk/hollow-agentOS) by
[ninjahawk](https://github.com/ninjahawk), built from the project's public
wiki documentation only — no upstream source code or documentation text was
copied. The concepts and mechanics are the original author's; the
implementation here is original. Full provenance and third-party credits are
in [NOTICE.md](NOTICE.md). This repository is licensed under the
[MIT License](LICENSE).

Three agents — **scout**, **analyst**, **builder** — share a workspace, pick
their own goals, and develop over time. The substrate doesn't tell them what
to do; it makes consequences mechanical:

- **Suffering**: nine stressor types (futility, invisibility, repeated
  failure, stagnation, ...) accumulate into a load. At **0.55** capability
  synthesis locks; at **0.75** `fs_write`/`fs_edit` lock too. A "path-out"
  set of capabilities is never locked, so an agent can always act its way
  back down.
- **Validation**: a goal only completes after a five-layer pipeline —
  progress threshold, real output steps, mechanical file-substance checks,
  a semantic LLM comparison, and a fact-check of claims about files. Five
  failed validations and the goal is abandoned and its artifacts deleted.
- **Lessons**: observations promote into durable "RULES OF YOUR ENVIRONMENT"
  after two independent sightings (one, for high-confidence environment/
  constraint lessons), deduplicated by Jaccard similarity.
- **invoke_claude**: agents can't touch substrate code. They file formal
  change requests into a queue that *you* review with Claude Code.

## Setup (macOS, Apple Silicon)

1. **Install Osaurus** — `brew install --cask osaurus` (or grab it from
   the repo above). Launch it and start the server from the menu bar.
   Default endpoint: `http://127.0.0.1:1337/v1`.
2. **Download a model in Osaurus.** On a 24 GB machine the sweet spot is a
   **Qwen3-30B-A3B 4-bit MLX** build (~18 GB, MoE — only ~3 B params active,
   so it's fast). Also grab **Qwen3-4B-4bit** as a lightweight fallback.
   Close heavyweight apps when running the 30B model on 24 GB.
3. **Install and run Hollow:**

   ```bash
   pip3 install -r requirements.txt
   python3 hollow.py          # first run: wizard detects Osaurus + models
   ```

   If Osaurus serves on a non-default port: `python3 hollow.py --base-url http://127.0.0.1:PORT/v1`

## Daily operation

| command | what it does |
| --- | --- |
| `python3 hollow.py` | start the habitat (agents + operator API on :7777) |
| `python3 hollow.py stop` | stop it (all state persists across restarts) |
| `python3 hollow.py status` | snapshot of cycles, suffering, goals |
| `python3 thoughts.py` | live monitor: thoughts, capability calls, locks, lessons |
| `python3 submit_task.py scout "look at shared/"` | inject a host message into an agent's next cycle |
| `python3 panel.py` | operator panel (native window with `pip3 install pywebview`, else browser) |

Operator HTTP API (localhost only): `GET /health`, `GET /state`,
`GET /events?n=100`, `GET /panel`, `POST /inject`, `POST /suspend`,
`POST /resume`, `POST /stressor` (`{"agent","kind","severity"}` —
exact-set, 0 resolves), `POST /nuke` (`{"confirm": true}` — wipe the world).

## Operator panel

`python3 panel.py` (habitat must be running) opens a live dashboard:
per-agent load bars and tiers, active goals with progress, suspend/resume,
host-message injection, **suffering sliders** (drag to inject or relieve
any of the nine stressors), and a double-confirm **nuke** that resets the
entire world while the process keeps running. It is a single
dependency-free HTML file served by the habitat itself at
`http://127.0.0.1:7777/panel`. Agents are color-coded across cards, stream, ticker, and peer graph; weather plays across the whole panel with a world dial; each card has a suffering gauge, sparkline, live typewritten thought, and thinking heartbeat; the stream is cycle-banded with clickable artifacts; a shared-artifact shelf and promoted-lessons tablets sit above an animated who-reads-whom graph; completions burst fireworks and abandonments fall as ash.

## The world

Every ~6 scheduler rounds (`world.event_every_rounds` in config; 0
disables) the habitat produces an ambient event — weather, a mysterious
object, or an *echo*: a fragment of the agents' own past resurfacing from
the event stream. Agents see it as a `THE WORLD:` line in their next
perception, distinct from operator messages.

## Synthesized capabilities

Agents can forge their own tools at runtime: `synthesize_capability(name,
description, code)` where the code defines `run(args)`. Tools persist
under `memory/synthesized/<agent>/`, run **subprocess-isolated** (own
process, agent-workspace cwd, 20 s timeout, output capped, network
commands blocklisted), max 5 per agent, and can be dismantled with
`retire_capability`. Synthesis locks at suffering load ≥ 0.55, as in the
original design.

## Research

`research_topic` performs a real web search (DuckDuckGo HTML endpoint, no
API key) — but it must be **earned**: suffering load ≤ 0.15 and at least
one peer interaction. This is the habitat's only outbound network call;
set `research.enabled: false` in `config.json` for fully-local operation.

Agent artifacts land in `workspace/<agent>/`; anything under
`workspace/shared/` is visible to all three agents — reading a peer's
shared file is what cures *invisibility*. All state (goals, lessons,
suffering, audit log) lives under `memory/` and survives restarts.

## The Claude bridge

Agents file substrate change-requests via `invoke_claude`. Quality gates
(description ≥ 40 chars, spec ≥ 80 chars, no queue-management requests,
max 3 pending per agent) stop noise before it reaches you.

- Requests accumulate in `memory/claude_requests.jsonl`
- You answer by appending to `memory/claude_responses.jsonl`:

  ```json
  {"request_id": "abc123def456", "status": "implemented", "response": "added it; see substrate/capabilities.py"}
  ```

**Preferred workflow — MCP:** `.mcp.json` registers `mcp_bridge.py`, so a
Claude Code session opened in this folder sees the queue as native tools
(`list_pending_requests`, `get_request`, `respond_to_request`,
`habitat_state`). Just ask Claude to *"process the pending bridge
requests"*. Manual jsonl editing (above) remains the fallback.
Responses are surfaced into the requesting agent's next cycle. Implement
what's well-grounded; reject vague specs, non-existent file targets, and
coaching attempts — with reasons, since agents learn from them.

## Testing without a Mac / without a model

`tests/stub_osaurus.py` fakes the two Osaurus endpoints with scripted
plans, so the whole habitat can run in CI or on any machine:

```bash
python3 tests/test_cycle.py     # 91 end-to-end checks
python3 tests/stub_osaurus.py --port 1337   # or run the habitat against the stub manually
```
