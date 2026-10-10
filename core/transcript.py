"""Whisper transcription (word timestamps) and transcript helpers."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


def transcribe(audio_path, out_json, model_size: str | None = None, language: str | None = None,
               progress=None) -> dict:
    from faster_whisper import WhisperModel  # lazy: heavy import

    model_size = model_size or os.environ.get("WHISPER_MODEL", "small")
    try:
        import ctranslate2
        gpu = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        gpu = False
    device = os.environ.get("WHISPER_DEVICE") or ("cuda" if gpu else "cpu")
    compute = os.environ.get("WHISPER_COMPUTE") or ("float16" if device == "cuda" else "int8")
    cache = os.environ.get("WHISPER_CACHE") or None
    model = WhisperModel(model_size, device=device, compute_type=compute, download_root=cache)
    segs, info = model.transcribe(str(audio_path), word_timestamps=True, vad_filter=True,
                                  language=language)
    out = {"language": info.language, "duration": round(info.duration, 2),
           "model": model_size, "segments": []}
    for s in segs:
        if progress and info.duration:
            progress(min(99, int(s.end / info.duration * 100)))
        out["segments"].append({
            "start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
            "words": [{"w": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2)}
                      for w in (s.words or [])],
        })
    Path(out_json).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def all_words(t: dict) -> list[dict]:
    return [w for s in t.get("segments", []) for w in s.get("words", [])]


def lines(t: dict, detail: str = "segments", start=None, end=None, max_window: float = 60.0) -> dict:
    """Token-cheap transcript view. Sentences by default; word times only for a short window."""
    segs = t.get("segments", [])
    if detail == "words":
        s = float(start or 0.0)
        e = float(end) if end is not None else s + 45.0
        e = min(e, s + max_window)
        out = []
        for seg in segs:
            if seg["end"] < s or seg["start"] > e:
                continue
            ws = [w for w in seg["words"] if s - 0.01 <= w["start"] <= e]
            if ws:
                out.append(" ".join(f"{w['w']}@{w['start']}" for w in ws))
        return {"window": [s, round(e, 2)], "duration": t.get("duration"), "words": out,
                "note": f"max {int(max_window)}s per call; use start/end for other parts"}
    s = float(start or 0.0)
    e = float(end) if end is not None else 1e9
    out = [f"{seg['start']:.1f}-{seg['end']:.1f} {seg['text']}"
           for seg in segs if seg["end"] >= s and seg["start"] <= e]
    return {"language": t.get("language"), "duration": t.get("duration"), "lines": out}


def _norm(s: str) -> str:
    return re.sub(r"[^\w']+", "", s.lower())


def find_phrase(t: dict, phrase: str, limit: int = 10) -> list[dict]:
    words = all_words(t)
    toks = [_norm(w["w"]) for w in words]
    target = [x for x in (_norm(p) for p in phrase.split()) if x]
    hits = []
    if not target:
        return hits
    n = len(target)
    for i in range(len(toks) - n + 1):
        if toks[i:i + n] == target:
            ctx = " ".join(w["w"] for w in words[max(0, i - 4): i + n + 4])
            hits.append({"start": words[i]["start"], "end": words[i + n - 1]["end"], "context": ctx})
            if len(hits) >= limit:
                break
    return hits


def auto_captions(t: dict, words_per_chunk: int = 3, max_chars: int = 28) -> list[dict]:
    words = all_words(t)
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        text = " ".join(x["w"] for x in cur)
        ends = w["w"].endswith((".", "!", "?", ","))
        if len(cur) >= words_per_chunk or len(text) >= max_chars or ends:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    caps = []
    for i, c in enumerate(chunks):
        start, end = c[0]["start"], c[-1]["end"]
        if i + 1 < len(chunks):
            nxt = chunks[i + 1][0]["start"]
            if 0 <= nxt - end < 0.3:
                end = nxt
        caps.append({"text": " ".join(x["w"] for x in c), "start": start, "end": max(end, start + 0.2)})
    return caps
