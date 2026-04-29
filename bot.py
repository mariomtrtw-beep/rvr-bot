import discord
from discord.ext import commands
import os
import re
import asyncio
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN     = os.environ["DISCORD_TOKEN"]
MONGO_URL = os.environ["MONGO_URL"]

SUBMISSION_CHANNEL      = "time-submissions"
APPROVAL_CHANNEL        = "admin-approvals"
LEADERBOARD_CHANNEL     = "leaderboard"
MONTHLY_RESULTS_CHANNEL = "monthly-results"

POINTS = [10, 7, 5, 3, 2, 1]

BANNER = "https://media.discordapp.net/attachments/1491238480993456259/1492468917094846547/banner_thin.png"

# ── MongoDB (async) ───────────────────────────────────────────────────────────
mongo_client = AsyncIOMotorClient(MONGO_URL)
db           = mongo_client["rvr_underground"]
times_col    = db["times"]
cycles_col   = db["cycles"]

# ── Cycle helpers ─────────────────────────────────────────────────────────────
async def get_current_cycle() -> str:
    """Return the name of the active cycle, creating one if none exists."""
    cycle = await cycles_col.find_one({"active": True})
    if not cycle:
        now  = datetime.now(timezone.utc)
        name = now.strftime("%B %Y")
        await cycles_col.insert_one({"name": name, "active": True, "started_at": now})
        # Tag all existing untagged times so they belong to this first cycle
        await times_col.update_many({"cycle": {"$exists": False}}, {"$set": {"cycle": name}})
        return name
    return cycle["name"]

# ── Helpers ───────────────────────────────────────────────────────────────────
def time_to_seconds(t: str) -> float:
    t = t.strip()
    parts = t.split(":")
    if len(parts) == 3:
        m, s, ms = parts
        return int(m) * 60 + int(s) + int(ms) / 1000
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(t)

async def get_track_entries(track: str, cycle: str = None):
    if cycle is None:
        cycle = await get_current_cycle()
    entries = await times_col.find({"track": track, "cycle": cycle}, {"_id": 0}).to_list(None)
    entries.sort(key=lambda x: time_to_seconds(x["time"]))
    return entries

async def get_all_tracks(cycle: str = None):
    if cycle is None:
        cycle = await get_current_cycle()
    return await times_col.distinct("track", {"cycle": cycle})

