# agentcut: guide for coding agents (Claude Code, Codex, DeepSeek harnesses...)

You edit a video by driving the CLI (`python cli.py ...`) or the MCP tools. Every command prints compact JSON.
Never touch FFmpeg yourself. The user gave you: a zip of numbered images and short clips (`01.jpg`, `02.mp4`...),
one voiceover (`voiceover.mp3`), and a description of the video. File numbers are the intended order.

## Fast path (fewest tokens)
```
python cli.py init my.zip
python cli.py transcribe
python cli.py sfx                      # sound effects: file|length|when to use
python cli.py draft --format vertical  # automatic first cut. Omit --format to guess from the files
python cli.py show                     # index|file|start-end|motion|transition-in|spoken words
python cli.py patch ops.json           # small fixes (list of ops, see below)
python cli.py render                   # fast half-size preview
python cli.py render --final
```
`draft` already places images in order, cuts on sentence boundaries, adds soft fades at pauses and captions.
Read `show`, compare each line with the user's description, and fix only what differs with `patch`.
Do not rewrite the whole timeline and do not read the full word-level transcript.

## Ops for `patch` (a JSON list, applied in order)
```json
[
 {"op":"set_clip","index":3,"fields":{"start_phrase":"billion dollars","motion":"zoom_out","transition":"fade","trim_start":1.5,"loop":true,"file":"07.jpg"}},
 {"op":"swap","a":2,"b":5},
 {"op":"reorder","order":["01.jpg","03.mp4","02.jpg"]},
 {"op":"insert_clip","index":4,"clip":{"file":"09.jpg","start_phrase":"new idea"}},
 {"op":"remove_clip","index":6},
 {"op":"add_sfx","file":"whoosh.mp3","at_phrase":"new idea","offset":-0.1,"volume":0.45},
 {"op":"remove_sfx","index":0},
 {"op":"set","key":"format","value":"vertical"},
 {"op":"set","key":"captions","value":{"mode":"auto","words_per_chunk":3}},
 {"op":"set","key":"caption_style","value":{"uppercase":true,"position":"center","size":80,"color":"#FFFFFF"}},
 {"op":"set","key":"music","value":{"file":"music.mp3","volume":0.12,"duck":true}}
]
```
- Place things with `start_phrase` / `at_phrase` using words copied exactly from the `show` output (optional
  `"occurrence": 2`). `python cli.py find "phrase"` returns exact seconds if you need them (`"at": 12.4` also works).
- Clips run back to back: a clip lasts until the next one starts, so changing `start` moves the cut.
- `motion` (images): zoom_in, zoom_out, pan_left, pan_right, none. `transition` (how a clip comes in): omit for a
  cut, or fade, dissolve, fadeblack, fadewhite, wipeleft, wiperight, wipeup, wipedown, slideleft, slideright,
  slideup, slidedown, circleopen, circleclose, pixelize, radial, zoomin, or `{"type":"fade","duration":0.3}`.
- Video clips lose their own audio. A video shorter than its slot is looped automatically.
- `format`: landscape 1920x1080, vertical 1080x1920, square 1080x1080.
- Errors are plain English and nothing is saved when an op fails. Fix and resubmit.

## One-call alternative
`python cli.py chat --zip my.zip "brief"` runs the built-in editor: transcribe, draft, one AI call, render.
It needs `--base`, `--key`, `--model` (or env AGENT_BASE_URL, AGENT_API_KEY, AGENT_MODEL).

## Editing craft
- Cut on meaning: change visuals when the narration changes idea. 2-5 seconds per image; no still longer than ~7s.
- Hard cuts inside a scene, one soft transition (0.25-0.4s) per topic change. Do not put a fancy transition on every cut.
- SFX sparingly, about one per 8-15 seconds, volume 0.3-0.6: whoosh just before a cut (offset -0.1), impact on a
  shocking fact, riser ~1.5s before a reveal. Never stack effects.
- Follow the user's description first; these rules only fill gaps. Use every file unless told otherwise.
