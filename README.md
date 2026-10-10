# agentcut

Turn a zip of **numbered images + short clips + a voiceover** into an edited video. Whisper times the narration,
code builds a first cut, **one AI call** applies your directions, FFmpeg renders (Ken Burns motion, transitions,
SFX, ducked music, burned-in captions). Built to be cheap on tokens, so free models last.

## Why it is cheap
- The first cut is made by code, not by the AI.
- The AI makes **one call per message** (about 1-2k tokens): it reads a compact state and answers with small edit ops.
  No tool loop, no resending old results, no word-level transcript.
- It places things by quoting spoken words ("whoosh at 'second idea'"), so it never does time maths.
- Buttons (Render final, Preview, Redraft) use **no AI at all**.
- Mistakes are repaired with a tiny retry that sends only the errors. Token usage is shown after every job.

## Your zip
```
voiceover.mp3      <- any audio with "voice", "narration" or "vo" in the name
01.jpg 02.jpg 03.mp4 04.png ...
music.mp3          <- optional
```

## Host on Modal (works from a phone)
1. In Modal: **Secrets > Create > Custom**, name it `agentcut`, add ONE key: `APP_PASSWORD` (your own password).
2. Modal **Settings > API Tokens**: create a token. In GitHub: repo **Settings > Secrets and variables > Actions**,
   add `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`.
3. Push to `main` (or run the "Deploy to Modal" workflow). Open the `web` URL from the Modal dashboard.
4. In the page: tap the AI chip, enter the password, pick a provider (OpenRouter, Groq, Gemini, DeepSeek or your own
   Base URL), paste your API key and model ID, tap **Test AI**, **Save**.
5. Upload the zip, describe the video, send.

Your AI key lives only in your browser. Whisper runs on a T4 GPU for the transcription step only; everything else
runs on CPU, so you pay GPU time for seconds, not for the whole job.

## Local use (ffmpeg + Python 3.10+)
```bash
pip install -r requirements.txt
python cli.py init my.zip && python cli.py transcribe
python cli.py chat "vertical, fast cuts, big captions" --base https://openrouter.ai/api/v1 --key KEY --model MODEL
```
Coding agents (Claude Code, Codex...): point them at `AGENTS.md`. MCP: `claude mcp add agentcut -- python mcp_server.py`.

## Sound effects
Drop audio files into `sfx/` and describe each in `sfx/sfx.json` (`file`, `description`). Keep descriptions to one
short line: the AI reads them on every call.

## Chat commands (no AI used)
`/render` final video, `/preview` quick half-size render, `/draft vertical` rebuild the first cut.
