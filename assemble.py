"""
assemble.py -- ffmpeg assembly. Two passes:

  1. make_segment(): every beat becomes a normalized, silent, video-only
     segment -- 1080x1920 @ 30fps, SAR 1:1, yuv420p -- exactly span+XFADE long
     (the tail is consumed by the crossfade; the last segment has no tail).
     Videos scale-to-cover + center-crop; too-short clips loop; photos get a
     slow Ken Burns pan.
  2. assemble(): xfade-chain the segments, burn the .ass captions, lay
     narration + (optional) ducked music underneath, encode the master.

Crossfade timing: with segment i lasting span_i + XFADE (last: span_i), the
xfade offset for transition i works out to cumsum(span_1..i) -- every
transition STARTS exactly on its beat boundary and the video's total length
equals the narration's. (xfade output length = A + B - XFADE.)

Encoding is software x264 on a shared 6-core box -- everything runs with
-threads 3 so renders never starve other services on the host.
"""

import json
import os
import random
import subprocess

XFADE = float(os.environ.get("STORY_XFADE", "0.2"))
W = int(os.environ.get("STORY_VIDEO_W", "1080"))
H = int(os.environ.get("STORY_VIDEO_H", "1920"))
FPS = int(os.environ.get("STORY_FPS", "60"))     # 1080x1920 @ 60fps default
THREADS = os.environ.get("STORY_FFMPEG_THREADS", "3")

BASE = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(BASE, "music")
MUSIC_EXTS = (".mp3", ".m4a", ".ogg", ".wav", ".flac", ".opus")


class AssembleError(RuntimeError):
    pass


def _run(args, timeout):
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"").decode(errors="replace")[-1200:]
        raise AssembleError(f"ffmpeg failed ({args[0]}):\n{tail}") from e
    except subprocess.TimeoutExpired as e:
        raise AssembleError(f"ffmpeg timed out after {timeout}s") from e


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries",
         "format=duration:stream=width,height,codec_type",
         "-of", "json", path],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def duration_of(path: str) -> float:
    return float(probe(path)["format"]["duration"])


# ---- pass 1: per-beat segments -----------------------------------------------------

_COVER = (f"scale=w={W}:h={H}:force_original_aspect_ratio=increase,"
          f"crop={W}:{H},setsar=1,fps={FPS},format=yuv420p")


def make_segment(clip: dict, span: float, idx: int, workdir: str,
                 last: bool) -> str:
    """Normalize one beat's footage into seg_<idx>.mp4 of exactly
    span (+XFADE tail unless last) seconds."""
    need = span if last else span + XFADE
    out = os.path.join(workdir, f"seg_{idx:02d}.mp4")
    src = clip["file"]

    if clip["source"] == "photo":
        # Ken Burns: oversize, then pan vertically across the crop window.
        direction = f"(ih-{H})*t/{need:.3f}" if idx % 2 == 0 \
            else f"(ih-{H})*(1-t/{need:.3f})"
        vf = (f"scale=w={W}:h={int(H * 1.25)}:"
              f"force_original_aspect_ratio=increase,"
              f"crop={W}:{H}:x=(iw-{W})/2:y='{direction}',"
              f"setsar=1,fps={FPS},format=yuv420p")
        # -framerate FPS on the looped still: without it image2 emits 25 fps
        # and the fps=FPS filter later duplicates frames, so the Ken Burns pan
        # (crop y depends on t) would step at 25 and stutter up to 30.
        args = ["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS),
                "-t", f"{need:.3f}", "-i", src,
                "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-threads", THREADS, out]
        _run(args, 300)
        return out

    clip_dur = clip.get("duration") or 0
    args = ["ffmpeg", "-y"]
    if clip_dur and clip_dur < need:
        args += ["-stream_loop", "-1"]
    else:
        # start a little into long clips -- the opening frames of stock
        # footage are often slates/fade-ins
        offset = max(0.0, min((clip_dur - need) / 3, 3.0)) if clip_dur else 0.0
        if offset > 0.05:
            args += ["-ss", f"{offset:.2f}"]
    args += ["-i", src, "-t", f"{need:.3f}", "-vf", _COVER, "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-threads", THREADS, out]
    _run(args, 300)
    return out


