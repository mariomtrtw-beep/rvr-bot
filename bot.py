import discord
from discord.ext import commands
import os
import re
import asyncio
import io
import jwt
import requests
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

POINTS = [15, 12, 10, 8, 7, 6, 5, 4, 3, 2, 1]

BANNER = "https://media.discordapp.net/attachments/1491238480993456259/1492468917094846547/banner_thin.png"

# ── MongoDB (async) ───────────────────────────────────────────────────────────
mongo_client = AsyncIOMotorClient(MONGO_URL)
db           = mongo_client["rvr_underground"]
times_col    = db["times"]
cycles_col   = db["cycles"]
medals_col   = db["medals"]
ratings_col  = db["ratings"]
wordle_daily_col = db["wordle_daily"]
wordle_users_col  = db["wordle_users"]

GATHER_CHANNEL   = "Gather"
DEFAULT_RATING   = 0.80

SEED_RATINGS = [
    ("Azaria", 1.15), ("Boban", 1.45), ("D.olo", 1.20), ("DC", 0.90),
    ("DracoPOW", 0.50), ("gamer42", 0.20), ("Goxi", 1.40), ("H i r u", 1.00),
    ("I VENDETT5 I", 1.35), ("Kilabarus", 1.40), ("Lager", 1.40), ("maci", 1.35),
    ("nuclearhythmics", 1.00), ("orissm", 1.20), ("pokers72", DEFAULT_RATING),
    ("rodik", 1.10), ("SebR", 1.00), ("Shigekix", 1.25), ("t0x1c", 1.20),
    ("Taco", 1.10), ("TioRotti", 1.15), ("Topke", 1.25), ("Tytan", 1.00),
    ("xpete", 1.25), ("yun", 1.15), ("Zigc", 1.15), ("ZipperZbieracz", 1.30),
    ("Zsolti", 1.10), ("— 𝐋𝐨𝐥𝐛𝐢𝐭.", 1.20), ("𝙆𝙤𝙩𝙞𝙠_𝙓𝙋", 1.15),
]

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

    if message.channel.name == SUBMISSION_CHANNEL and not message.content.startswith("!"):
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

        # Snapshot rankings before this approval
        _pre_all = await get_all_data(cycle)
        _pre_pts: dict[int, int] = {}
        for _t, _es in _pre_all.items():
            for _i, _e in enumerate(_es):
                _p = POINTS[_i] if _i < len(POINTS) else 0
                _pre_pts[_e["uid"]] = _pre_pts.get(_e["uid"], 0) + _p
        _pre_rank_map = {u: i for i, u in enumerate(sorted(_pre_pts, key=lambda u: _pre_pts[u], reverse=True))}

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

        # Compute post rankings and deltas
        _post_all = await get_all_data(cycle)
        _post_pts: dict[int, int] = {}
        for _t, _es in _post_all.items():
            for _i, _e in enumerate(_es):
                _p = POINTS[_i] if _i < len(POINTS) else 0
                _post_pts[_e["uid"]] = _post_pts.get(_e["uid"], 0) + _p
        _post_rank_list = sorted(_post_pts, key=lambda u: _post_pts[u], reverse=True)
        _post_rank_map  = {u: i for i, u in enumerate(_post_rank_list)}
        _rank_deltas    = {u: (_pre_rank_map.get(u, len(_pre_rank_map)) - _post_rank_map[u]) for u in _post_rank_map}

        sub_ch = guild.get_channel(sub["submission_channel_id"])
        if sub_ch:
            member  = guild.get_member(uid)
            mention = member.mention if member else sub["user"]
            await sub_ch.send(f"✅ {mention} your time **{time_str}** on **{track}** has been approved!")

        await reaction.message.edit(content="✅ Approved!")
        await reaction.message.clear_reactions()
        await update_leaderboard(guild, rank_deltas=_rank_deltas)

    elif str(reaction.emoji) == "❌":
        sub_ch = guild.get_channel(sub["submission_channel_id"])
        if sub_ch:
            member  = guild.get_member(sub["uid"])
            mention = member.mention if member else sub["user"]
            await sub_ch.send(f"❌ {mention} your time submission for **{sub['track']}** was rejected by an admin.")

        await reaction.message.edit(content="❌ Rejected.")
        await reaction.message.clear_reactions()


async def update_leaderboard(guild, rank_deltas: dict | None = None):
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
                player_points[uid] = {"user": entry["user"], "uid": uid, "points": 0}
            player_points[uid]["points"] += pts

    ranked = sorted(player_points.values(), key=lambda x: x["points"], reverse=True)
    medals = ["🥇", "🥈", "🥉"]

    # Track times embed
    tracks_text = ""
    for track, entries in all_data.items():
        tracks_text += f"\n**🏁 {track.upper()}**\n"
        for i, entry in enumerate(entries):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            pts = POINTS[i] if i < len(POINTS) else 0
            tracks_text += f"{medal} **{entry['user']}** — `{entry['time']}` *(+{pts} pts)*\n"
        tracks_text += "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"

    times_embed = discord.Embed(
        description=tracks_text if tracks_text else "*No times submitted yet!*",
        color=0x00cfff
    )

    await lb_ch.purge(limit=100)
    await asyncio.sleep(1)

    if ranked:
        img_buf = generate_leaderboard_image(cycle, ranked, rank_deltas)
        await lb_ch.send(file=discord.File(img_buf, filename="leaderboard.png"))
    else:
        await lb_ch.send(content="*No times submitted yet!*")

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
_FONT_DIR = os.path.dirname(os.path.abspath(__file__))

