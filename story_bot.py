"""
story_bot.py -- SCOUT, the story-pipeline gateway bot (single daemon: Discord
UI + daily generation schedule + the render worker).

Flow: /generate (or the daily loop) researches + judges story drafts and posts
cards to #story-feed. Approve queues a render; the worker turns the story into
a vertical Short (voice -> footage -> captions -> assembly) and delivers a
preview to #story-queue with Ship / Re-roll / Re-voice / Scrap buttons. Nothing
ever auto-approves -- tiers (RECOMMENDED/REVIEW/PASS) are advisory only.

Design: no privileged intents, guild-scoped slash
commands synced in on_ready, buttons handled globally in on_interaction by
custom_id prefix (survive restarts), blocking work via asyncio.to_thread,
JSON-free state (sqlite via store.py). Run under systemd: storybot.service.
"""

import asyncio
import datetime as dt
import os
import traceback
import urllib.parse

import discord
from discord import app_commands
from discord.ext import tasks

import criteria
import llm
import render
import store
import web as webmod

TOKEN = os.environ.get("STORY_DISCORD_TOKEN", "")
GUILD_ID = int(os.environ.get("STORY_GUILD_ID", "0"))
GUILD = discord.Object(id=GUILD_ID)
CATEGORY_NAME = os.environ.get("STORY_CATEGORY_NAME", "Story Pipeline")
FEED_NAME = os.environ.get("STORY_FEED_CHANNEL", "story-feed")
QUEUE_NAME = os.environ.get("STORY_QUEUE_CHANNEL", "story-queue")
BOT_NAME = os.environ.get("STORY_BOT_NAME", "SCOUT")
WORKSPACE = os.environ.get("STORY_WORKSPACE", "OBSCURA")
MAX_RENDERS_PER_DAY = int(os.environ.get("STORY_MAX_RENDERS_PER_DAY", "5"))
DAILY_ENABLED = os.environ.get("STORY_DAILY", "1") == "1"
# "A lot a day": spread STORY_DAILY_TARGET drafts across the day in
# STORY_GEN_BATCH-sized runs, rotating categories, capped per-day. Same envs the
# standalone web console uses so both paths generate the same volume.
DAILY_TARGET = int(os.environ.get("STORY_DAILY_TARGET", "12"))
GEN_BATCH = max(1, int(os.environ.get("STORY_GEN_BATCH", "3")))
_DAILY_RUNS = max(1, -(-DAILY_TARGET // GEN_BATCH))          # ceil(target/batch)
DAILY_TIMES = [dt.time(hour=int(i * 24 / _DAILY_RUNS), tzinfo=dt.timezone.utc)
               for i in range(_DAILY_RUNS)]
GOLD = 0xF2B64A

BASE = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT = os.path.join(BASE, "logs", "heartbeat")

intents = discord.Intents.none()
intents.guilds = True
intents.dm_messages = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

_bg_tasks: set = set()
_feed_chan: discord.TextChannel | None = None
_queue_chan: discord.TextChannel | None = None


def _spawn(coro, what):
    """Fire-and-forget with a strong reference + exception logging."""
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)

    def _done(t):
        _bg_tasks.discard(t)
        if not t.cancelled() and t.exception():
            print(f"[bg] {what} failed: {t.exception()!r}", flush=True)
    task.add_done_callback(_done)


# ---- embeds / cards -----------------------------------------------------------------

STATUS_LABEL = {"new": "NEW", "approved": "GREENLIT", "rejected": "PASSED",
                "shipped": "SHIPPED"}


def story_embed(s: dict) -> discord.Embed:
    cat = criteria.CATEGORIES.get(s["category"], {})
    tone = criteria.TONES.get(s["tone"], criteria.TONES["documentary"])
    has_source = bool(s.get("sources"))
    tier = criteria.tier(s["scores"], has_source)
    head = (f"{criteria.star_line(s['scores'])} · **{tier}** · "
            f"{STATUS_LABEL.get(s['status'], s['status'].upper())}")
    desc = [head, "", f"*“{s['hook']}”*", "", s["summary"]]
    e = discord.Embed(title=f"{cat.get('emoji', '')} {s['title']}",
                      description="\n".join(desc),
                      color=cat.get("color", GOLD))
    if s.get("yt_titles"):
        e.add_field(name="Suggested titles",
                    value="\n".join(f"▸ {t}" for t in s["yt_titles"][:3]),
                    inline=False)
    if has_source:
        src = s["sources"][0]
        more = f" (+{len(s['sources']) - 1} more)" if len(s["sources"]) > 1 \
            else ""
        e.add_field(name="🏷️ True story",
                    value=f"[{urllib.parse.urlparse(src).netloc}](<{src}>)"
                          + more, inline=True)
    e.add_field(name="Tone", value=tone["label"], inline=True)
    if s.get("beats"):
        e.add_field(name="Script", value=f"{len(s['beats'])} shots ready",
                    inline=True)
    e.set_footer(text=f"{BOT_NAME} · {WORKSPACE} story pipeline · "
                      f"#{s['id']} · {cat.get('label', s['category'])}")
    return e


def feed_view(s: dict) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    approved = s["status"] == "approved"
    v.add_item(discord.ui.Button(
        label="Greenlit" if approved else "Approve", emoji="✅",
        style=discord.ButtonStyle.success if not approved
        else discord.ButtonStyle.secondary,
        custom_id=f"app:{s['id']}", disabled=approved))
    v.add_item(discord.ui.Button(label="Pass", emoji="✖️",
                                 style=discord.ButtonStyle.secondary,
                                 custom_id=f"pass:{s['id']}"))
    v.add_item(discord.ui.Button(label="Regenerate", emoji="🔄",
                                 style=discord.ButtonStyle.secondary,
                                 custom_id=f"regen:{s['id']}"))
    v.add_item(discord.ui.Button(label="Details", emoji="🔍",
                                 style=discord.ButtonStyle.secondary,
                                 custom_id=f"det:{s['id']}"))
    sel = discord.ui.Select(custom_id=f"tone:{s['id']}",
                            placeholder="Tone", min_values=1, max_values=1)
    for key, t in criteria.TONES.items():
        sel.add_option(label=t["label"], value=key,
                       default=(key == s["tone"]))
    v.add_item(sel)
    return v


def queue_view(s: dict) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Ship it", emoji="🚢",
                                 style=discord.ButtonStyle.success,
                                 custom_id=f"ship:{s['id']}"))
    v.add_item(discord.ui.Button(label="Re-roll clips", emoji="🎲",
                                 style=discord.ButtonStyle.secondary,
                                 custom_id=f"reroll:{s['id']}"))
    v.add_item(discord.ui.Button(label="Re-voice", emoji="🎙️",
                                 style=discord.ButtonStyle.secondary,
                                 custom_id=f"revoice:{s['id']}"))
    v.add_item(discord.ui.Button(label="Scrap", emoji="🗑️",
                                 style=discord.ButtonStyle.danger,
                                 custom_id=f"scrap:{s['id']}"))
    return v


