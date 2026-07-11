# CLAUDE.md — Hollow on Osaurus

## Project overview

This repo is **Hollow on Osaurus**: a clean-room, Mac-native rebuild of
[Hollow AgentOS](https://ninjahawk.github.io/hollow-wiki/) — three autonomous
local LLM agents (**scout**, **analyst**, **builder**) that share a workspace,
pick their own goals, and develop over time. Instead of the original's
Ollama + Docker stack, all inference goes through
**[Osaurus](https://github.com/osaurus-ai/osaurus)** (Apple-Silicon-native MLX
server) via its OpenAI-compatible API. No Docker; the only outbound network
call is the earned `research_topic` capability (config kill-switch:
`research.enabled`). Provenance and licensing: see NOTICE.md (MIT, clean-room,
full credit to ninjahawk).

- Local checkout: `/Users/peterlowes/Code/workspace/hollow-agentsOS-Osaurus`
- Remote: `https://github.com/betterfuture2030/hollow-agentOS-osaurus.git`, branch `main`
- Target machine: MacBook Pro, Apple Silicon, **24 GB unified memory**

## Repo layout

```
(repo root)
  hollow.py                 entry point: first-run wizard (probes Osaurus, lists
                            /v1/models, writes config.json) + run/stop/status
  thoughts.py               live terminal monitor; tails memory/events.jsonl (rich)
  submit_task.py            inject a host message into an agent's next cycle
  panel.py                  operator panel launcher (pywebview optional, else browser)
  panel.html                the panel UI, served by the habitat at /panel
  mcp_bridge.py             stdio MCP server exposing the Claude bridge queue
  .mcp.json                 registers hollow-bridge for Claude Code sessions here
  config.example.json       reference config (osaurus.*, runtime.*, world.*, research.*)
  requirements.txt          httpx + rich — the only hard non-stdlib deps
  substrate/
    __init__.py             AGENT_NAMES, DEFAULT_CONFIG, config load/save
    llm.py                  OsaurusClient: /v1/chat/completions via httpx, global
                            inference lock, /no_think + <think>-stripping, robust
                            JSON extraction, per-call log to memory/llm_log.jsonl
    agents.py               identities + goal-selection prompt (incl. perception
                            digest, THE WORLD, completed-goals list) + grounded
                            fallback plan (path-out aware)
    loop.py                 Habitat: cycle scheduler, stressor bookkeeping incl.
                            ease-on-action, perception digest state, voluntary
                            abandonment, ambient event scheduling, nuke()
    world.py                ambient events: weather / objects / echoes of the
                            habitat's own event history
    goals.py                per-agent registry (memory/goals/<agent>/registry.jsonl);
                            abandonment cleanup spares shared/ artifacts
    suffering.py            9 stressor types, tier thresholds (0.55 / 0.75),
                            PATH_OUT set, locked_capabilities(), set_stressor()
    capabilities.py         capability registry + gating; fs_* confined to the
                            agent workspace + shared/; synthesize_capability
                            (subprocess-isolated agent-authored tools, max 5);
                            real research_topic (DuckDuckGo, earned)
    validation.py           five-layer pipeline; negation-aware placeholder check;
                            labeled evidence truncation for the semantic judge
    lessons.py              candidate→promoted lessons, Jaccard 0.55 dedupe,
                            operator retraction blocklist (matches at 0.4)
    memory.py               kv store, audit log, event stream, host messages,
                            shared-file author manifest
    claude_bridge.py        invoke_claude request/response jsonl queue + gates
    server.py               stdlib HTTP API on 127.0.0.1:7777 (+ /panel,
                            /stressor, /nuke)
  memory/                   runtime state (gitignored; .gitkeep only in git)
  workspace/                agent artifacts (gitignored; shared/ is cross-agent)
  tests/
    stub_osaurus.py         fake /v1/models + /v1/chat/completions with scripted
                            plans — lets the whole habitat run with no model
    test_cycle.py           88-check end-to-end test (run: python3 tests/test_cycle.py)
```

## Tech stack and conventions

- **Python 3.12+**, stdlib everywhere except `httpx` (HTTP) and `rich`
  (monitor; optional). `pywebview` is an optional extra for the panel.
  Use the project venv: `.venv/bin/python` (PEP 668 blocks global pip).
- **Osaurus** serves models at `http://127.0.0.1:1337/v1`. Models are
  downloaded in the Osaurus app; the wizard only offers what's installed.
  On this Mac: `qwen3.6-27b-mxfp4` (~8 tok/s — budget minutes per call;
  `timeout_seconds` is 600 for a reason) with `foundation` as fast fallback.
- **One model, one lock**: all LLM calls serialize through
  `OsaurusClient._lock`. Never add concurrent inference.
- **No `response_format`/JSON mode**: JSON is prompt-enforced, parsed by
  `llm.extract_json`, retried once, and every JSON consumer has a
  deterministic fallback. `/no_think` is appended for Qwen-family models;
  `<think>` blocks are always stripped. `JSON_MAX_TOKENS` caps plan length —
  raising it trades latency for artifact size (history: 1200 truncated
  fs_write documents).
- **State is jsonl/json under `memory/`**; goal registry lines are full
  snapshots — last line per id wins. **Runtime state is never committed.**
- **Tunable constants sit at module tops** — change them there, not inline.
- **Testing needs no model**: `.venv/bin/python tests/test_cycle.py` (88
  plain-assert checks against the stub). Keep it green; extend the stub's
  scripted plans when adding mechanics.
- **Every mechanic must be perceivable.** The hard-won rule of this project:
  agents (and the validator) reason correctly from what they can see, so any
  new state must reach the prompt (digest, goal block, THE WORLD) or it will
  produce rational-but-wrong behavior. See the 2026-07-09 git history for
  five case studies.

## Key mechanics (quick reference)

- **Suffering**: 9 stressors, load = min(1, sum). Load ≥ 0.55 locks
  `synthesize_capability`; ≥ 0.75 also locks `fs_write`/`fs_edit`. PATH_OUT
  (fs_read, fs_list, memory_set/get, llm_chat, invoke_claude, propose_change,
  shell_exec, retire_capability) is never locked. **Ease-on-production**: a
  cycle eases stagnation 0.2 / futility 0.1 only if it produced output (an
  OUTPUT_CAPS step) — except while fs_write is locked, when any successful
  step eases (the path-out escape hatch). Goals pinned at the 0.8 read
  ceiling 3+ cycles get a "write now" digest nudge.
- **Perception digest**: each prompt carries stressor deltas since last cycle,
  failed steps (3-cycle memory), truthful peer listings (ghost-filtered
  manifest), completed-goal titles, and the validator's last failure reason
  with the 5-attempt budget.
- **Goals**: voluntary `abandon_goal` action costs 0.2 futility and deletes
  private artifacts; `shared/` artifacts always survive cleanup. 5 validation
  failures still force abandonment.
- **Validation**: five layers; placeholder check is word-boundary +
  negation-aware; semantic-judge evidence is 4000 chars/artifact with an
  explicit truncation label.
- **Lessons**: promote after 2 observations (1 high-confidence for
  environment/constraints); dedupe at Jaccard ≥ 0.55; operator
  `Lessons.retract(text)` blocklists at ≥ 0.4 so vetoed beliefs can't
  re-promote reworded.
- **World**: ambient event every `world.event_every_rounds` rounds
  (weather / objects / echoes of past events), delivered as `THE WORLD:`.
- **Synthesized tools**: `synthesize_capability(name, description, code)`,
  code defines `run(args)`; subprocess-isolated, 20 s cap, blocklist-screened,
  max 5/agent, dismantled via `retire_capability`.
- **research_topic**: earned (load ≤ 0.15 + ≥ 1 peer interaction); real
  DuckDuckGo search; `research.enabled: false` for fully-local operation.
- **Claude bridge** (a workflow YOU, Claude Code, perform here): agents file
  requests via `invoke_claude`. Preferred: the **hollow-bridge MCP tools**
  (`list_pending_requests`, `get_request`, `respond_to_request`,
  `habitat_state`) from `.mcp.json`. Fallback: append verdict lines to
  `memory/claude_responses.jsonl`
  (`{"request_id", "status": "implemented"|"rejected", "response"}`).
  Implement what's well-grounded; reject with reasons — agents learn from them.
- **Operator API** (localhost:7777): GET `/health`, `/state`, `/events?n=100`,
  `/panel`; POST `/inject`, `/suspend`, `/resume`, `/stressor`
  (exact-set severity, 0 resolves), `/nuke` (`{"confirm": true}`).

## Operating notes

- Run/stop: `.venv/bin/python hollow.py run|stop|status`. State persists
  across restarts (cycle counters currently do not — known issue; a fix may
  land from a parallel session).
- Watch live: `thoughts.py`; panel: `panel.py`; poke: `submit_task.py`.
- When tuning prompts or diagnosing failures, read `memory/llm_log.jsonl`
  (latency, finish_reason, full unusable replies) before guessing.

## Remaining roadmap

The original feature roadmap is fully ported (operator panel, world events,
synthesize_capability, real research_topic, MCP bridge — July 2026). Open:

1. Persist per-agent cycle counters across restarts (in flight in a
   parallel session).
2. Consider extending `_futility_check` to near-duplicates of *completed*
   goals (observed: agents occasionally re-adopt finished work).
3. ~~Housekeeping: repo rename~~ — done (`hollow-agentOS-osaurus`, July 2026).

## History

Built July 2026 in a Claude Code cloud session from the Hollow AgentOS wiki
docs (clean-room — no upstream source was copied; see NOTICE.md). Hardened
the same month in a long live-tuning session against a real 27B model:
systematic timeouts, two suffering deadlocks, learned-helplessness lesson
loops, validator evidence truncation, and ghost artifacts were each found
live and fixed with tests. The 2026-07-09 git log reads as a case-study
series in agent-perception debugging.
