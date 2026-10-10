"""Place each image on the exact moment its narration line is spoken.

The user's brief says which narration belongs to which image. We read those lines, align them against
Whisper's word timestamps (one global alignment, so it survives misheard words), and start every
image where its line starts. No AI call, no tokens."""
from __future__ import annotations

import json
import re
import statistics
from difflib import SequenceMatcher

from .transcript import all_words

EXT = r"(?:jpe?g|png|webp|gif|bmp|mp4|mov|mkv|webm|m4v)"
FILE_RE = re.compile(r"[^\s\"'|]+\." + EXT + r"\b", re.I)
PIPE_RE = re.compile(r"^\s*[-*\u2022>\d.)\s]*(?P<f>\S+\." + EXT + r")\s*(?:\||->|=>|:|\u2013|\u2014)\s*(?P<t>\S.*?)\s*$", re.I)
STOP = set("the a an of to and in is it he she they that this on at for with as was were his her their be by "
           "from or but so then than there here".split())
LEAD = 0.06
MIN_UNIT = 1.0
SCRIPT_FILES = ("scenes.txt", "script.txt", "storyboard.txt", "plan.txt")


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text).lower().replace("'", "").replace("\u2019", ""))


def resolve_file(token: str, files: list[str]):
    """Map a filename from the script to a real project file (exact, then loose, then by number prefix)."""
    t = token.strip().strip("`*,;()[]")
    by_lower = {f.lower(): f for f in files}
    if t.lower() in by_lower:
        return by_lower[t.lower()]
    base = t.rsplit("/", 1)[-1].lower()
    for f in files:
        if f.rsplit("/", 1)[-1].lower() == base:
            return f
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.rsplit("/", 1)[-1].lower())
    hits = [f for f in files if norm(f) == norm(t)]
    if len(hits) == 1:
        return hits[0]
    m = re.match(r"(\d{1,3})__", base)
    if m:
        hits = [f for f in files if re.match(rf"0*{int(m.group(1))}__", f.rsplit('/', 1)[-1].lower())]
        if len(hits) == 1:
            return hits[0]
    return None


def _quoted(text: str) -> str:
    q = re.findall(r"[\"\u201c](.+?)[\"\u201d]", text, flags=re.S)
    return " ".join(q) if q else text


def parse_scenes(text: str, files: list[str]):
    """Read scene lines from free text. Returns (scenes, remaining_text).

    Understands both:  `03.jpg | narration line`   and the block form
    IMAGE 03:\\n03.jpg\\n\\nNARRATION:\\n"..."\\n\\nPURPOSE: ...
    """
    lines = (text or "").splitlines()
    scenes, used = [], set()
    i = 0
    while i < len(lines):
        raw = lines[i]
        m = PIPE_RE.match(raw)
        if m and resolve_file(m.group("f"), files):
            scenes.append({"file": resolve_file(m.group("f"), files), "text": _quoted(m.group("t"))})
            used.add(i)
            i += 1
            continue
        stripped = raw.strip().strip("-*\u2022 `")
        if FILE_RE.fullmatch(stripped) and resolve_file(stripped, files):
            f = resolve_file(stripped, files)
            j, narr, found = i + 1, [], False
            while j < len(lines):
                s = lines[j].strip()
                nxt = s.strip("-*\u2022 `")
                if (FILE_RE.fullmatch(nxt) and resolve_file(nxt, files)) or re.match(r"^IMAGE\s*\d+\s*:", s, re.I) \
                        or s.startswith("====="):
                    break
                if not found and re.match(r"^NARRATION\s*:?\s*$", s, re.I):
                    found = True
                elif not found and re.match(r"^NARRATION\s*:\s*\S", s, re.I):
                    found = True
                    narr.append(s.split(":", 1)[1].strip())
                elif found:
                    if not s:
                        if narr:
                            break
                    else:
                        narr.append(s)
                j += 1
            while narr and j < len(lines):  # labelled paragraphs after the narration belong to the block
                k = j
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and re.match(r"^(PURPOSE|IMPORTANT|NOTE)\b", lines[k].strip(), re.I):
                    while k < len(lines) and lines[k].strip():
                        k += 1
                    j = k
                else:
                    break
            if narr:
                scenes.append({"file": f, "text": _quoted(" ".join(narr))})
                used.update(range(i - 1 if i > 0 and re.match(r"^IMAGE\s*\d+\s*:", lines[i - 1].strip(), re.I) else i, j))
            i = j if narr else i + 1
            continue
        i += 1
    rest = "\n".join(l for k, l in enumerate(lines) if k not in used)
    rest = re.sub(r"\n{3,}", "\n\n", rest).strip()
    return scenes, rest


def read_script_file(project_assets, files: list[str]):
    for name in SCRIPT_FILES:
        for f in project_assets.glob("**/" + name):
            return parse_scenes(f.read_text(errors="replace"), files)[0]
    return []


