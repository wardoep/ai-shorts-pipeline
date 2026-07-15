"""
llm.py -- the three Claude stages, all synchronous (callers run them through
asyncio.to_thread):

  generate_candidates(category, n)  web_search-grounded research -> raw ideas
  judge_candidates(candidates)      no tools, forced structured tool call
                                    against criteria.JUDGE_SCHEMA
  write_shots(story)                forced structured tool call against
                                    criteria.SHOTS_SCHEMA
  rewrite_query(keyword, visual)    tiny fallback-keyword helper for stock.py

A generator never grades its own work -- the judge is a separate call with no
tools. Structured stages force a client-side tool (tool_choice type=tool), the
reliable way to get schema-shaped output without beta features.
"""

import json
import os
import re
import time

import anthropic

import criteria
import store

MODEL = os.environ.get("STORY_MODEL", "claude-sonnet-5")
FAST_MODEL = os.environ.get("STORY_MODEL_FAST", "claude-haiku-4-5")
MAX_SEARCHES = int(os.environ.get("STORY_MAX_SEARCHES", "6"))

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()   # ANTHROPIC_API_KEY from env
    return _client


def _track(model: str, resp) -> None:
    """Record token + web-search usage into the budgets table, split by model
    tier, keyed by month -- feeds the web console's Costs tab. Best-effort:
    a usage-accounting hiccup must never break generation."""
    try:
        u = getattr(resp, "usage", None)
        if not u:
            return
        tier = "fast" if model == FAST_MODEL else "main"
        mon = time.strftime("%Y-%m")
        it = (getattr(u, "input_tokens", 0) or 0) \
            + (getattr(u, "cache_read_input_tokens", 0) or 0) \
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        ot = getattr(u, "output_tokens", 0) or 0
        if it:
            store.bump(f"anthropic_in_{tier}:{mon}", it)
        if ot:
            store.bump(f"anthropic_out_{tier}:{mon}", ot)
        stu = getattr(u, "server_tool_use", None)
        ws = getattr(stu, "web_search_requests", 0) if stu else 0
        if ws:
            store.bump("anthropic_websearch:" + mon, ws)
    except Exception:
        pass


def _mk(**kwargs):
    """messages.create + usage accounting. Single funnel so every LLM call is
    metered for the Costs tab."""
    resp = client().messages.create(**kwargs)
    _track(kwargs.get("model", ""), resp)
    return resp


def _text_of(resp) -> str:
    return "\n".join(b.text for b in resp.content
                     if getattr(b, "type", "") == "text")


def _create(kwargs: dict, max_pauses: int = 3):
    """messages.create that resumes 'pause_turn' (the server web_search loop
    pausing an unfinished turn) and surfaces max_tokens truncation."""
    resp = _mk(**kwargs)
    pauses = 0
    while getattr(resp, "stop_reason", None) == "pause_turn" \
            and pauses < max_pauses:
        kwargs = {**kwargs, "messages": kwargs["messages"]
                  + [{"role": "assistant", "content": resp.content}]}
        resp = _mk(**kwargs)
        pauses += 1
    return resp


def _truncated(resp) -> bool:
    return getattr(resp, "stop_reason", None) == "max_tokens"


def _extract_json(text: str):
    """Pull the first JSON array/object out of model text (fenced or bare)."""
    m = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", text, re.S)
    if m:
        return json.loads(m.group(1))
    # bare: first [ ... last ] (the generator is asked for an array)
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no JSON found in model output")


# ---- stage 1: grounded generator ------------------------------------------------

def generate_candidates(category_key: str, n: int = 4,
                        avoid_titles: list[str] | None = None) -> list[dict]:
    """Research n real story candidates in a category. Returns
    [{title, angle, sources[]}] -- unscored raw material for the judge."""
    cat = criteria.CATEGORIES[category_key]
    avoid = ""
    if avoid_titles:
        avoid = ("\n\nDO NOT pitch any story that matches or closely "
                 "resembles these already-pitched titles:\n- "
                 + "\n- ".join(avoid_titles[:80]))
    prompt = f"""{cat['prompt']}

Use web search to find and VERIFY {n} distinct story candidates. Every
candidate needs at least one credible source URL you actually found (news,
journal, archive, museum, court record -- not Reddit or content farms).
{avoid}

Reply with ONLY a fenced JSON array, one object per candidate:
```json
[{{"title": "short evocative title",
   "angle": "2-3 sentences: the story and why it stops the scroll",
   "sources": ["https://..."]}}]
```"""
    resp = _create(dict(
        model=MODEL,
        max_tokens=8000,
        tools=[{"type": "web_search_20250305", "name": "web_search",
                "max_uses": MAX_SEARCHES}],
        messages=[{"role": "user", "content": prompt}],
    ))
    text = _text_of(resp)
    try:
        cands = _extract_json(text)
    except (ValueError, json.JSONDecodeError):
        if _truncated(resp):
            raise RuntimeError(
                "research output hit max_tokens before the JSON -- retry "
                "(or lower STORY_MAX_SEARCHES)") from None
        cands = _reextract(text)
    out = []
    for c in cands:
        if not isinstance(c, dict) or not c.get("title"):
            continue
        out.append({"title": str(c["title"]).strip(),
                    "angle": str(c.get("angle", "")).strip(),
                    "sources": [s for s in (c.get("sources") or [])
                                if isinstance(s, str) and s.startswith("http")]})
    return out


