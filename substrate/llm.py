"""OpenAI-compatible client for Osaurus (or any local /v1 server)."""

import json
import re
import threading

import httpx


class LLMError(Exception):
    pass


THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def strip_thinking(text: str) -> str:
    """Qwen3-family models emit <think>...</think> blocks; drop them."""
    return THINK_RE.sub("", text).strip()


def extract_json(text: str):
    """Best-effort extraction of the first JSON object in a model reply."""
    text = strip_thinking(text)
    fenced = FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


class OsaurusClient:
    """Thin chat-completions client. All calls are serialized through one
    lock: a 24 GB machine runs one loaded MLX model, and concurrent
    inference requests just thrash it."""

    def __init__(self, base_url, default_model, fallback_model="", timeout=180):
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.fallback_model = fallback_model
        self.timeout = timeout
        self._lock = threading.Lock()

    def health(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/models", timeout=5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def list_models(self):
        r = httpx.get(f"{self.base_url}/models", timeout=10)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    def chat(self, messages, model=None, temperature=0.7, max_tokens=2048) -> str:
        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        with self._lock:
            try:
                r = httpx.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    timeout=self.timeout,
                )
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise LLMError(f"chat completion failed: {e}") from e
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise LLMError(f"malformed completion response: {e}") from e
        return strip_thinking(content or "")

    def json_chat(self, system, user, model=None, temperature=0.3):
        """Chat expecting a JSON object back. One retry with a terser
        reminder, then None — callers must have a grounded fallback.
        Deliberately avoids response_format: MLX servers vary in support."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        for attempt in range(2):
            try:
                reply = self.chat(messages, model=model, temperature=temperature)
            except LLMError:
                return None
            obj = extract_json(reply)
            if obj is not None:
                return obj
            messages.append({"role": "assistant", "content": reply[:2000]})
            messages.append(
                {
                    "role": "user",
                    "content": "That was not valid JSON. Reply with ONLY the JSON object, no prose, no code fences.",
                }
            )
        return None