async def get_all_data(cycle: str = None):
    if cycle is None:
        cycle = await get_current_cycle()
    data = {}
    for track in await get_all_tracks(cycle):
        data[track] = await get_track_entries(track, cycle)
    return data

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot     = commands.Bot(command_prefix="!", intents=intents)
pending = {}  # approval_msg_id -> submission dict

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    # Ensure a cycle exists on startup
    await get_current_cycle()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name == SUBMISSION_CHANNEL:
        if not message.attachments:
            await message.reply("❌ Please attach a screenshot as proof with your submission!")
            return

        pattern = r"Track:\s*(.+?)\s*\|\s*Time:\s*([\d:\.]+)"
        match   = re.search(pattern, message.content, re.IGNORECASE)
        if not match:
            await message.reply(
                "❌ Wrong format! Please use:\n"
                "`Track: <track name> | Time: <time>`\n"
                "Example: `Track: Toys In The Hood | Time: 00:41:256`"
            )
            return

        track     = match.group(1).strip().title()
        time_str  = match.group(2).strip()
        proof_url = message.attachments[0].url

        approval_ch = discord.utils.get(message.guild.text_channels, name=APPROVAL_CHANNEL)
        if not approval_ch:
            await message.reply("⚠️ Admin approval channel not found. Contact an admin.")
            return

        embed = discord.Embed(
            title="⏱️ New Time Submission",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Player", value=f"{message.author.mention} ({message.author.name})", inline=False)
        embed.add_field(name="Track",  value=track,    inline=True)
        embed.add_field(name="Time",   value=time_str, inline=True)
        embed.set_image(url=proof_url)
        embed.set_footer(text="React ✅ to approve or ❌ to reject")

        approval_msg = await approval_ch.send(embed=embed)
        await approval_msg.add_reaction("✅")
        await approval_msg.add_reaction("❌")

        pending[approval_msg.id] = {
            "track":                 track,
            "time":                  time_str,
            "user":                  message.author.name,
            "uid":                   message.author.id,
            "proof_url":             proof_url,
            "submission_channel_id": message.channel.id,
        }

        await message.reply("✅ Your time has been submitted and is awaiting admin approval!")

    await bot.process_commands(message)


@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    if reaction.message.id not in pending:
        return
    if not user.guild_permissions.manage_guild:
        return

    sub   = pending.pop(reaction.message.id)
    guild = reaction.message.guild

    if str(reaction.emoji) == "✅":
        track    = sub["track"]
        time_str = sub["time"]
        uid      = sub["uid"]
        cycle    = await get_current_cycle()

        existing = await times_col.find_one({"track": track, "uid": uid, "cycle": cycle})
        if existing:
            old_seconds = time_to_seconds(existing["time"])
            new_seconds = time_to_seconds(time_str)
            if new_seconds >= old_seconds:
                await reaction.message.edit(content="⚠️ Rejected — player already has a better or equal time on this track.")
                await reaction.message.clear_reactions()
                return
            await times_col.delete_one({"track": track, "uid": uid, "cycle": cycle})

        await times_col.insert_one({
            "track": track,
            "user":  sub["user"],
            "uid":   uid,
            "time":  time_str,
            "proof": sub["proof_url"],
            "cycle": cycle,
        })

        sub_ch = guild.get_channel(sub["submission_channel_id"])
        if sub_ch:
            member  = guild.get_member(uid)
            mention = member.mention if member else sub["user"]
            await sub_ch.send(f"✅ {mention} your time **{time_str}** on **{track}** has been approved!")

        await reaction.message.edit(content="✅ Approved!")
        await reaction.message.clear_reactions()
        await update_leaderboard(guild)

    elif str(reaction.emoji) == "❌":
        sub_ch = guild.get_channel(sub["submission_channel_id"])
        if sub_ch:
            member  = guild.get_member(sub["uid"])
            mention = member.mention if member else sub["user"]
            await sub_ch.send(f"❌ {mention} your time submission for **{sub['track']}** was rejected by an admin.")

        await reaction.message.edit(content="❌ Rejected.")
        await reaction.message.clear_reactions()


async def update_leaderboard(guild):
    lb_ch = discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL)
    if not lb_ch:
        return

    cycle    = await get_current_cycle()
    all_data = await get_all_data(cycle)
    player_points = {}

    for track, entries in all_data.items():
        for i, entry in enumerate(entries):
            pts = POINTS[i] if i < len(POINTS) else 0
            uid = entry["uid"]
            if uid not in player_points:
                player_points[uid] = {"user": entry["user"], "points": 0}
            player_points[uid]["points"] += pts

    ranked = sorted(player_points.values(), key=lambda x: x["points"], reverse=True)
    medals = ["🥇", "🥈", "🥉"]

    # Overall standings
    if ranked:
        standings = ""
        for i, p in enumerate(ranked):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            standings += f"{medal} **{p['user']}** — **{p['points']} pts**\n"
    else:
        standings = "*No times submitted yet!*"

    standings_embed = discord.Embed(
        description=(
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"🏆 **__OVERALL STANDINGS — {cycle}__**\n"
            f"{standings}"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
        ),
        color=0x00cfff,
        timestamp=datetime.now(timezone.utc)
    )
    standings_embed.set_image(url=BANNER)
    standings_embed.set_footer(text="RVR Underground • Times are best laps")

    # Track times
    tracks_text = ""
    for track, entries in all_data.items():
        tracks_text += f"\n**🏁 {track.upper()}**\n"
        for i, entry in enumerate(entries):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            pts = POINTS[i] if i < len(POINTS) else 0
            tracks_text += f"{medal} **{entry['user']}** — `{entry['time']}` *(+{pts} pts)*\n"
        tracks_text += "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"

    times_embed = discord.Embed(
        description=tracks_text,
        color=0x00cfff
    )

    await lb_ch.purge(limit=100)
    await asyncio.sleep(1)
    await lb_ch.send(embed=standings_embed)
    await lb_ch.send(embed=times_embed)


# ── Commands ──────────────────────────────────────────────────────────────────
@bot.command(name="leaderboard", aliases=["lb"])
async def leaderboard_cmd(ctx):
    await update_leaderboard(ctx.guild)
    await ctx.send("📊 Leaderboard updated!")

