"""
stock.py -- find and download b-roll for one beat. Source order: Pexels ->
LLM-rewritten query -> Pixabay -> Pexels *photo* (Ken Burns pan in assemble).
Sources whose API key is unset are skipped (at least one of Pexels/Pixabay
must be configured). All blocking (callers use asyncio.to_thread).

Selection rules (from the recon):
  * skip Pexels `video_files` entries with quality 'hls' (playlist URLs)
  * pick renditions by width/height, never by the quality string
  * prefer portrait; landscape allowed only when tall enough to center-crop
    without ugly upscaling (>=1080p)
  * prefer clips at least as long as the beat needs (looping is the backstop)
  * never reuse a clip inside one story, nor any clip used in the last 14
    days (used_ids comes from the caller)
  * keyword -> normalized results cached 24h in sqlite (also satisfies
    Pixabay's mandatory caching rule); rate budget tracked per hour/month

License notes: Pexels License and Pixabay Content License both allow
commercial / monetized use without attribution; per-clip credits still go in
the render manifest + credits.txt.
"""

import os
import time

import httpx

import store

PEXELS_VIDEO_URL = "https://api.pexels.com/v1/videos/search"
PEXELS_PHOTO_URL = "https://api.pexels.com/v1/search"
PIXABAY_URL = "https://pixabay.com/api/videos/"

PEXELS_HOURLY_BUDGET = int(os.environ.get("STORY_PEXELS_HOURLY", "190"))
TARGET_W, TARGET_H = 1080, 1920


class StockError(RuntimeError):
    pass


class RateLimited(StockError):
    pass


# ---- search ---------------------------------------------------------------------

def _pexels_key() -> str:
    key = os.environ.get("PEXELS_API_KEY", "")
    if not key:
        raise StockError("PEXELS_API_KEY is not set")
    return key


def _budget_ok():
    hour_key = "pexels_hr:" + time.strftime("%Y-%m-%d-%H")
    if store.budget(hour_key) >= PEXELS_HOURLY_BUDGET:
        raise RateLimited("Pexels hourly request budget exhausted")


def _pexels_get(url: str, params: dict) -> dict:
    _budget_ok()
    try:
        r = httpx.get(url, params=params,
                      headers={"Authorization": _pexels_key()}, timeout=60)
    except httpx.HTTPError as e:
        # network hiccup / timeout -- surface as StockError so run_job routes
        # it to the 'blocked' (retryable) path, not an opaque 'failed'.
        raise StockError(f"Pexels request failed: {e}") from e
    store.bump("pexels_hr:" + time.strftime("%Y-%m-%d-%H"))
    store.bump("pexels_mo:" + time.strftime("%Y-%m"))
    if r.status_code == 429:
        raise RateLimited("Pexels returned 429")
    if r.status_code >= 400:
        raise StockError(f"Pexels returned {r.status_code}")
    return r.json()


def search_pexels_videos(keyword: str, portrait: bool = True) -> list[dict]:
    """Normalized candidates, cached 24h."""
    cache_key = f"v:{'p' if portrait else 'l'}:{keyword}"
    hit = store.cache_get(cache_key, "pexels")
    if hit is not None:
        return hit
    params = {"query": keyword, "per_page": 25, "size": "medium"}
    if portrait:
        params["orientation"] = "portrait"
    data = _pexels_get(PEXELS_VIDEO_URL, params)
    out = []
    for v in data.get("videos", []):
        files = [{"url": f["link"], "w": f.get("width") or 0,
                  "h": f.get("height") or 0}
                 for f in v.get("video_files", [])
                 if (f.get("quality") or "").lower() != "hls"
                 and "mp4" in (f.get("file_type") or "video/mp4")]
        if not files:
            continue
        user = v.get("user") or {}
        out.append({
            "source": "pexels", "video_id": str(v["id"]),
            "page_url": v.get("url", ""),
            "author": user.get("name", ""), "author_url": user.get("url", ""),
            "duration": float(v.get("duration") or 0),
            "width": v.get("width") or 0, "height": v.get("height") or 0,
            "files": files,
        })
    store.cache_put(cache_key, "pexels", out)
    return out


def search_pixabay_videos(keyword: str) -> list[dict]:
    key = os.environ.get("PIXABAY_API_KEY", "")
    if not key:
        return []
    hit = store.cache_get(f"v:{keyword}", "pixabay")
    if hit is not None:
        return hit
    r = httpx.get(PIXABAY_URL, params={
        "key": key, "q": keyword[:100], "orientation": "vertical",
        "per_page": 25, "safesearch": "true"}, timeout=60)
    if r.status_code == 429:
        raise RateLimited("Pixabay returned 429")
    r.raise_for_status()
    out = []
    for v in r.json().get("hits", []):
        files = [{"url": rend["url"], "w": rend.get("width") or 0,
                  "h": rend.get("height") or 0}
                 for name, rend in (v.get("videos") or {}).items()
                 if rend.get("url")]
        if not files:
            continue
        big = max(files, key=lambda f: f["h"])
        out.append({
            "source": "pixabay", "video_id": str(v["id"]),
            "page_url": v.get("pageURL", ""),
            "author": v.get("user", ""),
            "author_url": f"https://pixabay.com/users/{v.get('user','')}-"
                          f"{v.get('user_id','')}/",
            "duration": float(v.get("duration") or 0),
            "width": big["w"], "height": big["h"],
            "files": files,
        })
    store.cache_put(f"v:{keyword}", "pixabay", out)
    return out


