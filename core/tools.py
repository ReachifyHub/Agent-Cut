"""Project storage + the plain functions used by the CLI and the MCP server."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import autoplan, edit, media, render as renderer, timeline as tlmod, transcript as tr

REPO = Path(__file__).resolve().parent.parent
SFX_DIR = Path(os.environ.get("AGENTCUT_SFX", REPO / "sfx"))


def sfx_lines() -> list[str]:
    """'file | length | use' per sound effect, from sfx/sfx.json plus any extra audio files."""
    meta = {}
    idx = SFX_DIR / "sfx.json"
    if idx.exists():
        for e in json.loads(idx.read_text()):
            meta[e["file"]] = e
    rows = []
    for p in sorted(SFX_DIR.glob("*"), key=lambda p: media.natural_key(p.name)):
        if media.kind_of(p) != "audio":
            continue
        desc = meta.get(p.name, {}).get("description", p.stem.replace("_", " ").replace("-", " "))
        info = media.probe(p)
        dur = f"{info['duration']:.1f}s" if info and info["duration"] else "?"
        rows.append(f"{p.name}|{dur}|{desc}")
    return rows


class Project:
    def __init__(self, root):
        self.root = Path(root)
        self.assets = self.root / "assets"
        self.work = self.root / "work"
        self.output = self.root / "output"
        self.manifest_f = self.root / "manifest.json"
        self.transcript_f = self.root / "transcript.json"
        self.timeline_f = self.root / "timeline.json"
        self.chat_f = self.root / "chat.json"

    def _load(self, f):
        return json.loads(f.read_text()) if f.exists() else None

    def manifest(self):
        m = self._load(self.manifest_f)
        if m is None:
            raise ValueError("No project loaded. Run init with the zip file first.")
        return m

    def transcript(self):
        return self._load(self.transcript_f)

    def need_transcript(self):
        t = self.transcript()
        if t is None:
            raise ValueError("Not transcribed yet. Run transcribe first.")
        return t

    def timeline(self):
        return self._load(self.timeline_f)

    def save_timeline(self, tl):
        self.timeline_f.write_text(json.dumps(tl, indent=1))

    def context(self) -> dict:
        m = self.manifest()
        audio = {a["file"]: a["duration"] for a in m["other_audio"]}
        if m["voiceover"]:
            audio[m["voiceover"]] = m["voiceover_duration"]
        sfx_files = {p.name: p for p in SFX_DIR.glob("*") if media.kind_of(p) == "audio"}
        sfx_files.update({p.stem: p for p in list(sfx_files.values())})

        def find_audio(name):
            if not name:
                return None
            if name in audio:
                return self.assets / name
            return sfx_files.get(Path(name).name) or sfx_files.get(Path(name).stem)

        return {"visuals": {v["file"]: v for v in m["visuals"]}, "audio": audio,
                "voiceover": m["voiceover"], "audio_path": lambda n: self.assets / n,
                "find_audio": find_audio, "transcript": self.transcript()}

    def validate(self, tl) -> dict:
        return tlmod.validate(tl, self.context())

    # ---- tools (CLI / MCP)
    def init(self, zip_path, keep_old=False) -> dict:
        if not keep_old and self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        media.extract_zip(zip_path, self.assets)
        m = media.build_manifest(self.assets)
        self.manifest_f.write_text(json.dumps(m, indent=1))
        return m

    def summary(self) -> dict:
        """Short manifest view (few tokens)."""
        m = self.manifest()
        order = [v["file"] if v["kind"] == "image" else f"{v['file']} (video {v['duration']}s)"
                 for v in m["visuals"]]
        out = {"voiceover": m["voiceover"], "voiceover_seconds": m["voiceover_duration"],
               "files_in_order": order,
               "other_audio": [f"{a['file']} ({a['duration']}s)" for a in m["other_audio"]],
               "transcribed": self.transcript_f.exists(), "timeline_saved": self.timeline_f.exists()}
        if m["warnings"]:
            out["warnings"] = m["warnings"]
        return out

    def transcribe(self, model=None, language=None, progress=None) -> dict:
        m = self.manifest()
        if not m["voiceover"]:
            raise ValueError("No voiceover file in the zip. Name it voiceover.mp3")
        t = tr.transcribe(self.assets / m["voiceover"], self.transcript_f, model, language, progress)
        return {"language": t["language"], "duration": t["duration"], "words": len(tr.all_words(t))}

    def get_transcript(self, detail="segments", start=None, end=None) -> dict:
        return tr.lines(self.need_transcript(), detail, start, end)

    def find(self, phrase) -> dict:
        hits = tr.find_phrase(self.need_transcript(), phrase)
        return {"phrase": phrase, "matches": hits}

    def list_sfx(self) -> dict:
        return {"sfx_file|length|use": sfx_lines()}

    def draft(self, format=None, words_per_chunk=3, transition_sfx=None, captions=True, hint="") -> dict:
        m = self.manifest()
        fmt = format or autoplan.guess_format(m, hint)
        tl = autoplan.draft(m, self.need_transcript(), fmt, int(words_per_chunk), transition_sfx, bool(captions))
        v = self.validate(tl)
        if v["ok"]:
            self.save_timeline(tl)
        v["format"] = fmt
        return v

    def show(self) -> dict:
        tl = self.timeline()
        if tl is None:
            raise ValueError("No timeline yet. Run draft first.")
        return {"state": edit.state_text(self.context(), tl, self.transcript()).splitlines()}

    def patch(self, ops) -> dict:
        tl = self.timeline()
        if tl is None:
            raise ValueError("No timeline yet. Run draft first.")
        new, errors = edit.apply_ops(tl, ops, self.transcript())
        if errors:
            return {"ok": False, "errors": errors}
        ctx = self.context()
        new = edit.autoloop(new, ctx["visuals"], self.manifest()["voiceover_duration"])
        v = tlmod.validate(new, ctx)
        if v["ok"]:
            self.save_timeline(new)
        return v

    def write(self, timeline) -> dict:
        if isinstance(timeline, str):
            timeline = json.loads(timeline)
        v = self.validate(timeline)
        if v["ok"]:
            self.save_timeline(timeline)
        return v

    def render(self, quality="preview", progress=None) -> dict:
        preview = quality != "final"
        tl = self.timeline()
        if tl is None:
            raise ValueError("No timeline yet. Run draft first.")
        res, errors, warnings = tlmod.resolve(tl, self.context())
        if errors:
            return {"ok": False, "errors": errors}
        name = "preview.mp4" if preview else "final.mp4"
        info = renderer.render(res, self.assets, self.work, self.output / name, preview=preview,
                               progress=progress)
        info.update(ok=True, file=name, warnings=warnings)
        return info


def project_from_env(name=None) -> Project:
    base = Path(os.environ.get("AGENTCUT_WORKSPACE", Path.cwd() / "workspace"))
    return Project(base / (name or os.environ.get("AGENTCUT_PROJECT", "default")))
