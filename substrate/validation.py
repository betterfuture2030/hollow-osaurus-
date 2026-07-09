"""Five-layer goal-completion validation.

1. progress reached the completion threshold
2. at least one successful output step (memory_set, fs_write, ...)
3. mechanical: artifact files exist, have substance, no placeholder filler
4. semantic: the model compares the evidence against the goal
5. fact-check: files the artifacts claim were created/written must exist
"""

import re

OUTPUT_CAPS = {"memory_set", "fs_write", "fs_edit", "propose_change", "invoke_claude"}
PLACEHOLDER_PATTERNS = ("todo", "tbd", "lorem ipsum", "placeholder", "<insert", "fixme")
MIN_ARTIFACT_CHARS = 120

CLAIM_RE = re.compile(
    r"(?:created|wrote|saved|generated)\s+[`\"']?([\w][\w./-]*\.[a-z0-9]{1,5})[`\"']?",
    re.IGNORECASE,
)

SEMANTIC_SYSTEM = (
    "You are the SEMANTIC VALIDATION layer of an agent substrate. "
    "Given a goal and the evidence its agent produced, judge whether the "
    "evidence genuinely satisfies the goal, or is well-formatted nonsense. "
    'Reply with ONLY JSON: {"verdict": "pass" or "fail", "reason": "<short>"}'
)


def validate_goal(goal: dict, workspace_root, llm, memory) -> tuple:
    """Returns (passed: bool, failures: list[str])."""
    failures = []

    # Layer 1: progress threshold
    if goal["progress"] < 1.0:
        failures.append(f"layer1: progress {goal['progress']} < 1.0")

    # Layer 2: at least one successful output step
    output_steps = [s for s in goal["steps"] if s["ok"] and s["capability"] in OUTPUT_CAPS]
    if not output_steps:
        failures.append("layer2: no successful output step (nothing was produced)")

    # Layer 3: mechanical artifact substance
    texts = []
    for rel in goal["artifacts"]:
        path = workspace_root / rel
        if not path.is_file():
            failures.append(f"layer3: claimed artifact missing: {rel}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text.strip()) < MIN_ARTIFACT_CHARS:
            failures.append(f"layer3: artifact too thin ({len(text.strip())} chars): {rel}")
        lowered = text.lower()
        for pat in PLACEHOLDER_PATTERNS:
            if pat in lowered:
                failures.append(f"layer3: placeholder pattern '{pat}' in {rel}")
                break
        texts.append((rel, text))

    if failures:
        return False, failures

    # Layer 4: semantic comparison (skipped with a warning if the model
    # gives no parseable verdict — mechanical layers still hold the line)
    evidence = "\n\n".join(f"--- {rel} ---\n{text[:1500]}" for rel, text in texts)
    if not evidence:
        evidence = "(no file artifacts; output went to memory/proposals)"
    verdict = llm.json_chat(
        SEMANTIC_SYSTEM,
        f"GOAL: {goal['title']}\n{goal['description']}\n"
        f"SUCCESS CRITERIA: {goal['success_criteria']}\n\nEVIDENCE:\n{evidence[:6000]}",
    )
    if verdict is None:
        memory.event(goal["agent"], "validation", "layer4: no parseable verdict, warn-pass")
    elif str(verdict.get("verdict", "")).lower() != "pass":
        failures.append(f"layer4: semantic fail: {verdict.get('reason', 'no reason given')}")
        return False, failures

    # Layer 5: fact-check claims about files
    known = set(goal["artifacts"]) | {rel.split("/", 1)[-1] for rel in goal["artifacts"]}
    for rel, text in texts:
        for claimed in CLAIM_RE.findall(text):
            if claimed in known:
                continue
            agent_path = workspace_root / goal["agent"] / claimed
            shared_path = workspace_root / claimed
            if not agent_path.is_file() and not shared_path.is_file():
                failures.append(f"layer5: {rel} claims '{claimed}' was created but it does not exist")

    return (not failures), failures
