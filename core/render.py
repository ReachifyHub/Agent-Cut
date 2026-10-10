"""Resolved timeline -> FFmpeg. Segments are cached so revisions re-render fast."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


class RenderError(RuntimeError):
    pass


def _tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-12:])


def _run(cmd, cwd=None, total_s=None, on_pct=None):
    """Run ffmpeg. With total_s + on_pct, reports 0-99 progress from ffmpeg's -progress stream."""
    if on_pct and total_s:
        cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
        with tempfile.TemporaryFile() as err:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True, cwd=cwd)
            last = -1
            for line in p.stdout:
                if line.startswith("out_time_us="):
                    try:
                        pct = min(99, int(int(line.split("=")[1]) / 1e6 / total_s * 100))
                    except ValueError:
                        continue
                    if pct != last:
                        last = pct
                        on_pct(pct)
            p.wait()
            if p.returncode != 0:
                err.seek(0)
                raise RenderError("ffmpeg failed: " + _tail(err.read().decode(errors="replace")))
        return
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if p.returncode != 0:
        raise RenderError("ffmpeg failed: " + _tail(p.stderr))


def _even(x):
    return int(x) // 2 * 2


def _zoompan(motion, n):
    n = max(n, 1)
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    return {
        "zoom_in": (f"1+0.12*on/{n}", cx, cy),
        "zoom_out": (f"1.12-0.12*on/{n}", cx, cy),
        "pan_left": ("1.12", f"(iw-iw/zoom)*(1-on/{n})", cy),
        "pan_right": ("1.12", f"(iw-iw/zoom)*on/{n}", cy),
    }[motion]


