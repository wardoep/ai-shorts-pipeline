"""
criteria.py -- the single source of truth for what the bot generates and how it
rates. Everything the LLM stages do is driven by the constants here, so tuning
the channel's taste means editing this file, not the pipeline.

Three-stage design:
  * GENERATOR (llm.generate_candidates) uses CATEGORIES[...]["prompt"] + web
    search to find real, cited story ideas.
  * JUDGE (llm.judge_candidates) scores each candidate on the eight weighted
    CRITERIA using JUDGE_SCHEMA, picks a TONE, writes hook + summary.
  * SHOT WRITER (llm.write_shots) turns a story into a Shorts narration script:
    4-8 beats of {narration, visual, keywords} plus 3 YouTube title options
    (SHOTS_SCHEMA).

Rating model (mirrors the Story Bot UI prototype): eight 1-5 integer scores,
weighted average = overall. Tier is DISPLAY ONLY -- nothing auto-approves:
  RECOMMENDED  overall >= APPROVAL_THRESHOLD and min(score) >= 3 and >=1 source
  REVIEW       overall >= 3.0
  PASS         everything else
The operator greenlights by hand; greenlit stories go to the render queue.
"""

import os

# --- categories -------------------------------------------------------------
# key -> {label, emoji, color, prompt}. The prompt is the research brief handed
# to the grounded generator. "true-history" categories lean hard on verifiable
# sources; the softer science/experiment ones still require a citable basis.
# Colors come from the UI prototype's CATCOLOR map (used for embed accents).

CATEGORIES = {
    "strange_places": {
        "label": "Strange places",
        "emoji": "\U0001F5FA️",  # 🗺️
        "color": 0x6EA8FE,
        "prompt": (
            "Find real, verifiable STRANGE PLACES on Earth -- geologically, "
            "historically, or culturally bizarre locations a viewer has almost "
            "certainly never heard of. Avoid the overexposed (Area 51, Bermuda "
            "Triangle). Favor obscure, visually striking spots with a documented "
            "oddity."
        ),
    },
    "murder_mysteries": {
        "label": "Murder mysteries",
        "emoji": "\U0001F52A",  # 🔪
        "color": 0xF2777C,
        "prompt": (
            "Find real MURDER MYSTERIES -- documented cases (solved or unsolved) "
            "with a genuine twist, an unresolved question, or a shocking detail. "
            "Every case must be citable to news coverage, court records, or "
            "reputable true-crime reporting. Treat victims with respect; no gore "
            "for its own sake."
        ),
    },
    "billionaire_stories": {
        "label": "Billionaire stories",
        "emoji": "\U0001F4B0",  # 💰
        "color": 0xF2B64A,
        "prompt": (
            "Find real BILLIONAIRE / extreme-wealth stories -- rise, fall, feud, "
            "eccentricity, or hidden scandal, anchored to a specific named person "
            "and verifiable reporting. Favor the surprising and lesser-known over "
            "the endlessly-covered (Musk/Bezos daily news)."
        ),
    },
    "science_facts": {
        "label": "Science facts",
        "emoji": "\U0001F9EC",  # 🧬
        "color": 0x4FD1C5,
        "prompt": (
            "Find real, mind-bending SCIENCE FACTS or discoveries with a strong "
            "'wait, what?' hook -- physics, biology, space, deep sea, the human "
            "body. Must be scientifically accurate and citable to research or "
            "reputable science journalism. No debunked myths."
        ),
    },
    "unexplained_events": {
        "label": "Unexplained events",
        "emoji": "\U0001F47B",  # 👻
        "color": 0xA78BFA,
        "prompt": (
            "Find real UNEXPLAINED EVENTS -- documented incidents that genuinely "
            "happened but were never fully explained (disappearances, signals, "
            "mass phenomena). The EVENT must be real and sourced even if the "
            "explanation is open. Avoid pure hoaxes and debunked stories."
        ),
    },
    "abandoned_places": {
        "label": "Abandoned places",
        "emoji": "\U0001F3DA️",  # 🏚️
        "color": 0x9AA3B2,
        "prompt": (
            "Find real ABANDONED PLACES -- ghost towns, derelict facilities, "
            "sunken or evacuated sites -- with a compelling backstory and strong "
            "visual/b-roll potential. Must be a real, locatable place with citable "
            "history."
        ),
    },
    "unsolved_mysteries": {
        "label": "Unsolved mysteries",
        "emoji": "\U0001F50D",  # 🔍
        "color": 0xC084FC,
        "prompt": (
            "Find real UNSOLVED MYSTERIES beyond murder cases -- vanished people, "
            "artifacts, codes, signals, or historical puzzles that remain open. "
            "Must be a documented, sourced mystery, not internet folklore."
        ),
    },
    "businesses": {
        "label": "Businesses",
        "emoji": "\U0001F3E2",  # 🏢
        "color": 0x5EEAD4,
        "prompt": (
            "Find real BUSINESS stories -- a stunning rise, spectacular collapse, "
            "fraud, or bizarre company history, anchored to a named real company "
            "and verifiable reporting. Favor the untold or forgotten over the "
            "endlessly-covered (Enron, Theranos unless a fresh angle)."
        ),
    },
    "brands": {
        "label": "Brands",
        "emoji": "\U0001F3F7️",  # 🏷️
        "color": 0xFB923C,
        "prompt": (
            "Find real BRAND stories -- the hidden origin, dark chapter, bizarre "
            "pivot, or forgotten failure behind a household-name brand or product. "
            "Anchor every story to a named brand and verifiable reporting or "
            "company history. Favor 'you use this every day and never knew' "
            "angles over corporate PR lore."
        ),
    },
    "technology": {
        "label": "Technology",
        "emoji": "\U0001F4BB",  # 💻
        "color": 0x60A5FA,
        "prompt": (
            "Find real TECHNOLOGY stories -- lost inventions, catastrophic bugs, "
            "abandoned megaprojects, forgotten pioneers, or machines that changed "
            "history in a way nobody remembers. Must be citable to reputable tech "
            "journalism or history-of-computing sources. No vaporware rumors."
        ),
    },
    "weird_experiments": {
        "label": "Weird experiments",
        "emoji": "\U0001F9EA",  # 🧪
        "color": 0xA3E635,
        "prompt": (
            "Find real WEIRD / unsettling EXPERIMENTS -- historical scientific, "
            "psychological, or medical experiments that actually happened and are "
            "documented. Must be citable to history-of-science sources. No "
            "fictional or urban-legend experiments."
        ),
    },
}

