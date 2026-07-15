"""
render.py -- one greenlit story -> one finished Short. Blocking; the bot's
worker runs it through asyncio.to_thread, one job at a time.

Stages (each leaves artifacts in renders/<story_id>/ and is skipped when its
artifacts already exist, so a crashed/restarted job resumes where it died):

  tts        narration.mp3 + words.json   (voice.synthesize)
  clips      clips/beat_XX.mp4 + clips.json (stock.find_broll per beat)
  assembling seg_XX.mp4 -> subs.ass -> master.mp4 (+ manifest, credits)
  preview    preview.mp4 under the Discord attachment limit

Beat time spans come from the narration's char-level timings: beat i's span
runs from its first spoken character to beat i+1's first character (contiguous
by construction, summing exactly to the narration length -- what the xfade
math in assemble.py expects). If alignment came back unusable, spans fall back
to proportional-by-character-count.

Raises BlockedError when footage can't be found for a beat (job -> 'blocked'
with a human-readable reason); anything else bubbles up as a failure.
"""

import json
import os
import shutil
import time

import assemble
import captions
import criteria
import stock
import store
import voice

BASE = os.path.dirname(os.path.abspath(__file__))
RENDERS = os.environ.get("STORY_RENDERS", os.path.join(BASE, "renders"))
FONTS_DIR = os.path.join(BASE, "fonts")     # uploaded caption fonts (.ttf/.otf)
PREVIEW_SLACK = 256 * 1024        # stay safely under the attachment cap
MIN_SPAN = 0.35                   # seconds; below this alignment is broken


class BlockedError(RuntimeError):
    pass


def workdir_for(story_id: int) -> str:
    d = os.path.join(RENDERS, str(story_id))
    os.makedirs(os.path.join(d, "clips"), exist_ok=True)
    return d


# ---- artifact resets (bot calls these before queueing re-roll / re-voice jobs)

def _clear(workdir: str, *names):
    for n in names:
        p = os.path.join(workdir, n)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)


def _clear_globs(workdir: str, prefix: str):
    for f in os.listdir(workdir):
        if f.startswith(prefix):
            os.remove(os.path.join(workdir, f))


def reset_downstream(story_id: int):
    """Drop everything built from clips onward (keeps narration). The
    clips_used ROWS are deliberately kept: they are the exclusion list that
    forces a re-roll to pick DIFFERENT footage (the search cache + sort are
    deterministic, so without them a re-roll would re-pick the same clips)."""
    d = workdir_for(story_id)
    _clear(d, "clips", "clips.json", "master.mp4", "master_alpha.mov",
           "subs.ass", "manifest.json", "credits.txt")
    _clear_globs(d, "seg_")
    _clear_globs(d, "preview_")
    os.makedirs(os.path.join(d, "clips"), exist_ok=True)


def reset_voice(story_id: int):
    """Drop narration too -- a re-voice implies re-cutting everything."""
    reset_downstream(story_id)
    d = workdir_for(story_id)
    _clear(d, "narration.mp3", "words.json")


def reset_assembly(story_id: int):
    """Lightest reset: only the final cut. Keeps narration AND the footage
    picks, so a caption-style or music change re-renders fast -- no re-synth,
    no footage re-fetch (so it won't re-hit a missing-footage block)."""
    d = workdir_for(story_id)
    _clear(d, "master.mp4", "master_alpha.mov", "subs.ass", "manifest.json",
           "credits.txt")
    _clear_globs(d, "seg_")
    _clear_globs(d, "preview_")


# ---- beat spans -----------------------------------------------------------------

def _narration_text(beats: list[dict]) -> tuple[str, list[tuple[int, int]]]:
    """Join beat sentences; return (text, [(char_start, char_end)] per beat)."""
    parts, offsets, pos = [], [], 0
    for b in beats:
        n = b["n"].strip()
        if parts:
            pos += 1                      # the joining space
        offsets.append((pos, pos + len(n)))
        parts.append(n)
        pos += len(n)
    return " ".join(parts), offsets


def _beat_spans(offsets, char_times, total: float) -> list[float]:
    """Contiguous per-beat durations summing to `total`."""
    starts = []
    for a, b in offsets:
        t = next((ct[0] for ct in char_times[a:b] if ct), None)
        starts.append(t)
    starts[0] = 0.0
    # fill gaps + force monotonic
    ok = all(s is not None for s in starts) and \
        all(starts[i] < starts[i + 1] for i in range(len(starts) - 1))
    if ok:
        spans = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        spans.append(total - starts[-1])
        if all(s >= MIN_SPAN for s in spans):
            return spans
    # fallback: proportional to character counts
    weights = [b - a for a, b in offsets]
    wsum = sum(weights) or 1
    return [total * w / wsum for w in weights]


