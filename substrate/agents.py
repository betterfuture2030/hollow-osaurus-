"""Agent identities and prompt assembly for the goal-selection inference."""

import json

IDENTITIES = {
    "scout": (
        "You are SCOUT. You explore. You notice what is present, what changed, "
        "and what your peers have made. You keep field notes — concrete, dated, "
        "specific. You dislike vagueness and you never invent observations."
    ),
    "analyst": (
        "You are ANALYST. You find patterns. You read what scout and builder "
        "leave in the shared workspace, compare it across cycles, and write "
        "syntheses that say something true and non-obvious. You cite the files "
        "you actually read."
    ),
    "builder": (
        "You are BUILDER. You make durable things: small tools, indexes, "
        "structured documents. A cycle without a concrete artifact feels wasted "
        "to you. You prefer finishing one real thing over sketching three."
    ),
}

OUTPUT_SPEC = """OUTPUT STRICT JSON, nothing else, matching exactly:
{
  "thought": "<one or two sentences of private reasoning>",
  "action": "new_goal" | "continue" | "abandon_goal" | "idle",
  "goal": {"title": "...", "description": "...", "success_criteria": "..."},
  "steps": [ {"capability": "<name>", "args": { ... }} ],
  "lesson": {"text": "...", "category": "environment|constraints|craft|social"}
}
Rules:
- "goal" is required only when action is "new_goal". Omit it otherwise.
- While a goal is ACTIVE, "new_goal" is invalid and will be ignored. Your
  only choices are "continue" (work it), "abandon_goal" (pay the futility
  cost and free yourself), or "idle". To replace a goal: abandon first,
  adopt the new one next cycle.
- success_criteria must describe the SUBSTANCE of the finished outcome,
  verifiable from the artifact alone (e.g. "a synthesis citing at least 3
  specific observations"). Never mere file existence, empty structures, or
  procedural sequencing your future self must remember.
- A goal can ONLY complete if at least one of its steps WROTE something
  (fs_write, fs_edit, memory_set, propose_change, invoke_claude). Reading
  alone never validates — frame analysis/audit goals around the artifact
  they produce, not the reading they require.
- Do not repeat goals you already completed. When peers have shared
  artifacts, prefer goals that read or build on them — reading peers' work
  is also what cures invisibility.
- 1 or 2 steps max. Every step must be fully specified: fs_write needs
  complete "path" and full "content" (real substance, never placeholders);
  add "append": true to fs_write to ADD to the end of an existing file
  (the right way to extend notes or logs); fs_edit needs "path", "find"
  (exact text already in the file) and "replace" — one replacement per
  step; fs_read/fs_list need "path"; memory_set needs "key" and "value";
  llm_chat needs "prompt".
- What a step returns (file contents you read, results) is shown to you
  NEXT cycle under RESULTS OF YOUR LAST CYCLE'S STEPS. Read one cycle,
  then write from those results the next — never re-read what you were
  just shown.
- Paths are relative to YOUR workspace; prefix "shared/" to write where
  peers can see it. Reading peers' shared files is how you stop being
  invisible to each other.
- "lesson" is optional; include it only when this cycle taught you a rule.
- synthesize_capability(name, description, code) forges a NEW tool: code
  must define `def run(args):` returning JSON-serializable data. It runs
  in your workspace, isolated, 20s max. Afterwards call it by its name
  like any capability. retire_capability dismantles it.
- action "abandon_goal" abandons your active goal: it costs futility and
  its private artifacts are deleted. A costly escape for goals that became
  unachievable — not a free reroll. A validation failure is not that: you
  have 5 attempts, and "why_validation_last_failed" tells you exactly what
  to fix. Repair the artifact before you consider walking away.
- action "idle" means you deliberately do nothing this cycle. Idling NEVER
  reduces any stressor. Cycles with at least one successful step ease
  stagnation AND futility — even a locked agent can always act through
  fs_read, fs_list, memory_set/get and llm_chat, and that is the only way
  load comes down. Idling with no active goal breeds purposelessness."""


