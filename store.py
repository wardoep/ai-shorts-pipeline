"""
store.py -- SQLite persistence for the story pipeline. One writer (the bot
process); every call opens its own short-lived connection (WAL journal, busy
timeout) so the sync functions are safe from asyncio.to_thread's pool threads.

Tables:
  stories       every pitched story, all statuses (rejected rows feed the
                dedupe list so the generator never re-pitches a topic)
  jobs          render queue + state machine progress
  clips_used    every stock clip that made it into a render (14-day reuse ban)
  keyword_cache keyword -> search results (24h, also serves Pixabay's
                mandatory 24h caching rule)
  budgets       plain counters keyed 'what:period' (renders:2026-07-14,
                eleven_chars:2026-07, ...)
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("STORY_DB", os.path.join(BASE, "storybot.db"))

_JSON_FIELDS = ("scores", "yt_titles", "sources", "beats")

STORY_STATUSES = ("new", "approved", "rejected", "shipped")
JOB_STATES = ("queued", "tts", "clips", "assembling", "preview",
              "blocked", "failed", "done", "scrapped")


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS stories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            tone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            scores TEXT NOT NULL,             -- JSON [8 ints]
            viability INTEGER NOT NULL DEFAULT 0,
            hook TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            yt_titles TEXT DEFAULT '[]',      -- JSON [3 strings]
            sources TEXT DEFAULT '[]',        -- JSON [urls]
            beats TEXT DEFAULT '[]',          -- JSON [{n,v,k[3]}]
            tone_reason TEXT DEFAULT '',
            judge_reason TEXT DEFAULT '',
            feed_msg_id INTEGER,
            queue_msg_id INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id INTEGER NOT NULL REFERENCES stories(id),
            state TEXT NOT NULL DEFAULT 'queued',
            error TEXT DEFAULT '',
            keep_voice INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            status_msg_id INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clips_used(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id INTEGER NOT NULL,
            beat_idx INTEGER NOT NULL,
            source TEXT NOT NULL,             -- pexels | pixabay | photo
            video_id TEXT NOT NULL,
            url TEXT DEFAULT '',
            author TEXT DEFAULT '',
            author_url TEXT DEFAULT '',
            width INTEGER, height INTEGER, duration REAL,
            used_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_clips_used_at ON clips_used(used_at);
        CREATE TABLE IF NOT EXISTS keyword_cache(
            keyword TEXT NOT NULL,
            source TEXT NOT NULL,
            payload TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (keyword, source)
        );
        CREATE TABLE IF NOT EXISTS budgets(
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS kv(         -- JSON settings: production
            key TEXT PRIMARY KEY,              -- option defaults, per-story
            value TEXT NOT NULL                -- overrides, category filter
        );
        """)


# ---- stories -----------------------------------------------------------------

def _decode(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in _JSON_FIELDS:
        if f in d and isinstance(d[f], str):
            try:
                d[f] = json.loads(d[f])
            except (json.JSONDecodeError, TypeError):
                d[f] = []
    return d


def add_story(s: dict) -> int:
    now = time.time()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO stories(title, category, tone, status, scores,
                   viability, hook, summary, yt_titles, sources, beats,
                   tone_reason, judge_reason, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s["title"], s["category"], s["tone"], s.get("status", "new"),
             json.dumps(s["scores"]), int(s.get("viability", 0)),
             s.get("hook", ""), s.get("summary", ""),
             json.dumps(s.get("yt_titles", [])),
             json.dumps(s.get("sources", [])),
             json.dumps(s.get("beats", [])),
             s.get("tone_reason", ""), s.get("judge_reason", ""), now, now))
        return cur.lastrowid


def get_story(story_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM stories WHERE id=?",
                          (story_id,)).fetchone()
    return _decode(row)


def update_story(story_id: int, **fields):
    if not fields:
        return
    sets, vals = [], []
    for k, v in fields.items():
        if k in _JSON_FIELDS:
            v = json.dumps(v)
        sets.append(f"{k}=?")
        vals.append(v)
    sets.append("updated_at=?")
    vals.append(time.time())
    vals.append(story_id)
    with _conn() as con:
        con.execute(f"UPDATE stories SET {', '.join(sets)} WHERE id=?", vals)


