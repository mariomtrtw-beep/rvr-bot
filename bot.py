import discord
from discord.ext import commands
import os
import re
import asyncio
from datetime import datetime
from pymongo import MongoClient

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN     = os.environ["DISCORD_TOKEN"]
MONGO_URL = os.environ["MONGO_URL"]

SUBMISSION_CHANNEL  = "time-submissions"
APPROVAL_CHANNEL    = "admin-approvals"
LEADERBOARD_CHANNEL = "leaderboard"

POINTS = [10, 7, 5, 3, 2, 1]

# ── MongoDB setup ─────────────────────────────────────────────────────────────
client = MongoClient(MONGO_URL)
db     = client["rvr_underground"]
times_col = db["times"]

# ── Helpers ───────────────────────────────────────────────────────────────────
def time_to_seconds(t: str) -> float:
    t = t.strip()
    if ":" in t:
        m, s = t.split(":")
        return int(m) * 60 + float(s)
    return float(t)

def get_track_entries(track: str):
    entries = list(times_col.find({"track": track}, {"_id": 0}))
    entries.sort(key=lambda x: time_to_seconds(x["time"]))
    return entries

def get_all_tracks():
    return times_col.distinct("track")

def get_all_data():
    data = {}
    for track in get_all_tracks():
        data[track] = get_track_entries(track)
    return data

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
pending = {}

# ── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name == SUBMISSION_CHANNEL:
        if not message.attachments:
            await message.reply("❌ Please attach a screenshot as proof with your submission!")
            return

        pattern = r"Track:\s*(.+?)\s*\|\s*Time:\s*([\d:\.]+)"
        match = re.search(pattern, message.content, re.IGNORECASE)
        if not match:
            await message.reply(
                "❌ Wrong format! Please use:\n"
                "`Track: <track name> | Time: <time>`\n"
                "Example: `Track: Toys In The Hood | Time: 1:23.456`"
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
            timestamp=datetime.utcnow()
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
            "track":               track,
            "time":                time_str,
            "user":                message.author.name,
            "uid":                 message.author.id,
            "proof_url":           proof_url,
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

    sub = pending.pop(reaction.message.id)
    guild = reaction.message.guild

    if str(reaction.emoji) == "✅":
        track    = sub["track"]
        time_str = sub["time"]
        uid      = sub["uid"]

        existing = times_col.find_one({"track": track, "uid": uid})
        if existing:
            old_seconds = time_to_seconds(existing["time"])
            new_seconds = time_to_seconds(time_str)
            if new_seconds >= old_seconds:
                await reaction.message.edit(content="⚠️ Rejected — player already has a better or equal time on this track.")
                await reaction.message.clear_reactions()
                return
            times_col.delete_one({"track": track, "uid": uid})

        times_col.insert_one({
            "track": track,
            "user":  sub["user"],
            "uid":   uid,
            "time":  time_str,
            "proof": sub["proof_url"],
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

    all_data = get_all_data()
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

    points_embed = discord.Embed(
        title="🏆 RVR Underground — Overall Standings",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )
    if ranked:
        standings = ""
        for i, p in enumerate(ranked):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            standings += f"{medal} **{p['user']}** — {p['points']} pts\n"
        points_embed.description = standings
    else:
        points_embed.description = "No times submitted yet!"

    track_embeds = []
    for track, entries in all_data.items():
        embed = discord.Embed(title=f"🏁 {track}", color=discord.Color.blue())
        track_str = ""
        for i, entry in enumerate(entries):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            pts   = POINTS[i] if i < len(POINTS) else 0
            track_str += f"{medal} **{entry['user']}** — `{entry['time']}` (+{pts} pts)\n"
        embed.description = track_str
        track_embeds.append(embed)

    await lb_ch.purge(limit=100)
    await asyncio.sleep(1)
    await lb_ch.send(embed=points_embed)
    for te in track_embeds:
        await lb_ch.send(embed=te)


# ── Commands ──────────────────────────────────────────────────────────────────
@bot.command(name="leaderboard", aliases=["lb"])
async def leaderboard_cmd(ctx):
    await update_leaderboard(ctx.guild)
    await ctx.send("📊 Leaderboard updated!")

@bot.command(name="mystats")
async def mystats(ctx):
    uid = ctx.author.id
    embed = discord.Embed(title=f"📊 Stats for {ctx.author.name}", color=discord.Color.purple())
    all_data = get_all_data()
    found = False
    total_points = 0
    for track, entries in all_data.items():
        for i, entry in enumerate(entries):
            if entry["uid"] == uid:
                pts = POINTS[i] if i < len(POINTS) else 0
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
    tracks = get_all_tracks()
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
    result = times_col.delete_many({"track": track_name})
    if result.deleted_count == 0:
        await ctx.send(f"❌ Track `{track_name}` not found.")
        return
    await ctx.send(f"✅ Track `{track_name}` and all its times have been removed.")
    await update_leaderboard(ctx.guild)

@bot.command(name="removetime")
@commands.has_permissions(manage_guild=True)
async def remove_time(ctx, member: discord.Member, *, track_name: str):
    track_name = track_name.title()
    result = times_col.delete_one({"track": track_name, "uid": member.id})
    if result.deleted_count == 0:
        await ctx.send(f"❌ No time found for {member.name} on `{track_name}`.")
        return
    await ctx.send(f"✅ Removed {member.name}'s time from `{track_name}`.")
    await update_leaderboard(ctx.guild)

@bot.command(name="rvrhelp")
async def rvr_help(ctx):
    embed = discord.Embed(title="🤖 RVR Bot Commands", color=discord.Color.blurple())
    embed.add_field(name="!leaderboard (or !lb)", value="Show the full leaderboard", inline=False)
    embed.add_field(name="!mystats",              value="Show your personal track stats", inline=False)
    embed.add_field(name="!tracks",               value="List all tracks with times", inline=False)
    embed.add_field(name="── Admin only ──",      value="\u200b", inline=False)
    embed.add_field(name="!removetrack <track>",        value="Remove a track and all its times", inline=False)
    embed.add_field(name="!removetime @player <track>", value="Remove a player's time from a track", inline=False)
    embed.add_field(
        name="── Submitting a time ──",
        value="Post in #time-submissions with a screenshot:\n`Track: Toys In The Hood | Time: 1:23.456`",
        inline=False
    )
    await ctx.send(embed=embed)

bot.run(TOKEN)