CATEGORY_KEYS = list(CATEGORIES.keys())
CATEGORY_LABELS = [v["label"] for v in CATEGORIES.values()]


def resolve_category(text: str) -> str | None:
    """Loose match a user string ('murder', 'Murder Mysteries', key) -> key."""
    t = (text or "").strip().lower().replace(" ", "_")
    if t in CATEGORIES:
        return t
    for k, v in CATEGORIES.items():
        if t and (t in k or t in v["label"].lower().replace(" ", "_")):
            return k
    return None


def category_for_label(label: str) -> str | None:
    for k, v in CATEGORIES.items():
        if v["label"] == label:
            return k
    return None


# --- tone taxonomy ----------------------------------------------------------
# The five canonical tones from the UI prototype. The judge picks exactly one
# per story; the tone drives the narrator voice and the music folder.
# color: embed/dot accent. voice: default ElevenLabs premade voice id
# (override per tone with STORY_VOICE_<KEY> env vars). edge: fallback
# edge-tts neural voice used when no ElevenLabs key is configured.

TONES = {
    "mysterious": {
        "label": "Mysterious / eerie",
        "color": 0xA78BFA,
        "voice": "N2lVS1w4EtoT3dr4eOWO",   # Callum -- husky, intriguing
        "edge": "en-US-EricNeural",
        "guide": "Whispers of the unknown. Slow reveals, open questions, quiet "
                 "dread that never fully resolves.",
    },
    "cinematic": {
        "label": "Cinematic / dramatic",
        "color": 0xF2B64A,
        "voice": "onwK4e9ZLuTAKqWW03F9",   # Daniel -- authoritative broadcast
        "edge": "en-US-GuyNeural",
        "guide": "Big, sweeping delivery. Stakes and scale up front, momentum "
                 "the whole way, a payoff with weight.",
    },
    "dark": {
        "label": "Dark / unsettling",
        "color": 0xF2777C,
        "voice": "pqHfZKP75CvOlQylNhV4",   # Bill -- weathered, plain gravity
                                           # (Clyde retired 2026; his old id now
                                           # resolves to an unrelated community
                                           # voice -- do not reuse it)
        "edge": "en-US-ChristopherNeural",
        "guide": "Heavy and disquieting. Facts delivered plainly enough that "
                 "the horror does its own work. Respect for real victims.",
    },
    "suspense": {
        "label": "Suspenseful / thriller",
        "color": 0xFB923C,
        "voice": "pNInz6obpgDQGcFmaJgB",   # Adam -- deep, driving
        "edge": "en-US-AndrewNeural",
        "guide": "A ticking clock. Short sentences, withheld information, "
                 "every beat ends on a hook for the next.",
    },
    "documentary": {
        "label": "Documentary / informative",
        "color": 0x4FD1C5,
        "voice": "JBFqnCBsd6RMkjVDRZzb",   # George -- warm, measured
        "edge": "en-GB-RyanNeural",
        "guide": "Clear and credible. Curiosity over drama; specifics, names "
                 "and dates carry the interest.",
    },
}