def list_stories(status: str | None = None, limit: int = 50) -> list[dict]:
    q = "SELECT * FROM stories"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY id DESC LIMIT ?"
    with _conn() as con:
        rows = con.execute(q, args + (limit,)).fetchall()
    return [_decode(r) for r in rows]


def recent_titles(limit: int = 80) -> list[str]:
    """Everything recently pitched (any status) -- the generator's do-not-repeat
    list."""
    with _conn() as con:
        rows = con.execute(
            "SELECT title FROM stories ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    return [r["title"] for r in rows]


def story_by_msg(msg_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM stories WHERE feed_msg_id=? OR queue_msg_id=?",
            (msg_id, msg_id)).fetchone()
    return _decode(row)


# ---- jobs ----------------------------------------------------------------------

def create_job(story_id: int, keep_voice: bool = False) -> int | None:
    """Insert a queued job, but only if the story has no in-flight job already.
    The guard is a single atomic statement so two concurrent callers (double-
    clicked Approve, or Discord + web console at once) can't both insert.
    Returns the new job id, or None if one was already in flight."""
    now = time.time()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO jobs(story_id, state, keep_voice, created_at,
                                updated_at)
               SELECT ?, 'queued', ?, ?, ?
               WHERE NOT EXISTS (
                   SELECT 1 FROM jobs WHERE story_id=?
                   AND state NOT IN ('done','failed','scrapped'))""",
            (story_id, int(keep_voice), now, now, story_id))
        return cur.lastrowid if cur.rowcount else None


def get_job(job_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def update_job(job_id: int, **fields):
    if not fields:
        return
    sets = [f"{k}=?" for k in fields]
    vals = list(fields.values())
    sets.append("updated_at=?")
    vals += [time.time(), job_id]
    with _conn() as con:
        con.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", vals)


def next_queued_job() -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def active_job_for_story(story_id: int) -> dict | None:
    """The story's most recent job that is still in-flight (not terminal)."""
    with _conn() as con:
        row = con.execute(
            """SELECT * FROM jobs WHERE story_id=? AND state NOT IN
               ('done','failed','scrapped') ORDER BY id DESC LIMIT 1""",
            (story_id,)).fetchone()
    return dict(row) if row else None


def jobs_in_flight() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM jobs WHERE state NOT IN
               ('done','failed','scrapped','blocked') ORDER BY id""").fetchall()
    return [dict(r) for r in rows]


# ---- clips --------------------------------------------------------------------

def record_clip(story_id: int, beat_idx: int, meta: dict):
    with _conn() as con:
        con.execute(
            """INSERT INTO clips_used(story_id, beat_idx, source, video_id,
                   url, author, author_url, width, height, duration, used_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (story_id, beat_idx, meta["source"], str(meta["video_id"]),
             meta.get("page_url") or meta.get("url", ""),
             meta.get("author", ""),
             meta.get("author_url", ""), meta.get("width"),
             meta.get("height"), meta.get("duration"), time.time()))


def clips_for_story(story_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM clips_used WHERE story_id=? ORDER BY beat_idx, id",
            (story_id,)).fetchall()
    return [dict(r) for r in rows]


def clear_story_clips(story_id: int):
    """Re-roll: forget this story's clip picks so new ones can be chosen."""
    with _conn() as con:
        con.execute("DELETE FROM clips_used WHERE story_id=?", (story_id,))


def recent_clip_ids(days: int = 14) -> set[str]:
    cutoff = time.time() - days * 86400
    with _conn() as con:
        rows = con.execute(
            "SELECT source, video_id FROM clips_used WHERE used_at > ?",
            (cutoff,)).fetchall()
    return {f"{r['source']}:{r['video_id']}" for r in rows}


# ---- keyword cache --------------------------------------------------------------

def cache_get(keyword: str, source: str, max_age: float = 86400):
    with _conn() as con:
        row = con.execute(
            "SELECT payload, fetched_at FROM keyword_cache "
            "WHERE keyword=? AND source=?", (keyword.lower(), source)).fetchone()
    if row and time.time() - row["fetched_at"] < max_age:
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
    return None


def cache_put(keyword: str, source: str, payload):
    with _conn() as con:
        con.execute(
            """INSERT INTO keyword_cache(keyword, source, payload, fetched_at)
               VALUES(?,?,?,?)
               ON CONFLICT(keyword, source) DO UPDATE
               SET payload=excluded.payload, fetched_at=excluded.fetched_at""",
            (keyword.lower(), source, json.dumps(payload), time.time()))


# ---- budgets --------------------------------------------------------------------

def bump(key: str, n: int = 1) -> int:
    with _conn() as con:
        con.execute(
            """INSERT INTO budgets(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value = value + excluded.value""",
            (key, n))
        row = con.execute("SELECT value FROM budgets WHERE key=?",
                          (key,)).fetchone()
    return row["value"]


def budget(key: str) -> int:
    with _conn() as con:
        row = con.execute("SELECT value FROM budgets WHERE key=?",
                          (key,)).fetchone()
    return row["value"] if row else 0


def renders_today() -> int:
    return budget("renders:" + time.strftime("%Y-%m-%d"))


def count_render():
    bump("renders:" + time.strftime("%Y-%m-%d"))


# ---- settings / render options (kv table) --------------------------------------------

# Which production stages run. Default: everything on. footage off = a clean
# solid backdrop (no stock footage, no key needed).
RENDER_KEYS = ("footage", "music", "captions")
_RENDER_DEFAULTS = {"footage": True, "music": True, "captions": True}

# Caption look. font=family (or an uploaded .ttf's family); color=#hex;
# size=small/medium/large/xl; anim=sweep/fade/pop/plain; pos=high/mid/low.
# pos = % from the top (5-95); bg = backdrop opacity % (0 = transparent, footage
# off only). Both slider-driven. pos kept back-compat with old high/mid/low.
CAP_KEYS = ("font", "color", "size", "anim", "upper", "pos", "words", "bg")
_CAP_DEFAULTS = {"font": "Liberation Sans", "color": "#f2b64a",
                 "size": "medium", "anim": "sweep", "upper": True,
                 "pos": 55, "words": 2, "bg": 0}
_POS_ENUM = {"high": 30, "mid": 55, "low": 72}


def _merge_cap(base: dict, override) -> dict:
    d = dict(base)
    if isinstance(override, dict):
        for k in CAP_KEYS:
            if k not in override or override[k] is None:
                continue
            v = override[k]
            if k == "upper":
                d[k] = bool(v)
            elif k == "words":
                try:
                    d[k] = min(3, max(1, int(v)))
                except (ValueError, TypeError):
                    pass
            elif k == "pos":
                if isinstance(v, str) and v in _POS_ENUM:
                    v = _POS_ENUM[v]
                try:
                    d[k] = min(95, max(5, int(round(float(v)))))
                except (ValueError, TypeError):
                    pass
            elif k == "bg":
                try:
                    d[k] = min(100, max(0, int(round(float(v)))))
                except (ValueError, TypeError):
                    pass
            elif k == "size":
                # numeric = font px at 1080 wide (slider); legacy enum kept
                if isinstance(v, str) and v in ("small", "medium",
                                                "large", "xl"):
                    d[k] = v
                else:
                    try:
                        d[k] = min(200, max(40, int(round(float(v)))))
                    except (ValueError, TypeError):
                        pass
            else:
                d[k] = v
    return d


def kv_get(key: str, default=None):
    with _conn() as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return default


def kv_set(key: str, value):
    with _conn() as con:
        con.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))