def make_solid_segment(span: float, idx: int, workdir: str, last: bool,
                       color: str = "0x0E1116") -> str:
    """A footage-free beat: a solid brand-dark background of exactly the same
    duration contract as make_segment (span +XFADE tail unless last). Lets a
    render run with NO stock footage at all -- voice + captions over a clean
    backdrop the user can overlay their own footage on. Same 1080x1920/30fps/
    yuv420p normalization so it xfade-chains identically to real segments."""
    need = span if last else span + XFADE
    out = os.path.join(workdir, f"seg_{idx:02d}.mp4")
    args = ["ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c={color}:s={W}x{H}:r={FPS}", "-t", f"{need:.3f}",
            "-vf", "setsar=1,format=yuv420p", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-threads", THREADS, out]
    _run(args, 120)
    return out


# ---- music ---------------------------------------------------------------------------

def pick_music(tone_key: str) -> str | None:
    """Random track from music/<tone>/, falling back to music/any/."""
    for folder in (os.path.join(MUSIC_DIR, tone_key),
                   os.path.join(MUSIC_DIR, "any")):
        if os.path.isdir(folder):
            tracks = [os.path.join(folder, f) for f in os.listdir(folder)
                      if f.lower().endswith(MUSIC_EXTS)]
            if tracks:
                return random.choice(tracks)
    return None


def find_track(name: str) -> str | None:
    """Resolve a chosen track filename to a path (any music/ subfolder)."""
    if not name:
        return None
    for root, _dirs, files in os.walk(MUSIC_DIR):
        if name in files and name.lower().endswith(MUSIC_EXTS):
            return os.path.join(root, name)
    return None


def list_music() -> dict:
    """{folder: [filenames]} for every music/<folder>/ -- feeds the picker."""
    out = {}
    if os.path.isdir(MUSIC_DIR):
        for d in sorted(os.listdir(MUSIC_DIR)):
            p = os.path.join(MUSIC_DIR, d)
            if os.path.isdir(p):
                out[d] = sorted(f for f in os.listdir(p)
                                if f.lower().endswith(MUSIC_EXTS))
    return out


# ---- pass 2: the master --------------------------------------------------------------

def assemble(segments: list[str], spans: list[float], narration: str,
             subs_ass: str, music: str | None, total: float,
             out_path: str, fonts_dir: str | None = None) -> str:
    """xfade chain + captions + audio mix -> master mp4."""
    if len(segments) != len(spans) or not segments:
        raise AssembleError("segments/spans mismatch")

    args = ["ffmpeg", "-y"]
    for s in segments:
        args += ["-i", s]
    args += ["-i", narration]
    narr_idx = len(segments)
    music_idx = None
    if music:
        args += ["-stream_loop", "-1", "-i", music]
        music_idx = narr_idx + 1

    # video: chain xfades; transition i starts at cumsum(spans[:i+1])
    fc = []
    if len(segments) == 1:
        fc.append("[0:v]copy[vx]")
    else:
        prev = "[0:v]"
        offset = 0.0
        for i in range(1, len(segments)):
            offset += spans[i - 1]
            label = "[vx]" if i == len(segments) - 1 else f"[vc{i}]"
            fc.append(f"{prev}[{i}:v]xfade=transition=fade:"
                      f"duration={XFADE}:offset={offset:.3f}{label}")
            prev = label
    # captions optional: with none, [vx] passes straight through to [vout].
    # fonts_dir (uploaded .ttf/.otf) is handed to libass so custom caption
    # fonts resolve even though they aren't installed system-wide.
    if subs_ass:
        ass = f"ass='{subs_ass}'"
        if fonts_dir and os.path.isdir(fonts_dir):
            ass += f":fontsdir='{fonts_dir}'"
        fc.append(f"[vx]{ass}[vout]")
    else:
        fc.append("[vx]copy[vout]")

    # audio: narration, plus music trimmed/ducked/faded when present
    if music_idx is not None:
        fade_start = max(0.0, total - 1.5)
        fc.append(f"[{music_idx}:a]atrim=0:{total:.3f},volume=0.16,"
                  f"afade=t=out:st={fade_start:.3f}:d=1.5[bg]")
        fc.append(f"[{narr_idx}:a][bg]amix=inputs=2:duration=first:"
                  f"dropout_transition=0:normalize=0,"
                  f"loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        fc.append(f"[{narr_idx}:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")

    # Encode to a temp path and atomically rename on success. +faststart writes
    # the moov atom last, so a killed/timed-out encode leaves a truncated file
    # with no moov -- if that sat at out_path, render.py's resume logic would
    # treat its mere existence as "stage done" and feed the broken file to the
    # preview step forever. With temp+rename, out_path exists only when whole.
    tmp = out_path + ".part"
    args += ["-filter_complex", ";".join(fc),
             "-map", "[vout]", "-map", "[aout]",
             "-t", f"{total:.3f}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "22",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
             "-threads", THREADS, "-f", "mp4", tmp]   # muxer explicit: .part ext
    try:
        _run(args, 1800)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, out_path)
    return out_path


# ---- transparent export (footage-off) --------------------------------------------------

BG_COLOR = os.environ.get("STORY_BG_COLOR", "0x0E1116")   # backdrop tint


def make_transparent(subs_ass: str | None, narration: str, music: str | None,
                     total: float, out_path: str,
                     fonts_dir: str | None = None, bg: int = 0) -> str:
    """A footage-off render with an ALPHA background, encoded ProRes 4444 .mov
    so it drops straight onto your own footage in CapCut/Premiere/DaVinci/FCP.
    `bg` is the backdrop opacity 0-100 (0 = fully transparent; higher = a
    semi/solid dark tint). One continuous bg -- no xfades (no footage)."""
    aa = min(1.0, max(0.0, bg / 100.0))
    args = ["ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={BG_COLOR}:s={W}x{H}:r={FPS}",
            "-i", narration]
    narr_idx, music_idx = 1, None
    if music:
        args += ["-stream_loop", "-1", "-i", music]
        music_idx = 2

    # tint the canvas then set its alpha to the requested backdrop opacity
    fc = [f"[0:v]format=yuva420p,colorchannelmixer=aa={aa:.3f}[bg0]"]
    if subs_ass:
        # alpha=1 makes libass PROCESS the alpha channel, so only the caption
        # glyphs are opaque and the background stays transparent (without it the
        # ass filter fills the whole frame opaque).
        ass = f"ass='{subs_ass}':alpha=1"
        if fonts_dir and os.path.isdir(fonts_dir):
            ass += f":fontsdir='{fonts_dir}'"
        fc.append(f"[bg0]{ass}[vout]")
    else:
        fc.append("[bg0]copy[vout]")
    if music_idx is not None:
        fade_start = max(0.0, total - 1.5)
        fc.append(f"[{music_idx}:a]atrim=0:{total:.3f},volume=0.16,"
                  f"afade=t=out:st={fade_start:.3f}:d=1.5[bgm]")
        fc.append(f"[{narr_idx}:a][bgm]amix=inputs=2:duration=first:"
                  f"dropout_transition=0:normalize=0,"
                  f"loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        fc.append(f"[{narr_idx}:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")

    tmp = out_path + ".part"
    args += ["-filter_complex", ";".join(fc),
             "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
             "-c:v", "prores_ks", "-profile:v", "4444",
             "-pix_fmt", "yuva444p10le", "-alpha_bits", "16",
             "-c:a", "pcm_s16le", "-ar", "48000",
             "-threads", THREADS, "-f", "mov", tmp]
    try:
        _run(args, 1800)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, out_path)
    return out_path


# ---- Discord preview -------------------------------------------------------------------

def make_preview(master: str, limit_bytes: int, workdir: str) -> str | None:
    """Quality-stepped copy that fits the Discord attachment limit (the
    same transcode-to-fit pattern used elsewhere in this project). None if even 480p won't fit."""
    for height, crf in ((1280, 28), (960, 30), (854, 33)):
        out = os.path.join(workdir, f"preview_{height}.mp4")
        tmp = out + ".part"
        # temp+rename: a killed encode leaves a truncated .part (which is small,
        # hence under-limit) -- if it sat at preview_<h>.mp4 the resume loop in
        # render.py would reuse it and deliver an unplayable clip to Discord.
        try:
            _run(["ffmpeg", "-y", "-i", master, "-vf", f"scale=-2:{height}",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                  "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                  "-c:a", "aac", "-b:a", "96k", "-threads", THREADS,
                  "-f", "mp4", tmp], 600)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        if os.path.getsize(tmp) <= limit_bytes:
            os.replace(tmp, out)
            return out
        os.remove(tmp)
    return None
