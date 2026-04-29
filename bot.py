import discord
from discord.ext import commands
import os
import re
import asyncio
import io
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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

# ── Image generation ──────────────────────────────────────────────────────────
_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/ttf-dejavu/DejaVuSans-Bold.ttf",
]
_REG_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/ttf-dejavu/DejaVuSans.ttf",
]

def _load_font(bold: bool, size: int):
    for path in (_BOLD_PATHS if bold else _REG_PATHS):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

def pts_color(rank: int, total: int) -> tuple:
    """Green (1st) → Yellow (mid) → Red (last)"""
    if total <= 1:
        return (0, 220, 90)
    t      = rank / (total - 1)
    GREEN  = (0, 220, 90)
    YELLOW = (255, 200, 0)
    RED    = (255, 55, 55)
    if t <= 0.5:
        s = t / 0.5
    else:
        GREEN, YELLOW = YELLOW, RED
        s = (t - 0.5) / 0.5
    return tuple(int(GREEN[i] + s * (YELLOW[i] - GREEN[i])) for i in range(3))

def generate_results_image(cycle: str, ranked: list) -> io.BytesIO:
    W   = 1000
    PAD = 44

    BG_TOP  = (8,   10,  22)
    BG_BOT  = (16,   6,  32)
    CYAN    = (0,  207, 255)
    GOLD    = (255, 215,   0)
    SILVER  = (200, 210, 220)
    BRONZE  = (205, 127,  50)
    WHITE   = (255, 255, 255)
    GRAY    = (110, 120, 138)
    CARD_BG = (18,  22,  38, 235)
    DIV     = (38,  46,  60)

    fnt = {
        "title":     _load_font(True,  72),
        "sub":       _load_font(False, 22),
        "place_lbl": _load_font(True,  20),
        "name_top3": _load_font(True,  46),
        "pts_top3":  _load_font(True,  42),
        "name_rest": _load_font(True,  30),
        "pts_rest":  _load_font(True,  28),
        "sec_hdr":   _load_font(True,  18),
        "ftr":       _load_font(False, 16),
    }

    total    = len(ranked)
    top3     = ranked[:3]
    others   = ranked[3:]

    TOP3_CARD_H = 104
    TOP3_GAP    = 10
    OTHER_ROW_H = 58
    header_h    = 148
    top3_h      = len(top3) * TOP3_CARD_H + (len(top3) - 1) * TOP3_GAP + 24
    div_h       = 58
    others_h    = (len(others) * OTHER_ROW_H + 10) if others else 0
    footer_h    = 56
    H = header_h + top3_h + div_h + others_h + footer_h

    img = Image.new("RGBA", (W, H), (*BG_TOP, 255))

    # Gradient background
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        c = tuple(int(BG_TOP[i] + t * (BG_BOT[i] - BG_TOP[i])) for i in range(3))
        draw.line([(0, y), (W - 1, y)], fill=(*c, 255))

    # ── Helpers ───────────────────────────────────────────────────────────────
    def glow_text(text, pos, font, color, radius=16, anchor="mt"):
        nonlocal img
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(gl).text(pos, text, fill=(*color[:3], 170), font=font, anchor=anchor)
        gl = gl.filter(ImageFilter.GaussianBlur(radius))
        img = Image.alpha_composite(img, gl)
        img = Image.alpha_composite(img, gl)
        ImageDraw.Draw(img).text(pos, text, fill=(*color[:3], 255), font=font, anchor=anchor)

    def glow_card(x1, y1, x2, y2, color, glow_r=14):
        nonlocal img
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(gl).rounded_rectangle(
            (x1, y1, x2, y2), radius=10, outline=(*color[:3], 140), width=4
        )
        gl = gl.filter(ImageFilter.GaussianBlur(glow_r))
        img = Image.alpha_composite(img, gl)
        img = Image.alpha_composite(img, gl)
        ImageDraw.Draw(img).rounded_rectangle(
            (x1, y1, x2, y2), radius=10, fill=CARD_BG, outline=(*color[:3], 255), width=2
        )

    # ── HEADER ────────────────────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    draw.line([(PAD, 18), (W - PAD, 18)], fill=(*CYAN, 50), width=1)

    glow_text("RVR UNDERGROUND", (W // 2, 26), fnt["title"], CYAN, radius=22)

    draw = ImageDraw.Draw(img)
    draw.text(
        (W // 2, 112),
        f"★   {cycle.upper()} MONTHLY CHAMPIONSHIP   ★",
        fill=(*WHITE, 185), font=fnt["sub"], anchor="mt"
    )
    draw.line([(PAD, 144), (W - PAD, 144)], fill=(*CYAN, 110), width=1)

    # ── TOP 3 CARDS ───────────────────────────────────────────────────────────
    podium_colors = [GOLD, SILVER, BRONZE]
    podium_labels = ["1ST PLACE", "2ND PLACE", "3RD PLACE"]

    y = header_h + 14
    for i, p in enumerate(top3):
        color = podium_colors[i]
        pc    = pts_color(i, total)
        x1, y1, x2, y2 = PAD, y, W - PAD, y + TOP3_CARD_H

        glow_card(x1, y1, x2, y2, color, glow_r=16 if i == 0 else 10)

        draw = ImageDraw.Draw(img)
        # Left color stripe
        draw.rounded_rectangle((x1, y1, x1 + 10, y2), radius=10, fill=(*color, 255))
        # Place label
        draw.text((x1 + 26, y1 + 12), podium_labels[i], fill=(*color, 255), font=fnt["place_lbl"])
        # Player name
        draw.text((x1 + 26, y1 + 38), p["user"], fill=(*WHITE, 255), font=fnt["name_top3"])
        # Points (right side, colored)
        draw.text((x2 - 20, y1 + TOP3_CARD_H // 2), f"{p['points']} pts",
                  fill=(*pc, 255), font=fnt["pts_top3"], anchor="rm")

        y += TOP3_CARD_H + TOP3_GAP

    # ── OTHER FINISHERS ───────────────────────────────────────────────────────
    y = header_h + top3_h + 18
    if others:
        draw = ImageDraw.Draw(img)
        draw.line([(PAD, y), (W - PAD, y)], fill=(*DIV, 255), width=1)
        y += 10
        draw.text((W // 2, y), "OTHER FINISHERS", fill=(*GRAY, 255), font=fnt["sec_hdr"], anchor="mt")
        y += 36

        for i, p in enumerate(others, start=3):
            rank = i  # 0-indexed overall rank
            pc = pts_color(rank, total)
            if i % 2 == 1:
                draw.rectangle([(PAD, y), (W - PAD, y + OTHER_ROW_H - 2)], fill=(22, 27, 42, 255))
            draw.text((PAD + 14,      y + 14), f"#{rank + 1}",         fill=(*GRAY,  255), font=fnt["name_rest"])
            draw.text((PAD + 72,      y + 14), p["user"],              fill=(*WHITE, 255), font=fnt["name_rest"])
            draw.text((W - PAD - 14, y + 14), f"{p['points']} pts",   fill=(*pc,    255), font=fnt["pts_rest"], anchor="rm")
            y += OTHER_ROW_H

    # ── FOOTER ────────────────────────────────────────────────────────────────
    fy = H - footer_h
    draw = ImageDraw.Draw(img)
    draw.line([(PAD, fy + 6), (W - PAD, fy + 6)], fill=(*CYAN, 90), width=1)
    draw.text((W // 2, fy + 20), "RVR Underground  •  Times are best laps",
              fill=(*GRAY, 255), font=fnt["ftr"], anchor="mt")

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


def build_standings(ranked: list, mention: bool) -> tuple[str, str, str]:
    """Returns (podium_text, rest_text, winner_str) for use in embeds."""
    podium_labels = ["🥇 FIRST PLACE", "🥈 SECOND PLACE", "🥉 THIRD PLACE"]

    podium_text = ""
    rest_text   = ""
    winner_str  = "nobody (no times submitted!)"

    for i, p in enumerate(ranked):
        name = f"<@{p['uid']}>" if mention else p['user']
        if i == 0:
            winner_str   = name
            podium_text += f"**{podium_labels[0]}**\n**{name}** — **{p['points']} pts**\n\n"
        elif i == 1:
            podium_text += f"**{podium_labels[1]}**\n**{name}** — **{p['points']} pts**\n\n"
        elif i == 2:
            podium_text += f"**{podium_labels[2]}**\n**{name}** — **{p['points']} pts**\n"
        else:
            rest_text += f"`#{i+1}` **{name}** — **{p['points']} pts**\n"

    return podium_text, rest_text, winner_str


@bot.command(name="previewmonth")
@commands.has_permissions(manage_guild=True)
async def preview_month(ctx):
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

    if not ranked:
        await ctx.send("No times submitted this month yet.")
        return

    img_buf = generate_results_image(cycle, ranked)
    await ctx.send(
        content="👀 **PREVIEW** — nothing has been closed yet.",
        file=discord.File(img_buf, filename="preview.png")
    )


@bot.command(name="closemonth")
@commands.has_permissions(manage_guild=True)
async def close_month(ctx):
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

    results_ch = discord.utils.get(ctx.guild.text_channels, name=MONTHLY_RESULTS_CHANNEL)
    if not results_ch:
        await ctx.send(f"❌ Channel `#{MONTHLY_RESULTS_CHANNEL}` not found. Please create it first.")
        return

    winner_mention = f"<@{ranked[0]['uid']}>" if ranked else "nobody"

    # Track breakdown embed
    tracks_text = ""
    for track, entries in all_data.items():
        tracks_text += f"\n**🏁 {track.upper()}**\n"
        for i, entry in enumerate(entries):
            medals = ["🥇", "🥈", "🥉"]
            medal  = medals[i] if i < len(medals) else f"`#{i+1}`"
            pts    = POINTS[i] if i < len(POINTS) else 0
            tracks_text += f"{medal} <@{entry['uid']}> — `{entry['time']}` *(+{pts} pts)*\n"
        tracks_text += "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"

    tracks_embed = discord.Embed(
        title=f"📋 {cycle} — Track Breakdown",
        description=tracks_text if tracks_text else "*No track data.*",
        color=0xFFD700
    )

    months_role = discord.utils.get(ctx.guild.roles, name="Months")
    if months_role:
        await results_ch.send(months_role.mention)

    if ranked:
        img_buf = generate_results_image(cycle, ranked)
        await results_ch.send(
            content=f"🏁 **{cycle} — The Race Is Over!**\n👑 **Winner: {winner_mention}** — congratulations!",
            file=discord.File(img_buf, filename="results.png")
        )
    else:
        await results_ch.send(f"🏁 **{cycle}** has ended — no times were submitted this month.")

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