def _reextract(text: str) -> list[dict]:
    """Salvage pass: hand the messy text to a cheap forced-tool call."""
    schema = {
        "type": "object",
        "properties": {"candidates": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "title": {"type": "string"},
                "angle": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
            }, "required": ["title", "angle", "sources"]},
        }},
        "required": ["candidates"],
    }
    resp = _mk(
        model=FAST_MODEL,
        max_tokens=2000,
        tools=[{"name": "submit_candidates", "description":
                "Submit the story candidates extracted from the text.",
                "input_schema": schema}],
        tool_choice={"type": "tool", "name": "submit_candidates"},
        messages=[{"role": "user", "content":
                   "Extract every story candidate (title, angle, source URLs) "
                   "from this text:\n\n" + text[:20000]}],
    )
    for b in resp.content:
        if getattr(b, "type", "") == "tool_use":
            return b.input.get("candidates", [])
    return []


# ---- stage 2: the judge -----------------------------------------------------------

def judge_candidates(candidates: list[dict]) -> list[dict]:
    """Score candidates against the eight-criteria rubric. Returns the
    JUDGE_SCHEMA rating dicts (id indexes into `candidates`)."""
    if not candidates:
        return []
    listing = "\n\n".join(
        f"CANDIDATE {i}\nTitle: {c['title']}\nAngle: {c['angle']}\n"
        f"Sources: {', '.join(c['sources']) or '(none)'}"
        for i, c in enumerate(candidates))
    resp = _mk(
        model=MODEL,
        max_tokens=6000,
        system=criteria.RUBRIC,
        tools=[{"name": "submit_ratings",
                "description": "Submit one rating object per candidate.",
                "input_schema": criteria.JUDGE_SCHEMA}],
        tool_choice={"type": "tool", "name": "submit_ratings"},
        messages=[{"role": "user", "content":
                   "Rate these candidates. Echo each candidate's number as its "
                   "id.\n\n" + listing}],
    )
    if _truncated(resp):
        raise RuntimeError("judge output hit max_tokens -- fewer candidates "
                           "per batch, or raise the budget")
    for b in resp.content:
        if getattr(b, "type", "") == "tool_use":
            return b.input.get("ratings", [])
    return []


# ---- stage 3: shot writer -----------------------------------------------------------

def write_shots(story: dict) -> dict:
    """story: dict with title/hook/summary/tone/sources. Returns
    {'beats': [{n,v,k[3]}], 'yt_titles': [3]} per SHOTS_SCHEMA."""
    tone = criteria.TONES.get(story["tone"], criteria.TONES["documentary"])
    body = (f"Title: {story['title']}\n"
            f"Tone: {tone['label']} -- {tone['guide']}\n"
            f"Hook (use as beat 1, polish allowed): {story['hook']}\n"
            f"Story summary: {story['summary']}\n"
            f"Sources: {', '.join(story.get('sources') or []) or '(none)'}")
    resp = _mk(
        model=MODEL,
        max_tokens=4000,
        system=criteria.SHOTS_PROMPT,
        tools=[{"name": "submit_script",
                "description": "Submit the finished narration script.",
                "input_schema": criteria.SHOTS_SCHEMA}],
        tool_choice={"type": "tool", "name": "submit_script"},
        messages=[{"role": "user", "content": body}],
    )
    if _truncated(resp):
        raise RuntimeError("shot writer output hit max_tokens")
    for b in resp.content:
        if getattr(b, "type", "") == "tool_use":
            out = b.input
            beats = [{"n": str(x["n"]).strip(), "v": str(x["v"]).strip(),
                      "k": [str(k).strip() for k in x["k"]][:3]}
                     for x in out.get("beats", []) if x.get("n")]
            titles = [str(t).strip() for t in out.get("yt_titles", [])][:3]
            if len(beats) >= 3 and titles:
                return {"beats": beats, "yt_titles": titles}
    raise RuntimeError("shot writer returned no usable script")


