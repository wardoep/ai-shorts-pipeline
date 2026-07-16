# Story bot (SCOUT) — setup

Turns AI-researched story ideas into finished YouTube Shorts:
**generate → greenlight in Discord → auto-render (Pexels b-roll + narration +
karaoke captions + music) → preview in Discord → ship.**

## One-time setup (4 things only you can do)

1. **Discord bot** — <https://discord.com/developers/applications> → New
   Application `story bot` → Bot → Reset Token → paste into `storybot.env`
   as `STORY_DISCORD_TOKEN`. No privileged intents needed. Invite to the
   Your Discord server: OAuth2 → URL Generator → scopes `bot` + `applications.commands`,
   permissions: Manage Channels, Send Messages, Embed Links, Attach Files,
   Pin Messages.
2. **Footage key — at least one of** (both free + instant):
   - Pexels — <https://www.pexels.com/api/> → `PEXELS_API_KEY`
   - Pixabay — <https://pixabay.com/api/docs/> → `PIXABAY_API_KEY`

   With only Pixabay, the still-photo Ken-Burns last resort is unavailable
   (it's Pexels-backed) — beats with no video results block instead.
3. ~~**ElevenLabs Creator**~~ ✅ DONE 2026-07-14 — key in `storybot.env`
   with user-read scope: credit readout + pre-flight quota guard active,
   all 5 tone voices verified live. (Clyde was retired by ElevenLabs; the
   dark tone now uses Bill. Override any tone with `STORY_VOICE_<TONE>`.)
4. **Music** (optional but recommended) — drop mp3s into
   `music/<tone>/` (`mysterious`, `cinematic`, `dark`, `suspense`,
   `documentary`) or `music/any/`. Sources with safe licenses for monetized
   YouTube: Pixabay Music, YouTube Audio Library (download by hand — neither
   has an API). If a track requires attribution, note it in that folder's
   `manifest.json`:
   `{"track.mp3": {"attribution": "Song by X (YouTube Audio Library)"}}`
   Renders without music are silent-bed (voice only) — not an error.

Then:

```bash
cd /path/to/ai-shorts-pipeline
cp storybot.env.example storybot.env   # fill in tokens
chmod 600 storybot.env
systemctl --user enable --now storybot
tail -f logs/story_bot.log
```

The bot creates `#story-feed` + `#story-queue` under the **Story Pipeline**
category and pins the rubric on first run.

## Web console (OBSCURA UI)

The original Story Bot design, live against the real pipeline. Served by the
bot process at `http://127.0.0.1:8377` (change with `STORY_WEB_HOST/PORT`,
disable with `STORY_WEB=0`). Also runs without Discord:
`.venv/bin/python web.py` — jobs you queue from the browser are picked up by
the bot's worker whenever it runs.

- **#story-feed** — a LinkedIn-style split view: compact story list on the
  left (avatar, rating, status, ✕ to pass), full detail on the right (✓ pills,
  **Approve** button, the **SCOUT RATING** match panel with all eight criteria
  bars, hook/summary, YT titles, script). **Generate stories** opens a
  composer: pick **how many** (1–`STORY_MAX_GEN`), **what kind** (any of the
  11 categories or Auto), and **tone** (5 tones or Auto — Auto lets the judge
  pick per story). A **live loading panel** then shows the phase
  (Research → Judge → Write) with a bar + count. The standalone console also
  **auto-generates ~`STORY_DAILY_TARGET` drafts/day** (set
  `STORY_WEB_AUTOGEN=0` to disable) and runs a **render worker in-process**,
  so approving a story actually renders it (no Discord needed).
- **#review-board** — filter chips (rating/status/category/tone), search, sort.
- **#production-queue** — greenlit stories with a LIVE render **progress bar**
  (Narration → Footage → Cutting → Preview), the preview player once a render
  finishes, and 🚢 Ship · 🎲 Re-roll clips · 🎙 Re-voice · credits.txt.
- **#rating-rubric** — the eight weighted criteria + tone guide.
- **#spend-and-credits** — API spend + credit tab: estimated Anthropic spend
  this month (from tracked token usage), ElevenLabs credits remaining **and the
  reset date** (when to top up), and today's guardrails (drafts/renders/Pexels).
- **#settings** — your **defaults, set once**: which stages each render
  includes (**Footage / Music / Captions** switches) and **which story
  categories** get generated (11 chips, tap to toggle — feeds auto-gen +
  Generate). Every render inherits these; you can still flip any switch on an
  individual story via the **INCLUDE** pills on its production-queue card.
- **🎨 Style, voice & music** (per story, from its queue card):
  - **Captions** — **font** (system fonts or upload a `.ttf`/`.otf`),
    **highlight colour**, **words per caption** (1/2/3 — default 2 for a punchy
    look), **size**, **animation** (karaoke sweep / fade in / pop / plain),
    **position**, UPPERCASE — with a **live 9:16 preview** as you change them.
  - **Voice** — a list of your ElevenLabs voices; **tap ▶ to hear a sample**,
    then pick one (or leave the per-tone default). Changing it re-voices.
  - **Music** — *Auto* (random from the story's tone), a specific track, or
    off. **Upload tracks** right there; a starter set of royalty-free ambient
    beds ships in `music/<tone>/`.
  - **Apply & re-render** re-cuts with your changes — a caption/music tweak
    keeps footage + narration (fast); a voice change re-synths narration.
  Global caption defaults also live in **#settings**.
  **Footage off = a clean dark backdrop under the voice + captions — needs no
  footage key at all**, so you can produce videos today and drop your own
  footage over them (or ship as-is).

Changes sync both ways: web approvals update the Discord feed card, and the
page polls every 4s so Discord-side changes appear in the browser.

To reach it away from home, add a cloudflared ingress route (e.g.
`story.example.com`) pointing at `127.0.0.1:8377`, and/or set `STORY_WEB_SECRET`
(then open `/?key=<secret>` once — a cookie remembers it).

## Daily use

- Drafts arrive automatically (2/day ~13:00 UTC) or on demand:
  `/generate [category] [count]`, or DM `-gen murder`.
- Card buttons: ✅ Approve (queues the render) · ✖️ Pass · 🔄 Regenerate ·
  🔍 Details (scores + shot list) · tone dropdown.
- Render lands in `#story-queue` with the preview MP4 and:
  🚢 Ship (marks done, prints the YouTube description credits) ·
  🎲 Re-roll clips (new footage, same voice) · 🎙️ Re-voice · 🗑️ Scrap.
- The **master** (full quality) lives at `renders/<id>/master.mp4` — upload
  THAT to YouTube, not the Discord preview. `credits.txt` sits next to it.
- `/stats` = budgets (renders/day cap, ElevenLabs credits, Pexels calls).
- `/brief <id>` = the markdown production brief (with Envato search links
  for manual footage upgrades).

## Cost guards

- `STORY_MAX_RENDERS_PER_DAY=5` hard cap.
- ElevenLabs pre-flight credit check — a render never dies mid-way on quota.
- Pexels calls capped at 190/hr, cached 24h per keyword.
- A ~60s Short ≈ 1,100 ElevenLabs credits → Creator (121k) ≈ 100+/month.

## Licensing (what keeps monetization safe)

- Pexels/Pixabay footage: free for commercial use, no attribution required.
  Per-clip credits still land in `renders/<id>/credits.txt` (+ a Pexels
  mention — their API terms ask for a visible credit).
- ElevenLabs paid tiers include the commercial voice license. edge-tts is
  for testing only — don't monetize videos narrated with it.
- Music: only Pixabay/YT-Audio-Library tracks you put in `music/`;
  attribution flags from `manifest.json` are copied into `credits.txt`.
