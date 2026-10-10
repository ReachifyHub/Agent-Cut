"""Timeline edits as small operations, plus the compact STATE text the AI reads.

Ops let the AI say "start this clip when she says 'billion dollars'" instead of doing time maths."""
from __future__ import annotations

import copy

from . import timeline as tlmod
from .transcript import all_words, find_phrase

LEAD = 0.06
MAX_OPS = 80
CLIP_FIELDS = {"file", "start", "start_phrase", "duration", "motion", "transition", "trim_start", "loop"}
TOP_KEYS = {"width", "height", "format", "fps", "captions", "caption_style", "music", "voice_volume",
            "tail", "end"}
FORMATS = {"landscape": (1920, 1080), "vertical": (1080, 1920), "square": (1080, 1080)}


class EditError(ValueError):
    pass


def phrase_time(transcript, phrase, occurrence=1):
    if not transcript:
        raise EditError("no transcript yet, cannot use phrases")
    phrase = str(phrase).strip()
    words = phrase.split()
    tries = [phrase]
    if len(words) >= 3:  # forgive a slightly wrong first/last word
        tries += [" ".join(words[:-1]), " ".join(words[1:])]
    for p in tries:
        hits = find_phrase(transcript, p)
        if len(hits) >= int(occurrence):
            return hits[int(occurrence) - 1]["start"]
    raise EditError(f"phrase '{phrase}' not found in the narration. Copy words exactly from STATE")


def _clip_fields(fields, transcript):
    f = dict(fields or {})
    if "start_phrase" in f:
        ph = f.pop("start_phrase")
        if ph is not None:
            f["start"] = round(max(phrase_time(transcript, ph, f.pop("occurrence", 1)) - LEAD, 0.0), 2)
    bad = set(f) - CLIP_FIELDS
    if bad:
        raise EditError(f"unknown clip field(s) {sorted(bad)}; allowed: {sorted(CLIP_FIELDS)}")
    return f


def _idx(op, key, n):
    i = op[key]
    if not isinstance(i, int) or not -n <= i < n:
        raise EditError(f"{key}={i} is out of range (0..{n - 1})")
    return i


def apply_ops(tl: dict, ops, transcript):
    """Returns (new_timeline, errors). The input timeline is never modified."""
    tl = copy.deepcopy(tl)
    if ops in (None, ""):
        ops = []
    if not isinstance(ops, list):
        return tl, ["ops must be a list"]
    clips = tl.setdefault("clips", [])
    errors = []
    for n, op in enumerate(ops[:MAX_OPS]):
        k = op.get("op") if isinstance(op, dict) else None
        try:
            if k == "set_clip":
                c = clips[_idx(op, "index", len(clips))]
                for key, val in _clip_fields(op.get("fields"), transcript).items():
                    if val is None:
                        c.pop(key, None)
                    else:
                        c[key] = val
            elif k == "swap":
                a, b = _idx(op, "a", len(clips)), _idx(op, "b", len(clips))
                clips[a]["file"], clips[b]["file"] = clips[b]["file"], clips[a]["file"]
            elif k == "reorder":
                order = op.get("order")
                if not isinstance(order, list) or len(order) != len(clips):
                    raise EditError(f"order must list exactly {len(clips)} files (one per clip slot)")
                for c, f in zip(clips, order):
                    c["file"] = f
            elif k == "insert_clip":
                clip = _clip_fields(op.get("clip"), transcript)
                if "file" not in clip or "start" not in clip:
                    raise EditError("insert_clip needs file and start (or start_phrase)")
                clips.insert(max(0, min(int(op.get("index", len(clips))), len(clips))), clip)
            elif k == "remove_clip":
                clips.pop(_idx(op, "index", len(clips)))
            elif k == "add_sfx":
                if op.get("at_phrase"):
                    at = phrase_time(transcript, op["at_phrase"], op.get("occurrence", 1))
                else:
                    at = float(op["at"])
                at = round(max(at + float(op.get("offset", 0)), 0.0), 2)
                tl.setdefault("sfx", []).append(
                    {"file": op["file"], "at": at, "volume": float(op.get("volume", 0.5))})
            elif k == "remove_sfx":
                tl.get("sfx", []).pop(_idx(op, "index", len(tl.get("sfx", []))))
            elif k == "set":
                key, val = op["key"], op.get("value")
                if key not in TOP_KEYS:
                    raise EditError(f"cannot set '{key}'; allowed: {sorted(TOP_KEYS)}")
                if key == "format":
                    if val not in FORMATS:
                        raise EditError(f"format must be one of {sorted(FORMATS)}")
                    tl["width"], tl["height"] = FORMATS[val]
                elif val is None:
                    tl.pop(key, None)
                else:
                    tl[key] = val
            else:
                raise EditError(f"unknown op '{k}'. Use set_clip, swap, reorder, insert_clip, remove_clip, "
                                "add_sfx, remove_sfx, set")
        except EditError as e:
            errors.append(f"op {n} ({k}): {e}")
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
            errors.append(f"op {n} ({k}): bad or missing field {e!r}")
    if len(ops) > MAX_OPS:
        errors.append(f"too many ops ({len(ops)}); max {MAX_OPS}")
    return tl, errors


