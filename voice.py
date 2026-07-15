"""
voice.py -- narration synthesis with word timings. Two providers behind one
call; picked automatically (ElevenLabs when ELEVENLABS_API_KEY is set, else
free edge-tts so the whole pipeline runs keyless).

synthesize(text, tone_key, out_path) -> VoiceResult with:
  audio_path   the narration file (mp3)
  words        [{'w','start','end'}] spoken words for the karaoke captions
               (ElevenLabs: from normalized_alignment, i.e. '1977' becomes the
               words actually spoken; edge: WordBoundary events)
  char_times   list len(text) of (start, end) or None per INPUT character --
               this is what render.py uses to cut exact per-beat time spans,
               because it indexes the original narration string
  duration     seconds

All functions are blocking; callers use asyncio.to_thread. edge-tts is an
async library, so the edge path spins a private event loop inside the worker
thread (safe: that thread has no running loop).
"""

import asyncio
import base64
import json
import os
import subprocess

import httpx

import criteria

ELEVEN_URL = "https://api.elevenlabs.io/v1"
ELEVEN_MODEL = os.environ.get("STORY_TTS_MODEL", "eleven_multilingual_v2")
ELEVEN_FORMAT = os.environ.get("STORY_TTS_FORMAT", "mp3_44100_128")


class VoiceError(RuntimeError):
    pass


class VoiceResult:
    def __init__(self, audio_path, words, char_times, duration, provider):
        self.audio_path = audio_path
        self.words = words
        self.char_times = char_times
        self.duration = duration
        self.provider = provider


def provider() -> str:
    return "elevenlabs" if os.environ.get("ELEVENLABS_API_KEY") else "edge"


def synthesize(text: str, tone_key: str, out_path: str,
               voice_id: str | None = None) -> VoiceResult:
    if provider() == "elevenlabs":
        return _eleven(text, tone_key, out_path, voice_id)
    return _edge(text, tone_key, out_path)


_VOICE_CACHE = {"at": 0.0, "list": None}


def list_voices() -> list[dict]:
    """The account's ElevenLabs voices for the picker: id, name, category and
    a preview_url the browser can play. [] if no key / TTS-only scope. Cached
    ~10 min so the Style editor doesn't hammer the API."""
    import time
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return []
    if _VOICE_CACHE["list"] is not None and \
            time.time() - _VOICE_CACHE["at"] < 600:
        return _VOICE_CACHE["list"]
    try:
        r = httpx.get(ELEVEN_URL + "/voices", headers={"xi-api-key": key},
                      timeout=30)
        if r.status_code == 401:
            return []
        r.raise_for_status()
        out = []
        for v in r.json().get("voices", []):
            labels = v.get("labels") or {}
            desc = ", ".join(x for x in (labels.get("accent"),
                             labels.get("gender"), labels.get("age"),
                             labels.get("description")) if x)
            out.append({"voice_id": v.get("voice_id"), "name": v.get("name"),
                        "category": v.get("category"), "desc": desc,
                        "preview_url": v.get("preview_url")})
        _VOICE_CACHE.update(at=time.time(), list=out)
        return out
    except httpx.HTTPError:
        return _VOICE_CACHE["list"] or []


# ---- shared helpers -------------------------------------------------------------

def _probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True, timeout=30, check=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def _chars_to_words(chars, starts, ends):
    """Character alignment -> word list (split on whitespace)."""
    words, cur, w_start, w_end = [], [], None, None
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append({"w": "".join(cur), "start": w_start, "end": w_end})
                cur, w_start = [], None
        else:
            if w_start is None:
                w_start = s
            w_end = e
            cur.append(ch)
    if cur:
        words.append({"w": "".join(cur), "start": w_start, "end": w_end})
    return words


# ---- ElevenLabs ------------------------------------------------------------------

def eleven_credits() -> tuple[int, int] | None:
    """(used, limit) for the current billing cycle. None if no key OR the
    key is scoped without the user_read permission (e.g. a TTS-only key)
    -- callers treat None as 'can't check, proceed'."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return None
    r = httpx.get(ELEVEN_URL + "/user/subscription",
                  headers={"xi-api-key": key}, timeout=30)
    if r.status_code == 401 and "missing_permissions" in r.text:
        return None
    r.raise_for_status()
    d = r.json()
    return int(d.get("character_count", 0)), int(d.get("character_limit", 0))


def eleven_status() -> dict | None:
    """Fuller read of /user/subscription for the Costs tab: used, limit, the
    next monthly reset (unix seconds) and tier. None if no key OR the key lacks
    user_read (a TTS-only key -> the tab shows 'credit read unavailable')."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return None
    r = httpx.get(ELEVEN_URL + "/user/subscription",
                  headers={"xi-api-key": key}, timeout=30)
    if r.status_code == 401 and "missing_permissions" in r.text:
        return None
    r.raise_for_status()
    d = r.json()
    return {"used": int(d.get("character_count", 0)),
            "limit": int(d.get("character_limit", 0)),
            "reset_unix": d.get("next_character_count_reset_unix"),
            "tier": d.get("tier")}


