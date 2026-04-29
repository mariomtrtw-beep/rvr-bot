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
_FONT_BOLD    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()

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

    B = _FONT_BOLD
    R = _FONT_REGULAR
    fnt = {
        "title":   _load_font(B, 58),
        "sub":     _load_font(R, 20),
        "place":   _load_font(B, 15),
        "name1":   _load_font(B, 36),
        "name23":  _load_font(B, 26),
        "pts1":    _load_font(B, 32),
        "pts23":   _load_font(B, 22),
        "row":     _load_font(R, 20),
        "row_hdr": _load_font(B, 15),
        "ftr":     _load_font(R, 14),
    }

    others   = ranked[3:]
    header_h = 132
    podium_h = 250
    others_h = (len(others) * 46 + 68) if others else 0
    footer_h = 54
    H = header_h + podium_h + others_h + footer_h

    # Work in RGBA throughout for compositing
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

    def glow_card(x1, y1, x2, y2, color, radius=10, border=2, glow_r=16):
        nonlocal img
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(gl).rounded_rectangle(
            (x1, y1, x2, y2), radius=radius,
            outline=(*color[:3], 150), width=border + 3
        )
        gl = gl.filter(ImageFilter.GaussianBlur(glow_r))
        img = Image.alpha_composite(img, gl)
        img = Image.alpha_composite(img, gl)
        ImageDraw.Draw(img).rounded_rectangle(
            (x1, y1, x2, y2), radius=radius,
            fill=CARD_BG, outline=(*color[:3], 255), width=border
        )

    # ── HEADER ────────────────────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    draw.line([(PAD, 20), (W - PAD, 20)], fill=(*CYAN, 50), width=1)

    glow_text("RVR UNDERGROUND", (W // 2, 28), fnt["title"], CYAN, radius=20)

    draw = ImageDraw.Draw(img)
    draw.text(
        (W // 2, 98),
        f"★   {cycle.upper()} MONTHLY CHAMPIONSHIP   ★",
        fill=(*WHITE, 190), font=fnt["sub"], anchor="mt"
    )
    draw.line([(PAD, 128), (W - PAD, 128)], fill=(*CYAN, 110), width=1)

    # ── PODIUM ────────────────────────────────────────────────────────────────
    # Layout (left→right): 2ND  |  1ST (taller)  |  3RD
    y0     = header_h + 14
    C1W, C1H = 340, 210
    C23W, C23H = 256, 165
    GAP = 14

    c1x1 = W // 2 - C1W // 2
    c1x2 = W // 2 + C1W // 2
    c1y1 = y0
    c1y2 = y0 + C1H

    c2x2 = c1x1 - GAP
    c2x1 = c2x2 - C23W
    c2y1 = y0 + (C1H - C23H)
    c2y2 = c2y1 + C23H

    c3x1 = c1x2 + GAP
    c3x2 = c3x1 + C23W
    c3y1 = y0 + (C1H - C23H) + 20
    c3y2 = c3y1 + C23H - 20

    # 1st place — gold glow
    glow_card(c1x1, c1y1, c1x2, c1y2, GOLD, glow_r=22)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((c1x1, c1y1, c1x1 + 8, c1y2), radius=10, fill=(*GOLD, 255))
    draw.text((c1x1 + 22, c1y1 + 14), "1ST PLACE",          fill=(*GOLD,  255), font=fnt["place"])
    draw.text((c1x1 + 22, c1y1 + 16), "WINNER",             fill=(*GOLD,  120), font=fnt["place"])  # ghost label
    glow_text(ranked[0]["user"],       (W // 2, c1y1 + 50),  fnt["name1"],  WHITE, radius=8, anchor="mt")
    draw = ImageDraw.Draw(img)
    draw.text((W // 2, c1y1 + 102),   f"{ranked[0]['points']} pts", fill=(*GOLD, 255), font=fnt["pts1"],  anchor="mt")
    draw.text((W // 2, c1y1 + 150),   "★  CHAMPION  ★",    fill=(*GOLD, 200), font=fnt["place"], anchor="mt")

    # 2nd place — silver
    if len(ranked) >= 2:
        glow_card(c2x1, c2y1, c2x2, c2y2, SILVER, glow_r=10)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((c2x1, c2y1, c2x1 + 7, c2y2), radius=10, fill=(*SILVER, 255))
        draw.text((c2x1 + 18, c2y1 + 12), "2ND PLACE",             fill=(*SILVER, 255), font=fnt["place"])
        draw.text((c2x1 + 18, c2y1 + 36), ranked[1]["user"],        fill=(*WHITE,  255), font=fnt["name23"])
        draw.text((c2x1 + 18, c2y1 + 76), f"{ranked[1]['points']} pts", fill=(*SILVER, 255), font=fnt["pts23"])

    # 3rd place — bronze
    if len(ranked) >= 3:
        glow_card(c3x1, c3y1, c3x2, c3y2, BRONZE, glow_r=10)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((c3x1, c3y1, c3x1 + 7, c3y2), radius=10, fill=(*BRONZE, 255))
        draw.text((c3x1 + 18, c3y1 + 12), "3RD PLACE",             fill=(*BRONZE, 255), font=fnt["place"])
        draw.text((c3x1 + 18, c3y1 + 36), ranked[2]["user"],        fill=(*WHITE,  255), font=fnt["name23"])
        draw.text((c3x1 + 18, c3y1 + 76), f"{ranked[2]['points']} pts", fill=(*BRONZE, 255), font=fnt["pts23"])

    # ── OTHER FINISHERS ───────────────────────────────────────────────────────
    y = header_h + podium_h + 14
    if others:
        draw = ImageDraw.Draw(img)
        draw.line([(PAD, y), (W - PAD, y)], fill=(*DIV, 255), width=1)
        y += 12
        draw.text((W // 2, y), "OTHER FINISHERS", fill=(*GRAY, 255), font=fnt["row_hdr"], anchor="mt")
        y += 34
        for i, p in enumerate(others, start=4):
            if i % 2 == 0:
                draw.rectangle([(PAD, y - 4), (W - PAD, y + 38)], fill=(22, 27, 42, 255))
            draw.text((PAD + 12,      y + 6), f"#{i}",              fill=(*GRAY,  255), font=fnt["row"])
            draw.text((PAD + 60,      y + 6), p["user"],            fill=(*WHITE, 255), font=fnt["row"])
            draw.text((W - PAD - 12, y + 6), f"{p['points']} pts", fill=(*GRAY,  255), font=fnt["row"], anchor="rm")
            y += 46

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