def search_pexels_photo(keyword: str) -> dict | None:
    hit = store.cache_get(f"p:{keyword}", "pexels")
    if hit is not None:
        return hit or None
    data = _pexels_get(PEXELS_PHOTO_URL, {
        "query": keyword, "per_page": 5, "orientation": "portrait"})
    photos = data.get("photos", [])
    out = None
    if photos:
        p = photos[0]
        out = {"source": "photo", "video_id": f"photo-{p['id']}",
               "page_url": p.get("url", ""),
               "author": (p.get("photographer") or ""),
               "author_url": p.get("photographer_url", ""),
               "duration": 0.0,
               "width": p.get("width") or 0, "height": p.get("height") or 0,
               "files": [{"url": p["src"]["large2x"], "w": 0, "h": 0}]}
    store.cache_put(f"p:{keyword}", "pexels", out or {})
    return out


# ---- pick + download ------------------------------------------------------------

def _fit_score(c: dict, need: float) -> float:
    """Higher = better. Orientation/resolution tiers, then duration fit."""
    w, h = c["width"], c["height"]
    portrait = h > w
    if portrait and h >= 1920:
        tier = 5
    elif portrait and h >= 1280:
        tier = 4
    elif not portrait and h >= 2160:
        tier = 3            # 4K landscape center-crops cleanly
    elif not portrait and h >= 1080:
        tier = 2
    else:
        tier = 0            # would upscale badly
    dur = c["duration"]
    if dur >= need + 0.4:
        dfit = 2 if dur <= need + 12 else 1.5   # long clips trim fine
    elif dur >= need * 0.6:
        dfit = 0.5                              # short: will loop
    else:
        dfit = 0
    return tier * 10 + dfit


def _pick_file(c: dict) -> dict:
    """Smallest rendition that still fills 1080x1920 after crop; else biggest."""
    files = sorted(c["files"], key=lambda f: f["h"] or 0)
    for f in files:
        w, h = f["w"], f["h"]
        if not w or not h:
            continue
        portrait = h > w
        if (portrait and h >= TARGET_H) or (not portrait and h >= 1080):
            return f
    return files[-1]


def _download(url: str, dest: str):
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
        r.raise_for_status()
        size = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                size += len(chunk)
                if size > 400 << 20:
                    raise StockError(f"clip download exceeded 400MB: {url}")
                f.write(chunk)
    if os.path.getsize(dest) == 0:
        raise StockError(f"empty download: {url}")


def _try_candidates(cands: list[dict], need: float, used_ids: set[str],
                    dest_base: str) -> dict | None:
    usable = [c for c in cands
              if f"{c['source']}:{c['video_id']}" not in used_ids
              and _fit_score(c, need) >= 20]     # tier >= 2 only
    usable.sort(key=lambda c: _fit_score(c, need), reverse=True)
    for c in usable[:3]:                         # bad downloads: try next
        f = _pick_file(c)
        dest = f"{dest_base}_{c['source']}_{c['video_id']}.mp4"
        try:
            _download(f["url"], dest)
        except (httpx.HTTPError, StockError):
            continue
        return {**c, "file": dest, "picked_w": f["w"], "picked_h": f["h"]}
    return None


def find_broll(keywords: list[str], visual: str, need_duration: float,
               used_ids: set[str], dest_base: str,
               rewrite=None) -> dict | None:
    """The full fallback chain for one beat. Returns clip meta with a local
    'file' path (mp4, or jpg when source == 'photo'), or None if truly dry.
    `rewrite`: optional callable(keyword, visual) -> fresh query (llm helper);
    injected so stock.py stays importable without an Anthropic key."""
    # a source with no key is skipped, not fatal -- Pixabay-only (or
    # Pexels-only) setups still render. Neither key set is a config error.
    have_pexels = bool(os.environ.get("PEXELS_API_KEY", ""))
    if not have_pexels and not os.environ.get("PIXABAY_API_KEY", ""):
        raise StockError("no footage source configured: set PEXELS_API_KEY "
                         "and/or PIXABAY_API_KEY")
    tried = []
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        tried.append(kw)
        if not have_pexels:
            continue
        got = _try_candidates(search_pexels_videos(kw, portrait=True),
                              need_duration, used_ids, dest_base)
        if got:
            return got
        got = _try_candidates(search_pexels_videos(kw, portrait=False),
                              need_duration, used_ids, dest_base)
        if got:
            return got
    if rewrite and tried:
        try:
            fresh = rewrite(tried[0], visual)
        except Exception:
            fresh = None
        if fresh and fresh.lower() not in [t.lower() for t in tried]:
            if have_pexels:
                for portrait in (True, False):
                    got = _try_candidates(
                        search_pexels_videos(fresh, portrait=portrait),
                        need_duration, used_ids, dest_base)
                    if got:
                        return got
            tried.append(fresh)
    for kw in tried:
        got = _try_candidates(search_pixabay_videos(kw),
                              need_duration, used_ids, dest_base)
        if got:
            return got
    # last resort: a still photo, animated with a slow zoom in assemble.py
    for kw in (tried if have_pexels else []):
        photo = search_pexels_photo(kw)
        if photo and f"{photo['source']}:{photo['video_id']}" not in used_ids:
            dest = f"{dest_base}_photo.jpg"
            try:
                _download(photo["files"][0]["url"], dest)
            except (httpx.HTTPError, StockError):
                continue
            return {**photo, "file": dest, "picked_w": 0, "picked_h": 0}
    return None