def align(scenes: list[dict], transcript: dict):
    """Returns (placed, skipped). placed = [{files:[..], start_s, text, matched, total}] in script order."""
    words = all_words(transcript)
    tok, widx = [], []
    for wi, w in enumerate(words):
        for t in _tokens(w["w"]):
            tok.append(t)
            widx.append(wi)
    # merge scenes that share the same narration (e.g. prepared image + real photo)
    units, by_key = [], {}
    for s in scenes:
        key = " ".join(_tokens(s["text"]))
        if key in by_key:  # same narration listed twice (prepared image + real photo): share one slot
            if s["file"] not in by_key[key]["files"]:
                by_key[key]["files"].append(s["file"])
        else:
            u = {"key": key, "files": [s["file"]], "text": s["text"], "tokens": _tokens(s["text"])}
            by_key[key] = u
            units.append(u)
    script_tok, owner = [], []
    for ui, u in enumerate(units):
        for t in u["tokens"]:
            script_tok.append(t)
            owner.append(ui)
    sm = SequenceMatcher(None, script_tok, tok, autojunk=False)
    pairs = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            pairs[a + k] = b + k
    placed, skipped, prev_idx = [], [], -1
    base = 0
    for ui, u in enumerate(units):
        n = len(u["tokens"])
        good = [(p - base, pairs[p]) for p in range(base, base + n)
                if p in pairs and script_tok[p] not in STOP]
        base += n
        nonstop = sum(1 for t in u["tokens"] if t not in STOP)
        need = 1 if nonstop <= 3 else 2
        coverage = len(good) / max(nonstop, 1)
        if len(good) < need or coverage < 0.35:
            skipped.append({"files": u["files"], "text": u["text"], "reason": "narration not found in the voiceover"})
            continue
        est = int(round(statistics.median(t - p for p, t in good)))
        est = min(est, min(t for _, t in good))
        est = max(est, prev_idx + 1, 0)
        if est >= len(tok):
            skipped.append({"files": u["files"], "text": u["text"], "reason": "found after the end of the audio"})
            continue
        prev_idx = est
        w = words[widx[est]]
        placed.append({"files": u["files"], "start_s": max(round(w["start"] - LEAD, 2), 0.0), "text": u["text"],
                       "matched": len(good), "total": nonstop, "gap": (w["start"] - words[widx[est] - 1]["end"])
                       if widx[est] > 0 else 9.0})
    return placed, skipped


def build_timeline(manifest: dict, transcript: dict, placed: list[dict], fmt: str, captions: bool = True,
                   words_per_chunk: int = 3):
    from .autoplan import FORMATS
    duration = manifest.get("voiceover_duration") or transcript.get("duration") or 0
    W, H = FORMATS[fmt]
    vis = {v["file"]: v for v in manifest["visuals"]}
    end = duration + 0.6
    slots = []  # (file, start) with groups split evenly
    for k, p in enumerate(placed):
        nxt = placed[k + 1]["start_s"] if k + 1 < len(placed) else end
        n = len(p["files"])
        span = max(nxt - p["start_s"], 0.2)
        step = span / n
        for j, f in enumerate(p["files"]):
            slots.append({"file": f, "start": round(p["start_s"] + j * step, 2), "gap": p["gap"] if j == 0 else 0.0})
    slots.sort(key=lambda s: s["start"])
    clips = []
    for i, s in enumerate(slots):
        e = slots[i + 1]["start"] if i + 1 < len(slots) else end
        c = {"file": s["file"], "start": 0.0 if i == 0 else s["start"]}
        if i > 0 and s["gap"] >= 0.5:
            c["transition"] = {"type": "fade", "duration": 0.3}
        v = vis.get(s["file"], {})
        if v.get("kind") == "video" and e - s["start"] > (v.get("duration") or 0) + 0.1:
            c["loop"] = True
        clips.append(c)
    tl = {"width": W, "height": H, "fps": 30, "clips": clips}
    if manifest.get("other_audio"):
        tl["music"] = {"file": manifest["other_audio"][0]["file"], "volume": 0.1, "duck": True}
    if captions:
        tl["captions"] = {"mode": "auto", "words_per_chunk": words_per_chunk}
        tl["caption_style"] = {"uppercase": True, "position": "bottom"}
    return tl


def report(placed, skipped, manifest, scenes_total):
    used = {f for p in placed for f in p["files"]}
    n = len(used)
    out = f"Placed {n} images on their narration lines."
    if skipped:
        names = [f.split("__")[0] + ("" if "__" in f else "") if re.match(r"\d+__", f) else f for s in skipped for f in s["files"]]
        out += f" Left out (their line was not found in the voiceover): {', '.join(names[:12])}."
    unlisted = [v["file"] for v in manifest["visuals"] if v["file"] not in used and v["file"] not in
                {f for s in skipped for f in s["files"]}]
    if unlisted:
        short = [f.split("__")[0] if re.match(r"\d+__", f) else f for f in unlisted]
        out += f" Not in your script lines: {', '.join(short[:12])}."
    return out