def _segment_cmd(clip, assets, W, H, fps, frames, out, preview):
    src = str(assets / clip["file"])
    fit = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    enc = ["-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20" if preview else "14",
           "-pix_fmt", "yuv420p", "-threads", "2", "-frames:v", str(frames), out]
    if clip["kind"] == "image":
        if clip["motion"] == "none":
            return ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(fps), "-i", src,
                    "-vf", f"{fit},setsar=1,format=yuv420p"] + enc
        big_w, big_h = _even(W * 1.5), _even(H * 1.5)
        z, x, y = _zoompan(clip["motion"], frames)
        vf = (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={fps},setsar=1,format=yuv420p")
        return ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf", vf] + enc
    pre = ["-stream_loop", "-1"] if clip.get("loop") else []
    vf = f"{fit},fps={fps},setsar=1,tpad=stop_mode=clone:stop_duration=2,format=yuv420p"
    return ["ffmpeg", "-y", "-loglevel", "error"] + pre + ["-ss", f"{clip['trim_start']:.3f}", "-i", src,
            "-vf", vf] + enc


def _ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_color(hexstr, default):
    h = (hexstr or default).lstrip("#")
    if len(h) != 6:
        h = default.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper().replace("&H", "&H")


def _write_ass(path, caps, style, W, H):
    size = int(style.get("size", round(min(W, H) * 0.07)))
    pos = style.get("position", "bottom")
    align = {"bottom": 2, "center": 5, "top": 8}.get(pos, 2)
    margin = int(style.get("margin_v", round(H * (0.12 if pos == "bottom" else 0.08))))
    outline = style.get("outline", max(2, round(size * 0.08)))
    bold = -1 if style.get("bold", True) else 0
    font = style.get("font", "DejaVu Sans")
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,{font},{size},{_ass_color(style.get('color'), '#FFFFFF')},&H000000FF,"
            f"{_ass_color(style.get('outline_color'), '#000000')},&H80000000,{bold},0,0,0,100,100,0,0,1,"
            f"{outline},0,{align},{int(W * 0.06)},{int(W * 0.06)},{margin},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    for c in caps:
        t = c["text"].replace("{", "(").replace("}", ")").replace("\n", "\\N")
        if style.get("uppercase"):
            t = t.upper()
        lines.append(f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},Default,,0,0,0,,{t}")
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def render(res: dict, assets: Path, workdir: Path, out_path: Path, preview: bool = False,
           progress=None) -> dict:
    """progress(label, pct_or_None) is called as stages start and advance."""
    say = progress or (lambda label, pct=None: None)
    assets, workdir, out_path = Path(assets), Path(workdir), Path(out_path)
    seg_dir = workdir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    scale = 0.5 if preview else 1.0
    W, H, fps = _even(res["width"] * scale), _even(res["height"] * scale), res["fps"]
    clips = res["clips"]
    mixed = any(c["transition"] != "cut" for c in clips[1:])
    T = res["total_s"]

    jobs = []
    for i, c in enumerate(clips):
        extra = clips[i + 1]["tf"] if (mixed and i + 1 < len(clips)) else 0
        frames = c["frames"] + extra
        key = hashlib.sha1(json.dumps([c["file"], c.get("motion"), c.get("trim_start"), c.get("loop"),
                                       frames, W, H, fps, preview, (assets / c["file"]).stat().st_mtime_ns],
                                      sort_keys=True).encode()).hexdigest()[:14]
        jobs.append((c, frames, seg_dir / f"{key}.mp4"))

    todo = [j for j in jobs if not j[2].exists()]
    workers = int(os.environ.get("RENDER_WORKERS", min(os.cpu_count() or 2, 8)))

    def work(j):
        c, f, s = j
        tmp = s.with_suffix(".tmp.mp4")
        _run(_segment_cmd(c, assets, W, H, fps, f, str(tmp), preview))
        tmp.rename(s)

    if todo:
        say(f"Cutting clips (0/{len(todo)})", 0)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(work, j) for j in todo]
            for f in as_completed(futs):
                f.result()
                done += 1
                say(f"Cutting clips ({done}/{len(todo)})", int(done / len(todo) * 100))

    joined = workdir / "joined.mp4"
    say("Joining clips", None)
    if not mixed:
        lst = workdir / "concat.txt"
        lst.write_text("".join(f"file '{s.resolve()}'\n" for _, _, s in jobs))
        _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
              "-c", "copy", str(joined)])
    else:
        cmd, parts, last = ["ffmpeg", "-y", "-loglevel", "error"], [], "[0:v]"
        for _, _, s in jobs:
            cmd += ["-i", str(s)]
        for i in range(1, len(clips)):
            c = clips[i]
            ty = "fade" if c["transition"] == "cut" else c["transition"]
            outl = f"[x{i}]"
            parts.append(f"{last}[{i}:v]xfade=transition={ty}:duration={c['tf'] / fps:.4f}:"
                         f"offset={c['start_f'] / fps:.4f}{outl}")
            last = outl
        cmd += ["-filter_complex", ";".join(parts), "-map", last, "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "20" if preview else "14", "-pix_fmt", "yuv420p", str(joined)]
        _run(cmd, total_s=T, on_pct=lambda p: say("Joining clips with transitions", p))

    inputs, ai, flt, mix = ["-i", str(joined)], 1, [], []
    if res["captions"]:
        _write_ass(workdir / "captions.ass", res["captions"], res["caption_style"], W, H)
        flt.append("[0:v]ass=captions.ass[v]")
    else:
        flt.append("[0:v]null[v]")
    if res["voiceover"]:
        inputs += ["-i", str(res["voiceover"])]
        base = f"[{ai}:a]aresample=48000,aformat=channel_layouts=stereo,volume={res['voice_volume']}"
        flt.append(base + ("[vo];[vo]asplit=2[vo1][vo2]" if res["music"] and res["music"]["duck"] else "[vo1]"))
        mix.append("[vo1]")
        ai += 1
    for k, e in enumerate(res["sfx"]):
        inputs += ["-i", e["path"]]
        flt.append(f"[{ai}:a]aresample=48000,aformat=channel_layouts=stereo,volume={e['volume']},"
                   f"adelay=delays={int(e['at'] * 1000)}:all=1[s{k}]")
        mix.append(f"[s{k}]")
        ai += 1
    if res["music"]:
        m = res["music"]
        inputs += ["-stream_loop", "-1", "-i", m["path"]]
        fo = min(m["fade_out"], T)
        chain = (f"[{ai}:a]aresample=48000,aformat=channel_layouts=stereo,volume={m['volume']},"
                 f"atrim=0:{T:.3f},afade=t=out:st={max(T - fo, 0):.3f}:d={fo:.3f}")
        if m["duck"] and res["voiceover"]:
            flt.append(chain + "[mus];[mus][vo2]sidechaincompress=threshold=0.04:ratio=8:attack=20:release=500[musd]")
        else:
            flt.append(chain + "[musd]")
        mix.append("[musd]")
        ai += 1
    maps = ["-map", "[v]"]
    if mix:
        flt.append("".join(mix) + f"amix=inputs={len(mix)}:duration=longest:normalize=0,"
                   f"alimiter=limit=0.95,apad=whole_dur={T:.3f},atrim=0:{T:.3f}[a]")
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    cmd = (["ffmpeg", "-y", "-loglevel", "error"] + inputs + ["-filter_complex", ";".join(flt)] + maps +
           ["-c:v", "libx264", "-preset", "ultrafast" if preview else "medium", "-crf", "30" if preview else "19",
            "-pix_fmt", "yuv420p", "-r", str(fps), "-t", f"{T:.3f}", "-movflags", "+faststart", str(out_path)])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    say("Mixing audio and encoding", 0)
    _run(cmd, cwd=str(workdir), total_s=T, on_pct=lambda p: say("Mixing audio and encoding", p))
    say("Mixing audio and encoding", 100)
    return {"seconds": round(T, 2), "resolution": f"{W}x{H}",
            "size_mb": round(out_path.stat().st_size / 1e6, 2), "preview": preview}