def _bar(score: int) -> str:
    return "█" * score + "░" * (5 - score)


def details_embed(s: dict) -> discord.Embed:
    rows = [f"`{_bar(sc)}` **{sc}** {c['label']} ×{c['weight']}"
            for c, sc in zip(criteria.CRITERIA, s["scores"])]
    desc = ("**Score breakdown**\n" + "\n".join(rows)
            + f"\n\n**Tone** — {(s.get('tone_reason') or '—')[:300]}"
            f"\n**Judge** — {(s.get('judge_reason') or '—')[:300]}")[:4000]
    e = discord.Embed(
        title=f"#{s['id']} · {s['title']}"[:256],
        description=desc,
        color=criteria.CATEGORIES.get(s["category"], {}).get("color", GOLD))
    total = len(e.title or "") + len(desc)       # stay under the 6000 cap
    beats = s.get("beats", [])
    for i, b in enumerate(beats[:8]):
        links = " · ".join(
            f"[{k}](<{envato_url(k)}>)" for k in b["k"])
        val = f"*{b['n'][:150]}*\n{b['v'][:100]}\n{links}"[:1024]
        if total + len(val) + 20 > 5600:
            e.add_field(name="…",
                        value=f"+{len(beats) - i} more shots — `/brief "
                              f"{s['id']}` for the full list", inline=False)
            break
        e.add_field(name=f"Shot {i + 1:02d}", value=val, inline=False)
        total += len(val) + 20
    if not beats:
        e.add_field(name="Script",
                    value="No shot list yet — written on approval.",
                    inline=False)
    return e


