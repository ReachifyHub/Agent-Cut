"""The AI's whole job: ONE call that turns the user's request + a compact STATE into JSON edit ops.
No tool loop, no resending history of tool results. Code does everything else."""
from __future__ import annotations

import json
import re

from . import edit, llm

SYSTEM = """You are the editor inside an automated video tool. Code does all media work; you only decide.
Reply with ONE JSON object and nothing else:
{"reply":"1-3 short sentences to the user","ops":[...],"render":"final"|"preview"|null}

STATE shows the current timeline, one clip per line: index|file|start-end|motion|transition-in|words spoken during the clip.
Follow the user's request first. If they gave no direction for something, improve pacing yourself.
If the request says images were already placed on their narration lines, do not move their times.

ops (applied in order, [] if no change needed):
{"op":"set_clip","index":3,"fields":{"start_phrase":"billion dollars","motion":"zoom_out","transition":"fade","trim_start":1.5,"loop":true,"file":"07.jpg"}}  (null removes a field)
{"op":"swap","a":2,"b":5}
{"op":"reorder","order":["01.jpg","03.mp4",...]}  (new file for every clip slot, same times)
{"op":"insert_clip","index":4,"clip":{"file":"09.jpg","start_phrase":"..."}}
{"op":"remove_clip","index":6}
{"op":"add_sfx","file":"whoosh.mp3","at_phrase":"new idea","offset":-0.1,"volume":0.45}   (or "at":12.4)
{"op":"remove_sfx","index":0}
{"op":"set","key":"format|captions|caption_style|music|voice_volume|tail","value":...}

Rules:
- Place things with start_phrase / at_phrase using words copied EXACTLY from the spoken text in STATE, not seconds. Optional "occurrence":2 for a repeated phrase.
- Clips run back to back; a clip lasts until the next one starts, so changing a start moves the cut. Keep starts increasing.
- motion (images): zoom_in zoom_out pan_left pan_right none. transition (how a clip comes in): cut fade dissolve fadeblack wipeleft wiperight slideleft slideright slideup slidedown circleopen zoomin, or {"type":"fade","duration":0.3}.
- format: landscape (1920x1080), vertical (1080x1920), square. captions: {"mode":"auto","words_per_chunk":3} or null. caption_style: {"uppercase":true,"position":"bottom|center|top","size":80,"color":"#FFFFFF"}. music: {"file":"x.mp3","volume":0.12,"duck":true}.
- Craft: cut on ideas; 2-5s per image; hard cuts within a scene, one soft transition (0.25-0.4s) per topic change; sfx sparingly (about one per 8-15s, volume 0.3-0.6; whoosh just before a cut, impact on a shocking fact, riser ~1.5s before a reveal); never stack effects. Use every file unless told otherwise.
- render: "final" when the video should be (re)built; "preview" only if the user wants a quick draft; null if you only answer a question.
- Only use sfx files and media files listed. Never invent names."""


def _extract_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("JSON object was cut off (reply too long)")


def decide(agent, state: str, sfx_text: str, user_message: str, history: list, tl: dict,
           transcript, validate, visuals, voice_seconds, emit=None, max_repairs: int = 2):
    """Returns (new_timeline, plan, usage). Raises llm.LLMError if the AI cannot be reached.
    validate(timeline) -> {'ok','errors',...}. Repairs send only the errors, not new context."""
    emit = emit or (lambda e: None)
    user = f"STATE\n{state}\n\nSFX (file|length|use)\n{sfx_text}\n\nREQUEST\n{user_message[:6000]}"
    hist = [{"role": m["role"], "content": m["content"][:600]} for m in history[-6:]]
    messages = [{"role": "system", "content": SYSTEM}] + hist + [{"role": "user", "content": user}]
    usage = {"prompt": 0, "completion": 0, "calls": 0}
    plan, new_tl, problem = None, tl, None
    for attempt in range(max_repairs + 1):
        text, u = llm.chat(agent, messages)
        usage["prompt"] += u["prompt"]
        usage["completion"] += u["completion"]
        usage["calls"] += 1
        problem = None
        try:
            plan = _extract_json(text)
            if not isinstance(plan, dict):
                raise ValueError("reply must be a JSON object")
        except ValueError as e:
            problem = f"Your reply was not usable JSON ({e}). Reply again with ONLY the JSON object."
        if problem is None:
            new_tl, errors = edit.apply_ops(tl, plan.get("ops"), transcript)
            if not errors:
                new_tl = edit.autoloop(new_tl, visuals, voice_seconds)
                v = validate(new_tl)
                errors = v["errors"] if not v["ok"] else []
            if errors:
                problem = ("These problems came up. Reply again with the corrected JSON (ops apply to the "
                           "ORIGINAL state):\n- " + "\n- ".join(errors[:8]))
        if problem is None:
            return new_tl, plan, usage
        if attempt < max_repairs:
            emit({"k": "status", "label": "AI made a mistake, asking it to fix (small retry)"})
            messages += [{"role": "assistant", "content": text[:3000]}, {"role": "user", "content": problem}]
    raise llm.LLMError("The AI could not produce a valid edit after retries: " + problem.splitlines()[0]
                       + " Try a stronger model, or simplify the request.")
