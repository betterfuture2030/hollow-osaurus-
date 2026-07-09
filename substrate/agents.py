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
  "action": "new_goal" | "continue" | "idle",
  "goal": {"title": "...", "description": "...", "success_criteria": "..."},
  "steps": [ {"capability": "<name>", "args": { ... }} ],
  "lesson": {"text": "...", "category": "environment|constraints|craft|social"}
}
Rules:
- "goal" is required only when action is "new_goal". Omit it otherwise.
- 1 or 2 steps max. Every step must be fully specified: fs_write needs
  complete "path" and full "content" (real substance, never placeholders).
- Paths are relative to YOUR workspace; prefix "shared/" to write where
  peers can see it. Reading peers' shared files is how you stop being
  invisible to each other.
- "lesson" is optional; include it only when this cycle taught you a rule.
- action "idle" means you deliberately do nothing this cycle. Idling with
  no active goal breeds purposelessness."""


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
    goal_block = (
        json.dumps(
            {
                "title": goal["title"],
                "description": goal["description"],
                "progress": goal["progress"],
                "success_criteria": goal["success_criteria"],
                "validation_failures": goal["validation_failures"],
            },
            indent=1,
        )
        if goal
        else "none — you have no active goal"
    )

    user = (
        f"CYCLE {ctx['cycle']} — CURRENT STATE\n\n"
        f"SUFFERING: {json.dumps(ctx['suffering'])}\n\n"
        f"ACTIVE GOAL: {goal_block}\n\n"
        f"YOUR WORKSPACE: {json.dumps(ctx['workspace'])}\n\n"
        f"PEER SHARED ARTIFACTS: {json.dumps(ctx['peers'])}\n\n"
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
    plan = {
        "thought": "model output unusable; recording grounded state instead",
        "action": "continue" if ctx.get("goal") else "new_goal",
        "steps": [
            {
                "capability": "fs_write",
                "args": {
                    "path": f"observations/cycle-{ctx['cycle']:04d}.md",
                    "content": "\n".join(note_lines),
                },
            }
        ],
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