# brief_text / envato_url live in web.py (shared with the web console)
envato_url = webmod.envato_url
brief_text = webmod.brief_text


def rubric_embed() -> discord.Embed:
    e = discord.Embed(
        title=f"How {BOT_NAME} scores every story",
        description=(
            "Each draft is rated 1–5★ on eight weighted dimensions — hooks "
            "and virality count most because this channel runs on Shorts.\n\n"
            f"**GREENLIGHT RULE** — a story is tagged **RECOMMENDED** only "
            f"when its weighted score is **{criteria.APPROVAL_THRESHOLD:.1f}★ "
            f"or higher**, no dimension falls below 3★, and it carries a "
            f"source. 3.0★+ is **REVIEW**; below is **PASS**. Nothing "
            f"auto-approves — you greenlight by hand."),
        color=GOLD)
    for c in criteria.CRITERIA:
        e.add_field(name=f"{c['label']} ×{c['weight']}",
                    value=f"{c['desc']}\n1★ {c['one']}\n5★ {c['five']}"[:1024],
                    inline=False)
    return e


# ---- generation (the batch itself lives in llm.gen_batch, shared with web) -----------


async def post_drafts(stories: list[dict], divider: bool = True):
    if not stories:
        return
    if _feed_chan is None:                   # e.g. command raced startup
        await _ensure_channels()
    if divider:
        await _feed_chan.send(
            f"—— **TODAY · {BOT_NAME} DROPPED {len(stories)} NEW "
            f"DRAFT{'S' if len(stories) != 1 else ''}** ——")
    for s in stories:
        msg = await _feed_chan.send(embed=story_embed(s), view=feed_view(s))
        await asyncio.to_thread(store.update_story, s["id"],
                                feed_msg_id=msg.id)


async def generate_and_post(category_key: str | None, n: int) -> str:
    try:
        stories = await asyncio.to_thread(llm.gen_batch, category_key, n)
    except Exception as e:
        print(f"[gen] batch failed: {e!r}", flush=True)
        traceback.print_exc()
        return f"generation failed: `{e}`"
    if not stories:
        return "the generator came back empty — try again or another category"
    try:
        await post_drafts(stories)
    except Exception as e:
        print(f"[gen] posting failed: {e!r}", flush=True)
        return (f"generated {len(stories)} stories but couldn't post them: "
                f"`{e}` — they're in the DB, `/queue` after a restart")
    return (f"posted {len(stories)} new draft"
            f"{'s' if len(stories) != 1 else ''} to #{FEED_NAME}")


# ---- render worker ------------------------------------------------------------------

STAGE_LINE = {"tts": "🎙️ recording narration",
              "clips": "🎞️ hunting footage",
              "assembling": "🎬 cutting the video",
              "preview": "📦 packing the preview"}


def _beat():
    os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
    with open(HEARTBEAT, "w") as f:
        f.write(dt.datetime.now().isoformat())


async def _worker():
    """Polls the DB for queued jobs so ANY process (this bot, the web
    console, a CLI) can enqueue renders just by inserting a row.

    The entire loop body is guarded: nothing respawns this task, so a single
    uncaught transient error (a locked DB past the busy timeout, a disk-full
    write) would otherwise end it silently and leave every job stuck 'queued'
    until a full restart. The heartbeat write also goes through a thread so
    slow storage can't stall the Discord gateway heartbeat on the event loop."""
    await client.wait_until_ready()
    print("[worker] render worker up (db-polling)", flush=True)
    while True:
        job = None
        try:
            try:
                await asyncio.to_thread(_beat)
            except OSError:
                pass
            job = await asyncio.to_thread(store.next_queued_job)
            if not job:
                await asyncio.sleep(3)
                continue
            await _run_one(job["id"])
        except Exception as e:
            jid = job["id"] if job else "?"
            print(f"[worker] iteration for job {jid} errored: {e!r}", flush=True)
            traceback.print_exc()
            if job:
                try:
                    await asyncio.to_thread(store.update_job, job["id"],
                                            state="failed", error=repr(e)[:500])
                except Exception as e2:
                    print(f"[worker] couldn't mark job {job['id']} failed: "
                          f"{e2!r}", flush=True)
            await asyncio.sleep(3)          # never spin on a persistent error