def _merge_bools(base: dict, override) -> dict:
    d = dict(base)
    if isinstance(override, dict):
        d.update({k: bool(override[k]) for k in RENDER_KEYS if k in override})
    return d


def render_defaults() -> dict:
    """Global production defaults (Production tab). These apply to EVERY render;
    a per-story Style override merges on top. Includes stage bools, caption
    `cap` style, plus a global `voice` ("" = per-tone default) and
    `music_track` (None = auto/random per tone)."""
    saved = kv_get("render_defaults", {})
    saved = saved if isinstance(saved, dict) else {}
    d = _merge_bools(_RENDER_DEFAULTS, saved)
    d["cap"] = _merge_cap(_CAP_DEFAULTS, saved.get("cap"))
    d["voice"] = saved.get("voice") or ""              # "" = per-tone default
    d["music_track"] = saved.get("music_track")        # None = auto per tone
    return d


def set_render_defaults(partial: dict) -> dict:
    cur = kv_get("render_defaults", {})
    cur = cur if isinstance(cur, dict) else {}
    for k in RENDER_KEYS:
        if k in (partial or {}):
            cur[k] = bool(partial[k])
    if isinstance((partial or {}).get("cap"), dict):
        cur["cap"] = _merge_cap(cur.get("cap") or _CAP_DEFAULTS, partial["cap"])
    if "voice" in (partial or {}):
        cur["voice"] = partial["voice"] or None
    if "music_track" in (partial or {}):
        cur["music_track"] = partial["music_track"] or None
    kv_set("render_defaults", cur)
    return render_defaults()