@bot.command(name="mystats")
async def mystats(ctx):
    uid      = ctx.author.id
    cycle    = await get_current_cycle()
    embed    = discord.Embed(title=f"📊 Stats for {ctx.author.name} — {cycle}", color=discord.Color.purple())
    all_data = await get_all_data(cycle)
    found    = False
    total_points = 0
    for track, entries in all_data.items():
        for i, entry in enumerate(entries):
            if entry["uid"] == uid:
                pts           = POINTS[i] if i < len(POINTS) else 0
                total_points += pts
                embed.add_field(
                    name=f"🏁 {track}",
                    value=f"Time: `{entry['time']}` | Position: #{i+1} | Points: {pts}",
                    inline=False
                )
                found = True
    if not found:
        embed.description = "No approved times yet! Submit your first time in #time-submissions."
    else:
        embed.set_footer(text=f"Total points: {total_points}")
    await ctx.send(embed=embed)

@bot.command(name="tracks")
async def tracks_cmd(ctx):
    tracks = await get_all_tracks()
    if not tracks:
        await ctx.send("No tracks with times yet!")
        return
    track_list = "\n".join([f"🏁 {t}" for t in tracks])
    embed = discord.Embed(title="Available Tracks", description=track_list, color=discord.Color.green())
    await ctx.send(embed=embed)

@bot.command(name="removetrack")
@commands.has_permissions(manage_guild=True)
async def remove_track(ctx, *, track_name: str):
    track_name = track_name.title()
    cycle      = await get_current_cycle()
    result     = await times_col.delete_many({"track": track_name, "cycle": cycle})
    if result.deleted_count == 0:
        await ctx.send(f"❌ Track `{track_name}` not found in the current cycle.")
        return
    await ctx.send(f"✅ Track `{track_name}` and all its times have been removed.")
    await update_leaderboard(ctx.guild)

@bot.command(name="removetime")
@commands.has_permissions(manage_guild=True)
async def remove_time(ctx, member: discord.Member, *, track_name: str):
    track_name = track_name.title()
    cycle      = await get_current_cycle()
    result     = await times_col.delete_one({"track": track_name, "uid": member.id, "cycle": cycle})
    if result.deleted_count == 0:
        await ctx.send(f"❌ No time found for {member.name} on `{track_name}` in the current cycle.")
        return
    await ctx.send(f"✅ Removed {member.name}'s time from `{track_name}`.")
    await update_leaderboard(ctx.guild)

@bot.command(name="previewmonth")
@commands.has_permissions(manage_guild=True)
async def preview_month(ctx):
    """Preview what the monthly results will look like without closing the cycle."""
    cycle    = await get_current_cycle()
    all_data = await get_all_data(cycle)

    player_points = {}
    for track, entries in all_data.items():
        for i, entry in enumerate(entries):
            pts = POINTS[i] if i < len(POINTS) else 0
            uid = entry["uid"]
            if uid not in player_points:
                player_points[uid] = {"user": entry["user"], "uid": uid, "points": 0}
            player_points[uid]["points"] += pts

    ranked = sorted(player_points.values(), key=lambda x: x["points"], reverse=True)
    medals = ["🥇", "🥈", "🥉"]

    if ranked:
        winner         = ranked[0]
        winner_mention = f"<@{winner['uid']}>"
        standings_text = ""
        for i, p in enumerate(ranked):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            standings_text += f"{medal} <@{p['uid']}> — **{p['points']} pts**\n"
    else:
        winner_mention = "nobody (no times submitted!)"
        standings_text = "*No times were submitted this month.*"

    embed = discord.Embed(
        title=f"👀 PREVIEW — {cycle} Monthly Results",
        description=(
            f"*This is a preview only — nothing has been closed.*\n\n"
            f"👑 **Winner: {winner_mention}**\n\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"**FINAL STANDINGS**\n"
            f"{standings_text}"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
        ),
        color=0x888888,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="RVR Underground • Preview only — use !closemonth to finalize")
    await ctx.send(embed=embed)