async def _run_one(job_id: int):
    job = await asyncio.to_thread(store.get_job, job_id)
    if not job or job["state"] in ("done", "failed", "scrapped"):
        return
    story = await asyncio.to_thread(store.get_story, job["story_id"])
    if not story or story["status"] != "approved":
        await asyncio.to_thread(store.update_job, job_id, state="scrapped",
                                error="story no longer approved")
        return
    # The cap only gates NEW renders. A job whose master already exists is a
    # resume/re-deliver (e.g. a restart between 'done' and delivery), and the
    # render that filled the cap was its own -- don't block it on itself.
    master = os.path.join(render.RENDERS, str(story["id"]), "master.mp4")
    master_exists = await asyncio.to_thread(os.path.exists, master)
    cap = await asyncio.to_thread(store.render_cap)      # 0 = unlimited
    if not master_exists and cap and \
            await asyncio.to_thread(store.renders_today) >= cap:
        await asyncio.to_thread(store.update_job, job_id, state="blocked",
                                error="daily render cap reached")
        if _queue_chan:
            await _queue_chan.send(
                f"⏸️ #{story['id']} **{story['title']}** — daily render cap "
                f"({cap}) reached; raise it in the web Settings or "
                f"`/render {story['id']}` tomorrow.")
        return

    status_msg = None
    if _queue_chan:
        status_msg = await _queue_chan.send(
            f"🎬 **Rendering #{story['id']} · {story['title']}** — starting…")
        await asyncio.to_thread(store.update_job, job_id,
                                status_msg_id=status_msg.id)

    loop = asyncio.get_running_loop()

    def notify(stage):
        store.update_job(job_id, state=stage)   # sync sqlite, worker thread
        if status_msg:
            line = STAGE_LINE.get(stage, stage)
            asyncio.run_coroutine_threadsafe(
                status_msg.edit(content=f"🎬 **Rendering #{story['id']} · "
                                        f"{story['title']}** — {line}…"),
                loop)

    guild = client.get_guild(GUILD_ID)
    limit = guild.filesize_limit if guild else 10 * 1024 * 1024

    try:
        result = await asyncio.to_thread(render.run_job, job, story, notify,
                                         limit)
    except render.BlockedError as e:
        await asyncio.to_thread(store.update_job, job_id, state="blocked",
                                error=str(e)[:500])
        if status_msg:
            await status_msg.edit(
                content=f"🚧 **#{story['id']} · {story['title']}** blocked: "
                        f"{e}\nFix the issue (keys/keywords) and press "
                        f"Re-roll or `/render {story['id']}`.")
        return
    except Exception as e:
        await asyncio.to_thread(store.update_job, job_id, state="failed",
                                error=repr(e)[:500])
        if status_msg:
            short = str(e).splitlines()[-1][:300] if str(e) else repr(e)[:300]
            await status_msg.edit(
                content=f"💥 **#{story['id']} · {story['title']}** render "
                        f"failed: `{short}`")
        return

    # Re-check status: the story may have been scrapped/passed/shipped while
    # the render ran. If so, the render is complete (mark 'done') but we don't
    # post a queue card for a story that's no longer approved.
    story = await asyncio.to_thread(store.get_story, story["id"]) or story
    if story["status"] != "approved":
        await asyncio.to_thread(store.update_job, job_id, state="done")
        return
    # Deliver BEFORE marking 'done'. If the process dies here, the job stays
    # in-flight and on_ready re-queues it -> run_job sees the master and jumps
    # straight to re-delivery, so a finished render is never silently lost.
    # A Discord failure during delivery is logged, not propagated -- otherwise
    # the worker's catch-all would flip a successful render to 'failed'.
    try:
        await _deliver(story, result, status_msg)
    except Exception as e:
        # the render itself succeeded (master on disk) -- a delivery failure
        # must not flip the job to 'failed'. Log and mark done; `/render`
        # re-delivers from the existing master.
        print(f"[worker] delivery failed for #{story['id']}: {e!r}", flush=True)
    await asyncio.to_thread(store.update_job, job_id, state="done")