def autoloop(tl: dict, visuals: dict, voice_seconds):
    """Silently set loop:true on video clips that are shorter than their slot (saves a repair call)."""
    clips = tl.get("clips") or []
    end = tl.get("end") or ((voice_seconds or 0) + float(tl.get("tail", 0.6)))
    for i, c in enumerate(clips):
        v = visuals.get(c.get("file"))
        if not v or v["kind"] != "video" or c.get("loop"):
            continue
        s = c.get("start", 0.0) or 0.0
        if c.get("duration") is not None:
            d = float(c["duration"])
        elif i + 1 < len(clips) and clips[i + 1].get("start") is not None:
            d = float(clips[i + 1]["start"]) - float(s)
        else:
            d = float(end) - float(s)
        if float(c.get("trim_start", 0) or 0) + d > (v.get("duration") or 0) + 0.1:
            c["loop"] = True
    return tl


def state_text(ctx: dict, tl: dict, transcript, word_budget: int = 900) -> str:
    """The AI's whole view of the project, as compact text."""
    res, errors, _ = tlmod.resolve(tl, ctx)
    words = all_words(transcript) if transcript else []
    cap = tl.get("captions")
    cap_s = ("off" if not cap else "custom" if isinstance(cap, list)
             else f"auto,{cap.get('words_per_chunk', 3)} words" if isinstance(cap, dict) else "auto")
    music = tl.get("music")
    head = (f"size {tl.get('width', 1920)}x{tl.get('height', 1080)} | captions {cap_s} | music "
            f"{(music or {}).get('file', 'none')}")
    if not res:
        return head + "\nTIMELINE INVALID: " + "; ".join(errors[:3])
    n = len(res["clips"])
    per = max(8, min(45, word_budget // max(n, 1)))
    rows = []
    for i, c in enumerate(res["clips"]):
        a, b = c["start"], c["start"] + c["duration"]
        spoken = [w["w"] for w in words if a - 0.05 <= w["start"] < b - 0.05]
        text = " ".join(spoken[:per]) + (" ..." if len(spoken) > per else "")
        kind = c.get("motion") or ("video-loop" if c.get("loop") else "video")
        rows.append(f"{i}|{c['file']}|{a:.1f}-{b:.1f}|{kind}|{c['transition']}|{text}")
    out = [head + f" | length {res['total_s']:.1f}s",
           "clips (index|file|start-end|motion|transition-in|spoken):"] + rows
    if tl.get("sfx"):
        out.append("sfx (index|file|at|volume):")
        out += [f"{i}|{e.get('file')}|{e.get('at')}|{e.get('volume', 0.7)}" for i, e in enumerate(tl["sfx"])]
    vids = [f"{f} {v['duration']:.1f}s" for f, v in ctx["visuals"].items() if v["kind"] == "video"]
    if vids:
        out.append("video lengths: " + ", ".join(vids))
    used = {c["file"] for c in res["clips"]}
    unused = [f for f in ctx["visuals"] if f not in used]
    if unused:
        out.append("unused files: " + ", ".join(unused))
    return "\n".join(out)
