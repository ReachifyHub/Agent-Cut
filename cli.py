#!/usr/bin/env python3
"""agentcut CLI. Every command prints JSON, so any coding agent can drive it from a shell.

For an agent (Claude Code, Codex...): init -> transcribe -> draft -> show -> patch -> render --final.
Or let the built-in AI do it in one call:  python cli.py chat --zip my.zip "make it vertical, punchy" """
import argparse
import json
import os
import sys

from core.tools import project_from_env


def out(x):
    print(json.dumps(x, indent=1, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(prog="agentcut")
    ap.add_argument("--project", help="project name (default: 'default')")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="extract a zip of assets"); s.add_argument("zip")
    sub.add_parser("manifest")
    s = sub.add_parser("transcribe"); s.add_argument("--model"); s.add_argument("--language")
    s = sub.add_parser("transcript"); s.add_argument("--words", action="store_true")
    s.add_argument("--start", type=float); s.add_argument("--end", type=float)
    s = sub.add_parser("find"); s.add_argument("phrase")
    sub.add_parser("sfx")
    sub.add_parser("guide")
    s = sub.add_parser("draft", help="build the automatic first cut")
    s.add_argument("--format", choices=["landscape", "vertical", "square"])
    s.add_argument("--words-per-chunk", type=int, default=3)
    s.add_argument("--transition-sfx")
    s.add_argument("--no-captions", action="store_true")
    sub.add_parser("show", help="compact view of the current timeline")
    s = sub.add_parser("patch", help="apply edit ops from a JSON file (a list of ops)"); s.add_argument("file")
    s = sub.add_parser("write", help="replace the whole timeline from a JSON file"); s.add_argument("file")
    s = sub.add_parser("render"); s.add_argument("--final", action="store_true")
    s = sub.add_parser("chat", help="one AI-driven turn: transcribe, draft, edit, render")
    s.add_argument("brief"); s.add_argument("--zip")
    s.add_argument("--base", default=os.environ.get("AGENT_BASE_URL", "https://openrouter.ai/api/v1"))
    s.add_argument("--key", default=os.environ.get("AGENT_API_KEY") or os.environ.get("OPENROUTER_API_KEY"))
    s.add_argument("--model", default=os.environ.get("AGENT_MODEL"))
    a = ap.parse_args()
    p = project_from_env(a.project)

    if a.cmd == "init":
        out(p.init(a.zip))
    elif a.cmd == "manifest":
        out(p.summary())
    elif a.cmd == "transcribe":
        out(p.transcribe(a.model, a.language, progress=lambda pct: print(f"\rtranscribing {pct}%", end="", file=sys.stderr)))
    elif a.cmd == "transcript":
        out(p.get_transcript("words" if a.words else "segments", a.start, a.end))
    elif a.cmd == "find":
        out(p.find(a.phrase))
    elif a.cmd == "sfx":
        out(p.list_sfx())
    elif a.cmd == "guide":
        print((__import__("pathlib").Path(__file__).parent / "AGENTS.md").read_text())
    elif a.cmd == "draft":
        out(p.draft(a.format, a.words_per_chunk, a.transition_sfx, not a.no_captions))
    elif a.cmd == "show":
        out(p.show())
    elif a.cmd == "patch":
        out(p.patch(json.load(open(a.file))))
    elif a.cmd == "write":
        out(p.write(json.load(open(a.file))))
    elif a.cmd == "render":
        out(p.render("final" if a.final else "preview",
                     progress=lambda label, pct=None: print(f"\r{label} {'' if pct is None else str(pct) + '%'}   ", end="", file=sys.stderr)))
    elif a.cmd == "chat":
        from core.pipeline import run_turn
        if a.zip:
            p.init(a.zip)
        agent = {"base": a.base, "key": a.key, "model": a.model}
        def show(e):
            k = e["k"]
            if k == "status":
                print(f"-> {e['label']}", file=sys.stderr, flush=True)
            elif k == "say":
                print("\n" + e["text"])
            elif k == "usage":
                print(f"   AI tokens: {e['prompt']} in, {e['completion']} out ({e['calls']} call)", file=sys.stderr)
            elif k == "error":
                print("ERROR:", e["text"], file=sys.stderr)
            elif k == "file":
                print(f"   wrote {p.output / e['file']}", file=sys.stderr)
        run_turn(p, a.brief, agent, [], emit=show)


if __name__ == "__main__":
    main()