TONE_KEYS = list(TONES.keys())
TONE_LABELS = [v["label"] for v in TONES.values()]


def resolve_tone(text: str) -> str | None:
    """Loose match 'Mysterious / eerie', 'dark', a key, etc. -> tone key."""
    t = (text or "").strip().lower()
    if t in TONES:
        return t
    for k, v in TONES.items():
        if t and (t in v["label"].lower() or v["label"].lower() in t):
            return k
    return None


def tone_voice(tone_key: str) -> str:
    """ElevenLabs voice id for a tone, env-overridable per tone or globally."""
    v = os.environ.get(f"STORY_VOICE_{tone_key.upper()}")
    if v:
        return v
    default = os.environ.get("STORY_VOICE_DEFAULT")
    if default:
        return default
    return TONES.get(tone_key, TONES["documentary"])["voice"]


def tone_edge_voice(tone_key: str) -> str:
    return os.environ.get("STORY_EDGE_VOICE") or \
        TONES.get(tone_key, TONES["documentary"])["edge"]


# --- the rubric: eight weighted criteria (from the Story Bot prototype) ------
# scores[] is index-aligned to CRITERIA. overall = weighted average (1 decimal).

CRITERIA = [
    {"key": "interest", "label": "Interest", "weight": 1.3,
     "desc": "How intriguing the premise is the instant you hear it.",
     "one": "Common knowledge -- viewers already know how it ends.",
     "five": "A premise so strange the viewer physically can't scroll past."},
    {"key": "engagement", "label": "Engagement", "weight": 1.2,
     "desc": "Whether the story keeps pulling viewers to the end.",
     "one": "Front-loaded -- the title is the best part and it fizzles.",
     "five": "Escalates the whole way with a loop that only closes at the payoff."},
    {"key": "originality", "label": "Originality", "weight": 1.0,
     "desc": "How fresh or untold it is on YouTube right now.",
     "one": "Ten big channels covered it this year.",
     "five": "Barely documented -- no major channel has told it."},
    {"key": "hook", "label": "Hook strength", "weight": 1.5,
     "desc": "How hard the first line grabs. Critical for Shorts.",
     "one": "Slow, generic opener that earns a swipe.",
     "five": "The first three seconds are unskippable and demand an answer."},
    {"key": "virality", "label": "Virality", "weight": 1.4,
     "desc": "How shareable and clickable it is.",
     "one": "Niche -- hard to package into a title or thumbnail.",
     "five": "Begs to be shared, screenshotted and argued about in the comments."},
    {"key": "factual", "label": "Factual richness", "weight": 0.7,
     "desc": "Whether there's enough real detail to fill the runtime.",
     "one": "One thin fact stretched past its limit.",
     "five": "Layers of verifiable specifics -- names, dates and a twist."},
    {"key": "emotional", "label": "Emotional impact", "weight": 1.0,
     "desc": "The strength of the feeling it leaves behind.",
     "one": "Flat and forgettable a minute later.",
     "five": "Lands a real gut-punch of dread, awe or disbelief."},
    {"key": "faceless", "label": "Faceless-friendly", "weight": 0.9,
     "desc": "How well it works with stock/AI visuals and voiceover.",
     "one": "Needs an on-camera host or has nothing to show.",
     "five": "Rich, ready-made visuals -- carries entirely on voiceover and b-roll."},
]

WEIGHTS = [c["weight"] for c in CRITERIA]
_WSUM = sum(WEIGHTS)

APPROVAL_THRESHOLD = float(os.environ.get("STORY_THRESHOLD", "4.0"))
MIN_SOURCES = 1        # a RECOMMENDED story must carry at least one source URL


def overall(scores) -> float:
    """Weighted average of the eight 1-5 subscores."""
    return sum(s * w for s, w in zip(scores, WEIGHTS)) / _WSUM


def tier(scores, has_source: bool, threshold: float | None = None) -> str:
    """Display tier -- never auto-approves anything."""
    th = APPROVAL_THRESHOLD if threshold is None else threshold
    ov = overall(scores)
    if ov >= th and min(scores) >= 3 and has_source:
        return "RECOMMENDED"
    if ov >= 3.0:
        return "REVIEW"
    return "PASS"