def can_afford(n_chars: int) -> bool:
    """Pre-flight so a render never dies halfway through on quota."""
    try:
        cred = eleven_credits()
    except httpx.HTTPError:
        # a transient error checking the balance must not fail the render at
        # pre-flight -- proceed; a genuine quota shortfall surfaces at synth.
        return True
    if cred is None:
        return True                      # edge path: free / TTS-only key
    used, limit = cred
    return limit - used >= n_chars


def _eleven(text: str, tone_key: str, out_path: str,
            voice_id: str | None = None) -> VoiceResult:
    key = os.environ["ELEVENLABS_API_KEY"]
    voice_id = voice_id or criteria.tone_voice(tone_key)
    url = (f"{ELEVEN_URL}/text-to-speech/{voice_id}/with-timestamps"
           f"?output_format={ELEVEN_FORMAT}")
    body = {
        "text": text,
        "model_id": ELEVEN_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                           "style": 0.35, "use_speaker_boost": True},
    }
    last_err = None
    for attempt in range(3):
        try:
            r = httpx.post(url, json=body,
                           headers={"xi-api-key": key}, timeout=180)
            r.raise_for_status()
            break
        except httpx.HTTPError as e:
            last_err = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            # 429 (rate limit) and 5xx are transient -> retry with backoff.
            # Any other 4xx is a real rejection (bad voice/model/text) -> stop.
            if status is not None and status < 500 and status != 429:
                detail = ""
                try:
                    detail = e.response.text[:300]
                except Exception:
                    pass
                raise VoiceError(f"ElevenLabs rejected the request "
                                 f"({status}): {detail}") from e
            if attempt < 2:
                import time
                time.sleep(2 * (attempt + 1))
    else:
        raise VoiceError(f"ElevenLabs unreachable: {last_err}") from last_err

    d = r.json()
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(d["audio_base64"]))

    align = d.get("alignment") or {}
    chars = align.get("characters") or []
    starts = align.get("character_start_times_seconds") or []
    ends = align.get("character_end_times_seconds") or []

    # char_times indexes the INPUT text. The raw alignment echoes the input
    # characters; align positionally but STOP at the first divergence --
    # past that point coincidental matches would get timings belonging to
    # different characters (monotonic, so no later sanity check would catch
    # it). A None tail makes render._beat_spans fall back to proportional.
    char_times: list = [None] * len(text)
    for i in range(min(len(text), len(chars))):
        if chars[i] != text[i]:
            break
        char_times[i] = (starts[i], ends[i])

    norm = d.get("normalized_alignment") or {}
    if norm.get("characters"):
        words = _chars_to_words(norm["characters"],
                                norm["character_start_times_seconds"],
                                norm["character_end_times_seconds"])
    else:
        words = _chars_to_words(chars, starts, ends)

    duration = _probe_duration(out_path)
    return VoiceResult(out_path, words, char_times, duration, "elevenlabs")


# ---- edge-tts (free fallback) -------------------------------------------------------

EDGE_SAFE_VOICE = "en-US-ChristopherNeural"   # known-good fallback


def _edge(text: str, tone_key: str, out_path: str) -> VoiceResult:
    import edge_tts

    async def _run(voice):
        com = edge_tts.Communicate(text, voice, rate="+4%",
                                   boundary="WordBoundary")
        boundaries = []
        with open(out_path, "wb") as f:
            async for chunk in com.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    boundaries.append(chunk)
        return boundaries

    voice = criteria.tone_edge_voice(tone_key)
    try:
        boundaries = asyncio.run(_run(voice))
    except edge_tts.exceptions.NoAudioReceived:
        # Microsoft retires Edge voices without notice -- fall back to a
        # voice that is known to exist rather than failing the render
        if voice == EDGE_SAFE_VOICE:
            raise
        boundaries = asyncio.run(_run(EDGE_SAFE_VOICE))
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise VoiceError("edge-tts produced no audio")

    words, char_times = [], [None] * len(text)
    cursor = 0
    for b in boundaries:
        w = str(b.get("text", "")).strip()
        if not w:
            continue
        start = b["offset"] / 1e7                  # 100ns ticks -> seconds
        end = start + b.get("duration", 0) / 1e7
        words.append({"w": w, "start": start, "end": end})
        # map the word back onto the input text for char_times
        idx = text.find(w, cursor)
        if idx != -1:
            for i in range(idx, min(idx + len(w), len(text))):
                char_times[i] = (start, end)
            cursor = idx + len(w)

    duration = _probe_duration(out_path)
    return VoiceResult(out_path, words, char_times, duration, "edge")
