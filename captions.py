"""
captions.py -- word timings -> burned-in karaoke subtitles (.ass).

Style is user-configurable per story (production-queue "Style" editor) or
globally (Settings). Knobs: font family, highlight colour, size, on-screen
position, UPPERCASE, and the animation:

  sweep  karaoke fill -- words start white and turn the highlight colour as
         they're spoken (the classic Shorts look; the default)
  fade   each 1-3 word group fades in solid in the highlight colour
  pop    each group pops in with a small scale bounce
  plain  each group just appears, solid, no animation

PlayResX/PlayResY are set to the render resolution so font sizing is exact.
An optional fonts dir (uploaded .ttf/.otf) is handed to libass by render.py.
"""

import os

WHITE = "&H00FFFFFF"
BLACK = "&H00000000"
SHADOW = "&HA0000000"

# defaults (overridable via the style dict, then env as a last resort)
FONT = os.environ.get("STORY_CAPTION_FONT", "Liberation Sans")
UPPER = os.environ.get("STORY_CAPTION_UPPER", "1") == "1"
COLOR = os.environ.get("STORY_CAPTION_COLOR", "#f2b64a")

SIZE_MULT = {"small": 0.070, "medium": 0.085, "large": 0.100, "xl": 0.118}
POS_FRAC = {"high": 0.30, "mid": 0.55, "low": 0.72}   # legacy enum
ANIMS = ("sweep", "fade", "pop", "plain")


def _pos_frac(pos) -> float:
    """pos may be a % from top (5-95), or a legacy 'high'/'mid'/'low' string."""
    if isinstance(pos, str) and pos in POS_FRAC:
        return POS_FRAC[pos]
    try:
        return min(0.95, max(0.05, float(pos) / 100.0))
    except (ValueError, TypeError):
        return POS_FRAC["mid"]


def _size_mult(size) -> float:
    """size may be a legacy enum, or a number = font px at 1080 wide (slider)."""
    if isinstance(size, str) and size in SIZE_MULT:
        return SIZE_MULT[size]
    try:
        return min(0.20, max(0.035, float(size) / 1080.0))
    except (ValueError, TypeError):
        return SIZE_MULT["medium"]

MAX_WORDS = 3            # per caption group
MAX_CHARS = 16           # soft cap; long words force smaller groups
GAP_BREAK = 0.6          # silence that forces a new group (s)
HOLD = 0.12              # linger after the last word of a group (s)


def hex_to_ass(hex_color: str, default: str = "&H004AB6F2") -> str:
    """#RRGGBB -> ASS &HAABBGGRR (opaque). Falls back to gold on bad input."""
    try:
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"&H00{b:02X}{g:02X}{r:02X}"
    except (ValueError, AttributeError, IndexError, TypeError):
        return default


def _style(style: dict | None) -> dict:
    style = style or {}
    anim = style.get("anim", "sweep")
    try:
        words = int(style.get("words", 2))
    except (ValueError, TypeError):
        words = 2
    return {
        "font": style.get("font") or FONT,
        "color": hex_to_ass(style.get("color") or COLOR),
        "mult": _size_mult(style.get("size", "medium")),
        "posf": _pos_frac(style.get("pos", 55)),
        "anim": anim if anim in ANIMS else "sweep",
        "upper": style.get("upper", UPPER),
        "words": min(3, max(1, words)),
    }


def _esc(t: str) -> str:
    return t.replace("{", "(").replace("}", ")").replace("\\", "/")


def _ts(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def group_words(words: list[dict], max_words: int = MAX_WORDS,
                max_chars: int | None = None) -> list[list[dict]]:
    """[{'w','start','end'}] -> caption groups of up to max_words words."""
    if max_chars is None:                        # scale the char cap to size
        max_chars = max(MAX_CHARS, max_words * 12)
    groups, cur, chars = [], [], 0
    for w in words:
        text = w["w"].strip()
        if not text:
            continue
        breaking = False
        if cur:
            if len(cur) >= max_words:
                breaking = True
            elif chars + 1 + len(text) > max_chars:
                breaking = True
            elif w["start"] - cur[-1]["end"] > GAP_BREAK:
                breaking = True
            elif cur[-1]["w"].rstrip()[-1:] in ".!?":
                breaking = True
        if breaking:
            groups.append(cur)
            cur, chars = [], 0
        cur.append(w)
        chars += len(text) + (1 if chars else 0)
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[dict], out_path: str, style: dict | None = None,
              play_w: int = 1080, play_h: int = 1920) -> str:
    """Write the subtitle file in the requested style; returns out_path."""
    st = _style(style)
    font_size = int(play_w * st["mult"])
    pos_x = play_w // 2
    pos_y = int(play_h * st["posf"])
    # sweep needs Secondary=white (pre-fill) + Primary=colour (post-fill);
    # solid modes just use the colour as Primary.
    primary = st["color"]
    secondary = WHITE if st["anim"] == "sweep" else st["color"]

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{st['font']},{font_size},{primary},{secondary},{BLACK},{SHADOW},-1,0,0,0,100,100,1,0,1,6,2,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    groups = group_words(words, max_words=st["words"])
    for gi, g in enumerate(groups):
        start = g[0]["start"]
        end = g[-1]["end"] + HOLD
        if gi + 1 < len(groups):
            end = min(end, groups[gi + 1][0]["start"])
        if end <= start:
            end = start + 0.05
        pos_tag = f"\\an5\\pos({pos_x},{pos_y})"

        if st["anim"] == "sweep":
            parts = []
            for wi, w in enumerate(g):
                if wi + 1 < len(g):
                    k_cs = round((g[wi + 1]["start"] - w["start"]) * 100)
                else:
                    k_cs = round((w["end"] - w["start"]) * 100)
                text = _esc(w["w"].upper() if st["upper"] else w["w"])
                sep = " " if wi + 1 < len(g) else ""
                parts.append(f"{{\\k{max(1, k_cs)}}}{text}{sep}")
            body = f"{{{pos_tag}}}" + "".join(parts)
        else:
            words_txt = " ".join(_esc(w["w"].upper() if st["upper"] else w["w"])
                                 for w in g)
            if st["anim"] == "fade":
                eff = "\\fad(160,60)"
            elif st["anim"] == "pop":
                eff = ("\\fad(50,40)\\fscx72\\fscy72"
                       "\\t(0,150,\\fscx106\\fscy106)"
                       "\\t(150,230,\\fscx100\\fscy100)")
            else:                                    # plain
                eff = ""
            body = f"{{{pos_tag}{eff}}}" + words_txt

        lines.append(
            f"Dialogue: 0,{_ts(start)},{_ts(end)},Karaoke,,0,0,0,,{body}")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    return out_path