def stars(scores) -> int:
    """Rounded overall -> 1-5 star count for cards."""
    return max(1, min(5, round(overall(scores))))


def star_line(scores) -> str:
    n = stars(scores)
    return "★" * n + "☆" * (5 - n) + f"  {overall(scores):.1f}/5.0"


# --- judge rubric text --------------------------------------------------------

def _criteria_text() -> str:
    out = []
    for c in CRITERIA:
        out.append(f"{c['label'].upper()} (x{c['weight']}): {c['desc']}\n"
                   f"  1 = {c['one']}\n  5 = {c['five']}")
    return "\n".join(out)


RUBRIC = f"""You are the story editor for a faceless YouTube SHORTS channel
(45-90 second vertical videos: stock b-roll + voiceover + captions). Rate each
candidate story on EIGHT criteria, each an integer 1-5. Hooks and virality
carry the most weight -- on Shorts the first three seconds decide everything.

{_criteria_text()}

VIABILITY (pass/fail, not scored) -- a story fails viability unless ALL hold:
  * at least {MIN_SOURCES} credible source URL is present,
  * there is enough visual / b-roll potential (places, objects, phenomena,
    archival-style footage) to sustain a faceless vertical video,
  * it can carry a tight 45-90 second narration with a real payoff -- one
    strong revelation, not a topic that needs ten minutes of setup.

Be a harsh gatekeeper: most ideas should score in the 2-3 range on several
criteria. Do not inflate scores. Never invent facts; if a candidate looks
fabricated or unsourced, score it low and set viability_pass = false.

For every candidate also pick the single best TONE from the allowed list and
give a one-line reason, write a tight 2-3 sentence SUMMARY, and a one-line
HOOK -- the exact opening line a narrator would say, engineered to stop the
scroll."""


# --- JSON schema the judge fills (structured output) ------------------------
# One object per candidate, echoing the id so results can be matched back.

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "ratings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "scores": {
                        "type": "object",
                        "properties": {
                            c["key"]: {"type": "integer", "enum": [1, 2, 3, 4, 5]}
                            for c in CRITERIA
                        },
                        "required": [c["key"] for c in CRITERIA],
                        "additionalProperties": False,
                    },
                    "viability_pass": {"type": "boolean"},
                    "tone": {"type": "string", "enum": TONE_KEYS},
                    "tone_reason": {"type": "string"},
                    "summary": {"type": "string"},
                    "hook": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "id", "title", "scores", "viability_pass",
                    "tone", "tone_reason", "summary", "hook", "reason",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ratings"],
    "additionalProperties": False,
}


def scores_list(scores_obj: dict) -> list[int]:
    """Schema's keyed scores object -> ordered list aligned with CRITERIA."""
    return [int(scores_obj[c["key"]]) for c in CRITERIA]


# --- shot writer ---------------------------------------------------------------

SHOTS_PROMPT = """You are the script writer for a faceless YouTube Shorts
channel. Turn the story below into a vertical-video narration script.

Rules:
  * 4 to 8 beats. Each beat is ONE spoken sentence (max ~22 words) that reads
    naturally aloud -- contractions welcome, no headline-speak.
  * The first beat IS the hook: it must demand an answer within 3 seconds.
  * The last beat lands the payoff or the unresolved question.
  * Total narration must run 45-90 seconds read aloud (roughly 110-220 words).
  * Never invent facts. Stay strictly inside the sourced story summary.
  * For each beat: "visual" = one concrete shot description; "keywords" =
    exactly 3 STOCK-FOOTAGE search phrases (2-4 words each, generic and
    visual -- 'abandoned hospital corridor', not names of real people).
    Stock libraries have no footage of specific people or events, so ask for
    atmosphere, objects and places that stand in for them.
  * Also write 3 YouTube title options: punchy, curiosity-gap, under 70 chars.
"""

SHOTS_SCHEMA = {
    "type": "object",
    "properties": {
        "beats": {
            "type": "array",
            "minItems": 4,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "string", "description": "narration sentence"},
                    "v": {"type": "string", "description": "on-screen visual"},
                    "k": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 3,
                        "maxItems": 3,
                    },
                },
                "required": ["n", "v", "k"],
                "additionalProperties": False,
            },
        },
        "yt_titles": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 3,
        },
    },
    "required": ["beats", "yt_titles"],
    "additionalProperties": False,
}