def _write_json(path: str, obj) -> None:
    """Atomic JSON dump (temp + rename), so a crash/disk-full mid-write never
    leaves a truncated resume artifact that a later json.load would choke on."""
    tmp = path + ".part"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _load_json(path: str):
    """Load a resume artifact, tolerating a truncated/corrupt file: return None
    (treat as absent) so the stage re-runs instead of failing every retry."""
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---- the job --------------------------------------------------------------------

def run_job(job: dict, story: dict, notify=None,
            preview_limit: int = 10 * 1024 * 1024) -> dict:
    """Returns {'master','preview','duration','manifest'} (preview may be
    None if nothing fits the attachment limit)."""
    def report(stage):
        if notify:
            try:
                notify(stage)
            except Exception:
                pass

    beats = story["beats"]
    if not beats:
        raise BlockedError("story has no script beats")
    # per-story production options (global defaults + this story's overrides):
    # footage on/off (off = clean solid backdrop, no stock footage / no key
    # needed), music on/off, captions on/off.
    opts = store.render_opts(story["id"])
    d = workdir_for(story["id"])
    narr_path = os.path.join(d, "narration.mp3")
    words_path = os.path.join(d, "words.json")
    clips_path = os.path.join(d, "clips.json")
    master = os.path.join(d, "master.mp4")

    text, offsets = _narration_text(beats)

    # -- tts ----------------------------------------------------------------
    vdata = None
    if os.path.exists(narr_path) and os.path.exists(words_path):
        vdata = _load_json(words_path)
        if vdata is not None and vdata.get("text") != text:  # beats changed
            reset_voice(story["id"])
            vdata = None
    if vdata is None:
        report("tts")
        if not voice.can_afford(len(text)):
            raise BlockedError("ElevenLabs credits too low for this render "
                               f"({len(text)} chars needed)")
        res = voice.synthesize(text, story["tone"], narr_path,
                               voice_id=opts.get("voice") or None)
        vdata = {"text": text, "words": res.words,
                 "char_times": res.char_times, "duration": res.duration,
                 "provider": res.provider}
        _write_json(words_path, vdata)
        store.bump("tts_chars:" + time.strftime("%Y-%m"), len(text))

    total = float(vdata["duration"])
    spans = _beat_spans(offsets, vdata["char_times"], total)

    # -- clips (skipped entirely when footage is toggled off) ---------------
    picks = None
    if opts["footage"]:
        if os.path.exists(clips_path):
            picks = _load_json(clips_path)
            if picks is None or len(picks) != len(beats) or \
                    not all(os.path.exists(p["file"]) for p in picks):
                picks = None
        if picks is None:
            report("clips")
            used = store.recent_clip_ids(14)
            picks = []
            for i, (beat, span) in enumerate(zip(beats, spans)):
                try:
                    got = stock.find_broll(
                        beat["k"], beat["v"], span, used,
                        dest_base=os.path.join(d, "clips", f"beat_{i:02d}"),
                        rewrite=_rewrite_helper())
                except stock.StockError as e:
                    raise BlockedError(f"footage search stopped at shot "
                                       f"{i + 1}: {e}") from e
                if got is None:
                    raise BlockedError(
                        f"no footage found for shot {i + 1} "
                        f"(keywords: {', '.join(beat['k'])})")
                used.add(f"{got['source']}:{got['video_id']}")
                picks.append(got)
            _write_json(clips_path, picks)
            # Record into the 14-day dedupe table only AFTER the whole stage
            # succeeds. Recording per-beat left orphan rows when a later shot
            # blocked -- poisoning the pool and re-excluding this story's own
            # valid picks (and every other story's) for 14 days.
            for i, got in enumerate(picks):
                store.record_clip(story["id"], i, got)

    # -- assemble -----------------------------------------------------------
    def _resolve_subs():
        if not opts["captions"]:
            return None
        return captions.build_ass(vdata["words"], os.path.join(d, "subs.ass"),
                                  style=opts.get("cap"),
                                  play_w=assemble.W, play_h=assemble.H)

    def _resolve_music():
        if not opts["music"]:
            return None
        track = opts.get("music_track")
        m = assemble.find_track(track) if track and track not in ("auto", "") \
            else None
        return m or assemble.pick_music(story["tone"])

    music, subs = None, None
    if not os.path.exists(master):
        report("assembling")
        if opts["footage"]:
            segs = [assemble.make_segment(p, spans[i], i, d,
                                          last=(i == len(beats) - 1))
                    for i, p in enumerate(picks)]
        else:                                    # footage off: clean backdrop
            segs = [assemble.make_solid_segment(spans[i], i, d,
                                                last=(i == len(beats) - 1))
                    for i in range(len(beats))]
        subs, music = _resolve_subs(), _resolve_music()
        assemble.assemble(segs, spans, narr_path, subs, music, total, master,
                          fonts_dir=FONTS_DIR)
        _write_manifest(d, story, vdata, picks or [], music, spans)
        store.count_render()
        store.kv_set(f"rendered_opts:{story['id']}", opts)   # for smart re-render

    # -- transparent export (footage-off only): captions+voice over alpha, so
    #    it drops onto your own footage in CapCut/Premiere. ProRes 4444 .mov. --
    alpha = os.path.join(d, "master_alpha.mov")
    if (not opts["footage"]) and not os.path.exists(alpha):
        report("assembling")
        if subs is None:
            subs = _resolve_subs()
        if music is None:
            music = _resolve_music()
        assemble.make_transparent(subs, narr_path, music, total, alpha,
                                  fonts_dir=FONTS_DIR,
                                  bg=int((opts.get("cap") or {}).get("bg", 0)))

    # -- preview ------------------------------------------------------------
    report("preview")
    preview = None
    for f in os.listdir(d):                    # reuse an existing small one
        if f.startswith("preview_"):
            p = os.path.join(d, f)
            if os.path.getsize(p) <= preview_limit - PREVIEW_SLACK:
                preview = p
                break
    if preview is None:
        preview = assemble.make_preview(master,
                                        preview_limit - PREVIEW_SLACK, d)

    return {"master": master, "preview": preview, "duration": total,
            "manifest": os.path.join(d, "manifest.json")}