async def _deliver(story: dict, result: dict, status_msg):
    story = await asyncio.to_thread(store.get_story, story["id"]) or story
    mins, secs = divmod(int(result["duration"]), 60)
    yt = (story.get("yt_titles") or [story["title"]])[0]
    head = (f"✅ **#{story['id']} · {story['title']}** — {mins}:{secs:02d} · "
            f"`{yt}`\nmaster: `{result['master']}`")
    if _queue_chan is None:
        return
    if result.get("preview"):
        msg = await _queue_chan.send(
            head, file=discord.File(result["preview"]),
            view=queue_view(story))
    else:
        msg = await _queue_chan.send(
            head + "\n(no preview fit under the upload limit — play the "
            "master from disk)", view=queue_view(story))
    await asyncio.to_thread(store.update_story, story["id"],
                            queue_msg_id=msg.id)
    if status_msg:
        try:
            await status_msg.delete()
        except discord.HTTPException:
            pass


async def _queue_render(story_id: int, keep_voice: bool = False) -> str:
    story = await asyncio.to_thread(store.get_story, story_id)
    if not story:
        return f"no story #{story_id}"
    if story["status"] != "approved":
        return f"#{story_id} isn't greenlit"
    active = await asyncio.to_thread(store.active_job_for_story, story_id)
    if active and active["state"] != "blocked":
        return f"#{story_id} already has a render in flight"
    if active:                                # blocked: retire it, start fresh
        await asyncio.to_thread(store.update_job, active["id"],
                                state="scrapped", error="superseded")
    if not story.get("beats"):
        import llm
        try:
            shots = await asyncio.to_thread(llm.write_shots, story)
        except Exception as e:
            return f"shot writer failed: `{e}`"
        await asyncio.to_thread(store.update_story, story_id,
                                beats=shots["beats"],
                                yt_titles=shots["yt_titles"])
    await asyncio.to_thread(store.create_job, story_id, keep_voice)
    return f"render queued for #{story_id}"


# ---- interactions -------------------------------------------------------------------

async def _refresh_card(story_id: int, message):
    s = await asyncio.to_thread(store.get_story, story_id)
    if s and message:
        try:
            await message.edit(embed=story_embed(s), view=feed_view(s))
        except discord.HTTPException as e:
            print(f"[card] refresh failed for #{story_id}: {e}", flush=True)