def render_opts(story_id: int) -> dict:
    """Effective options for one story: global defaults + its own overrides.
    A per-story override only replaces a key it explicitly sets; anything it
    leaves alone inherits the global default (so setting a global look once
    carries to every story)."""
    d = render_defaults()
    ov = kv_get(f"render_opts:{story_id}", {})
    ov = ov if isinstance(ov, dict) else {}
    for k in RENDER_KEYS:
        if k in ov:
            d[k] = bool(ov[k])
    d["cap"] = _merge_cap(d["cap"], ov.get("cap"))
    if "music_track" in ov:                            # else inherit global
        d["music_track"] = ov["music_track"]
    if "voice" in ov:                                  # else inherit global
        d["voice"] = ov["voice"] or ""
    return d


def set_render_opts(story_id: int, partial: dict) -> dict:
    cur = kv_get(f"render_opts:{story_id}", {})
    cur = cur if isinstance(cur, dict) else {}
    for k in RENDER_KEYS:
        if k in (partial or {}):
            cur[k] = bool(partial[k])
    if isinstance((partial or {}).get("cap"), dict):
        cur["cap"] = _merge_cap(cur.get("cap") or {}, partial["cap"])
    if "music_track" in (partial or {}):
        cur["music_track"] = partial["music_track"] or None
    if "voice" in (partial or {}):
        cur["voice"] = partial["voice"] or None
    kv_set(f"render_opts:{story_id}", cur)
    return render_opts(story_id)


def render_cap() -> int:
    """Max renders/day, or **0 = unlimited** (no cap). A cap guards ElevenLabs
    credits (~1,100 chars/render); user-settable in Settings, env is fallback."""
    v = kv_get("max_renders", None)
    try:
        return max(0, min(9999, int(v))) if v is not None else \
            int(os.environ.get("STORY_MAX_RENDERS_PER_DAY", "5"))
    except (ValueError, TypeError):
        return 5


def set_render_cap(n) -> int:
    try:
        kv_set("max_renders", max(0, min(9999, int(n))))
    except (ValueError, TypeError):
        pass
    return render_cap()


def autogen_on() -> bool:
    """Whether drafts auto-generate through the day. **Default OFF** — you click
    Generate. User-settable in Settings (env STORY_WEB_AUTOGEN can force a
    default if you want it on out of the box)."""
    v = kv_get("autogen", None)
    if v is not None:
        return bool(v)
    return os.environ.get("STORY_WEB_AUTOGEN", "0") == "1"


def set_autogen(on) -> bool:
    kv_set("autogen", bool(on))
    return autogen_on()


def daily_target() -> int:
    """Auto-generated drafts/day, or **0 = unlimited** (keep drafting all day).
    User-settable in Settings; env is the fallback. Drafts are Anthropic-only."""
    v = kv_get("daily_target", None)
    try:
        return max(0, min(9999, int(v))) if v is not None else \
            int(os.environ.get("STORY_DAILY_TARGET", "12"))
    except (ValueError, TypeError):
        return 12


def set_daily_target(n) -> int:
    try:
        kv_set("daily_target", max(0, min(9999, int(n))))
    except (ValueError, TypeError):
        pass
    return daily_target()


def gen_categories() -> list:
    """Allowed categories for auto/on-demand generation ([] = all)."""
    v = kv_get("gen_categories", [])
    return v if isinstance(v, list) else []


def set_gen_categories(cats) -> list:
    kv_set("gen_categories", list(cats or []))
    return gen_categories()