_BOLD_PATHS = [
    os.path.join(_FONT_DIR, "Exo2-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
_REG_PATHS = [
    os.path.join(_FONT_DIR, "Exo2-SemiBold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
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

def pts_color(points: int, max_points: int) -> tuple:
    """Green (max pts) → Yellow (mid) → Red (0 pts)"""
    GREEN  = (0, 220, 90)
    YELLOW = (255, 200, 0)
    RED    = (255, 55, 55)
    if max_points <= 0:
        return RED
    t = 1.0 - min(points, max_points) / max_points  # 0 = green, 1 = red
    if t <= 0.5:
        s = t / 0.5
        a, b = GREEN, YELLOW
    else:
        s = (t - 0.5) / 0.5
        a, b = YELLOW, RED
    return tuple(int(a[i] + s * (b[i] - a[i])) for i in range(3))

def generate_results_image(cycle: str, ranked: list) -> io.BytesIO:
    import random
    W   = 1000
    PAD = 44

    BG_TOP  = (2,    8,  22)
    BG_BOT  = (5,    3,  26)
    CYAN    = (0,  200, 255)
    GOLD    = (255, 200,   0)
    SILVER  = (140, 190, 240)
    BRONZE  = (190, 105,  40)
    WHITE   = (255, 255, 255)
    GRAY    = (80,  105, 140)
    CARD_BG = (5,   12,  28)
    DIV     = (28,  48,  78)

    fnt = {
        "title":     _load_font(True,  83),
        "sub":       _load_font(False, 24),
        "place_lbl": _load_font(True,  33),
        "name_top3": _load_font(True,  53),
        "pts_top3":  _load_font(True,  56),
        "name_rest": _load_font(True,  35),
        "pts_rest":  _load_font(True,  38),
        "sec_hdr":   _load_font(True,  30),
        "ftr":       _load_font(False, 17),
    }

    # ── Load banner ───────────────────────────────────────────────────────────
    banner_img  = None
    banner_h    = 0
    MAX_BANNER_H = 200
    banner_path  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.png")
    try:
        raw       = Image.open(banner_path).convert("RGBA")
        bw, bh    = raw.size
        full_h    = int(W * bh / bw)
        resized   = raw.resize((W, full_h), Image.LANCZOS)
        if full_h > MAX_BANNER_H:
            crop_top   = (full_h - MAX_BANNER_H) // 2
            banner_img = resized.crop((0, crop_top, W, crop_top + MAX_BANNER_H))
            banner_h   = MAX_BANNER_H
        else:
            banner_img = resized
            banner_h   = full_h
    except Exception:
        pass

    COL_RANK       = PAD + 20
    COL_PLAYER     = PAD + 110
    COL_PTS        = W - PAD - 20
    TABLE_HDR_H    = 52

    total          = len(ranked)
    max_points     = ranked[0]["points"] if ranked else 1
    top3           = ranked[:3]
    others         = ranked[3:]
    TOP3_CARD_H    = [158, 120, 120]
    TOP3_GAP       = 10
    OTHER_ROW_H    = 66
    subtitle_h     = 48
    header_h       = banner_h + subtitle_h + 16
    top3_h         = TABLE_HDR_H + sum(TOP3_CARD_H[:len(top3)]) + (len(top3) - 1) * TOP3_GAP + 16
    others_h       = (len(others) * OTHER_ROW_H + 30) if others else 0
    footer_h       = 24
    H = header_h + top3_h + others_h + footer_h

    # ── Base + gradient ───────────────────────────────────────────────────────
    img = Image.new("RGBA", (W, H), (*BG_TOP, 255))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        c = tuple(int(BG_TOP[i] + t * (BG_BOT[i] - BG_TOP[i])) for i in range(3))
        draw.line([(0, y), (W - 1, y)], fill=(*c, 255))

    # ── Circuit board traces ──────────────────────────────────────────────────
    rng     = random.Random(1337)
    GRID    = 50
    circuit = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cdraw   = ImageDraw.Draw(circuit)
    grid    = {}
    for col in range(W // GRID + 2):
        for row in range(H // GRID + 2):
            if rng.random() < 0.58:
                grid[(col, row)] = (
                    col * GRID + rng.randint(-6, 6),
                    row * GRID + rng.randint(-6, 6),
                )
    for (col, row), (x, y) in grid.items():
        if (col + 1, row) in grid and rng.random() < 0.40:
            nx, ny = grid[(col + 1, row)]
            cdraw.line([(x, y), (nx, ny)], fill=(0, 140, 255, 100), width=1)
        if (col, row + 1) in grid and rng.random() < 0.40:
            nx, ny = grid[(col, row + 1)]
            cdraw.line([(x, y), (nx, ny)], fill=(0, 140, 255, 100), width=1)
        if rng.random() < 0.20:
            r = rng.randint(1, 3)
            cdraw.ellipse([(x - r, y - r), (x + r, y + r)], fill=(0, 210, 255, 140))
    img = Image.alpha_composite(img, circuit)

    # ── Scanlines ─────────────────────────────────────────────────────────────
    scan  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scan)
    for sy in range(0, H, 3):
        sdraw.line([(0, sy), (W, sy)], fill=(0, 0, 0, 7), width=1)
    img = Image.alpha_composite(img, scan)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def glow_text(text, pos, font, color, radius=16, anchor="mt"):
        nonlocal img
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(gl).text(pos, text, fill=(*color[:3], 170), font=font, anchor=anchor)
        gl = gl.filter(ImageFilter.GaussianBlur(radius))
        img = Image.alpha_composite(img, gl)
        img = Image.alpha_composite(img, gl)
        ImageDraw.Draw(img).text(pos, text, fill=(*color[:3], 255), font=font, anchor=anchor)

    def glow_line(x1, y1, x2, y2, color, width=1, radius=4):
        nonlocal img
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(gl).line([(x1, y1), (x2, y2)], fill=(*color[:3], 200), width=width + 2)
        gl = gl.filter(ImageFilter.GaussianBlur(radius))
        img = Image.alpha_composite(img, gl)
        ImageDraw.Draw(img).line([(x1, y1), (x2, y2)], fill=(*color[:3], 255), width=width)

    def bracket_card(x1, y1, x2, y2, color, bl=28, bw=2):
        nonlocal img
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(gl)
        corners = [
            [(x1, y1 + bl), (x1, y1), (x1 + bl, y1)],
            [(x2 - bl, y1), (x2, y1), (x2, y1 + bl)],
            [(x1, y2 - bl), (x1, y2), (x1 + bl, y2)],
            [(x2 - bl, y2), (x2, y2), (x2, y2 - bl)],
        ]
        for pts in corners:
            gd.line(pts, fill=(*color[:3], 150), width=bw + 3)
        gl = gl.filter(ImageFilter.GaussianBlur(9))
        img = Image.alpha_composite(img, gl)
        img = Image.alpha_composite(img, gl)
        d = ImageDraw.Draw(img)
        d.rectangle([(x1 + 1, y1 + 1), (x2 - 1, y2 - 1)], fill=CARD_BG)
        for pts in corners:
            d.line(pts, fill=(*color[:3], 255), width=bw)

    # ── HEADER ────────────────────────────────────────────────────────────────
    if banner_img:
        img.paste(banner_img, (0, 0), banner_img)
    else:
        glow_line(PAD, 28, W // 2 - 220, 28, CYAN, radius=3)
        glow_line(W // 2 + 220, 28, W - PAD, 28, CYAN, radius=3)
        glow_text("RVR UNDERGROUND", (W // 2, 26), fnt["title"], CYAN, radius=22)

    draw = ImageDraw.Draw(img)
    sub_y = banner_h + 8
    draw.text((W // 2, sub_y), f"//  {cycle.upper()} MONTHLY CHAMPIONSHIP  //",
              fill=(*WHITE, 165), font=fnt["sub"], anchor="mt")
    glow_line(PAD, sub_y + 30, W - PAD, sub_y + 30, CYAN, radius=3)

    # ── GLOBAL TABLE HEADER ───────────────────────────────────────────────────
    podium_colors = [GOLD, SILVER, BRONZE]
    podium_labels = ["1ST PLACE", "2ND PLACE", "3RD PLACE"]

    ty = header_h + 10
    draw = ImageDraw.Draw(img)
    draw.text((COL_RANK,   ty + 8), "#",      fill=(*CYAN, 210), font=fnt["sec_hdr"])
    draw.text((COL_PLAYER, ty + 8), "PLAYER", fill=(*CYAN, 210), font=fnt["sec_hdr"])
    draw.text((COL_PTS,    ty + 8), "PTS",    fill=(*CYAN, 210), font=fnt["sec_hdr"], anchor="rt")
    glow_line(PAD, ty + TABLE_HDR_H - 4, W - PAD, ty + TABLE_HDR_H - 4, CYAN, radius=2)

    # ── TOP 3 CARDS ───────────────────────────────────────────────────────────
    y = header_h + TABLE_HDR_H + 14
    for i, p in enumerate(top3):
        color  = podium_colors[i]
        pc     = pts_color(p["points"], max_points)
        card_h = TOP3_CARD_H[i]
        x1, y1, x2, y2 = PAD, y, W - PAD, y + card_h

        bracket_card(x1, y1, x2, y2, color, bl=30, bw=2)

        draw = ImageDraw.Draw(img)
        # Rank number aligned to # column
        draw.text((COL_RANK,      y1 + card_h // 2 - 20), f"#{i+1}", fill=(*color, 255), font=fnt["pts_top3"], anchor="lm")
        # Place label small, above name
        draw.text((COL_PLAYER,    y1 + 12), podium_labels[i],  fill=(*color, 200), font=fnt["place_lbl"])
        # Player name
        tint_w = 0.75 if i != 1 else 0.50
        name_tint = tuple(int(WHITE[j] * tint_w + color[j] * (1 - tint_w)) for j in range(3))
        draw.text((COL_PLAYER,    y1 + 48), p["user"],         fill=(*name_tint, 255), font=fnt["name_top3"])
        # Shine highlight pass
        shine = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(shine).text((COL_PLAYER, y1 + 44), p["user"], fill=(255, 255, 255, 160), font=fnt["name_top3"])
        shine = shine.filter(ImageFilter.GaussianBlur(2))
        img = Image.alpha_composite(img, shine)
        draw = ImageDraw.Draw(img)
        # Medal shape (clasp bar on top + disc with rank number) after name
        name_w    = draw.textlength(p["user"], font=fnt["name_top3"])
        disc_r    = 20 if i == 0 else 16
        clasp_w   = 12
        clasp_h   = 9
        medal_cx  = int(COL_PLAYER + name_w + 18 + disc_r)
        name_mid  = y1 + 48 + fnt["name_top3"].size // 2
        disc_cy   = name_mid + 4
        clasp_x1  = medal_cx - clasp_w // 2
        clasp_y1  = disc_cy - disc_r - clasp_h - 1
        clasp_x2  = medal_cx + clasp_w // 2
        clasp_y2  = disc_cy - disc_r + 2
        # Glow layer
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(gl)
        gd.rectangle([(clasp_x1, clasp_y1), (clasp_x2, clasp_y2)], fill=(*color[:3], 160))
        gd.ellipse([(medal_cx - disc_r, disc_cy - disc_r), (medal_cx + disc_r, disc_cy + disc_r)], fill=(*color[:3], 160))
        gl = gl.filter(ImageFilter.GaussianBlur(9))
        img = Image.alpha_composite(img, gl)
        draw = ImageDraw.Draw(img)
        # Clasp bar
        draw.rectangle([(clasp_x1, clasp_y1), (clasp_x2, clasp_y2)], fill=(*color[:3], 255))
        # Disc
        draw.ellipse([(medal_cx - disc_r, disc_cy - disc_r), (medal_cx + disc_r, disc_cy + disc_r)],
                     fill=(*color[:3], 255), outline=(*WHITE, 160), width=2)
        # Rank number inside disc
        draw.text((medal_cx, disc_cy), str(i + 1), fill=(10, 10, 10, 255), font=fnt["place_lbl"], anchor="mm")
        # Points right-aligned to PTS column, just number
        draw.text((COL_PTS,       y1 + card_h // 2), str(p["points"]), fill=(*pc, 255), font=fnt["pts_top3"], anchor="rm")

        y += card_h + TOP3_GAP

    # ── OTHER FINISHERS ───────────────────────────────────────────────────────
    if others:
        oy = header_h + top3_h + 10
        glow_line(PAD, oy, W - PAD, oy, CYAN, radius=2)
        draw = ImageDraw.Draw(img)
        oy += 20

        box_h = len(others) * OTHER_ROW_H + 6
        bracket_card(PAD, oy - 4, W - PAD, oy + box_h, CYAN, bl=20, bw=1)
        draw = ImageDraw.Draw(img)

        for idx, p in enumerate(others):
            rank   = idx + 3
            place  = idx + 4
            pc     = pts_color(p["points"], max_points)
            ry     = oy + idx * OTHER_ROW_H
            mid_y  = ry + OTHER_ROW_H // 2

            if idx > 0:
                draw.line([(PAD + 14, ry), (W - PAD - 14, ry)], fill=(*DIV, 255), width=1)
            if idx % 2 == 1:
                draw.rectangle([(PAD + 2, ry + 1), (W - PAD - 2, ry + OTHER_ROW_H - 2)],
                               fill=(10, 18, 38))

            draw.text((COL_RANK,   mid_y), f"#{place}",      fill=(*GRAY,  255), font=fnt["name_rest"], anchor="lm")
            draw.text((COL_PLAYER, mid_y), p["user"],        fill=(*WHITE, 255), font=fnt["name_rest"], anchor="lm")
            draw.text((COL_PTS,    mid_y), str(p["points"]), fill=(*pc,    255), font=fnt["pts_rest"],  anchor="rm")

    # ── FOOTER ────────────────────────────────────────────────────────────────
    glow_line(PAD, H - 14, W - PAD, H - 14, CYAN, radius=2)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


def generate_leaderboard_image(cycle: str, ranked: list, rank_deltas: dict | None = None) -> io.BytesIO:
    import random
    W   = 1000
    PAD = 44

    BG_TOP  = (2,    8,  22)
    BG_BOT  = (5,    3,  26)
    CYAN    = (0,  200, 255)
    GOLD    = (255, 200,   0)
    SILVER  = (140, 190, 240)
    BRONZE  = (190, 105,  40)
    WHITE   = (255, 255, 255)
    GRAY    = (80,  105, 140)
    CARD_BG = (5,   12,  28)
    DIV     = (28,  48,  78)

    fnt = {
        "title":     _load_font(True,  83),
        "sub":       _load_font(False, 24),
        "place_lbl": _load_font(True,  33),
        "name_top3": _load_font(True,  53),
        "pts_top3":  _load_font(True,  56),
        "name_rest": _load_font(True,  35),
        "pts_rest":  _load_font(True,  38),
        "sec_hdr":   _load_font(True,  30),
        "ftr":       _load_font(False, 17),
    }

    # ── Load banner ───────────────────────────────────────────────────────────
    banner_img  = None
    banner_h    = 0
    MAX_BANNER_H = 200
    banner_path  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.png")
    try:
        raw       = Image.open(banner_path).convert("RGBA")
        bw, bh    = raw.size
        full_h    = int(W * bh / bw)
        resized   = raw.resize((W, full_h), Image.LANCZOS)
        if full_h > MAX_BANNER_H:
            crop_top   = (full_h - MAX_BANNER_H) // 2
            banner_img = resized.crop((0, crop_top, W, crop_top + MAX_BANNER_H))
            banner_h   = MAX_BANNER_H
        else:
            banner_img = resized
            banner_h   = full_h
    except Exception:
        pass

    COL_RANK       = PAD + 20
    COL_ARROW      = PAD + 116
    COL_PLAYER     = PAD + 158
    COL_PTS        = W - PAD - 20
    TABLE_HDR_H    = 52

    total          = len(ranked)
    max_points     = ranked[0]["points"] if ranked else 1
    top3           = ranked[:3]
    others         = ranked[3:]
    TOP3_CARD_H    = [158, 120, 120]
    TOP3_GAP       = 10
    OTHER_ROW_H    = 66
    subtitle_h     = 48
    header_h       = banner_h + subtitle_h + 16
    top3_h         = TABLE_HDR_H + sum(TOP3_CARD_H[:len(top3)]) + (len(top3) - 1) * TOP3_GAP + 16
    others_h       = (len(others) * OTHER_ROW_H + 30) if others else 0
    footer_h       = 24
    H = header_h + top3_h + others_h + footer_h

    # ── Base + gradient ───────────────────────────────────────────────────────
    img = Image.new("RGBA", (W, H), (*BG_TOP, 255))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        c = tuple(int(BG_TOP[i] + t * (BG_BOT[i] - BG_TOP[i])) for i in range(3))
        draw.line([(0, y), (W - 1, y)], fill=(*c, 255))

    # ── Circuit board traces ──────────────────────────────────────────────────
    rng     = random.Random(1337)
    GRID    = 50
    circuit = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cdraw   = ImageDraw.Draw(circuit)
    grid    = {}
    for col in range(W // GRID + 2):
        for row in range(H // GRID + 2):
            if rng.random() < 0.58:
                grid[(col, row)] = (
                    col * GRID + rng.randint(-6, 6),
                    row * GRID + rng.randint(-6, 6),
                )
    for (col, row), (x, y) in grid.items():
        if (col + 1, row) in grid and rng.random() < 0.40:
            nx, ny = grid[(col + 1, row)]
            cdraw.line([(x, y), (nx, ny)], fill=(0, 140, 255, 100), width=1)
        if (col, row + 1) in grid and rng.random() < 0.40:
            nx, ny = grid[(col, row + 1)]
            cdraw.line([(x, y), (nx, ny)], fill=(0, 140, 255, 100), width=1)
        if rng.random() < 0.20:
            r = rng.randint(1, 3)
            cdraw.ellipse([(x - r, y - r), (x + r, y + r)], fill=(0, 210, 255, 140))
    img = Image.alpha_composite(img, circuit)

    # ── Scanlines ─────────────────────────────────────────────────────────────
    scan  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scan)
    for sy in range(0, H, 3):
        sdraw.line([(0, sy), (W, sy)], fill=(0, 0, 0, 7), width=1)
    img = Image.alpha_composite(img, scan)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def glow_text(text, pos, font, color, radius=16, anchor="mt"):
        nonlocal img
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(gl).text(pos, text, fill=(*color[:3], 170), font=font, anchor=anchor)
        gl = gl.filter(ImageFilter.GaussianBlur(radius))
        img = Image.alpha_composite(img, gl)
        img = Image.alpha_composite(img, gl)
        ImageDraw.Draw(img).text(pos, text, fill=(*color[:3], 255), font=font, anchor=anchor)

    def glow_line(x1, y1, x2, y2, color, width=1, radius=4):
        nonlocal img
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(gl).line([(x1, y1), (x2, y2)], fill=(*color[:3], 200), width=width + 2)
        gl = gl.filter(ImageFilter.GaussianBlur(radius))
        img = Image.alpha_composite(img, gl)
        ImageDraw.Draw(img).line([(x1, y1), (x2, y2)], fill=(*color[:3], 255), width=width)

    def bracket_card(x1, y1, x2, y2, color, bl=28, bw=2):
        nonlocal img
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(gl)
        corners = [
            [(x1, y1 + bl), (x1, y1), (x1 + bl, y1)],
            [(x2 - bl, y1), (x2, y1), (x2, y1 + bl)],
            [(x1, y2 - bl), (x1, y2), (x1 + bl, y2)],
            [(x2 - bl, y2), (x2, y2), (x2, y2 - bl)],
        ]
        for pts in corners:
            gd.line(pts, fill=(*color[:3], 150), width=bw + 3)
        gl = gl.filter(ImageFilter.GaussianBlur(9))
        img = Image.alpha_composite(img, gl)
        img = Image.alpha_composite(img, gl)
        d = ImageDraw.Draw(img)
        d.rectangle([(x1 + 1, y1 + 1), (x2 - 1, y2 - 1)], fill=CARD_BG)
        for pts in corners:
            d.line(pts, fill=(*color[:3], 255), width=bw)

    def draw_arrow(cx, cy, delta):
        d = ImageDraw.Draw(img)
        s = 11
        if delta > 0:
            color = (30, 220, 80, 255)
            d.polygon([(cx, cy - s), (cx - s, cy + s // 2), (cx + s, cy + s // 2)], fill=color)
        elif delta < 0:
            color = (220, 60, 60, 255)
            d.polygon([(cx, cy + s), (cx - s, cy - s // 2), (cx + s, cy - s // 2)], fill=color)
        else:
            color = (110, 130, 155, 255)
            d.rectangle([(cx - s, cy - 3), (cx + s, cy + 3)], fill=color)

    # ── HEADER ────────────────────────────────────────────────────────────────
    if banner_img:
        img.paste(banner_img, (0, 0), banner_img)
    else:
        glow_line(PAD, 28, W // 2 - 220, 28, CYAN, radius=3)
        glow_line(W // 2 + 220, 28, W - PAD, 28, CYAN, radius=3)
        glow_text("RVR UNDERGROUND", (W // 2, 26), fnt["title"], CYAN, radius=22)

    draw = ImageDraw.Draw(img)
    sub_y = banner_h + 8
    draw.text((W // 2, sub_y), f"//  {cycle.upper()} LIVE STANDINGS  //",
              fill=(*WHITE, 165), font=fnt["sub"], anchor="mt")
    glow_line(PAD, sub_y + 30, W - PAD, sub_y + 30, CYAN, radius=3)

    # ── GLOBAL TABLE HEADER ───────────────────────────────────────────────────
    podium_colors = [GOLD, SILVER, BRONZE]
    podium_labels = ["1ST PLACE", "2ND PLACE", "3RD PLACE"]

    ty = header_h + 10
    draw = ImageDraw.Draw(img)
    draw.text((COL_RANK,   ty + 8), "#",      fill=(*CYAN, 210), font=fnt["sec_hdr"])
    draw.text((COL_PLAYER, ty + 8), "PLAYER", fill=(*CYAN, 210), font=fnt["sec_hdr"])
    draw.text((COL_PTS,    ty + 8), "PTS",    fill=(*CYAN, 210), font=fnt["sec_hdr"], anchor="rt")
    glow_line(PAD, ty + TABLE_HDR_H - 4, W - PAD, ty + TABLE_HDR_H - 4, CYAN, radius=2)

    # ── TOP 3 CARDS ───────────────────────────────────────────────────────────
    y = header_h + TABLE_HDR_H + 14
    for i, p in enumerate(top3):
        color  = podium_colors[i]
        pc     = pts_color(p["points"], max_points)
        card_h = TOP3_CARD_H[i]
        x1, y1, x2, y2 = PAD, y, W - PAD, y + card_h
        mid_y  = y1 + card_h // 2

        bracket_card(x1, y1, x2, y2, color, bl=30, bw=2)

        draw = ImageDraw.Draw(img)
        draw.text((COL_RANK,   mid_y - 20), f"#{i+1}", fill=(*color, 255), font=fnt["pts_top3"], anchor="lm")
        draw_arrow(COL_ARROW, mid_y - 20, rank_deltas.get(p.get("uid"), 0) if rank_deltas else 0)
        draw.text((COL_PLAYER, y1 + 12), podium_labels[i], fill=(*color, 200), font=fnt["place_lbl"])
        tint_w = 0.75 if i != 1 else 0.50
        name_tint = tuple(int(WHITE[j] * tint_w + color[j] * (1 - tint_w)) for j in range(3))
        draw.text((COL_PLAYER, y1 + 48), p["user"],        fill=(*name_tint, 255), font=fnt["name_top3"])
        # Shine highlight pass
        shine = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(shine).text((COL_PLAYER, y1 + 44), p["user"], fill=(255, 255, 255, 160), font=fnt["name_top3"])
        shine = shine.filter(ImageFilter.GaussianBlur(2))
        img = Image.alpha_composite(img, shine)
        draw = ImageDraw.Draw(img)
        draw.text((COL_PTS,    mid_y),   str(p["points"]), fill=(*pc,        255), font=fnt["pts_top3"], anchor="rm")

        y += card_h + TOP3_GAP

    # ── OTHER FINISHERS ───────────────────────────────────────────────────────
    if others:
        oy = header_h + top3_h + 10
        glow_line(PAD, oy, W - PAD, oy, CYAN, radius=2)
        draw = ImageDraw.Draw(img)
        oy += 20

        box_h = len(others) * OTHER_ROW_H + 6
        bracket_card(PAD, oy - 4, W - PAD, oy + box_h, CYAN, bl=20, bw=1)
        draw = ImageDraw.Draw(img)

        for idx, p in enumerate(others):
            rank  = idx + 3
            place = idx + 4
            pc    = pts_color(p["points"], max_points)
            ry    = oy + idx * OTHER_ROW_H
            mid_y = ry + OTHER_ROW_H // 2

            if idx > 0:
                draw.line([(PAD + 14, ry), (W - PAD - 14, ry)], fill=(*DIV, 255), width=1)
            if idx % 2 == 1:
                draw.rectangle([(PAD + 2, ry + 1), (W - PAD - 2, ry + OTHER_ROW_H - 2)], fill=(10, 18, 38))

            draw.text((COL_RANK,   mid_y), f"#{place}",      fill=(*GRAY,  255), font=fnt["name_rest"], anchor="lm")
            draw_arrow(COL_ARROW, mid_y, rank_deltas.get(p.get("uid"), 0) if rank_deltas else 0)
            draw.text((COL_PLAYER, mid_y), p["user"],         fill=(*WHITE, 255), font=fnt["name_rest"], anchor="lm")
            draw.text((COL_PTS,    mid_y), str(p["points"]),  fill=(*pc,    255), font=fnt["pts_rest"],  anchor="rm")

    # ── FOOTER ────────────────────────────────────────────────────────────────
    glow_line(PAD, H - 14, W - PAD, H - 14, CYAN, radius=2)

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

    now = datetime.now(timezone.utc)
    if now.month == 12:
        next_month_name = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc).strftime("%B")
    else:
        next_month_name = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc).strftime("%B")

    winner_name = ranked[0]["user"] if ranked else "nobody"

    announcement = (
        f"<:RVRU:1495544256444633198> The **{cycle}** Months Championship is over! <:RVRU:1495544256444633198>\n"
        f"The dust has settled, the times are locked in — **{winner_name}** 👑 takes the crown this month.\n"
        f"See you on the track in **{next_month_name}**.\n\n"
        f"*(preview — nothing has been closed yet)*"
    )

    img_buf = generate_results_image(cycle, ranked)
    await ctx.send(content=announcement, file=discord.File(img_buf, filename="preview.png"))

    # Preview what the medals table will look like after closing
    preview_medals = list(await medals_col.find().to_list(None))
    # Simulate adding this month's podium on top of existing records
    medal_keys = ["gold", "silver", "bronze"]
    preview_map = {m["uid"]: dict(m) for m in preview_medals}
    for idx, p in enumerate(ranked[:3]):
        uid = p["uid"]
        if uid not in preview_map:
            preview_map[uid] = {"uid": uid, "user": p["user"], "gold": 0, "silver": 0, "bronze": 0}
        preview_map[uid][medal_keys[idx]] = preview_map[uid].get(medal_keys[idx], 0) + 1
        preview_map[uid]["user"] = p["user"]

    def medal_sort_key(m):
        return (m.get("gold", 0), m.get("silver", 0), m.get("bronze", 0))
    sorted_medals = sorted(preview_map.values(), key=medal_sort_key, reverse=True)

    medals_text = ""
    for idx, m in enumerate(sorted_medals):
        parts = []
        if m.get("gold",   0): parts.append(f"🥇 x{m['gold']}")
        if m.get("silver", 0): parts.append(f"🥈 x{m['silver']}")
        if m.get("bronze", 0): parts.append(f"🥉 x{m['bronze']}")
        medals_text += f"`#{idx + 1}` **{m['user']}** — {' '.join(parts)}\n"

    medals_embed = discord.Embed(
        title="🏅 All-Time Podium Record (preview after close)",
        description=medals_text or "*No records yet.*",
        color=0xFFD700
    )
    await ctx.send(embed=medals_embed)


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

    # Build next month name for the closing message
    now = datetime.now(timezone.utc)
    if now.month == 12:
        next_month_name = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc).strftime("%B")
    else:
        next_month_name = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc).strftime("%B")

    months_role   = discord.utils.get(ctx.guild.roles, name="Months")
    role_ping     = months_role.mention if months_role else ""
    winner_mention = f"<@{ranked[0]['uid']}>" if ranked else "nobody"

    if ranked:
        img_buf = generate_results_image(cycle, ranked)
        announcement = (
            f"{role_ping} <:RVRU:1495544256444633198> The **{cycle}** Months Championship is over! <:RVRU:1495544256444633198>\n"
            f"The dust has settled, the times are locked in — {winner_mention} 👑 takes the crown this month.\n"
            f"See you on the track in **{next_month_name}**."
        )
        await results_ch.send(content=announcement, file=discord.File(img_buf, filename="results.png"))
    else:
        await results_ch.send(
            f"{role_ping} <:RVRU:1495544256444633198> The **{cycle}** Months Championship is over! <:RVRU:1495544256444633198>\n"
            f"No times were submitted this month — see you in **{next_month_name}**."
        )

    if tracks_text:
        await results_ch.send(embed=tracks_embed)

    # Update all-time medals
    medal_keys = ["gold", "silver", "bronze"]
    for idx, p in enumerate(ranked[:3]):
        await medals_col.update_one(
            {"uid": p["uid"]},
            {"$inc": {medal_keys[idx]: 1}, "$set": {"user": p["user"]}},
            upsert=True
        )

    # Post all-time medals table
    all_medals = await medals_col.find().to_list(None)
    def medal_sort_key(m):
        return (m.get("gold", 0), m.get("silver", 0), m.get("bronze", 0))
    all_medals.sort(key=medal_sort_key, reverse=True)

    medals_text = ""
    for idx, m in enumerate(all_medals):
        parts = []
        if m.get("gold",   0): parts.append(f"🥇 x{m['gold']}")
        if m.get("silver", 0): parts.append(f"🥈 x{m['silver']}")
        if m.get("bronze", 0): parts.append(f"🥉 x{m['bronze']}")
        medals_text += f"`#{idx + 1}` **{m['user']}** — {' '.join(parts)}\n"

    medals_embed = discord.Embed(
        title="🏅 All-Time Podium Record",
        description=medals_text or "*No records yet.*",
        color=0xFFD700
    )
    await results_ch.send(embed=medals_embed)

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

@bot.command(name="listmembers")
@commands.has_permissions(manage_guild=True)
async def list_members(ctx):
    members = [m for m in ctx.guild.members if not m.bot]
    members.sort(key=lambda m: m.display_name.lower())
    names = "\n".join(m.display_name for m in members)
    await ctx.author.send(f"**Server members ({len(members)}):**\n```\n{names}\n```")
    await ctx.message.delete()


@bot.command(name="cleanratings")
@commands.has_permissions(manage_guild=True)
async def clean_ratings(ctx):
    result = await ratings_col.delete_many({"rating": {"$gt": 1.5}})
    await ctx.author.send(f"✅ Removed {result.deleted_count} bad rating entries.")
    await ctx.message.delete()


@bot.command(name="seedratings")
@commands.has_permissions(manage_guild=True)
async def seed_ratings(ctx):
    for name, rating in SEED_RATINGS:
        await ratings_col.update_one(
            {"user_lower": name.lower()},
            {"$set": {"user": name, "user_lower": name.lower(), "rating": rating}},
            upsert=True
        )
    await ctx.author.send(f"✅ Seeded {len(SEED_RATINGS)} player ratings.")
    await ctx.message.delete()


@bot.command(name="setrating")
@commands.has_permissions(manage_guild=True)
async def set_rating(ctx, member: discord.Member, rating: float):
    await ratings_col.update_one(
        {"uid": member.id},
        {"$set": {"uid": member.id, "user": member.display_name, "user_lower": member.display_name.lower(), "rating": rating}},
        upsert=True
    )
    await ctx.send(f"✅ **{member.display_name}** rated **{rating}**.")


@bot.command(name="ratings")
@commands.has_permissions(manage_guild=True)
async def show_ratings(ctx):
    all_ratings = await ratings_col.find().sort("rating", -1).to_list(None)
    if not all_ratings:
        await ctx.author.send("No ratings set yet.")
        return
    lines = "\n".join(f"`#{i+1}` **{r['user']}** — {r['rating']}" for i, r in enumerate(all_ratings))
    embed = discord.Embed(title="⭐ Player Ratings", description=lines, color=0x00cfff)
    await ctx.author.send(embed=embed)
    await ctx.message.delete()


@bot.command(name="maketeams")
@commands.has_permissions(manage_guild=True)
async def make_teams(ctx):
    vc = discord.utils.get(ctx.guild.voice_channels, name=GATHER_CHANNEL)
    if not vc:
        await ctx.send(f"❌ Voice channel `{GATHER_CHANNEL}` not found.")
        return

    members = [m for m in vc.members if not m.bot]
    if len(members) < 2:
        await ctx.send("❌ Not enough players in Gather.")
        return

    # Fetch ratings for all members (try uid first, then display name)
    players = []
    for m in members:
        doc = await ratings_col.find_one({"uid": m.id})
        if not doc:
            doc = await ratings_col.find_one({"user_lower": m.display_name.lower()})
        rating = doc["rating"] if doc else DEFAULT_RATING
        players.append({"user": m.display_name, "uid": m.id, "rating": rating, "rated": bool(doc)})

    # Sort by rating descending, snake draft into 2 teams
    players.sort(key=lambda p: p["rating"], reverse=True)
    team1, team2 = [], []
    for i, p in enumerate(players):
        if i % 4 in (0, 3):
            team1.append(p)
        else:
            team2.append(p)

    def fmt_team(team):
        return "\n".join(f"**{p['user']}**" for p in team)

    embed = discord.Embed(title="🏎️ Teams", color=0x00cfff)
    embed.add_field(name=f"🔵 Team 1 ({len(team1)} players)", value=fmt_team(team1), inline=True)
    embed.add_field(name=f"🔴 Team 2 ({len(team2)} players)", value=fmt_team(team2), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="wordle", aliases=["w"])
async def wordle_cmd(ctx):
    """Launch RVR-Wordle web app with auto-login"""
    import jwt
    import requests
    
    # Generate JWT token for user
    import datetime
    payload = {
        "uid": ctx.author.id,
        "username": ctx.author.name,
        "avatar": str(ctx.author.avatar.url) if ctx.author.avatar else None,
        "source": "bot",
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    }
    token = jwt.encode(payload, os.environ.get("JWT_SECRET", "fallback_secret"), algorithm="HS256")
    
    # Send to Wordle backend to store/verify
    try:
        backend_url = os.environ.get("WORDLE_BACKEND_URL", "http://localhost:3001")
        response = requests.post(
            f"{backend_url}/api/generate-token",
            json={"uid": ctx.author.id, "username": ctx.author.name},
            timeout=5
        )
        if response.status_code == 200:
            web_token = response.json().get("token")
            if web_token:
                # Create ephemeral message with Play button
                embed = discord.Embed(
                    title="🎮 RVR-Wordle",
                    description="Click below to play the multiplayer Wordle game!",
                    color=discord.Color.blue()
                )
                backend_url = os.environ.get("WORDLE_BACKEND_URL", "http://localhost:3001")
                embed.add_field(
                    name="🏎️ Play Now",
                    value=f"[Click here to play]({backend_url}?token={web_token})",
                    inline=False
                )
                embed.set_footer(text="Token expires in 1 hour")
                await ctx.send(embed=embed, ephemeral=True)
            else:
                await ctx.send("❌ Failed to generate game token", ephemeral=True)
        else:
            await ctx.send("❌ Wordle server unavailable", ephemeral=True)
    except Exception as e:
        await ctx.send("❌ Failed to connect to Wordle server", ephemeral=True)

@bot.command(name="wordlestats")
async def wordlestats_cmd(ctx, member: discord.Member = None):
    """Show Wordle stats for user"""
    if not member:
        member = ctx.author
    
    # Get user stats from Wordle database
    user_stats = await wordle_users_col.find_one({"uid": member.id})
    
    if not user_stats:
        embed = discord.Embed(
            title=f"📊 Wordle Stats for {member.name}",
            description="No Wordle games played yet!",
            color=discord.Color.orange()
        )
        embed.add_field(name="🎮 Start Playing", value="Type `!wordle` to begin!", inline=False)
        await ctx.send(embed=embed)
        return
    
    stats = user_stats.get("stats", {})
    classic = stats.get("classic", {})
    race = stats.get("race", {})
    battle = stats.get("battle", {})
    timeattack = stats.get("timeattack", {})
    
    embed = discord.Embed(
        title=f"📊 Wordle Stats for {member.name}",
        color=discord.Color.purple()
    )
    
    # Classic stats
    embed.add_field(
        name="🎯 Classic Mode",
        value=f"**{classic.get('wins', 0)}/{classic.get('played', 0)}** wins\n🔥 Streak: {classic.get('streak', 0)}",
        inline=True
    )
    
    # Race stats
    embed.add_field(
        name="🏁 Race Mode",
        value=f"**{race.get('wins', 0)}/{race.get('played', 0)}** wins\n🎯 First Bloods: {race.get('firstBloods', 0)}",
        inline=True
    )
    
    # Battle stats
    embed.add_field(
        name="⚔️ Battle Mode",
        value=f"**{battle.get('wins', 0)}/{battle.get('played', 0)}** wins\n🛢️ Oil Used: {battle.get('oilUsed', 0)}",
        inline=True
    )
    
    # Time attack stats
    embed.add_field(
        name="⏱️ Time Attack",
        value=f"**{timeattack.get('played', 0)}** games played",
        inline=True
    )
    
    # Achievements
    achievements = user_stats.get("achievements", [])
    if achievements:
        achievement_text = " ".join([f"🏆" for _ in achievements])
        embed.add_field(name="🏆 Achievements", value=achievement_text, inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name="wordleboard")
async def wordleboard_cmd(ctx, mode: str = "today"):
    """Show Wordle leaderboard"""
    # Get today's data
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = await wordle_daily_col.find_one({"date": today})
    
    if not daily:
        await ctx.send("❌ No Wordle data for today")
        return
    
    embed = discord.Embed(
        title=f"🏆 Wordle Leaderboard - {mode.title()}",
        color=discord.Color.gold()
    )
    
    if mode.lower() == "classic":
        solves = daily.get("modes", {}).get("classic", {}).get("solves", [])
        if solves:
            # Sort by guesses first, then by time
            def sort_key(x):
                guesses = x.get("guesses", 6)
                time_val = x.get("time", float('inf'))
                return (guesses, time_val)
            solves.sort(key=sort_key)
            
            for i, solve in enumerate(solves[:10]):
                user = await bot.fetch_user(solve["uid"])
                username = user.name if user else f"User {solve['uid']}"
                embed.add_field(
                    name=f"#{i+1} {username}",
                    value=f"**{solve.get('guesses', 6)}** guesses",
                    inline=True
                )
        else:
            embed.description = "No classic solves today"
    
    elif mode.lower() == "race":
        players = daily.get("modes", {}).get("race", {}).get("players", [])
        solved_players = [p for p in players if p.get("solved", False)]
        if solved_players:
            # Sort by solve time
            def race_sort_key(x):
                return x.get("solveTime", float('inf'))
            solved_players.sort(key=race_sort_key)
            
            for i, player in enumerate(solved_players[:10]):
                user = await bot.fetch_user(player["uid"])
                username = user.name if user else f"User {player['uid']}"
                embed.add_field(
                    name=f"#{i+1} {username}",
                    value=f"**{player.get('guesses', 6)}** guesses ⏱️ {player.get('solveTime', 0)//1000}s",
                    inline=True
                )
        else:
            embed.description = "No race winners today"
    
    elif mode.lower() == "timeattack":
        entries = daily.get("modes", {}).get("timeattack", {}).get("entries", [])
        if entries:
            # Sort by time in milliseconds
            def time_sort_key(x):
                return x.get("timeMs", float('inf'))
            entries.sort(key=time_sort_key)
            
            for i, entry in enumerate(entries[:10]):
                user = await bot.fetch_user(entry["uid"])
                username = user.name if user else f"User {entry['uid']}"
                time_sec = entry.get("timeMs", 0) / 1000
                embed.add_field(
                    name=f"#{i+1} {username}",
                    value=f"⏱️ **{time_sec:.2f}s** ({entry.get('guesses', 0)} guesses)",
                    inline=True
                )
        else:
            embed.description = "No time attack entries today"
    
    else:
        embed.description = "Available modes: `today`, `classic`, `race`, `timeattack`"
    
    await ctx.send(embed=embed)

@bot.command(name="rvrhelp")
async def rvr_help(ctx):
    embed = discord.Embed(title="🤖 RVR Bot Commands", color=discord.Color.blurple())
    embed.add_field(name="🏎️ Wordle Commands", value="**!wordle** - Play multiplayer Wordle game\n**!wordlestats [@user]** - Show Wordle stats\n**!wordleboard [mode]** - Show Wordle leaderboard", inline=False)
    embed.add_field(name="!leaderboard (or !lb)", value="Show the full leaderboard for the current month", inline=False)
    embed.add_field(name="!mystats",              value="Show your personal stats for the current month", inline=False)
    embed.add_field(name="!tracks",               value="List all tracks with times this month", inline=False)
    embed.add_field(name="── Admin only ──",      value="\u200b", inline=False)
    embed.add_field(name="!previewmonth",                value="Preview what this month's results will look like (no changes made)", inline=False)
    embed.add_field(name="!closemonth",                  value="Close the current monthly cycle, post results to #monthly-results, and start a new cycle", inline=False)
    embed.add_field(name="!removetrack <track>",         value="Remove a track and all its times (current cycle)", inline=False)
    embed.add_field(name="!removetime @player <track>",  value="Remove a player's time from a track (current cycle)", inline=False)
    embed.add_field(name="!setrating @player <1-10>",    value="Set a player's skill rating for team balancing", inline=False)
    embed.add_field(name="!maketeams",                   value="Auto-split players in Gather VC into 2 balanced teams", inline=False)
    embed.add_field(name="!ratings",                     value="Show all player ratings", inline=False)
    embed.add_field(
        name="── Submitting a time ──",
        value="Post in #time-submissions with a screenshot:\n`Track: Toys In The Hood | Time: 1:23.456`",
        inline=False
    )
    await ctx.send(embed=embed)

bot.run(TOKEN)