@client.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if ":" not in custom_id:
        return
    action, _, raw_id = custom_id.partition(":")
    try:
        sid = int(raw_id)
    except ValueError:
        return

    if action == "app":
        await interaction.response.defer(ephemeral=True, thinking=True)
        await asyncio.to_thread(store.update_story, sid, status="approved")
        await _refresh_card(sid, interaction.message)
        reply = await _queue_render(sid)
        await interaction.followup.send(f"✅ Greenlit — {reply}",
                                        ephemeral=True)

    elif action == "pass":
        await asyncio.to_thread(store.update_story, sid, status="rejected")
        await _refresh_card(sid, interaction.message)
        await interaction.response.send_message(
            "✖️ Passed — removed from the queue", ephemeral=True)

    elif action == "regen":
        await interaction.response.defer(ephemeral=True, thinking=True)
        s = await asyncio.to_thread(store.get_story, sid)
        if not s:
            await interaction.followup.send("story vanished", ephemeral=True)
            return
        await asyncio.to_thread(store.update_story, sid, status="rejected")
        try:
            fresh = await asyncio.to_thread(llm.gen_batch, s["category"], 1)
        except Exception as e:
            await interaction.followup.send(f"regen failed: `{e}`",
                                            ephemeral=True)
            return
        if not fresh:
            await interaction.followup.send("no new angle found",
                                            ephemeral=True)
            return
        ns = fresh[0]
        try:
            await interaction.message.edit(embed=story_embed(ns),
                                           view=feed_view(ns))
            # the message now shows the NEW story -- detach the old (rejected)
            # row from it, or a later web action on the old story would fetch
            # this shared message and overwrite the new card.
            await asyncio.to_thread(store.update_story, sid, feed_msg_id=None)
            await asyncio.to_thread(store.update_story, ns["id"],
                                    feed_msg_id=interaction.message.id)
        except discord.HTTPException:
            await post_drafts([ns], divider=False)
        await interaction.followup.send(
            f"🔄 {BOT_NAME} drafted a new angle: **{ns['title']}**",
            ephemeral=True)

    elif action == "det":
        s = await asyncio.to_thread(store.get_story, sid)
        if not s:
            await interaction.response.send_message("story vanished",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(embed=details_embed(s),
                                                ephemeral=True)

    elif action == "tone":
        values = (interaction.data or {}).get("values") or []
        if values and values[0] in criteria.TONES:
            await asyncio.to_thread(store.update_story, sid, tone=values[0])
            await _refresh_card(sid, interaction.message)
            await interaction.response.send_message(
                f"tone → {criteria.TONES[values[0]]['label']}",
                ephemeral=True)

    elif action == "ship":
        s = await asyncio.to_thread(store.get_story, sid)
        if not s:
            return
        # Guard against a stale card: don't ship a story whose master was
        # deleted by a re-roll, or one with a render still in flight.
        active = await asyncio.to_thread(store.active_job_for_story, sid)
        if active and active["state"] != "blocked":
            await interaction.response.send_message(
                f"#{sid} has a render in flight — let it finish before "
                "shipping.", ephemeral=True)
            return
        master = os.path.join(render.RENDERS, str(sid), "master.mp4")
        if not await asyncio.to_thread(os.path.exists, master):
            await interaction.response.send_message(
                f"#{sid} has no finished master to ship (was it re-rolled?). "
                f"Re-render with `/render {sid}` first.", ephemeral=True)
            return
        await asyncio.to_thread(store.update_story, sid, status="shipped")
        d = await asyncio.to_thread(render.workdir_for, sid)

        def _read_credits():
            p = os.path.join(d, "credits.txt")
            if os.path.exists(p):
                with open(p) as f:
                    return f.read().strip()
            return ""
        creds = await asyncio.to_thread(_read_credits)
        await interaction.response.send_message(
            f"🚢 **#{sid} shipped.** Master: `{os.path.join(d, 'master.mp4')}`"
            f"\nPaste into the YouTube description:\n```\n{creds[:1600]}\n```",
            ephemeral=True)
        try:
            await interaction.message.edit(
                content=(interaction.message.content or "") + "\n🚢 **SHIPPED**",
                view=None)
        except discord.HTTPException:
            pass

    elif action in ("reroll", "revoice"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        # greenlit check BEFORE the destructive reset -- a stale button on a
        # shipped/rejected story would otherwise delete its master before
        # _queue_render bounced the request for not being approved.
        s = await asyncio.to_thread(store.get_story, sid)
        if not s or s["status"] != "approved":
            await interaction.followup.send(
                f"#{sid} isn't greenlit — re-approve it first (leaves the "
                "existing master untouched).", ephemeral=True)
            return
        # in-flight check BEFORE the destructive reset -- a reset while the
        # worker is rendering would yank files out from under ffmpeg
        active = await asyncio.to_thread(store.active_job_for_story, sid)
        if active and active["state"] != "blocked":
            await interaction.followup.send(
                f"#{sid} already has a render in flight — let it finish "
                "(or fail) first", ephemeral=True)
            return
        if action == "reroll":
            await asyncio.to_thread(render.reset_downstream, sid)
        else:
            await asyncio.to_thread(render.reset_voice, sid)
        reply = await _queue_render(sid, keep_voice=(action == "reroll"))
        await interaction.followup.send(f"🎲 {reply}", ephemeral=True)

    elif action == "scrap":
        await asyncio.to_thread(store.update_story, sid, status="rejected")
        await interaction.response.send_message(
            f"🗑️ #{sid} scrapped (back to rejected).", ephemeral=True)
        try:
            await interaction.message.edit(
                content=(interaction.message.content or "") + "\n🗑️ scrapped",
                view=None)
        except discord.HTTPException:
            pass


# ---- slash commands -----------------------------------------------------------------

CATEGORY_CHOICES = [app_commands.Choice(name=v["label"], value=k)
                    for k, v in criteria.CATEGORIES.items()]


@tree.command(name="generate", description="Research and pitch new story "
              "drafts", guild=GUILD)
@app_commands.describe(category="Story category (default: rotates)",
                       count="How many candidates to research (1-10)")
@app_commands.choices(category=CATEGORY_CHOICES)
async def cmd_generate(interaction: discord.Interaction,
                       category: app_commands.Choice[str] = None,
                       count: app_commands.Range[int, 1, 10] = GEN_BATCH):
    await interaction.response.defer(thinking=True)
    reply = await generate_and_post(category.value if category else None,
                                    count)
    await interaction.followup.send(reply)


@tree.command(name="queue", description="Greenlit stories and render states",
              guild=GUILD)
async def cmd_queue(interaction: discord.Interaction):
    stories = await asyncio.to_thread(store.list_stories, "approved", 15)
    if not stories:
        await interaction.response.send_message(
            "◇ Nothing greenlit yet. Approve drafts in #" + FEED_NAME,
            ephemeral=True)
        return
    lines = []
    for s in stories:
        job = await asyncio.to_thread(store.active_job_for_story, s["id"])
        state = job["state"] if job else "—"
        lines.append(f"`#{s['id']:>3}` **{s['title']}** · "
                     f"{criteria.overall(s['scores']):.1f}★ · {state}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="render", description="Queue (or retry) a render for a "
              "greenlit story", guild=GUILD)
async def cmd_render(interaction: discord.Interaction, story_id: int):
    await interaction.response.defer(ephemeral=True, thinking=True)
    reply = await _queue_render(story_id)
    await interaction.followup.send(reply, ephemeral=True)


@tree.command(name="brief", description="Download a story's production brief",
              guild=GUILD)
async def cmd_brief(interaction: discord.Interaction, story_id: int):
    s = await asyncio.to_thread(store.get_story, story_id)
    if not s:
        await interaction.response.send_message(f"no story #{story_id}",
                                                ephemeral=True)
        return
    text = await asyncio.to_thread(brief_text, s)
    slug = "".join(c if c.isalnum() else "-" for c in s["title"].lower())
    slug = "-".join(filter(None, slug.split("-")))[:60]
    path = os.path.join(render.workdir_for(story_id), f"{slug}-brief.md")

    def _write():
        with open(path, "w") as f:
            f.write(text)
    await asyncio.to_thread(_write)
    await interaction.response.send_message(file=discord.File(path),
                                            ephemeral=True)


@tree.command(name="stats", description="Pipeline budgets and queue depth",
              guild=GUILD)
async def cmd_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    import voice as _voice
    month = dt.datetime.now().strftime("%Y-%m")
    hour = dt.datetime.now().strftime("%Y-%m-%d-%H")
    cred = None
    try:
        cred = await asyncio.to_thread(_voice.eleven_credits)
    except Exception as e:
        print(f"[stats] credits check failed: {e!r}", flush=True)

    def _budgets():
        queued = sum(1 for j in store.jobs_in_flight()
                     if j["state"] == "queued")
        return (store.renders_today(), store.budget("tts_chars:" + month),
                store.budget("pexels_hr:" + hour),
                store.budget("pexels_mo:" + month), queued)
    renders, tts_chars, px_hr, px_mo, queued = \
        await asyncio.to_thread(_budgets)
    lines = [
        f"renders today: **{renders}** / {MAX_RENDERS_PER_DAY}",
        f"jobs queued: **{queued}**",
        f"TTS provider: **{_voice.provider()}**"
        + (f" — {cred[0]:,}/{cred[1]:,} credits used" if cred else ""),
        f"TTS chars this month: **{tts_chars:,}**",
        f"Pexels calls: **{px_hr}** this hour · "
        f"**{px_mo}** this month / 20,000",
    ]
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@tree.command(name="rubric", description="The scoring rubric", guild=GUILD)
async def cmd_rubric(interaction: discord.Interaction):
    await interaction.response.send_message(embed=rubric_embed(),
                                            ephemeral=True)


# ---- DMs ------------------------------------------------------------------------------

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is not None:
        return
    text = (message.content or "").strip()
    low = text.lower()
    if low.startswith("-gen"):
        parts = text.split()
        cat = criteria.resolve_category(parts[1]) if len(parts) > 1 else None
        await message.channel.send("on it — researching…")
        reply = await generate_and_post(cat, GEN_BATCH)
        await message.channel.send(reply)
    elif low.startswith("-queue"):
        stories = await asyncio.to_thread(store.list_stories, "approved", 15)
        if not stories:
            await message.channel.send("nothing greenlit yet")
            return
        await message.channel.send("\n".join(
            f"#{s['id']} {s['title']} ({criteria.overall(s['scores']):.1f}★)"
            for s in stories))
    elif low.startswith("-help"):
        await message.channel.send(
            "`-gen [category]` research 2 new drafts · `-queue` greenlit "
            "list · everything else lives in the server: /generate /queue "
            "/render /brief /stats /rubric")


# ---- lifecycle -----------------------------------------------------------------------

async def _ensure_channels():
    global _feed_chan, _queue_chan
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        raise RuntimeError(f"bot is not in guild {GUILD_ID}")
    cat = discord.utils.find(
        lambda c: c.name.lower() == CATEGORY_NAME.lower(), guild.categories)
    if cat is None:
        cat = await guild.create_category(CATEGORY_NAME)

    async def ensure(name):
        chan = discord.utils.find(lambda c: c.name == name, cat.text_channels)
        if chan is None:
            chan = await guild.create_text_channel(name, category=cat)
        return chan

    _feed_chan = await ensure(FEED_NAME)
    _queue_chan = await ensure(QUEUE_NAME)

    pins = await _feed_chan.pins()
    if not any(p.author == client.user and p.embeds
               and (p.embeds[0].title or "").startswith("How ")
               for p in pins):
        msg = await _feed_chan.send(embed=rubric_embed())
        try:
            await msg.pin()
        except discord.HTTPException:
            pass


@tasks.loop(time=DAILY_TIMES)
async def daily_batch():
    try:
        if not await asyncio.to_thread(store.autogen_on):
            return                               # off — user generates manually
        target = await asyncio.to_thread(store.daily_target)     # 0 = unlimited
        key = "autogen:" + dt.datetime.now().strftime("%Y-%m-%d")
        made = await asyncio.to_thread(store.budget, key)
        if target and made >= target:
            print(f"[daily] target {target} already met today", flush=True)
            return
        n = GEN_BATCH if target == 0 else min(GEN_BATCH, target - made)
        print(f"[daily] generating {n} (rotating category)", flush=True)
        stories = await asyncio.to_thread(llm.gen_batch, None, n)
        if stories:
            await post_drafts(stories)
            await asyncio.to_thread(store.bump, key, len(stories))
        print(f"[daily] posted {len(stories)}; today {made + len(stories)}/"
              f"{target or '∞'}", flush=True)
    except Exception as e:
        print(f"[daily] failed: {e!r}", flush=True)
        traceback.print_exc()


_started = False
_worker_started = False
_web_started = False


async def _refresh_feed_card(story_id: int):
    """Web-console hook: keep the Discord feed card in sync after web-side
    approve/pass/tone changes."""
    s = await asyncio.to_thread(store.get_story, story_id)
    if not (s and s.get("feed_msg_id") and _feed_chan):
        return
    try:
        msg = await _feed_chan.fetch_message(s["feed_msg_id"])
        await msg.edit(embed=story_embed(s), view=feed_view(s))
    except discord.HTTPException as e:
        print(f"[web-hook] card refresh failed for #{story_id}: {e}",
              flush=True)


@client.event
async def on_ready():
    global _started, _worker_started, _web_started
    if _started:
        return
    _started = True
    try:
        await tree.sync(guild=GUILD)         # once, not on every reconnect
        await asyncio.to_thread(store.init_db)
        await _ensure_channels()
        if not _worker_started:
            _worker_started = True
            _spawn(_worker(), "render-worker")
        # resume renders interrupted by a restart (worker polls the DB, so
        # resetting the state is all it takes)
        for job in await asyncio.to_thread(store.jobs_in_flight):
            if job["state"] != "queued":
                await asyncio.to_thread(store.update_job, job["id"],
                                        state="queued")
        if not _web_started and webmod.WEB_ENABLED:
            try:
                await webmod.start(hooks={"post_drafts": post_drafts,
                                          "refresh_card": _refresh_feed_card})
                _web_started = True
                print(f"[web] console on http://{webmod.HOST}:{webmod.PORT}",
                      flush=True)
            except OSError as e:
                # a standalone web.py (storyweb.service) already holds the
                # port -- keep the bot alive, note the clash
                print(f"[web] not started ({e}) — is storyweb.service still "
                      "running? disable it so the bot serves the console "
                      "with Discord hooks", flush=True)
        if DAILY_ENABLED and not daily_batch.is_running():
            daily_batch.start()
    except Exception as e:
        _started = False                     # retry startup in a minute
        print(f"[startup] failed ({e!r}); retrying in 60s", flush=True)
        traceback.print_exc()

        async def _retry():
            await asyncio.sleep(60)
            await on_ready()
        _spawn(_retry(), "startup-retry")
        return
    print(f"{BOT_NAME} ready as {client.user} — feed #{FEED_NAME}, queue "
          f"#{QUEUE_NAME}, daily={'on' if DAILY_ENABLED else 'off'}",
          flush=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("STORY_DISCORD_TOKEN is not set (see storybot.env)")
    client.run(TOKEN)
