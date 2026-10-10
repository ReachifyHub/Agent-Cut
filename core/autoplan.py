"""Build a first-draft timeline from the manifest and transcript. No AI model involved."""
from __future__ import annotations

from .transcript import all_words

FORMATS = {"landscape": (1920, 1080), "vertical": (1080, 1920), "square": (1080, 1080)}
MIN_CLIP = 1.2   # seconds, shortest clip the planner will make
LEAD = 0.06      # cut slightly before the first word of the new idea


def _candidates(words):
    """Moments where the narration changes idea: after . ! ? , ; : or a pause."""
    out = []
    for i in range(1, len(words)):
        prev = words[i - 1]["w"]
        gap = words[i]["start"] - words[i - 1]["end"]
        strong = prev.endswith((".", "!", "?"))
        weak = prev.endswith((",", ";", ":")) or gap >= 0.45
        if strong or weak:
            out.append({"t": words[i]["start"], "strong": strong, "gap": gap})
    return out


def _pick_cuts(n, duration, cands):
    cuts, prev = [], 0.0
    for k in range(1, n):
        target = k * duration / n
        lo = prev + MIN_CLIP
        hi = duration - MIN_CLIP * (n - k)
        pool = [c for c in cands if lo <= c["t"] <= hi]
        if pool:
            best = min(pool, key=lambda c: abs(c["t"] - target) - (1.5 if c["strong"] else 0.0))
        else:
            t = min(max(target, lo), max(hi, lo))
            best = {"t": round(t, 2), "strong": False, "gap": 0.0}
        cuts.append(best)
        prev = best["t"]
    return cuts


def draft(manifest: dict, transcript: dict, fmt: str = "landscape", words_per_chunk: int = 3,
          transition_sfx: str | None = None, captions: bool = True) -> dict:
    visuals = manifest["visuals"]
    if not visuals:
        raise ValueError("no images or videos in the project")
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {sorted(FORMATS)}")
    duration = manifest.get("voiceover_duration") or transcript.get("duration")
    if not duration:
        raise ValueError("voiceover length unknown")
    W, H = FORMATS[fmt]
    cuts = _pick_cuts(len(visuals), duration, _candidates(all_words(transcript)))
    starts = [0.0]
    for c in cuts:
        starts.append(round(max(c["t"] - LEAD, starts[-1] + 0.2), 2))
    end = duration + 0.6

    clips, sfx = [], []
    for i, v in enumerate(visuals):
        e = starts[i + 1] if i + 1 < len(starts) else end
        clip = {"file": v["file"], "start": starts[i]}
        if i > 0 and cuts[i - 1]["gap"] >= 0.5:  # a real pause = topic change = soft transition
            clip["transition"] = {"type": "fade", "duration": 0.3}
            if transition_sfx:
                sfx.append({"file": transition_sfx, "at": round(max(starts[i] - 0.1, 0), 2), "volume": 0.45})
        if v["kind"] == "video" and (e - starts[i]) > (v.get("duration") or 0) + 0.1:
            clip["loop"] = True
        clips.append(clip)

    tl = {"width": W, "height": H, "fps": 30, "clips": clips}
    if sfx:
        tl["sfx"] = sfx
    if manifest.get("other_audio"):
        tl["music"] = {"file": manifest["other_audio"][0]["file"], "volume": 0.12, "duck": True}
    if captions:
        tl["captions"] = {"mode": "auto", "words_per_chunk": words_per_chunk}
        tl["caption_style"] = {"uppercase": True, "position": "bottom"}
    return tl


_VERT = ("vertical", "portrait", "tiktok", "reel", "youtube short", "9:16", "9x16", "1080x1920")
_SQ = ("square", "1:1", "1080x1080")
_LAND = ("landscape", "horizontal", "widescreen", "16:9", "16x9", "1920x1080")


def guess_format(manifest: dict, message: str = "") -> str:
    """Pick landscape/vertical/square from the user's words, else from the shape of their files."""
    m = (message or "").lower()
    flat = m.replace(" ", "")
    for name, keys in (("square", _SQ), ("vertical", _VERT), ("landscape", _LAND)):
        if any(k in m or k in flat for k in keys):
            return name
    vis = [v for v in manifest.get("visuals", []) if v.get("width") and v.get("height")]
    if vis and sum(1 for v in vis if v["height"] > v["width"]) > len(vis) / 2:
        return "vertical"
    return "landscape"
