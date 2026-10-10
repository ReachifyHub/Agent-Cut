"""One chat turn, end to end. Used by the Modal app and the CLI `chat` command.

Order: transcribe (if needed) -> first-cut draft (if needed) -> ONE AI call -> apply -> render.
Messages starting with / skip the AI completely: /render, /preview, /draft [format], /align.

If the message holds scene lines (`file | narration` or IMAGE blocks) each image is placed on the moment its
narration is spoken, by code, before the AI is called."""
from __future__ import annotations

import json

from . import autoplan, director, edit, llm, script_align
from .tools import Project, sfx_lines

KEEP_HISTORY = 8


def run_turn(project: Project, message: str, agent: dict, history: list, emit=None,
             transcriber=None, checkpoint=None) -> list:
    """Returns the updated chat history (plain user/assistant text only)."""
    emit = emit or (lambda e: None)
    checkpoint = checkpoint or (lambda: None)
    history = [m for m in (history or []) if m.get("role") in ("user", "assistant") and m.get("content")]
    msg = (message or "").strip()
    cmd = msg.lower().split()[0] if msg.startswith("/") else None
    shown = msg if len(msg) <= 1500 else msg[:1500] + " ..."
    new_hist = history + [{"role": "user", "content": shown}]

    def say(text):
        emit({"k": "say", "text": text})
        new_hist.append({"role": "assistant", "content": text})

    stage = {"name": None}

    def progress(label, pct=None):
        name = label.split(" (")[0].replace(" with transitions", "")
        if stage["name"] != name:
            stage["name"] = name
            emit({"k": "status", "label": name})
        emit({"k": "pct", "v": pct, "label": label})

    def render(quality):
        stage["name"] = None
        r = project.render(quality, progress=progress)
        if not r.get("ok"):
            raise RuntimeError("; ".join(r.get("errors", ["render failed"]))[:300])
        emit({"k": "file", "file": r["file"], "seconds": r["seconds"], "size_mb": r["size_mb"],
              "resolution": r["resolution"]})
        checkpoint()
        return r

    try:
        manifest = project.manifest()
        if not manifest["visuals"]:
            raise RuntimeError("The zip has no images or videos.")
        if not manifest["voiceover"]:
            raise RuntimeError("No voiceover found. Name the audio file voiceover.mp3 and upload again.")

        if project.transcript() is None:
            emit({"k": "status", "label": "Transcribing the voiceover"})
            (transcriber or (lambda p: p.transcribe(progress=lambda pct: emit({"k": "pct", "v": pct}))))(project)
            checkpoint()

        tl = project.timeline()
        files = [v["file"] for v in manifest["visuals"]]
        scenes_f = project.root / "scenes.json"
        scenes, brief = [], msg
        if cmd is None:
            sc, rest = script_align.parse_scenes(msg, files)
            if len(sc) >= 3:
                scenes, brief = sc, rest
            elif tl is None:
                scenes = script_align.read_script_file(project.assets, files)
        elif cmd in ("/draft", "/align"):
            scenes = json.loads(scenes_f.read_text()) if scenes_f.exists() else \
                script_align.read_script_file(project.assets, files)
        use_script = len(scenes) >= 3
        if use_script:
            scenes_f.write_text(json.dumps(scenes))
        note = ""

        if cmd == "/align" and not use_script:
            say("No script lines found. Put lines like `03.jpg | the narration said for this image` in your "
                "message, or add scenes.txt to the zip.")
            return new_hist[-KEEP_HISTORY:]

        if use_script or tl is None or cmd == "/draft":
            arg = msg.split()[1].lower() if cmd in ("/draft", "/align") and len(msg.split()) > 1 else None
            fmt = arg if arg in autoplan.FORMATS else autoplan.guess_format(manifest, "" if cmd else msg)
            if use_script:
                emit({"k": "status", "label": "Aligning images to your script"})
                transcript = project.transcript()
                placed, skipped = script_align.align(scenes, transcript)
                if not placed:
                    raise RuntimeError("None of your script lines matched the voiceover. Check that the quoted "
                                       "narration is what is actually spoken.")
                new = script_align.build_timeline(manifest, transcript, placed, fmt)
                v = project.validate(new)
                if not v["ok"]:
                    raise RuntimeError("Could not build the cut: " + "; ".join(v["errors"][:2]))
                project.save_timeline(new)
                tl = new
                checkpoint()
                rep = script_align.report(placed, skipped, manifest, len(scenes))
                say(rep)
                note = ("NOTE: code already placed images on their narration lines from the user's script. "
                        "Keep those times unless asked. The script lines were removed from this message.\n")
                if cmd in ("/draft", "/align"):
                    return new_hist[-KEEP_HISTORY:]
                if len(brief.strip()) < 30:  # nothing left for the AI to decide: no tokens spent
                    render("final")
                    return new_hist[-KEEP_HISTORY:]
            else:
                emit({"k": "status", "label": "Planning the first cut"})
                d = project.draft(format=fmt)
                if not d["ok"]:
                    raise RuntimeError("Could not build a first cut: " + "; ".join(d["errors"][:2]))
                tl = project.timeline()
                checkpoint()
                if cmd == "/draft":
                    say(f"Rebuilt the first cut ({fmt}, {d['summary']['clips']} clips, {d['summary']['total_seconds']}s). "
                        "Say what to change, or press Render.")
                    return new_hist[-KEEP_HISTORY:]

        if cmd in ("/render", "/preview"):
            quality = "preview" if cmd == "/preview" else "final"
            r = render(quality)
            say(f"Rendered the {quality} video ({r['seconds']}s, {r['size_mb']} MB).")
            return new_hist[-KEEP_HISTORY:]
        if cmd:
            say("Unknown command. Use /render, /preview, /draft [landscape|vertical|square] or /align.")
            return new_hist[-KEEP_HISTORY:]

        # ---- the single AI call
        ctx = project.context()
        transcript = project.transcript()
        emit({"k": "status", "label": f"Asking the AI ({agent.get('model', '?')})"})
        validate = lambda t: project.validate(t)
        try:
            new_tl, plan, usage = director.decide(
                agent, edit.state_text(ctx, tl, transcript), "\n".join(sfx_lines()), note + brief, history, tl,
                transcript, validate, ctx["visuals"], manifest["voiceover_duration"], emit)
        except llm.LLMError as e:
            emit({"k": "error", "text": str(e)})
            say("The AI call failed, so nothing was changed. Your first cut is saved: fix the AI settings and "
                "resend, or press Render to build it as is.")
            return new_hist[-KEEP_HISTORY:]
        emit({"k": "usage", **usage})
        changed = new_tl != tl
        if changed:
            project.save_timeline(new_tl)
            checkpoint()
        say(str(plan.get("reply") or "Done.")[:900])
        want = plan.get("render", "final" if changed else None)
        if want and want != "preview":
            want = "final"
        if want:
            render(want)
    except Exception as e:  # never leave the page hanging
        emit({"k": "error", "text": f"{type(e).__name__}: {e}"})
        new_hist.append({"role": "assistant", "content": f"(failed: {e})"})
    return new_hist[-KEEP_HISTORY:]
