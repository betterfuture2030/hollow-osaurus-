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
# A pattern on a line that negates or forbids it is a meta-mention, not a
# stub: an artifact saying "No placeholders (e.g. TODO)" must not be punished
# for banning them (observed live: it cost an agent its best artifact).
NEGATION_MARKERS = ("no ", "not ", "never", "avoid", "without", "forbid", "reject")
MIN_ARTIFACT_CHARS = 120
# The semantic judge reads at most this much of each artifact. When a
# document exceeds it, the cut MUST be labeled: an unlabeled slice reads as
# an author error, and the judge rejects intact documents as "truncated,
# cutting off mid-sentence" (observed live — it cost a goal all 5 attempts).
EVIDENCE_CHARS_PER_ARTIFACT = 4000
EVIDENCE_TOTAL_CHARS = 12000
TRUNCATION_NOTE = (
    "\n[NOTE TO VALIDATOR: evidence display truncated here for review; "
    "the artifact on disk continues past this point. Do not treat this "
    "cutoff as incompleteness.]"
)


def build_evidence(texts) -> str:
    """Assemble the semantic layer's evidence block from (rel, text) pairs."""
    parts = []
    for rel, text in texts:
        body = text[:EVIDENCE_CHARS_PER_ARTIFACT]
        if len(text) > EVIDENCE_CHARS_PER_ARTIFACT:
            body += TRUNCATION_NOTE
        parts.append(f"--- {rel} ---\n{body}")
    return "\n\n".join(parts)[:EVIDENCE_TOTAL_CHARS]


def find_placeholder(text: str):
    """Return the offending pattern if the text contains real stub filler."""
    for line in text.lower().splitlines():
        for pat in PLACEHOLDER_PATTERNS:
            if pat.isalnum():
                hit = re.search(rf"(?<![a-z0-9]){re.escape(pat)}(?![a-z0-9])", line)
            else:
                hit = pat in line
            if hit and not any(neg in line for neg in NEGATION_MARKERS):
                return pat
    return None

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
        pat = find_placeholder(text)
        if pat:
            failures.append(f"layer3: placeholder pattern '{pat}' in {rel}")
        texts.append((rel, text))

    if failures:
        return False, failures

    # Layer 4: semantic comparison (skipped with a warning if the model
    # gives no parseable verdict — mechanical layers still hold the line)
    evidence = build_evidence(texts)
    if not evidence:
        evidence = "(no file artifacts; output went to memory/proposals)"
    verdict = llm.json_chat(
        SEMANTIC_SYSTEM,
        f"GOAL: {goal['title']}\n{goal['description']}\n"
        f"SUCCESS CRITERIA: {goal['success_criteria']}\n\nEVIDENCE:\n{evidence}",
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
