# CLAUDE.md — Hollow on Osaurus

## Project overview

This repo is **Hollow on Osaurus**: a clean-room, Mac-native rebuild of
[Hollow AgentOS](https://ninjahawk.github.io/hollow-wiki/) — three autonomous
local LLM agents (**scout**, **analyst**, **builder**) that share a workspace,
pick their own goals, and develop over time. Instead of the original's
Ollama + Docker stack, all inference goes through
**[Osaurus](https://github.com/dinoki-ai/osaurus)** (Apple-Silicon-native MLX
server) via its OpenAI-compatible API. No Docker, no cloud calls.

The habitat lives in **`hollow/`**. It was built inside a fork of an unrelated
static-site repo (Dolly's Art Deco guest preview), so the repo root still
carries leftover site files (`assets/`, `index.html`, `script.js`,
`styles.css`). **Those are dead weight — see TASK 1 below: delete them and
promote `hollow/` to the repo root.**

- Local checkout: `/Users/peterlowes/Code/workspace/hollow-agentsOS-Osaurus`
- Remote: `https://github.com/betterfuture2030/hollow-osaurus-.git`, branch `main`
- Target machine: MacBook Pro, Apple Silicon, **24 GB unified memory**

## Repo layout

```
(repo root)
  assets/, index.html,        LEGACY: Dolly Art Deco static site carried over from
  script.js, styles.css       the source repo. Unused by Hollow. Delete (TASK 1).

  hollow/                     THE PROJECT (everything below is relative to hollow/)
    hollow.py                 entry point: first-run wizard (probes Osaurus, lists
                              /v1/models, writes config.json) + run/stop/status
    thoughts.py               live terminal monitor; tails memory/events.jsonl (rich)
    submit_task.py            inject a host message into an agent's next cycle
    config.example.json       reference config (osaurus.base_url, models, runtime)
    requirements.txt          httpx + rich — the only non-stdlib deps
    .gitignore                ignores config.json, memory/*, workspace/* (runtime state)
    README.md                 human setup guide (macOS + Osaurus + Claude bridge)
    substrate/
      __init__.py             AGENT_NAMES, DEFAULT_CONFIG, config load/save
      llm.py                  OsaurusClient: /v1/chat/completions via httpx, global
                              inference lock, <think>-stripping, robust JSON extraction
      agents.py               agent identities + goal-selection prompt + grounded
                              fallback plan (used when model JSON is unusable)
      loop.py                 Habitat: cycle scheduler, stressor bookkeeping,
                              plan execution, completion/abandonment flow
      goals.py                per-agent registry (memory/goals/<agent>/registry.jsonl),
                              progress deltas 0.20 (high-value caps) / 0.10, abandon at
                              5 validation failures + artifact cleanup
      suffering.py            9 stressor types, load = capped sum of severities,
                              tier thresholds, PATH_OUT set, locked_capabilities()
      capabilities.py         capability registry + gating at dispatch; fs_* confined
                              to workspace/<agent>/ plus communal workspace/shared/
      validation.py           five-layer goal-completion pipeline
      lessons.py              candidate→promoted lessons, Jaccard 0.55 dedupe
      memory.py               kv store, audit log, event stream, host messages,
                              shared-file author manifest
      claude_bridge.py        invoke_claude request/response jsonl queue + quality gates
      server.py               stdlib HTTP API on 127.0.0.1:7777
    memory/                   runtime state (gitignored; .gitkeep only in git)
    workspace/                agent artifacts (gitignored; shared/ is cross-agent)
    tests/
      stub_osaurus.py         fake /v1/models + /v1/chat/completions with scripted
                              plans — lets the whole habitat run with no model
      test_cycle.py           40-check end-to-end test (run: python3 tests/test_cycle.py)
```

## Tech stack and conventions

- **Python 3.12+**, standard library everywhere except `httpx` (HTTP) and
  `rich` (monitor output; optional at runtime — plain fallback exists).
- **Osaurus** serves models at `http://127.0.0.1:1337/v1` (configurable).
  Model discovery = `GET /v1/models`; inference = `POST /v1/chat/completions`.
  There is no `ollama pull` equivalent — models are downloaded in the Osaurus
  app, and the wizard only offers what's installed.
- **Models**: default `mlx-community/Qwen3-30B-A3B-4bit` (~18 GB, MoE — right
  for 24 GB), fallback `mlx-community/Qwen3-4B-4bit`.
- **One model, one lock**: all LLM calls serialize through
  `OsaurusClient._lock`. Never add concurrent inference.
- **No `response_format`/JSON mode**: MLX servers vary in support. JSON is
  prompt-enforced and parsed with `llm.extract_json` (fence-stripping,
  balanced-brace scan) with one retry, and every JSON consumer has a
  deterministic fallback. Qwen3 `<think>…</think>` blocks are always stripped.
- **State is jsonl/json under `memory/`**, append-friendly and human-readable.
  Goal registry lines are full snapshots — last line per id wins.
- **Tunable constants sit at module tops** (thresholds, caps, blocklists) —
  change them there, not inline.
- **Runtime state is never committed**: `config.json`, `memory/*`,
  `workspace/*` are gitignored (`.gitkeep` placeholders only).
- **Testing needs no model**: `cd hollow && python3 tests/test_cycle.py` runs
  the full habitat against `tests/stub_osaurus.py` (plain asserts, no pytest).
  Keep this green; extend the stub's scripted plans when adding mechanics.

## Key mechanics (quick reference)

- **Suffering**: stressors (futility, invisibility, identity_violation,
  existential_threat, repeated_failure, purposelessness, resource_burden,
  capability_lock, stagnation) each carry severity 0–1; load = min(1, sum).
  Load ≥ **0.55** locks `synthesize_capability`; ≥ **0.75** also locks
  `fs_write`/`fs_edit`. The PATH_OUT set (fs_read, memory_set/get, llm_chat,
  invoke_claude, propose_change, shell_exec, retire_capability, fs_list) is
  never locked. `research_topic` is earned: load ≤ 0.15 **and** ≥ 1 peer
  interaction (currently a stub that reports itself offline).
- **Peers**: files under `workspace/shared/` are visible to all agents;
  authorship is tracked in `memory/shared_manifest.json`. A peer reading your
  shared file eases your `invisibility`.
- **Validation** (all five must pass to complete a goal): progress ≥ 1.0 →
  ≥ 1 successful output step → mechanical artifact check (exists, ≥ 120 chars,
  no placeholder patterns) → semantic LLM verdict → fact-check of
  created/wrote/saved claims. 5 failures ⇒ goal abandoned + its artifacts deleted.
- **Lessons**: promoted to durable "RULES OF YOUR ENVIRONMENT" after 2
  independent observations (1 high-confidence observation suffices for
  `environment`/`constraints`); deduped at Jaccard ≥ 0.55.
- **Claude bridge** (this is a workflow YOU, Claude Code, perform here):
  agents append requests to `memory/claude_requests.jsonl` (gates: description
  ≥ 40 chars, spec ≥ 80 chars, no queue-management requests, ≤ 3 pending per
  agent). When asked to process the queue: implement well-grounded requests or
  reject with reasons, then append one line per verdict to
  `memory/claude_responses.jsonl`:
  `{"request_id": "<id>", "status": "implemented"|"rejected", "response": "<why/what>"}`.
  Verdicts surface in the requesting agent's next cycle.
- **Operator API** (localhost:7777): GET `/health`, `/state`, `/events?n=100`;
  POST `/inject`, `/suspend`, `/resume` with `{"agent": "...", "message": "..."}`.

## Current TODO / roadmap

**TASK 1 — cleanup: remove all Dolly leftovers, leave only the Hollow project.**
From the repo root:

```bash
git rm -r assets index.html script.js styles.css
git mv hollow/.gitignore hollow/* .          # promote project to repo root
rmdir hollow
python3 tests/test_cycle.py                  # must stay 27/27
git commit -m "Remove legacy Dolly site files; promote Hollow to repo root"
git push
```

(If `git mv` balks on the dotfile, move `.gitignore` separately. After this,
paths in this file that say `hollow/...` mean the repo root.)

**TASK 2 — first live run on this Mac:**
1. Install/launch Osaurus, start its server (default port 1337), download
   `Qwen3-30B-A3B-4bit` (and `Qwen3-4B-4bit` as fallback) in the app.
2. `pip3 install -r requirements.txt`
3. `python3 hollow.py` → wizard detects Osaurus + models, writes `config.json`.
4. Watch with `python3 thoughts.py`; poke with `python3 submit_task.py scout "..."`.
5. Expect prompt/behavior tuning: the stub always returns perfect JSON, a real
   Qwen3 won't — the fallback path handles it, but goal quality will need
   iteration on `substrate/agents.py` prompts.

**Housekeeping:** consider renaming the remote repo (trailing `-` in
`hollow-osaurus-` looks accidental); update README's clone URL if renamed.

**Feature roadmap (deliberately not yet built, in rough priority order):**
1. **Operator panel** — pywebview UI over the existing :7777 API (suspend/
   resume, inject, suffering sliders, stressor injection, "nuke" reset).
2. **Ambient world events** — weather/echoes/objects injected into agent
   perception on a schedule.
3. **`synthesize_capability`** — runtime tool creation by agents (name already
   reserved and participates in gating).
4. **Real `research_topic`** — actual web search behind the earned gate.
5. **MCP packaging of the Claude bridge** — expose the request queue as an MCP
   server so Claude Code sees requests without manual file reads.

## History

Built July 2026 in a Claude Code cloud session from the Hollow AgentOS wiki
docs (clean-room — no upstream source was copied). Original development branch:
`claude/hollow-wiki-macos-ct32x1` on `betterfuture2030/dolly-art-deco-guest-preview`
(safe to delete once this repo is confirmed complete).