# ---- the full batch: research -> judge -> shots -> store -----------------------------

def next_category() -> str:
    """Rotate through the categories the user allows (Settings → category
    filter). Empty filter = all categories. This one place governs auto-gen,
    the daily batch, and any on-demand generate that didn't name a category."""
    allowed = store.gen_categories()
    keys = [k for k in criteria.CATEGORY_KEYS if k in allowed] or \
        criteria.CATEGORY_KEYS
    idx = store.bump("cat_rotation") % len(keys)
    return keys[idx]


def gen_batch(category_key: str | None, n: int, progress=None,
              tone_key: str | None = None) -> list[dict]:
    """Blocking: research -> judge -> shots -> store. Returns stored stories.
    Shared by the Discord bot and the web console.

    `progress(phase, done, total, label)` (optional) is called at each stage so
    a caller (the web console) can render a live loading bar. Phases:
    'research' -> 'judge' -> 'write' (per story) -> 'done'. Any callback error
    is swallowed -- progress reporting must never break generation.

    `tone_key` (optional) forces every story's tone (the Generate composer's
    tone picker); default lets the judge suggest one per story."""
    def _p(phase, done, total, label):
        if progress:
            try:
                progress(phase, done, total, label)
            except Exception:
                pass

    cat = category_key or next_category()
    cat_label = criteria.CATEGORIES.get(cat, {}).get("label", cat)
    _p("research", 0, n, f"Researching {n} real event{'s' if n != 1 else ''} "
                         f"· {cat_label}")
    avoid = store.recent_titles()
    cands = generate_candidates(cat, n, avoid)
    if not cands:
        _p("done", 0, 0, "No fresh angle turned up — try again")
        return []
    _p("judge", 0, len(cands),
       f"Scoring {len(cands)} idea{'s' if len(cands) != 1 else ''} "
       f"against the rubric")
    ratings = judge_candidates(cands)
    out = []
    total = len(ratings)
    for i, r in enumerate(ratings):
        _p("write", len(out), total,
           f"Writing story {min(i + 1, total)} of {total}")
        try:                       # one malformed rating skips itself only
            cand = cands[int(r["id"])]
            scores = criteria.scores_list(r["scores"])
            tone = tone_key or r.get("tone")
            story = {
                "title": r.get("title") or cand["title"],
                "category": cat,
                "tone": tone if tone in criteria.TONES else "documentary",
                "status": "new",
                "scores": scores,
                "viability": int(bool(r.get("viability_pass"))),
                "hook": r.get("hook", ""),
                "summary": r.get("summary") or cand["angle"],
                "sources": cand["sources"],
                "tone_reason": r.get("tone_reason", ""),
                "judge_reason": r.get("reason", ""),
            }
        except (IndexError, ValueError, KeyError, TypeError) as e:
            print(f"[gen] skipping malformed rating: {e!r}", flush=True)
            continue
        if criteria.overall(scores) >= 3.0:
            try:
                shots = write_shots(story)
                story["beats"] = shots["beats"]
                story["yt_titles"] = shots["yt_titles"]
            except Exception as e:
                print(f"[gen] shot writer failed for '{story['title']}': "
                      f"{e!r}", flush=True)
        story["id"] = store.add_story(story)
        out.append(store.get_story(story["id"]))
        _p("write", len(out), total, f"Wrote {len(out)} of {total}")
    _p("done", len(out), total,
       f"Posted {len(out)} draft{'s' if len(out) != 1 else ''}")
    return out


# ---- stock-search helper -------------------------------------------------------------

def rewrite_query(keyword: str, visual: str) -> str:
    """All keywords struck out on the stock sites -- ask for one fresh, more
    generic 2-4 word search phrase for the beat's visual."""
    resp = _mk(
        model=FAST_MODEL,
        max_tokens=50,
        messages=[{"role": "user", "content":
                   f"A stock-video search for '{keyword}' found nothing. The "
                   f"shot needs: {visual}. Reply with ONE alternative stock "
                   "video search phrase, 2-4 generic visual words, nothing "
                   "else."}],
    )
    text = _text_of(resp).strip().strip('"').strip("'")
    return " ".join(text.split()[:4]) or keyword
