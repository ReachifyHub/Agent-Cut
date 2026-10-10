"""One OpenAI-compatible chat call, with friendly errors. Works with OpenRouter, Groq, Gemini, DeepSeek..."""
from __future__ import annotations

import time

import requests


class LLMError(RuntimeError):
    pass


FRIENDLY = {
    401: "The API key was rejected. Check the key and the Base URL in Settings.",
    402: "Out of credit on this provider. Add credit or switch model.",
    403: "The provider refused the request (403). The key may lack access to this model, or the model is blocked for your account.",
    404: "Model or URL not found. Check the Model ID and Base URL in Settings.",
    413: "The request is bigger than this model's limit. Try a model with a larger limit.",
    429: "Rate limit hit. Wait a minute or pick another model.",
}


def check_agent(agent: dict) -> dict:
    base = str(agent.get("base") or "").strip().rstrip("/")
    key = str(agent.get("key") or "").strip()
    model = str(agent.get("model") or "").strip()
    if not (base and key and model):
        raise LLMError("AI is not set up. Open Settings and fill Base URL, API key and Model ID.")
    if not base.startswith("https://"):
        raise LLMError("Base URL must start with https://")
    return {"base": base, "key": key, "model": model}


def chat(agent: dict, messages: list, max_tokens: int = 2500, retries: int = 3) -> tuple[str, dict]:
    """Returns (text, usage). usage = {'prompt': n, 'completion': n}."""
    a = check_agent(agent)
    headers = {"Authorization": f"Bearer {a['key']}", "Content-Type": "application/json",
               "X-Title": "agentcut"}
    body = {"model": a["model"], "messages": messages, "temperature": 0.3, "max_tokens": max_tokens}
    last = ""
    for attempt in range(retries + 1):
        try:
            r = requests.post(a["base"] + "/chat/completions", headers=headers, json=body, timeout=180)
        except requests.RequestException as e:
            last = f"network error: {e}"
            time.sleep(4 * 2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            last = f"HTTP {r.status_code}"
            time.sleep(6 * 3 ** attempt)  # 6s, 18s, 54s
            continue
        if r.status_code >= 400:
            detail = r.text[:240].replace("\n", " ")
            raise LLMError(f"{FRIENDLY.get(r.status_code, f'HTTP {r.status_code}')} ({detail})")
        try:
            data = r.json()
            msg = data["choices"][0]["message"]
            text = msg.get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("The provider sent an unexpected reply: " + r.text[:200])
        u = data.get("usage") or {}
        return text, {"prompt": int(u.get("prompt_tokens") or 0), "completion": int(u.get("completion_tokens") or 0)}
    raise LLMError(f"The provider kept failing ({last}). Try again in a minute or switch model.")
