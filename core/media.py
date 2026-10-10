"""Zip extraction, ffprobe, and the asset manifest."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def kind_of(path) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return None


def probe(path) -> dict | None:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True)
    if p.returncode != 0:
        return None
    data = json.loads(p.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = data.get("format", {}).get("duration")
    return {
        "width": v.get("width") if v else None,
        "height": v.get("height") if v else None,
        "duration": float(dur) if dur not in (None, "N/A") else None,
        "has_audio": a is not None,
    }


def extract_zip(zip_path, dest) -> None:
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            name = m.filename
            if m.is_dir() or "__MACOSX" in name or Path(name).name.startswith("."):
                continue
            target = (dest / name).resolve()
            if root not in target.parents:  # zip-slip guard
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    # If everything sits inside one top-level folder, lift it up.
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in inner.iterdir():
            shutil.move(str(child), str(dest / child.name))
        inner.rmdir()


_VO_RE = re.compile(r"(voice|narrat|^vo$|^vo[_\-\s]|[_\-\s]vo$)", re.I)


def build_manifest(assets_dir) -> dict:
    assets_dir = Path(assets_dir)
    visuals, audio, warnings, ignored = [], [], [], []
    files = sorted((p for p in assets_dir.rglob("*") if p.is_file()),
                   key=lambda p: natural_key(p.relative_to(assets_dir).as_posix()))
    for p in files:
        rel = p.relative_to(assets_dir).as_posix()
        kind = kind_of(p)
        if kind is None:
            ignored.append(rel)
            continue
        info = probe(p)
        if info is None:
            warnings.append(f"could not read {rel} (corrupt or unsupported)")
            continue
        entry = {"file": rel, "kind": kind, "duration": None,
                 "width": info["width"], "height": info["height"]}
        if kind != "image":
            entry["duration"] = round(info["duration"] or 0, 3)
        if kind == "video":
            entry["has_audio"] = info["has_audio"]
            visuals.append(entry)
        elif kind == "image":
            visuals.append(entry)
        else:
            audio.append(entry)

    vo = None
    named = [a for a in audio if _VO_RE.search(Path(a["file"]).stem)]
    if len(named) == 1:
        vo = named[0]
    elif len(named) > 1:
        warnings.append("several audio files look like a voiceover: " + ", ".join(a["file"] for a in named))
    elif len(audio) == 1:
        vo = audio[0]
        warnings.append(f"no file named like a voiceover; assuming {vo['file']}")
    else:
        warnings.append("no voiceover found. Name it e.g. voiceover.mp3")
    return {
        "voiceover": vo["file"] if vo else None,
        "voiceover_duration": vo["duration"] if vo else None,
        "visuals": visuals,
        "other_audio": [a for a in audio if a is not vo],
        "ignored_files": ignored,
        "warnings": warnings,
    }