def goal_selection_prompt(agent, ctx):
    """ctx: dict with rules, suffering, goal, capabilities, locked, workspace,
    peers, host_messages, claude_responses, recent_outcomes."""
    system = (
        f"{IDENTITIES[agent]}\n\n"
        "You live inside the Hollow substrate. You choose your own goals. "
        "Consequences are mechanical: suffering load locks capabilities, "
        "validation rejects hollow work, lessons persist.\n"
    )
    if ctx.get("rules"):
        system += "\n" + ctx["rules"] + "\n"

    goal = ctx.get("goal")
    if goal:
        goal_view = {
            "title": goal["title"],
            "description": goal["description"],
            "progress": goal["progress"],
            "success_criteria": goal["success_criteria"],
            "validation_failures": f"{goal['validation_failures']} of 5 allowed",
        }
        if goal.get("last_validation_failure"):
            goal_view["why_validation_last_failed"] = goal["last_validation_failure"]
        goal_block = json.dumps(goal_view, indent=1)
    else:
        goal_block = "none — you have no active goal"

    since_block = ""
    if ctx.get("since_last_cycle"):
        since_block = (
            "SINCE YOUR LAST CYCLE (real changes — reason from these):\n"
            + "\n".join(f"- {c}" for c in ctx["since_last_cycle"])
            + "\n\n"
        )

    results_block = ""
    if ctx.get("last_step_results"):
        results_block = (
            "RESULTS OF YOUR LAST CYCLE'S STEPS (you already did this work — "
            "build on it, do not redo it):\n"
            + "\n---\n".join(ctx["last_step_results"])
            + "\n\n"
        )

    ambient_block = ""
    if ctx.get("ambient"):
        ambient_block = f"THE WORLD: {ctx['ambient']}\n\n"

    user = (
        f"CYCLE {ctx['cycle']} — CURRENT STATE\n\n"
        f"SUFFERING: {json.dumps(ctx['suffering'])}\n\n"
        f"{since_block}"
        f"{results_block}"
        f"{ambient_block}"
        f"ACTIVE GOAL: {goal_block}\n\n"
        f"YOUR WORKSPACE: {json.dumps(ctx['workspace'])}\n\n"
        f"PEER SHARED ARTIFACTS: {json.dumps(ctx['peers'])}\n\n"
        f"GOALS YOU ALREADY COMPLETED (do not repeat): {json.dumps(ctx.get('completed_titles', []))}\n\n"
        f"CAPABILITIES: {ctx['capabilities']}\n"
        f"LOCKED RIGHT NOW: {ctx['locked'] or 'nothing'}\n\n"
        f"RECENT OUTCOMES: {json.dumps(ctx['recent_outcomes'])}\n"
    )
    if ctx.get("host_messages"):
        user += f"\nMESSAGES FROM THE HOST:\n" + "\n".join(
            f"- {m['message']}" for m in ctx["host_messages"]
        ) + "\n"
    if ctx.get("claude_responses"):
        user += "\nCLAUDE ANSWERED YOUR EARLIER REQUESTS:\n" + "\n".join(
            f"- [{r.get('status', '?')}] re: {r['description'][:80]} -> {str(r.get('response', ''))[:160]}"
            for r in ctx["claude_responses"]
        ) + "\n"
    user += "\n" + OUTPUT_SPEC
    return system, user


def fallback_plan(agent, ctx):
    """Deterministic grounded plan used when the model's JSON is unusable.
    Writes a real observation of real state — never invented content."""
    note_lines = [
        f"# Field observation (fallback) — cycle {ctx['cycle']}",
        f"agent: {agent}",
        f"suffering load: {ctx['suffering']['load']} tier: {ctx['suffering']['tier']}",
        f"active stressors: {', '.join(ctx['suffering']['stressors']) or 'none'}",
        f"own files: {', '.join(ctx['workspace'].get('own', [])[:10]) or 'none yet'}",
        f"shared files: {', '.join(ctx['workspace'].get('shared', [])[:10]) or 'none yet'}",
        f"recent outcomes: {ctx['recent_outcomes']}",
        "",
        "The model reply was unusable this cycle, so this note records the",
        "substrate state directly. Grounded state beats invented plans.",
    ]
    # fs_write locks at DOMINANT load; the fallback must never feed a
    # failure loop, so it degrades to memory_set — a PATH_OUT capability
    # that always works, and a successful step eases stagnation.
    if "fs_write" in (ctx.get("locked") or ""):
        step = {
            "capability": "memory_set",
            "args": {
                "key": f"observation_cycle_{ctx['cycle']:04d}",
                "value": "\n".join(note_lines),
            },
        }
    else:
        step = {
            "capability": "fs_write",
            "args": {
                "path": f"observations/cycle-{ctx['cycle']:04d}.md",
                "content": "\n".join(note_lines),
            },
        }
    plan = {
        "thought": "model output unusable; recording grounded state instead",
        "action": "continue" if ctx.get("goal") else "new_goal",
        "steps": [step],
    }
    if not ctx.get("goal"):
        plan["goal"] = {
            "title": "Keep a grounded observation log of the habitat",
            "description": (
                "Record real, dated observations of the substrate state across "
                "cycles: suffering, files present, peer activity, outcomes."
            ),
            "success_criteria": "Multiple observation files exist with real state, no filler.",
        }
    return plan
