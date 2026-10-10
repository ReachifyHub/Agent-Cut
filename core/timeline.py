"""Timeline JSON: resolve to frame-exact clips and validate."""
from __future__ import annotations

from pathlib import Path

from .transcript import auto_captions

XFADE = {"fade", "fadeblack", "fadewhite", "dissolve", "wipeleft", "wiperight", "wipeup",
         "wipedown", "slideleft", "slideright", "slideup", "slidedown", "circleopen",
         "circleclose", "pixelize", "radial", "zoomin"}
MOTIONS = {"none", "zoom_in", "zoom_out", "pan_left", "pan_right", "auto"}
AUTO_CYCLE = ["zoom_in", "pan_right", "zoom_out", "pan_left"]


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _transition(t):
    if t in (None, "", "cut", "none"):
        return "cut", 0.0
    if isinstance(t, str):
        return t, 0.3
    if isinstance(t, dict):
        ty = t.get("type", "fade")
        if ty in ("cut", "none"):
            return "cut", 0.0
        return ty, _f(t.get("duration"), 0.3)
    return "?", 0.0


def resolve(tl: dict, ctx: dict):
    """Returns (resolved_or_None, errors, warnings). ctx comes from Project.context()."""
    errors, warnings = [], []
    W, H, fps = int(tl.get("width", 1920)), int(tl.get("height", 1080)), int(tl.get("fps", 30))
    if W % 2 or H % 2 or W < 64 or H < 64:
        errors.append("width/height must be even numbers (e.g. 1920x1080 or 1080x1920)")
    if not 12 <= fps <= 60:
        errors.append("fps must be between 12 and 60")
    clips = tl.get("clips") or []
    if not clips:
        return None, ["timeline has no clips"], warnings

    vo = tl.get("voiceover") or ctx.get("voiceover")
    vo_dur = ctx["audio"].get(vo) if vo else None
    if vo and vo_dur is None:
        errors.append(f"voiceover '{vo}' not found in the zip")
    tail = _f(tl.get("tail"), 0.6)
    end_time = _f(tl.get("end"))
    if end_time is None and vo_dur:
        end_time = vo_dur + tail

    out, cursor, prev_end_f = [], 0.0, 0
    for i, c in enumerate(clips):
        label = f"clip {i} ({c.get('file')})"
        info = ctx["visuals"].get(c.get("file"))
        if info is None:
            errors.append(f"{label}: file not found among images/videos. Use names from get_manifest")
            continue
        s = _f(c.get("start"))
        if i == 0:
            if s not in (None, 0.0) and s > 0.05:
                warnings.append(f"{label}: first clip must start at 0; moved from {s} to 0")
            s = 0.0
        elif s is None:
            s = cursor
        elif abs(s - cursor) > 0.06:
            errors.append(f"{label}: starts at {s}s but the previous clip ends at {cursor:.2f}s "
                          "(gap or overlap). Set start = previous end, or fix the previous duration")
            s = cursor
        else:
            s = cursor
        d = _f(c.get("duration"))
        if d is None:
            nxt = _f(clips[i + 1].get("start")) if i + 1 < len(clips) else end_time
            if nxt is None:
                errors.append(f"{label}: needs 'duration' (or 'start' on the next clip)")
                continue
            d = nxt - s
        if d < 0.15:
            errors.append(f"{label}: duration {d:.2f}s is too short (min 0.15s)")
            continue
        start_f = prev_end_f
        end_f = round((s + d) * fps)
        frames = end_f - start_f
        if frames < 2:
            errors.append(f"{label}: shorter than 2 frames")
            continue

        r = {"file": c["file"], "kind": info["kind"], "start": s, "duration": d,
             "start_f": start_f, "frames": frames}
        if info["kind"] == "image":
            m = c.get("motion", "auto")
            if m not in MOTIONS:
                errors.append(f"{label}: unknown motion '{m}'. Use one of {sorted(MOTIONS)}")
                m = "none"
            if m == "auto":
                m = AUTO_CYCLE[i % len(AUTO_CYCLE)]
            r["motion"] = m
        else:
            ts = _f(c.get("trim_start"), 0.0)
            loop = bool(c.get("loop", False))
            src = info.get("duration") or 0
            if ts + d > src + 0.1 and not loop:
                errors.append(f"{label}: needs {ts + d:.2f}s of source but '{c['file']}' is only "
                              f"{src:.2f}s long. Shorten the duration, lower trim_start, or set \"loop\": true")
            r.update(trim_start=ts, loop=loop, speed=1.0)
        ty, td = _transition(c.get("transition"))
        if i == 0:
            if ty != "cut":
                warnings.append(f"{label}: transition on the first clip is ignored")
            ty, td = "cut", 0.0
        elif ty != "cut" and ty not in XFADE:
            errors.append(f"{label}: unknown transition '{ty}'. Use cut or one of {sorted(XFADE)}")
            ty, td = "cut", 0.0
        r["transition"] = ty
        r["transition_s"] = td
        out.append(r)
        cursor, prev_end_f = s + d, end_f

    if errors:
        return None, errors, warnings

    # transition frames (clamped so a transition never outlasts its neighbours)
    for i, r in enumerate(out):
        if i == 0:
            r["tf"] = 0
            continue
        if r["transition"] == "cut":
            r["tf"] = 1
        else:
            want = max(1, round(r["transition_s"] * fps))
            cap = max(1, min(out[i - 1]["frames"], r["frames"]) // 2)
            if want > cap:
                warnings.append(f"clip {i} ({r['file']}): transition shortened to fit the clip")
            r["tf"] = min(want, cap)
    total_f = out[-1]["start_f"] + out[-1]["frames"]
    total_s = total_f / fps

    if vo_dur and total_s < vo_dur - 0.25:
        warnings.append(f"video is {total_s:.1f}s but the voiceover is {vo_dur:.1f}s: the end of the "
                        "narration will have no picture. Extend the last clip")
    used = {r["file"] for r in out}
    unused = [v for v in ctx["visuals"] if v not in used]
    if unused:
        warnings.append("files not used: " + ", ".join(unused))

    # sfx
    sfx = []
    for j, e in enumerate(tl.get("sfx") or []):
        name = e.get("file")
        path = ctx["find_audio"](name)
        at = _f(e.get("at"))
        if path is None:
            errors.append(f"sfx {j}: '{name}' not found (see list_sfx)")
            continue
        if at is None or not 0 <= at <= total_s:
            errors.append(f"sfx {j} ({name}): 'at' must be between 0 and {total_s:.1f}")
            continue
        sfx.append({"path": str(path), "at": at, "volume": min(max(_f(e.get("volume"), 0.7), 0), 2)})

    music = None
    m = tl.get("music")
    if m:
        mp = ctx["find_audio"](m.get("file"))
        if mp is None:
            errors.append(f"music '{m.get('file')}' not found")
        else:
            music = {"path": str(mp), "volume": _f(m.get("volume"), 0.12),
                     "duck": bool(m.get("duck", True)), "fade_out": _f(m.get("fade_out"), 1.5)}

    # captions
    caps, cap_style = [], dict(tl.get("caption_style") or {})
    c = tl.get("captions")
    if c in ("auto",) or (isinstance(c, dict) and c.get("mode") == "auto"):
        opts = c if isinstance(c, dict) else {}
        if not ctx.get("transcript"):
            errors.append("captions 'auto' needs a transcript: run transcribe first")
        else:
            caps = auto_captions(ctx["transcript"], int(opts.get("words_per_chunk", 3)),
                                 int(opts.get("max_chars", 28)))
    elif isinstance(c, list):
        for k, item in enumerate(c):
            a, b = _f(item.get("start")), _f(item.get("end"))
            if not item.get("text") or a is None or b is None or b <= a:
                errors.append(f"caption {k}: needs text, start, end (end > start)")
            else:
                caps.append({"text": str(item["text"]), "start": a, "end": b})
    elif c not in (None, False):
        errors.append("captions must be 'auto', {mode:'auto',...}, a list, or omitted")

    if errors:
        return None, errors, warnings
    return {"width": W, "height": H, "fps": fps, "total_frames": total_f, "total_s": total_s,
            "clips": out, "voiceover": ctx["audio_path"](vo) if vo else None,
            "voice_volume": _f(tl.get("voice_volume"), 1.0), "sfx": sfx, "music": music,
            "captions": caps, "caption_style": cap_style}, errors, warnings


def validate(tl: dict, ctx: dict) -> dict:
    res, errors, warnings = resolve(tl, ctx)
    summary = None
    if res:
        summary = {"total_seconds": round(res["total_s"], 2), "clips": len(res["clips"]),
                   "sfx": len(res["sfx"]), "captions": len(res["captions"]),
                   "resolution": f"{res['width']}x{res['height']}@{res['fps']}"}
    return {"ok": not errors, "errors": errors, "warnings": warnings, "summary": summary}
