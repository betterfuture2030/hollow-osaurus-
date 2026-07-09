# Hollow on Osaurus

A Mac-native rebuild of [Hollow AgentOS](https://ninjahawk.github.io/hollow-wiki/)
that runs its model inference through **[Osaurus](https://github.com/dinoki-ai/osaurus)**
(Apple-Silicon MLX server) instead of Ollama. No Docker, no cloud calls —
three local agents living in a substrate on your MacBook.

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
   cd hollow
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

Operator HTTP API (localhost only): `GET /health`, `GET /state`,
`GET /events?n=100`, `POST /inject`, `POST /suspend`, `POST /resume`
(JSON bodies like `{"agent": "scout", "message": "..."}`).

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

The workflow with Claude Code on your Mac: open this folder in Claude Code
and ask it to *"review pending requests in memory/claude_requests.jsonl,
implement or reject each, and append verdicts to memory/claude_responses.jsonl"*.
Responses are surfaced into the requesting agent's next cycle. Implement
what's well-grounded; reject vague specs, non-existent file targets, and
coaching attempts — with reasons, since agents learn from them.

## Testing without a Mac / without a model

`tests/stub_osaurus.py` fakes the two Osaurus endpoints with scripted
plans, so the whole habitat can run in CI or on any machine:

```bash
python3 tests/test_cycle.py     # 27 end-to-end checks
python3 tests/stub_osaurus.py --port 1337   # or run the habitat against the stub manually
```

## Not yet ported (follow-ups)

- pywebview operator panel (suspend/inject/nuke via UI — the HTTP API
  already exposes the hooks)
- ambient world events (weather, echoes, objects)
- runtime capability synthesis (`synthesize_capability` is reserved and
  already participates in gating)
- real `research_topic` web search (currently earned-but-offline)