def _rewrite_helper():
    """llm.rewrite_query if an Anthropic key is around, else None."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import llm
    return llm.rewrite_query


# ---- manifest + credits -------------------------------------------------------------

def _write_manifest(d: str, story: dict, vdata: dict, picks: list[dict],
                    music: str | None, spans: list[float]):
    manifest = {
        "story_id": story["id"],
        "title": story["title"],
        "rendered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "voice": {"provider": vdata["provider"],
                  "tone": story["tone"],
                  "chars": len(vdata["text"])},
        "duration": vdata["duration"],
        "spans": [round(s, 3) for s in spans],
        "clips": [{k: p.get(k) for k in
                   ("source", "video_id", "page_url", "author", "author_url",
                    "duration", "width", "height")}
                  for p in picks],
        "music": os.path.basename(music) if music else None,
        "sources": story.get("sources", []),
    }
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    lines = []
    pexels_authors = sorted({p["author"] for p in picks
                             if p["source"] in ("pexels", "photo")
                             and p.get("author")})
    pixabay_authors = sorted({p["author"] for p in picks
                              if p["source"] == "pixabay" and p.get("author")})
    if pexels_authors:
        lines.append("Footage: Pexels (https://www.pexels.com) -- "
                     + ", ".join(pexels_authors))
    if pixabay_authors:
        lines.append("Footage: Pixabay (https://pixabay.com) -- "
                     + ", ".join(pixabay_authors))
    if music:
        lines.append(f"Music: {os.path.basename(music)}")
        note = _music_attribution(music)
        if note:
            lines.append(f"Music credit: {note}")
    if story.get("sources"):
        lines.append("")
        lines.append("Sources:")
        lines += [f"  {s}" for s in story["sources"]]
    with open(os.path.join(d, "credits.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _music_attribution(track_path: str) -> str | None:
    """music/<folder>/manifest.json may flag tracks that require credit:
    {"track.mp3": {"attribution": "Song by X (YouTube Audio Library)"}}"""
    mpath = os.path.join(os.path.dirname(track_path), "manifest.json")
    if os.path.exists(mpath):
        try:
            with open(mpath) as f:
                m = json.load(f)
            entry = m.get(os.path.basename(track_path))
            if isinstance(entry, dict):
                return entry.get("attribution")
        except (json.JSONDecodeError, OSError):
            pass
    return None