@bot.command(name="closemonth")
@commands.has_permissions(manage_guild=True)
async def close_month(ctx):
    cycle    = await get_current_cycle()
    all_data = await get_all_data(cycle)

    # Calculate final standings
    player_points = {}
    for track, entries in all_data.items():
        for i, entry in enumerate(entries):
            pts = POINTS[i] if i < len(POINTS) else 0
            uid = entry["uid"]
            if uid not in player_points:
                player_points[uid] = {"user": entry["user"], "uid": uid, "points": 0}
            player_points[uid]["points"] += pts

    ranked = sorted(player_points.values(), key=lambda x: x["points"], reverse=True)
    medals = ["🥇", "🥈", "🥉"]

    # Find the monthly results channel
    results_ch = discord.utils.get(ctx.guild.text_channels, name=MONTHLY_RESULTS_CHANNEL)
    if not results_ch:
        await ctx.send(f"❌ Channel `#{MONTHLY_RESULTS_CHANNEL}` not found. Please create it first.")
        return

    # Build the winner line and standings
    if ranked:
        winner         = ranked[0]
        winner_mention = f"<@{winner['uid']}>"
        standings_text = ""
        for i, p in enumerate(ranked):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            standings_text += f"{medal} <@{p['uid']}> — **{p['points']} pts**\n"
    else:
        winner_mention = "nobody (no times submitted!)"
        standings_text = "*No times were submitted this month.*"

    # Main results embed
    results_embed = discord.Embed(
        title=f"🏆 {cycle} — Monthly Results",
        description=(
            f"The **{cycle}** monthly cycle has come to an end!\n\n"
            f"👑 **Winner: {winner_mention}** — congratulations!\n\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"**FINAL STANDINGS**\n"
            f"{standings_text}"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
        ),
        color=0xFFD700,
        timestamp=datetime.now(timezone.utc)
    )
    results_embed.set_image(url=BANNER)
    results_embed.set_footer(text="RVR Underground • Monthly Cycle Closed")

    # Track breakdown embed
    tracks_text = ""
    for track, entries in all_data.items():
        tracks_text += f"\n**🏁 {track.upper()}**\n"
        for i, entry in enumerate(entries):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            pts   = POINTS[i] if i < len(POINTS) else 0
            tracks_text += f"{medal} <@{entry['uid']}> — `{entry['time']}` *(+{pts} pts)*\n"
        tracks_text += "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"

    tracks_embed = discord.Embed(
        title=f"📋 {cycle} — Track Breakdown",
        description=tracks_text if tracks_text else "*No track data.*",
        color=0xFFD700
    )

    await results_ch.send(embed=results_embed)
    if tracks_text:
        await results_ch.send(embed=tracks_embed)

    # Close current cycle
    now = datetime.now(timezone.utc)
    await cycles_col.update_one({"active": True}, {"$set": {"active": False, "closed_at": now}})

    # Start next cycle (next calendar month)
    if now.month == 12:
        new_cycle_name = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc).strftime("%B %Y")
    else:
        new_cycle_name = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc).strftime("%B %Y")

    await cycles_col.insert_one({"name": new_cycle_name, "active": True, "started_at": now})

    # Reset leaderboard for the new empty cycle
    await update_leaderboard(ctx.guild)

    await ctx.send(
        f"✅ **{cycle}** has been closed! Results posted in {results_ch.mention}.\n"
        f"📅 New cycle **{new_cycle_name}** has started — leaderboard reset!"
    )

@bot.command(name="rvrhelp")
async def rvr_help(ctx):
    embed = discord.Embed(title="🤖 RVR Bot Commands", color=discord.Color.blurple())
    embed.add_field(name="!leaderboard (or !lb)", value="Show the full leaderboard for the current month", inline=False)
    embed.add_field(name="!mystats",              value="Show your personal stats for the current month", inline=False)
    embed.add_field(name="!tracks",               value="List all tracks with times this month", inline=False)
    embed.add_field(name="── Admin only ──",      value="\u200b", inline=False)
    embed.add_field(name="!previewmonth",                value="Preview what this month's results will look like (no changes made)", inline=False)
    embed.add_field(name="!closemonth",                  value="Close the current monthly cycle, post results to #monthly-results, and start a new cycle", inline=False)
    embed.add_field(name="!removetrack <track>",         value="Remove a track and all its times (current cycle)", inline=False)
    embed.add_field(name="!removetime @player <track>",  value="Remove a player's time from a track (current cycle)", inline=False)
    embed.add_field(
        name="── Submitting a time ──",
        value="Post in #time-submissions with a screenshot:\n`Track: Toys In The Hood | Time: 1:23.456`",
        inline=False
    )
    await ctx.send(embed=embed)

bot.run(TOKEN)
