# AI Shorts Pipeline

An end-to-end pipeline that turns AI-researched story ideas into finished, publish-ready
vertical videos (YouTube Shorts) — with a human approval gate in the middle and hard cost
controls throughout.

## Overview

A single Python daemon ("SCOUT") drafts short-form story scripts with Claude, posts them
as cards to Discord and to a local web console, and waits. Nothing auto-publishes: an
operator reviews each draft and greenlights it by hand. Approved stories are rendered
automatically — narration is synthesized, word-timed karaoke captions are generated,
stock b-roll is fetched per scene, and ffmpeg assembles a 1080x1920 master with crossfades
and an optional ducked music bed. The preview lands back in Discord with
Ship / Re-roll clips / Re-voice / Scrap buttons.

The same pipeline also runs entirely without Discord: the bundled web console
(aiohttp + a single-page UI in `webui/`) can generate, review, approve, and render on its own.

## Pipeline architecture

```
 Claude (web-search research)        generate story candidates, with sources
        |
 Claude judge (separate call)        scores 8 weighted criteria -> RECOMMENDED / REVIEW / PASS
        |                            (advisory only -- a human always decides)
 Claude shot writer                  4-8 beats: narration + visual + search keywords
        |
 Greenlight gate ------------------- Discord card buttons  <->  web console (synced both ways)
        |
 Render worker (one job at a time, resumable state machine):
   tts         ElevenLabs (or free edge-tts) -> narration.mp3 + per-word timings
   clips       Pexels -> LLM-rewritten query -> Pixabay -> Pexels photo (Ken Burns fallback)
   assembling  per-beat segments -> xfade chain -> burn .ass captions -> master.mp4
   preview     re-encode under the Discord attachment size limit
        |
 Ship ------------------------------ master.mp4 + credits.txt (per-clip/music attribution)
```

State lives in SQLite (WAL mode): stories, the render job queue, per-period budget
counters, a 14-day clip-reuse ban list, and a 24-hour stock-search cache.

## Key features

- **Pre-flight quota checks** — before a render starts, the ElevenLabs credit balance is
  read and compared against the script length, so a render never dies halfway through on
  quota. Shortfalls mark the job `blocked` with a human-readable reason instead of failing.
- **Resumable renders** — each stage (tts, clips, assembling, preview) leaves artifacts in
  `renders/<id>/` and is skipped on re-run, so a crashed or restarted job resumes where it died.
- **Budget and cost tracking** — hard daily render cap, Pexels calls rate-limited per hour
  and month, TTS characters counted per month, and a spend tab that estimates monthly
  Anthropic API cost from tracked token usage plus ElevenLabs credits remaining and reset date.
- **Per-stage caching** — stock keyword searches are cached 24 h in SQLite (which also
  satisfies Pixabay's mandatory caching rule); clips are never reused within a story or
  within 14 days across stories.
- **Graceful degradation** — no ElevenLabs key falls back to free edge-tts; no footage key
  (or "footage off") renders a clean dark backdrop under voice + captions; missing music
  means a silent bed, not an error; failed stock lookups walk a Pexels → rewritten-query →
  Pixabay → still-photo fallback chain.
- **Word-timed karaoke captions** — per-word timestamps from the TTS provider drive burned-in
  `.ass` subtitles with configurable font, color, size, position, and animation
  (sweep / fade / pop / plain), previewed live in the web console.
- **License hygiene** — every render writes a `credits.txt` and manifest with per-clip and
  music attribution, so monetization-safe sourcing is auditable.
- **Frame-accurate assembly** — beat boundaries come from character-level narration
  timings, and the xfade math guarantees the video's total length equals the narration's.

## Tech stack

- **Python 3** — asyncio daemon; blocking work isolated in `asyncio.to_thread`
- **Anthropic API** — three separated Claude stages (generator with web search, judge,
  shot writer); structured output via forced tool calls
- **discord.py** — slash commands, button-driven review UI, restart-safe interactions
- **ElevenLabs / edge-tts** — narration with word-level timing alignment
- **Pexels / Pixabay APIs** — portrait-first stock b-roll selection
- **ffmpeg** — segment normalization, xfade chaining, libass caption burn-in, audio ducking
- **SQLite (WAL)** — persistence, job queue, budgets, caches
- **aiohttp** — local web console, exposed via a reverse-proxy tunnel

## What I learned

- **Orchestrating a multi-service pipeline.** Five external APIs (Anthropic, Discord,
  ElevenLabs, Pexels, Pixabay) feed one render, and any of them can fail or throttle at any
  time. Designing the flow as a resumable state machine with explicit `blocked`/`failed`
  states — rather than one long fragile script — was the single biggest reliability win.
- **Media processing with ffmpeg.** I learned filter graphs the hard way: scale-to-cover and
  center-crop for mixed aspect ratios, xfade offset math so transitions land exactly on
  narration beats, libass subtitle burn-in with custom fonts, and sidechain-style music
  ducking under a voice track.
- **API quota and budget management.** Every metered resource gets a counter keyed by
  period (`renders:2026-07-14`, `pexels_hr:...`), checked *before* spending. The pre-flight
  ElevenLabs credit check taught me that failing early with a clear reason is far cheaper
  than failing halfway through a paid operation.
- **Graceful failure handling.** Each dependency has a defined degraded mode (free TTS,
  no-footage backdrop, silent music bed), so a missing key downgrades output quality
  instead of taking the service down.
- **Running services on Linux.** The bot runs as a systemd user service with an
  `EnvironmentFile` for secrets (cron/systemd don't read shell profiles — learned that one
  in practice), WAL-mode SQLite for safe access from worker threads, and a capped ffmpeg
  thread count so renders coexist politely with other services on a shared 6-core box.
- **Reverse-proxying a local web console.** The console binds to loopback only; remote
  access goes through a Cloudflare Tunnel (cloudflared) ingress route with an access policy
  in front, plus an optional shared-secret cookie — no ports ever exposed directly.

## Setup

See [SETUP.md](SETUP.md) for API keys, the Discord application, systemd installation, and
daily operation. `storybot.env.example` documents every configuration knob.

---
Built and maintained by **Edward J. Penna** — [github.com/wardoep](https://github.com/wardoep)
