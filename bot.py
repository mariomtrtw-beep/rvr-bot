import discord
from discord.ext import commands
import os
import random
import re
import io
import asyncio
import signal
import sys
import traceback
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# Shared with the result ingest so a name normalises identically on both sides.
from rvgl_results import name_key, REQUIRED_LAPS
from coordinator_ingest import (ALLOW_PICKUPS, apply_rules as apply_race_rules,
                                fetch_session, ms_to_time, races_from)
from coordinator_probe import read_frames, sessions_from
import rvr_scoring as scoring
import beef_judge

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN     = os.environ["DISCORD_TOKEN"]
MONGO_URL = os.environ["MONGO_URL"]

TEAM_RACE_CHANNEL       = "team-race-chat"


# ── MongoDB (async) ───────────────────────────────────────────────────────────
mongo_client = AsyncIOMotorClient(MONGO_URL)
db           = mongo_client["rvr_underground"]
ratings_col  = db["ratings"]
aliases_col  = db["aliases"]      # in-game name -> discord user
skips_col    = db["link_skips"]   # names deliberately left unlinked
races_col    = db["races"]        # every race ingested, one doc per GameID
boards_col   = db["track_boards"] # the posted message we edit per track
state_col    = db["bot_state"]    # small key/value bits, e.g. the armed session
driver_stats_col = db["driver_stats"]   # cached overall score/title per discord user
known_sessions_col = db["known_sessions"] # private session ids we've been given before
beef_col     = db["beef_stats"]   # roast battle record per discord user

GATHER_CHANNEL   = "Gather"
DEV_CHANNEL      = "development"
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

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot     = commands.Bot(command_prefix="!", intents=intents)

# Serialises anything that reads-then-writes a board or the standings post -
# without it, two overlapping !fetch/!refresh/!standings calls (two people
# at once, or a double-click) can both read the old state before either
# writes, producing two Discord messages for the same track/standings.
board_lock = asyncio.Lock()

# Guard against two bot processes holding the same Discord token at once
# (the "every command answered twice" incident this exists to catch).
#
# Newest-wins, deliberately. The obvious design - first process to claim the
# lock keeps it, newcomers refuse to start - is exactly wrong for a hosted
# rolling deploy: the platform starts the replacement before stopping the
# old one, so the replacement always loses and exits, the platform then
# kills the old one anyway, and nothing is left running. Instead a starting
# process always takes the lock, and the incumbent notices it lost the lease
# on its next heartbeat and exits itself.
INSTANCE_ID        = uuid.uuid4().hex[:12]
INSTANCE_HEARTBEAT = 5    # seconds between lease renewals
INSTANCE_LOCK_TTL  = 20   # a lease older than this means that process is gone
# How long a newcomer waits for a live incumbent to notice and exit before
# connecting to Discord. Must exceed the incumbent's heartbeat interval, or
# both would be connected at once and answer commands twice - the whole
# thing this guards against. Skipped entirely when there is no incumbent.
INSTANCE_HANDOVER_WAIT = INSTANCE_HEARTBEAT * 2 + 2

def is_staff(member) -> bool:
    """True if the member is an admin (manage_guild) or has the moderator role."""
    if member.guild_permissions.manage_guild:
        return True
    if any(r.name.lower() == "moderator" for r in member.roles):
        return True
    return False

def admin_or_dev():
    async def predicate(ctx):
        if ctx.channel.name == DEV_CHANNEL:
            return True
        return is_staff(ctx.author)
    return commands.check(predicate)

# ── Where commands may be used ────────────────────────────────────────────────
@bot.check
async def commands_channel_only(ctx):
    """Keep the leaderboard and activity feeds free of command chatter.

    Nothing is enforced until a commands channel is configured, so it is not
    possible to lock everyone out by setting the others first.
    """
    if ctx.guild is None:
        return False                              # DMs have no channels or roles
    if getattr(ctx.channel, "name", None) == DEV_CHANNEL:
        return True
    doc = await state_col.find_one({"key": "channels"})
    allowed = (doc or {}).get("ids", {}).get("commands")
    if allowed is None:
        return True
    return ctx.channel.id == allowed


@bot.event
async def on_command_error(ctx, error):
    # A command used in the wrong channel should be ignored, not shouted about
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.send(f"❌ {error}")
        return

    # Anything else used to vanish into the console, so a command that died
    # halfway just went quiet. Say so where it was typed.
    original = getattr(error, "original", error)
    if isinstance(original, discord.Forbidden):
        await ctx.send("❌ I am missing permissions there — I need **View Channel**, "
                       "**Send Messages**, **Embed Links** and **Read Message History** "
                       "in the leaderboard and activity channels.")
    else:
        await ctx.send(f"❌ `!{ctx.command}` failed: "
                       f"`{original.__class__.__name__}: {original}`"[:1900])
    traceback.print_exception(type(original), original, original.__traceback__)


def _lease_age(doc: dict) -> float:
    """Seconds since that lease was last renewed.

    Mongo/BSON round-trips datetimes without tzinfo, so a heartbeat read back
    is naive even though it was written as UTC - subtracting it from an aware
    `now` raises TypeError on the mismatch rather than on any real problem.
    """
    heartbeat = doc.get("heartbeat")
    if heartbeat is None:
        return float("inf")
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - heartbeat).total_seconds()


async def _claim_instance_lock() -> bool:
    """Take the lock unconditionally. Returns whether a live incumbent held it.

    Always succeeds - see the newest-wins note above. The return value only
    says whether someone else was still actively renewing the lease, which
    tells the caller whether it has to wait for that process to stand down
    before connecting to Discord.

    Must run before the gateway connection opens. discord.py dispatches
    commands as soon as it connects, independently of on_ready, so a process
    that defers this check until on_ready can already have answered several
    commands by the time it runs.
    """
    doc = await state_col.find_one({"key": "instance_lock"})
    incumbent = bool(doc
                     and doc.get("owner") not in (None, INSTANCE_ID)
                     and _lease_age(doc) < INSTANCE_LOCK_TTL)
    await state_col.update_one(
        {"key": "instance_lock"},
        {"$set": {"key": "instance_lock", "owner": INSTANCE_ID,
                  "heartbeat": datetime.now(timezone.utc)}},
        upsert=True)
    return incumbent


async def _release_instance_lock() -> None:
    """Drop the lease on a clean shutdown, so the next process starts at once
    instead of waiting out the handover delay for a process already gone.
    """
    try:
        await state_col.delete_one({"key": "instance_lock", "owner": INSTANCE_ID})
    except Exception:
        pass          # shutting down anyway; the lease expires on its own


async def _instance_heartbeat_loop() -> None:
    """Renew our lease, and stand down the moment a newer process takes it."""
    while True:
        await asyncio.sleep(INSTANCE_HEARTBEAT)
        doc = await state_col.find_one({"key": "instance_lock"})
        if doc and doc.get("owner") != INSTANCE_ID:
            print("⛔ A newer instance took over - exiting.", flush=True)
            os._exit(0)      # already connected; a clean exit can hang, this cannot
        await state_col.update_one(
            {"key": "instance_lock"},
            {"$set": {"heartbeat": datetime.now(timezone.utc)}})


_instance_loop_started = False


# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    print("🥩 beef judge: " + ("Claude enabled" if beef_judge.available()
                               else "no ANTHROPIC_API_KEY - battles will be crowd-voted"),
          flush=True)
    global _instance_loop_started, _auto_session_loop_started
    if not _instance_loop_started:
        _instance_loop_started = True
        asyncio.create_task(_instance_heartbeat_loop())
    if not _auto_session_loop_started:
        _auto_session_loop_started = True
        asyncio.create_task(_auto_session_loop())

    # One name can only belong to one player
    await aliases_col.create_index("name_key", unique=True)
    await skips_col.create_index("name_key", unique=True)
    # One race is ingested once, however many times we re-read the session
    await races_col.create_index("game_id", unique=True)
    await boards_col.create_index("track", unique=True)
    await driver_stats_col.create_index("uid", unique=True)


# ── Commands ──────────────────────────────────────────────────────────────────


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


@bot.command(name="resetseason")
@admin_or_dev()
async def resetseason_cmd(ctx, confirm: str = ""):
    """Wipe every stored race, board and standing - !resetseason confirm

    For starting the 13-track season clean. Deletes the actual #times and
    standings messages too, best effort, not just the database rows - nothing
    is left behind for someone to notice and wonder about.
    """
    if confirm.lower() != "confirm":
        races_n  = await races_col.count_documents({})
        boards_n = await boards_col.count_documents({})
        drivers_n = await driver_stats_col.count_documents({})
        await ctx.send(
            f"⚠️ This deletes **{races_n} race(s)**, **{boards_n} track board(s)** and "
            f"**{drivers_n} scored driver(s)** for good, including the posted messages.\n"
            f"Run `!resetseason confirm` to actually do it.")
        return

    deleted_messages = 0
    async for board in boards_col.find({}, {"channel_id": 1, "message_id": 1}):
        if not board.get("channel_id") or not board.get("message_id"):
            continue
        channel = ctx.guild.get_channel(board["channel_id"])
        if channel:
            try:
                msg = await channel.fetch_message(board["message_id"])
                await msg.delete()
                deleted_messages += 1
            except (discord.NotFound, discord.Forbidden):
                pass

    standings_doc = await state_col.find_one({"key": "standings_board"})
    if standings_doc and standings_doc.get("channel_id") and standings_doc.get("message_id"):
        channel = ctx.guild.get_channel(standings_doc["channel_id"])
        if channel:
            try:
                msg = await channel.fetch_message(standings_doc["message_id"])
                await msg.delete()
                deleted_messages += 1
            except (discord.NotFound, discord.Forbidden):
                pass

    races_n   = (await races_col.delete_many({})).deleted_count
    boards_n  = (await boards_col.delete_many({})).deleted_count
    drivers_n = (await driver_stats_col.delete_many({})).deleted_count
    await state_col.delete_one({"key": "standings_board"})

    await ctx.send(f"✅ Wiped **{races_n} race(s)**, **{boards_n} track board(s)**, "
                   f"**{drivers_n} scored driver(s)**, and deleted {deleted_messages} "
                   f"posted message(s). The 13-track season starts clean from here.")


@bot.command(name="listmembers")
@admin_or_dev()
async def list_members(ctx):
    members = [m for m in ctx.guild.members if not m.bot]
    members.sort(key=lambda m: m.display_name.lower())

    width = max((len(m.display_name) for m in members), default=0)
    lines = [f"{m.display_name:<{width}}  {m.id}" for m in members]

    await ctx.author.send(f"**Server members ({len(members)})** — id on the right, "
                          f"usable with `!link <ingame name> <id>`")
    # Long rosters do not fit in one message
    chunk: list[str] = []
    for line in lines:
        if sum(len(x) + 1 for x in chunk) + len(line) > 1800:
            await ctx.author.send("```\n" + "\n".join(chunk) + "\n```")
            chunk = []
        chunk.append(line)
    if chunk:
        await ctx.author.send("```\n" + "\n".join(chunk) + "\n```")

    await ctx.message.delete()


@bot.command(name="cleanratings")
@admin_or_dev()
async def clean_ratings(ctx):
    result = await ratings_col.delete_many({"rating": {"$gt": 1.5}})
    await ctx.author.send(f"✅ Removed {result.deleted_count} bad rating entries.")
    await ctx.message.delete()


@bot.command(name="seedratings")
@admin_or_dev()
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
@admin_or_dev()
async def set_rating(ctx, member: discord.Member, rating: float):
    await ratings_col.update_one(
        {"uid": member.id},
        {"$set": {"uid": member.id, "user": member.display_name, "user_lower": member.display_name.lower(), "rating": rating}},
        upsert=True
    )
    await ctx.send(f"✅ **{member.display_name}** rated **{rating}**.")


@bot.command(name="ratings")
@admin_or_dev()
async def show_ratings(ctx):
    all_ratings = await ratings_col.find().sort("rating", -1).to_list(None)
    if not all_ratings:
        await ctx.author.send("No ratings set yet.")
        return
    lines = "\n".join(f"`#{i+1}` **{r['user']}** — {r['rating']}" for i, r in enumerate(all_ratings))
    embed = discord.Embed(title="⭐ Player Ratings", description=lines, color=0x00cfff)
    await ctx.author.send(embed=embed)
    await ctx.message.delete()


# ── Name linking ──────────────────────────────────────────────────────────────
def _strip_mentions(text: str) -> str:
    return re.sub(r"<@[!&]?\d+>", "", text).strip()


_SNOWFLAKE = re.compile(r"\b(\d{17,20})\b")


def find_member(guild, needle: str):
    """A member whose display name or username matches, or None."""
    key = name_key(needle)
    if not key:
        return None
    for m in guild.members:
        if not m.bot and key in (name_key(m.display_name), name_key(m.name)):
            return m
    return None


def split_member_and_name(ctx, args: str):
    """Separate the Discord user from the in-game name in a !link argument.

    Accepts a mention, a raw user id, or a username as the final word, because
    private channels do not offer @ autocomplete for members who are not in them.
    """
    if ctx.message.mentions:
        return ctx.message.mentions[0], _strip_mentions(args)

    snowflake = _SNOWFLAKE.search(args)
    if snowflake:
        member = ctx.guild.get_member(int(snowflake.group(1)))
        rest = (args[:snowflake.start()] + args[snowflake.end():]).strip()
        return member, rest

    parts = args.split()
    if len(parts) >= 2:
        member = find_member(ctx.guild, parts[-1])
        if member:
            return member, " ".join(parts[:-1]).strip()
    return None, args.strip()


async def resolve_uid(ingame_name: str) -> int | None:
    """Discord id behind an in-game name, or None if nobody has claimed it."""
    doc = await aliases_col.find_one({"name_key": name_key(ingame_name)})
    return doc["uid"] if doc else None


@bot.command(name="link")
@admin_or_dev()
async def link_cmd(ctx, *, args: str = ""):
    """Link an in-game name to a Discord user: !link SHIGEKIX @Shigekix

    The name may contain spaces and the two arguments work in either order.
    """
    member, raw = split_member_and_name(ctx, args)
    if member is None:
        await ctx.send(
            "❌ Could not work out which Discord user you meant. Any of these work:\n"
            "`!link SHIGEKIX @Shigekix`  (mention)\n"
            "`!link SHIGEKIX 123456789012345678`  (user id — use `!listmembers` to get ids)\n"
            "`!link SHIGEKIX shigekix`  (their Discord username as the last word)")
        return

    key = name_key(raw)
    if not key:
        await ctx.send("❌ Give the in-game name exactly as it appears in the results, "
                       "e.g. `!link JN 2002 @someone`")
        return
    if raw.isdigit():
        await ctx.send(f"❌ `{raw}` looks like a user id, not an in-game name. "
                       f"Put the name first: `!link <ingame name> {raw}`")
        return

    existing = await aliases_col.find_one({"name_key": key})
    if existing and existing["uid"] != member.id:
        await ctx.send(f"⚠️ **{raw}** is already linked to <@{existing['uid']}>. "
                       f"Run `!unlink {raw}` first if that is wrong.")
        return

    await aliases_col.update_one(
        {"name_key": key},
        {"$set": {
            "name_key":  key,
            "name_raw":  raw,
            "uid":       member.id,
            "user":      member.display_name,
            "linked_by": ctx.author.id,
            "linked_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    await ctx.send(f"✅ Linked **{raw}** → {member.mention}")


@bot.command(name="unlink")
@admin_or_dev()
async def unlink_cmd(ctx, *, ingame_name: str):
    doc = await aliases_col.find_one_and_delete({"name_key": name_key(_strip_mentions(ingame_name))})
    if not doc:
        await ctx.send(f"❌ No link found for **{ingame_name}**.")
        return
    await ctx.send(f"🗑️ Unlinked **{doc.get('name_raw', ingame_name)}** (was <@{doc['uid']}>).")


@bot.command(name="links")
@admin_or_dev()
async def links_cmd(ctx):
    docs = await aliases_col.find().to_list(None)
    if not docs:
        await ctx.send("No names linked yet — use `!link <ingame name> @user`.")
        return

    by_uid: dict[int, list[str]] = {}
    for d in docs:
        by_uid.setdefault(d["uid"], []).append(d.get("name_raw", d["name_key"]))

    lines = "\n".join(f"<@{uid}> — " + ", ".join(f"`{n}`" for n in sorted(names))
                      for uid, names in by_uid.items())
    embed = discord.Embed(
        title=f"🔗 Linked names ({len(docs)} across {len(by_uid)} players)",
        description=lines[:4000],
        color=0x00cfff,
    )
    await ctx.send(embed=embed)


# ── Channels ──────────────────────────────────────────────────────────────────
CHANNEL_ROLES = ("leaderboard", "times", "activity", "commands")


async def channel_ids() -> dict:
    doc = await state_col.find_one({"key": "channels"})
    return (doc or {}).get("ids", {})


async def get_channel(guild, role: str):
    """The channel configured for a role, or None if unset or deleted."""
    cid = (await channel_ids()).get(role)
    return guild.get_channel(cid) if cid else None


async def announce(guild, text: str) -> None:
    """Post to the activity feed, if one is configured."""
    channel = await get_channel(guild, "activity")
    if channel:
        await channel.send(text)


@bot.command(name="setchannel")
@admin_or_dev()
async def setchannel_cmd(ctx, role: str = "", channel: discord.TextChannel = None):
    """!setchannel leaderboard #leaderboard"""
    role = role.lower().strip()
    if role not in CHANNEL_ROLES:
        await ctx.send(f"❌ Usage: `!setchannel <{'|'.join(CHANNEL_ROLES)}> #channel`")
        return
    channel = channel or ctx.channel
    ids = await channel_ids()
    ids[role] = channel.id
    await state_col.update_one({"key": "channels"},
                               {"$set": {"key": "channels", "ids": ids}}, upsert=True)
    await ctx.send(f"✅ **{role}** is now {channel.mention}.")


@bot.command(name="channels")
@admin_or_dev()
async def channels_cmd(ctx):
    ids = await channel_ids()
    lines = []
    for role in CHANNEL_ROLES:
        cid = ids.get(role)
        channel = ctx.guild.get_channel(cid) if cid else None
        lines.append(f"**{role}** — " + (channel.mention if channel else "*not set*"))
    embed = discord.Embed(title="📺 Channels", description="\n".join(lines), color=0x00cfff)
    embed.set_footer(text="!setchannel <role> #channel")
    await ctx.send(embed=embed)


# ── Race results ──────────────────────────────────────────────────────────────
SESSION_ID_RE = re.compile(r"([0-9a-f]{32})")


async def store_races(session: dict, races: list) -> tuple[list, int]:
    """Save races we have not seen before. Returns (new races, already had)."""
    fresh, already = [], 0
    for race in races:
        if race.finished_at is None:
            continue                      # still being played
        if not race.game_id:
            # races_from already filters these out - this is defense in depth
            # against the exact failure mode that guard exists for: a
            # GameID-less race matching every other GameID-less race in the
            # upsert filter below and silently overwriting/skipping instead
            # of storing.
            print(f"⚠️ store_races: race with no game_id, skipping: {race.track!r}")
            continue

        # Only the 13 scored tracks are tracked at all now - mutated in place
        # so the Mongo doc and the race object fetch_cmd reports from (and
        # decides which #times board to refresh) always agree.
        if race.counted and not scoring.is_canonical_track(race.track):
            race.counted = False
            race.reject_reason = "not one of the 13 scored tracks"
        elif scoring.is_canonical_track(race.track):
            # The coordinator's own casing varies ("SuperMarket 2") from ours
            # ("Supermarket 2") - normalise to one string so every board and
            # query agrees on what a track is called, not just what track it is.
            race.track = scoring.TRACK_DISPLAY[scoring.track_key(race.track)]

        doc = {
            "game_id":    race.game_id,
            "session_id": session.get("ID"),
            "session":    session.get("Name"),
            "number":     race.number,
            "track":      race.track,
            "track_dir":  race.track_dir,
            "laps":       race.laps,
            "pickups":    race.pickups,
            "counted":    race.counted,
            "reject":     race.reject_reason,
            "finished_at": race.finished_at,
            "entries": [{
                "name_raw":    e.name_raw,
                "name_key":    e.name_key,
                "car":         e.car,
                "time_ms":     e.time_ms,
                "best_lap_ms": e.best_lap_ms,
                "counted":     e.counted,
                "reject":      e.reject_reason,
            } for e in race.entries],
            "ingested_at": datetime.now(timezone.utc),
        }
        result = await races_col.update_one({"game_id": race.game_id},
                                            {"$setOnInsert": doc}, upsert=True)
        if result.upserted_id is not None:
            fresh.append(race)
        else:
            already += 1
    return fresh, already


async def link_map() -> dict:
    """name_key -> discord uid, for everyone linked."""
    out = {}
    async for a in aliases_col.find({}, {"name_key": 1, "uid": 1}):
        out[a["name_key"]] = a["uid"]
    return out


# ── Overall standings (13-track score and title) ────────────────────────────

def _paste_icon_at(img, icon, left_x: int, mid_y: int, size: int = 26):
    """Place `icon` centered within a fixed [left_x, left_x+size) slot,
    vertically centered on `mid_y`.

    Centered within the slot, not flush against left_x - aspect-preserving
    thumbnail() means two icons with different width:height ratios (a wide
    crest vs a round coin, say) end up different actual widths even at the
    same `size`, so pasting both flush-left would still visibly drift
    row to row despite using the same anchor. Centering in a fixed-width
    slot keeps every tier's icon looking like one column regardless of its
    own shape.
    """
    if icon is None:
        return
    if icon.width > size or icon.height > size:
        icon = icon.copy()
        icon.thumbnail((size, size), Image.LANCZOS)
    icon_x = int(left_x + (size - icon.width) / 2)
    icon_y = int(mid_y - icon.height / 2)
    img.paste(icon, (icon_x, icon_y), icon)


def _load_tier_background(tier: str, size: tuple):
    """A hand-made bg_<tier>.png from the project root, filled to exactly
    `size` - or None if that tier has no custom art yet, same fallback
    philosophy as the tier emoji: missing just means "use the plain look".

    Scaled to cover the canvas and center-cropped, never stretched, so an
    image with a different aspect ratio than the card doesn't distort.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"bg_{tier.lower()}.png")
    try:
        img = Image.open(path).convert("RGB")
    except (FileNotFoundError, OSError):
        return None
    W, H = size
    src_w, src_h = img.size
    scale = max(W / src_w, H / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    x, y = (new_w - W) // 2, (new_h - H) // 2
    return img.crop((x, y, x + W, y + H))


def _shade_row(img, x1: int, y1: int, x2: int, y2: int,
               color: tuple = (10, 18, 38), alpha: float = 0.55):
    """Darken a row for zebra striping without fully erasing whatever
    background sits under it - a plain opaque rectangle here would blank out
    the glow/watermark/custom art on every other row instead of just tinting it.
    """
    region = img.crop((x1, y1, x2, y2))
    tint = Image.new("RGB", region.size, color)
    img.paste(Image.blend(region, tint, alpha), (x1, y1))


def _tier_glow_background(tier: str, size: tuple, icon) -> "Image.Image":
    """Automatic per-rank styling for when no hand-made bg_<tier>.png exists:
    a soft glow in the tier's color, a huge faint watermark of its own icon,
    and a thin accent border - built from data already on hand, no art needed.
    """
    W, H = size
    color = scoring.TIER_COLOR.get(tier, scoring.UNRANKED_COLOR)
    BG_TOP, BG_BOT = (2, 8, 22), (5, 3, 26)

    img = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        c = tuple(int(BG_TOP[i] + t * (BG_BOT[i] - BG_TOP[i])) for i in range(3))
        draw.line([(0, y), (W - 1, y)], fill=c)

    # Built small and scaled up, not drawn pixel-by-pixel at full card size.
    GS = 160
    glow = Image.new("L", (GS, GS), 0)
    gdraw = ImageDraw.Draw(glow)
    for r in range(GS // 2, 0, -1):
        gdraw.ellipse([GS / 2 - r, GS / 2 - r, GS / 2 + r, GS / 2 + r],
                     fill=int(130 * (1 - r / (GS / 2)) ** 2))
    glow = glow.resize((W, int(H * 0.6)), Image.LANCZOS)
    img.paste(Image.new("RGB", glow.size, color), (0, int(H * 0.1)), glow)

    # A huge, faint version of the player's own rank icon as background
    # texture, if their server has uploaded one - this is the "bigger rank
    # symbol" a tiny inline icon can't be without crowding the header line.
    if icon is not None:
        wm = icon.convert("RGBA").copy()
        wm.thumbnail((int(W * 0.62), int(W * 0.62)), Image.LANCZOS)
        alpha = wm.split()[3].point(lambda a: int(a * 0.16))
        wm.putalpha(alpha)
        img.paste(wm, ((W - wm.width) // 2, int(H * 0.27)), wm)

    ImageDraw.Draw(img).rectangle([(0, 0), (W - 1, H - 1)], outline=color, width=3)
    return img


def generate_standings_image(rows: list[dict], icons: dict | None = None) -> io.BytesIO:
    """rows: sorted by title then score_ms (see compute_all_driver_stats), each with name/score_ms/overall_tier/coverage.
    icons: optional {tier: PIL.Image} from get_tier_icons(), for servers with
    custom tier emoji uploaded - falls back to text-only for any tier missing.

    Every value column is right-aligned with its own fixed right edge, sized to
    the widest word that can appear in it ("Unranked" is the long pole) so
    columns cannot run into each other regardless of content.
    """
    icons = icons or {}
    W, PAD = 1000, 44
    ROW_H, HEADER_H, FOOTER_H = 67, 280, 42     # header grew for the logo + title stack
    H = HEADER_H + len(rows) * ROW_H + FOOTER_H

    BG_TOP, BG_BOT = (2, 8, 22), (5, 3, 26)
    WHITE, GRAY, DIV, CYAN = (255, 255, 255), (140, 150, 170), (28, 48, 78), (0, 200, 255)

    # Plain gradient only - no custom board art. A roster of a handful of
    # racers vs. dozens makes the board's height wildly variable, and a single
    # static image cover-cropped to that range does not hold up seamlessly.
    img = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        c = tuple(int(BG_TOP[i] + t * (BG_BOT[i] - BG_TOP[i])) for i in range(3))
        draw.line([(0, y), (W - 1, y)], fill=c)

    # A soft glow behind the title, tinted to whoever is currently #1 - the
    # leaderboard's tone shifts with the leader's rank instead of always
    # looking the same regardless of who's on top. Built as a single small
    # blurred dot rather than hand-drawn rings, so the falloff is smooth and
    # spread wide instead of reading as a hard-edged circle.
    leader_tier = rows[0]["overall_tier"] if rows else None
    glow_color = scoring.TIER_COLOR.get(leader_tier, scoring.UNRANKED_COLOR)
    GS = 200
    dot = Image.new("L", (GS, GS), 0)
    ImageDraw.Draw(dot).ellipse([GS * 0.35, GS * 0.35, GS * 0.65, GS * 0.65], fill=255)
    dot = dot.filter(ImageFilter.GaussianBlur(GS * 0.22))
    glow_h = int(HEADER_H * 1.6)
    glow = dot.resize((int(W * 0.75), glow_h), Image.LANCZOS)
    glow = glow.point(lambda a: int(a * 0.4))
    img.paste(Image.new("RGB", glow.size, glow_color), ((W - glow.width) // 2, -int(glow_h * 0.25)), glow)
    draw = ImageDraw.Draw(img)

    fnt_title = _load_font(True, 34)      # title line below the logo
    fnt_hdr   = _load_font(True, 22)      # was 18
    fnt_row   = _load_font(True, 30)      # was 24
    fnt_small = _load_font(False, 20)     # was 16

    # Logo above a text title, not instead of it - the logo alone read as too
    # bare without any words on the board itself.
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
    try:
        logo = Image.open(logo_path).convert("RGBA")
    except (FileNotFoundError, OSError):
        logo = None

    if logo is not None:
        target_h = 150
        scale = target_h / logo.height
        logo = logo.resize((max(1, round(logo.width * scale)), target_h), Image.LANCZOS)
        img.paste(logo, ((W - logo.width) // 2, 10), logo)
        title_y = 10 + target_h + 6
    else:
        title_y = 30

    draw.text((W // 2, title_y), "RVRU B3L RANKINGS", fill=CYAN, font=fnt_title, anchor="mt")
    subtitle_y = title_y + 46
    draw.text((W // 2, subtitle_y), "13 stock tracks · summed best time · lower is better",
              fill=GRAY, font=fnt_small, anchor="mt")

    COL_POS, COL_NAME = PAD, PAD + 70
    COL_TRACKS  = W - PAD
    COL_TITLE   = COL_TRACKS - 130
    COL_SCORE   = COL_TITLE - 240
    # gaps re-measured at the bigger font: Unranked=138px, 655.393=111px, plus
    # room for a ~38px tier icon between the score and title columns

    hdr_y = 246
    draw.text((COL_POS,   hdr_y), "#",      fill=CYAN, font=fnt_hdr)
    draw.text((COL_NAME,  hdr_y), "DRIVER", fill=CYAN, font=fnt_hdr)
    draw.text((COL_SCORE, hdr_y), "SCORE",  fill=CYAN, font=fnt_hdr, anchor="ra")
    draw.text((COL_TITLE, hdr_y), "TITLE",  fill=CYAN, font=fnt_hdr, anchor="ra")
    draw.text((COL_TRACKS,hdr_y), "TRACKS", fill=CYAN, font=fnt_hdr, anchor="ra")
    draw.line([(PAD, HEADER_H - 10), (W - PAD, HEADER_H - 10)], fill=DIV, width=1)

    # Icons sit at one fixed left edge down the whole column, sized off the
    # widest tier word ("Hustler") - not measured per-row - so every icon
    # lines up regardless of how long that row's own tier word is.
    icon_size = 38
    icon_gap = 10
    max_word_w = max(draw.textlength(t, font=fnt_row) for t in scoring.TIER_ORDER)
    icon_x = int(COL_TITLE - max_word_w - icon_gap - icon_size)

    for idx, row in enumerate(rows):
        y = HEADER_H + idx * ROW_H
        mid = y + ROW_H // 2
        tier = row["overall_tier"]
        color = scoring.TIER_COLOR.get(tier, scoring.UNRANKED_COLOR)
        # Every row is tinted with its own title color - not just every other
        # one - so a tier never goes unhighlighted just by landing on an even
        # row. Alternating alpha keeps a subtle zebra read without any row
        # going fully untinted.
        row_tint = tuple(max(6, int(c * 0.32)) for c in color)
        _shade_row(img, PAD, y, W - PAD, y + ROW_H, row_tint,
                  alpha=0.8 if idx % 2 == 1 else 0.5)

        draw.text((COL_POS, mid), f"#{idx + 1}", fill=GRAY, font=fnt_row, anchor="lm")
        draw.text((COL_NAME, mid), row["name"], fill=WHITE, font=fnt_row, anchor="lm")
        draw.text((COL_SCORE, mid), scoring.format_score(row["score_ms"]),
                  fill=color, font=fnt_row, anchor="rm")
        draw.text((COL_TITLE, mid), tier, fill=color, font=fnt_row, anchor="rm")
        _paste_icon_at(img, icons.get(tier), icon_x, mid, size=icon_size)
        draw.text((COL_TRACKS, mid), f"{row['coverage']}/{row['total_tracks']}",
                  fill=GRAY, font=fnt_small, anchor="rm")

    draw.line([(PAD, H - FOOTER_H + 8), (W - PAD, H - FOOTER_H + 8)], fill=DIV, width=1)
    draw.text((W // 2, H - FOOTER_H + 14), "Title is earned from points per track - no need to race all 13",
              fill=GRAY, font=fnt_small, anchor="mt")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def generate_card_image(name: str, result: dict, best_times: dict,
                        icons: dict | None = None) -> io.BytesIO:
    """One player's time and title on every one of the 13 tracks."""
    icons = icons or {}
    W, PAD = 700, 36
    ROW_H, HEADER_H, FOOTER_H = 53, 215, 33     # +~25% over the original sizing
    tracks = scoring.CANONICAL_TRACK_KEYS
    H = HEADER_H + len(tracks) * ROW_H + FOOTER_H

    BG_TOP, BG_BOT = (2, 8, 22), (5, 3, 26)
    WHITE, GRAY, DIV = (255, 255, 255), (140, 150, 170), (28, 48, 78)
    HDR_COLOR = (215, 218, 225)   # was cyan - column labels read as neutral now

    # Custom art per rank, e.g. bg_legend.png in the project root, sized to
    # exactly (W, H) - always 700x740 for a 13-track card. Falls back to the
    # plain gradient for any rank with no file yet, so this can be filled in
    # one tier at a time.
    custom_bg = _load_tier_background(result["overall_tier"], (W, H))
    if custom_bg is not None:
        # Darken so text drawn on top stays legible regardless of how bright
        # or busy the supplied artwork is - the layout was designed against a
        # near-black background and every text color assumes that contrast.
        img = Image.new("RGB", (W, H), BG_TOP)
        img.paste(Image.blend(custom_bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.45))
    else:
        # No hand-made background for this rank - style one automatically
        # instead of the old flat gradient, using the tier's own color and icon.
        img = _tier_glow_background(result["overall_tier"], (W, H),
                                    (icons or {}).get(result["overall_tier"]))
    draw = ImageDraw.Draw(img)

    fnt_title    = _load_font(True, 40)      # was 32
    fnt_rank_big = _load_font(True, 36)      # the "Rank: X" line - its own big line now,
                                             # not sharing a row with the score/coverage detail
    fnt_detail   = _load_font(True, 19)      # the smaller "(N pts) · Score · tracks" line
    fnt_hdr      = _load_font(True, 19)      # was 15
    fnt_row      = _load_font(True, 24)      # was 19
    fnt_tier     = _load_font(True, 24)      # was 19
    fnt_small    = _load_font(False, 19)     # was 15

    overall = result["overall_tier"]
    # Unranked has no tier color of its own; TIER_COLOR.get's fallback used to
    # be a dim gray that all but disappeared against the near-black background.
    overall_color = scoring.TIER_COLOR.get(overall, scoring.UNRANKED_COLOR)
    draw.text((W // 2, 14), name, fill=WHITE, font=fnt_title, anchor="mt")

    # "Rank: <icon> <Tier>" is now its own big, standalone line - centered as
    # one unit even though the icon is a pasted image, not text, so its width
    # has to be measured and folded into the centering math like the rest.
    rank_y  = 66
    prefix  = "Rank: "
    icon    = icons.get(overall)
    ICON_H  = 42
    prefix_w  = draw.textlength(prefix, font=fnt_rank_big)
    tier_w    = draw.textlength(overall, font=fnt_rank_big)
    icon_span = (ICON_H + 8) if icon else 0
    x = (W - (prefix_w + icon_span + tier_w)) / 2

    draw.text((x, rank_y), prefix, fill=overall_color, font=fnt_rank_big, anchor="la")
    x += prefix_w
    if icon:
        _paste_icon_at(img, icon, int(x), int(rank_y + 22), size=ICON_H)
        x += ICON_H + 8
    draw.text((x, rank_y), overall, fill=overall_color, font=fnt_rank_big, anchor="la")

    # Smaller supporting line underneath: points, score, coverage
    detail = (f"{result['overall_points']} pts  ·  "
             f"Score {scoring.format_score(result['score_ms'])}"
             f"  ·  {result['coverage']}/{result['total_tracks']} tracks")
    draw.text((W // 2, 122), detail, fill=overall_color, font=fnt_detail, anchor="mt")

    COL_TRACK, COL_TIME = PAD, W - PAD - 180
    COL_RANK = W - PAD - 150     # left edge of the rank column, icon then text
    RANK_ICON_SLOT = 33          # reserved whether or not this row has an icon,
                                 # so every row's text starts at the same x

    hdr_y = 168
    draw.text((COL_TRACK, hdr_y), "TRACK", fill=HDR_COLOR, font=fnt_hdr)
    draw.text((COL_TIME,  hdr_y), "TIME",  fill=HDR_COLOR, font=fnt_hdr, anchor="ra")
    draw.text((COL_RANK,  hdr_y), "RANK",  fill=HDR_COLOR, font=fnt_hdr)
    draw.line([(PAD, HEADER_H - 10), (W - PAD, HEADER_H - 10)], fill=DIV, width=1)

    # A dark, muted version of the player's own rank color, not a fixed navy -
    # keeps the same hue as the border/glow instead of a color unrelated to it.
    # Scaled well down so it still reads as a subtle stripe, not a colored bar.
    row_tint = tuple(max(4, int(c * 0.16)) for c in overall_color)

    for idx, key in enumerate(tracks):
        y = HEADER_H + idx * ROW_H
        mid = y + ROW_H // 2
        if idx % 2 == 1:
            _shade_row(img, PAD, y, W - PAD, y + ROW_H, color=row_tint)

        tier = result["per_track_tier"].get(key)
        color = scoring.TIER_COLOR.get(tier, GRAY)
        has_time = key in best_times
        # A single dash lines up cleanly with the time column; the old phrase
        # "— not set —" had dashes on both ends and read as misaligned next
        # to plain digits even though both were right-anchored to the same x.
        time_txt = scoring.ms_to_time(best_times[key]) if has_time else "—"
        tier_txt = tier or "—"

        draw.text((COL_TRACK, mid), scoring.TRACK_DISPLAY[key], fill=WHITE, font=fnt_row, anchor="lm")
        draw.text((COL_TIME, mid), time_txt, fill=(GRAY if not has_time else color),
                  font=fnt_row, anchor="rm")
        # Left-aligned: icon at a fixed x, text always starts right after the
        # reserved icon slot - right-aligning both meant a short word like
        # "Elite" put its icon in a different spot than a long one like
        # "Hustler", so the icons never lined up down the column.
        icon = icons.get(tier)
        if icon:
            _paste_icon_at(img, icon, COL_RANK, mid, size=RANK_ICON_SLOT)
        draw.text((COL_RANK + RANK_ICON_SLOT + 6, mid), tier_txt, fill=color,
                  font=fnt_tier, anchor="lm")

    draw.line([(PAD, H - FOOTER_H + 2), (W - PAD, H - FOOTER_H + 2)], fill=DIV, width=1)
    draw.text((W // 2, H - FOOTER_H + 4), "Rank comes from points earned per track (Street=1 .. Legend=4)",
              fill=GRAY, font=fnt_small, anchor="mt")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def compute_all_driver_stats(guild) -> list[dict]:
    """Best time per canonical track per discord user, scored.

    A user's times are pooled across every in-game name linked to them, so
    switching names mid-season does not split their results in two.
    """
    links = await link_map()
    best: dict[int, dict[str, int]] = {}
    async for doc in races_col.find({"counted": True}, {"track": 1, "entries": 1}):
        if not scoring.is_canonical_track(doc["track"]):
            continue
        tkey = scoring.track_key(doc["track"])
        for e in doc["entries"]:
            if not e.get("counted"):
                continue
            uid = links.get(e["name_key"])
            if uid is None:
                continue                      # held until linked, same as everywhere else
            slot = best.setdefault(uid, {})
            if tkey not in slot or e["time_ms"] < slot[tkey]:
                slot[tkey] = e["time_ms"]

    out = []
    for uid, times in best.items():
        member = guild.get_member(uid)
        name = member.display_name if member else f"user {uid}"
        out.append({"uid": uid, "name": name, "best_times": times,
                    **scoring.score_driver(times)})
    # Title outranks raw score - a driver with an actual title should never
    # sit below someone Unranked just because their pooled time is lower.
    # Within the same title, faster score still wins.
    out.sort(key=lambda r: (-scoring.TIER_RANK.get(r["overall_tier"], 0), r["score_ms"]))
    return out


async def recompute_standings(guild) -> list[str]:
    """Recompute every driver's score/title, update the board, return activity events.

    Tier-up events only fire against a driver we already had a previous
    snapshot for - otherwise the very first run after this feature ships would
    announce a "promotion" for every single existing time.
    """
    rows = await compute_all_driver_stats(guild)
    if not rows:
        return []

    events = []
    now = datetime.now(timezone.utc)
    for row in rows:
        prev = await driver_stats_col.find_one({"uid": row["uid"]})
        if prev:
            # score_driver fills in every one of the 13 tracks, untouched ones
            # included (as tier None), so a missing key here does not exist -
            # only best_times tells us whether they had ever actually raced
            # this specific track before. Without this check, a player's
            # first-ever result on any new track "promotes" them the moment
            # they already have a driver_stats row from some other track.
            prev_times = prev.get("best_times") or {}
            for tkey, new_tier in row["per_track_tier"].items():
                if tkey not in prev_times:
                    continue
                old_tier = (prev.get("per_track_tier") or {}).get(tkey)
                if scoring.TIER_RANK.get(new_tier, 0) > scoring.TIER_RANK.get(old_tier, 0):
                    events.append(f"{tier_display(guild, new_tier)} <@{row['uid']}> reached "
                                 f"**{new_tier}** on **{scoring.TRACK_DISPLAY[tkey]}**")
            old_overall = prev.get("overall_tier")
            new_overall = row["overall_tier"]
            if (new_overall != scoring.UNRANKED
                    and scoring.TIER_RANK.get(new_overall, 0) >
                        scoring.TIER_RANK.get(None if old_overall == scoring.UNRANKED else old_overall, 0)):
                events.append(f"{tier_display(guild, new_overall)} <@{row['uid']}> is now "
                             f"**{new_overall}** overall!")

        await driver_stats_col.update_one(
            {"uid": row["uid"]},
            {"$set": {"uid": row["uid"], "name": row["name"], "score_ms": row["score_ms"],
                      "coverage": row["coverage"], "overall_tier": row["overall_tier"],
                      "overall_points": row["overall_points"],
                      "per_track_tier": row["per_track_tier"], "best_times": row["best_times"],
                      "updated_at": now}},
            upsert=True)

    await refresh_standings_board(guild, rows)
    return events


async def refresh_standings_board(guild, rows: list[dict]) -> None:
    """Delete the previous standings message and post a fresh one, only when
    it actually changed - never edited in place, so the board always reads as
    a clean new post rather than a silently-updated attachment.
    """
    channel = await get_channel(guild, "leaderboard")
    if not channel:
        return

    fingerprint = "|".join(f"{r['uid']}:{r['score_ms']}:{r['overall_tier']}" for r in rows)
    doc = await state_col.find_one({"key": "standings_board"})
    if doc and doc.get("fingerprint") == fingerprint:
        return

    if doc and doc.get("channel_id") == channel.id and doc.get("message_id"):
        try:
            old = await channel.fetch_message(doc["message_id"])
            await old.delete()
        except (discord.NotFound, discord.Forbidden):
            pass                              # already gone, or we cannot see it

    icons = await get_tier_icons(guild)
    buf = generate_standings_image(rows, icons)
    message = await channel.send(file=discord.File(buf, filename="standings.png"))
    await state_col.update_one(
        {"key": "standings_board"},
        {"$set": {"key": "standings_board", "channel_id": channel.id,
                  "message_id": message.id, "fingerprint": fingerprint}},
        upsert=True)


@bot.command(name="standings")
async def standings_cmd(ctx):
    """Recompute and repost the overall standings image."""
    async with board_lock:
        async with ctx.typing():
            events = await recompute_standings(ctx.guild)
        if not await get_channel(ctx.guild, "leaderboard"):
            await ctx.send("❌ No leaderboard channel set — `!setchannel leaderboard #channel`.")
            return
        for event in events:
            await announce(ctx.guild, event)
        await ctx.send("✅ Standings updated.")


@bot.command(name="card")
async def card_cmd(ctx, member: discord.Member = None):
    """Show a player's time and title on every track: !card [@player]"""
    member = member or ctx.author
    doc = await driver_stats_col.find_one({"uid": member.id})
    if not doc:
        await ctx.send(f"❓ No scored times for {member.mention} yet — "
                       f"race a session, get linked, then `!fetch` or `!standings`.")
        return

    result = {"score_ms": doc["score_ms"], "coverage": doc["coverage"],
             "total_tracks": len(scoring.CANONICAL_TRACK_KEYS),
             "overall_tier": doc["overall_tier"], "per_track_tier": doc["per_track_tier"],
             "overall_points": doc.get("overall_points", 0)}
    icons = await get_tier_icons(ctx.guild)
    buf = generate_card_image(doc["name"], result, doc["best_times"], icons)
    await ctx.send(file=discord.File(buf, filename="card.png"))


def actually_raced(entry) -> bool:
    """Did this person take part, or were they just sitting in the lobby?

    Every race lists everyone connected, so people who sat one out appear with
    zeroed times. Reporting them as "did not finish" names players who were
    never on the track.
    """
    return (entry.get("time_ms") or 0) > 0 or (entry.get("best_lap_ms") or 0) > 0


async def best_per_track(track: str) -> tuple[list, dict, dict]:
    """Each player's fastest run on a track, plus anyone not linked yet.

    A repeat run only replaces a stored time if it is faster, so a board moves
    only when someone sets a personal best.
    """
    want = scoring.track_key(track)
    best: dict[str, dict] = {}
    rejects: dict[str, dict] = {}
    # Match by normalised key, not exact string - documents stored before
    # store_races started canonicalising the track name still have whatever
    # casing the coordinator happened to send (e.g. "SuperMarket 2"), and an
    # exact match would silently find nothing for them.
    async for doc in races_col.find({"counted": True}, {"track": 1, "entries": 1}):
        if scoring.track_key(doc.get("track", "")) != want:
            continue
        for entry in doc["entries"]:
            if not entry.get("counted"):
                if not actually_raced(entry):
                    continue          # sat this race out, not a result at all
                # Remember why, in case nothing of theirs ever counts here
                reasons = rejects.setdefault(entry["name_key"],
                                             {"name_raw": entry["name_raw"], "why": {}})["why"]
                reason = entry.get("reject") or "not counted"
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            prev = best.get(entry["name_key"])
            if prev is None:
                best[entry["name_key"]] = {**entry, "runs": 1}
            else:
                prev["runs"] += 1
                if entry["time_ms"] < prev["time_ms"]:
                    prev.update({k: entry[k] for k in ("time_ms", "best_lap_ms", "car")})

    links   = await link_map()
    ranked  = sorted((b for k, b in best.items() if k in links), key=lambda b: b["time_ms"])
    waiting = {k: b["name_raw"] for k, b in best.items() if k not in links}
    # Only worth showing for people who got no usable time here at all
    excluded = {k: (v["name_raw"], max(v["why"], key=v["why"].get))
                for k, v in rejects.items() if k not in best}
    return [(links[b["name_key"]], b) for b in ranked], waiting, excluded


def tier_display(guild, tier: str) -> str:
    """Custom :street:/:hustler:/:elite:/:legend: emoji if the server has
    uploaded one, else the built-in unicode fallback - so this works before
    anyone uploads anything.
    """
    if guild:
        custom = discord.utils.get(guild.emojis, name=tier.lower())
        if custom:
            return str(custom)
    return scoring.TIER_EMOJI[tier]


# Keyed by (guild_id, tier, emoji_id) so re-uploading an emoji (new id) or
# moving servers naturally misses the cache instead of serving a stale icon.
_TIER_ICON_CACHE: dict[tuple, "Image.Image"] = {}


async def get_tier_icons(guild) -> dict[str, "Image.Image"]:
    """Pre-fetch each tier's custom emoji as a small RGBA image, for pasting
    into the rendered boards. A tier missing from the result just means "no
    custom icon uploaded for it" - the renderers fall back to text alone.

    Renderers stay synchronous, pure PIL functions with no network access of
    their own; this is the only part that talks to Discord, run once up front.
    """
    icons: dict[str, "Image.Image"] = {}
    if not guild:
        return icons
    for tier in scoring.TIER_ORDER:
        emoji = discord.utils.get(guild.emojis, name=tier.lower())
        if not emoji:
            continue
        key = (guild.id, tier, emoji.id)
        if key not in _TIER_ICON_CACHE:
            try:
                data = await emoji.read()
                icon = Image.open(io.BytesIO(data)).convert("RGBA")
                # Trim to the visible glyph before resizing - different emoji
                # exports carry different amounts of transparent padding in
                # their canvas, which otherwise left every tier's icon
                # sitting at a slightly different offset even when pasted at
                # the exact same x (e.g. Elite vs Hustler not lining up).
                bbox = icon.split()[-1].getbbox()
                if bbox:
                    icon = icon.crop(bbox)
                icon.thumbnail((40, 40), Image.LANCZOS)
                _TIER_ICON_CACHE[key] = icon
            except Exception:
                continue          # network hiccup - just skip it this render
        icons[tier] = _TIER_ICON_CACHE[key]
    return icons


def board_embed(track: str, ranked: list, guild=None) -> discord.Embed:
    """The leaderboard block: the ranking and nothing else.

    Held and excluded results are reported where !fetch was run, not here - the
    leaderboard channel stays readable and free of bookkeeping.
    """
    canonical = scoring.is_canonical_track(track)

    # Resolve display names once, purely to size the name column - padding
    # position+name to the longest one on this board so time/rank reads as a
    # right-hand block instead of a ragged wall of mentions. Best-effort only:
    # Discord renders mentions in a variable, non-monospace width that isn't
    # ours to control, so this cannot be pixel-perfect the way a code block
    # or rendered image can - that trade was made deliberately to keep
    # mentions clickable and pingable.
    names = {}
    for uid, _b in ranked:
        member = guild.get_member(uid) if guild else None
        names[uid] = member.display_name if member else f"user {uid}"
    max_name_len = max((len(n) for n in names.values()), default=0)

    lines = []
    for place, (uid, b) in enumerate(ranked, 1):
        # Plain numbers for finishing position - a medal here would collide
        # with the same medals meaning Street/Hustler/Elite in the tier text
        # right next to it, e.g. a 🥈 could mean "2nd" or "Hustler".
        position = f"`{place}.`"
        # Only the 13 scored tracks have tier thresholds - Rooftops and
        # customs get a time but no title, same as they get no overall score.
        # No tier text at all when a canonical time misses Street - "below
        # Street" was long enough to wrap mid-phrase in the embed, which read
        # as a rendering glitch rather than a real result.
        tier_txt = ""
        if canonical:
            tier = scoring.tier_for_time(b["time_ms"], track)
            if tier:
                tier_txt = f"  ·  {tier_display(guild, tier)} {tier}"
        pad = " " * (max_name_len - len(names[uid]) + 2)
        # Deliberately no run count: it would change the board on every race,
        # and this should only move when a time actually improves.
        lines.append(f"{position} <@{uid}>{pad}—  **`{ms_to_time(b['time_ms'])}`**{tier_txt}")

    body = "\n".join(lines) if lines else "*no times posted yet*"
    embed = discord.Embed(title=f"🏁 {track}", description=body, color=0x00cfff)
    embed.set_footer(text="updates when someone sets a personal best")
    return embed


async def why_nothing(track: str) -> str:
    """Plain reason a track produced no usable times, for the report."""
    want = scoring.track_key(track)
    reasons: dict[str, int] = {}
    async for doc in races_col.find({}, {"track": 1, "counted": 1, "reject": 1, "entries": 1}):
        if scoring.track_key(doc.get("track", "")) != want:
            continue
        if not doc.get("counted"):
            reasons[doc.get("reject") or "race not counted"] = \
                reasons.get(doc.get("reject") or "race not counted", 0) + 1
        else:
            for e in doc["entries"]:
                if not e.get("counted"):
                    r = e.get("reject") or "result not counted"
                    reasons[r] = reasons.get(r, 0) + 1
    if not reasons:
        return "no results"
    top = max(reasons, key=reasons.get)
    return top


def podium_events(track, ranked, old_bests, old_podium, new_podium,
                  had_board: bool, raced: set | None) -> list[str]:
    """Activity lines for a board that just changed.

    Only for boards we already had a podium for - the first time a track is
    posted, everyone is trivially "new" and none of it is news.
    """
    if not had_board or raced is None or not ranked:
        return []

    lines, by_key = [], {b["name_key"]: (uid, b) for uid, b in ranked}

    # P1 changed hands, or the holder beat their own record
    if new_podium and new_podium[0] != (old_podium[0] if old_podium else None):
        uid, best = by_key[new_podium[0]]
        if new_podium[0] in raced:
            beaten = old_podium[0] if old_podium else None
            margin = ""
            if beaten and beaten in old_bests:
                gap = (old_bests[beaten] - best["time_ms"]) / 1000
                margin = f" — beat <@{by_key[beaten][0]}> by `{gap:.3f}s`" \
                    if beaten in by_key else ""
            lines.append(f"🏆 <@{uid}> set a new **{SESSION_NAME} track record** on "
                         f"**{track}**\n`{ms_to_time(best['time_ms'])}`{margin}")

    # Someone new on the podium who was not there before
    for place, key in enumerate(new_podium[1:], start=2):
        if key in old_podium[:3] or key not in raced or key not in by_key:
            continue
        uid, best = by_key[key]
        lines.append(f"{['🥈','🥉'][place-2]} <@{uid}> moved into **P{place}** on "
                     f"**{track}** — `{ms_to_time(best['time_ms'])}`")
    return lines


async def refresh_board(channel, track: str, raced: set | None = None,
                        events: list | None = None, force: bool = False) -> str:
    """Create or edit this track's block. Returns a plain sentence about it.

    `raced` is the set of name keys that appear in the races just ingested. Only
    they can be announced as setting a best - somebody who did not play tonight
    must never be named, whatever the stored board happens to say. Anything worth
    posting to the activity feed is appended to `events`.

    `force` reposts even when nothing changed - needed whenever some other
    track's board is about to move to the bottom of the channel, so this one
    moves down with it and the fixed 13-track order stays intact. Without it,
    an unrelated board's repost would be the only thing appended, leaving
    this one behind at its old position - exactly the bug this exists for.
    """
    events = events if events is not None else []
    ranked, waiting, excluded = await best_per_track(track)
    empty = not ranked and not waiting

    embed       = board_embed(track, ranked, guild=channel.guild)
    fingerprint = embed.description
    board       = await boards_col.find_one({"track": track})
    old_bests   = (board or {}).get("bests", {})
    # Boards written before podiums were stored still know everyone's best time,
    # so the previous order can be worked out instead of losing the first result
    # after the upgrade to a missing baseline.
    old_podium  = (board or {}).get("podium")
    if old_podium is None and old_bests:
        old_podium = sorted(old_bests, key=lambda k: old_bests[k])[:3]
    old_podium  = old_podium or []
    new_bests   = {b["name_key"]: b["time_ms"] for _uid, b in ranked}
    new_podium  = [b["name_key"] for _uid, b in ranked[:3]]

    held = f"  ·  {len(waiting)} waiting on a link" if waiting else ""
    changed = not (board and board.get("fingerprint") == fingerprint)

    # Nothing changed and nobody else forced a reorder - do not touch Discord
    # at all. The board still exists (or gets created below) either way, even
    # for a track nobody has raced - this is the fixed 13-track board, not a
    # lazily-appearing one.
    if not changed and not force:
        if empty:
            return f"⚪ **{track}** — no times posted yet"
        return f"➖ **{track}** — no change, nobody improved{held}"

    # Work out what is worth announcing, before the board is overwritten -
    # only for a real change, never for a force-only reorder repost.
    improved = []
    if changed:
        events.extend(podium_events(track, ranked, old_bests, old_podium, new_podium,
                                    had_board=bool(old_podium), raced=raced))
        # Who actually got quicker, so the report can name them. Restricted to
        # people who raced just now, and to boards we already had a baseline
        # for - a brand new board has nothing to compare against yet.
        if raced is not None and old_bests:
            improved = [f"<@{uid}> `{ms_to_time(b['time_ms'])}`" for uid, b in ranked
                        if b["name_key"] in raced
                        and (b["name_key"] not in old_bests
                             or b["time_ms"] < old_bests[b["name_key"]])]

    async def save(extra: dict):
        await boards_col.update_one(
            {"track": track},
            {"$set": {"track": track, "fingerprint": fingerprint,
                      "bests": new_bests, "podium": new_podium, **extra}}, upsert=True)

    # A board row can exist without a message: !refresh clears the id on purpose,
    # and an older row may predate it. Only try to delete when we really have
    # one, and only in the channel it was posted to - then always post fresh,
    # never edit in place, so a change always reads as a clean new post.
    existing = board.get("message_id") if board else None
    was_update = bool(existing)
    if existing and board.get("channel_id") == channel.id:
        try:
            old = await channel.fetch_message(existing)
            await old.delete()
        except (discord.NotFound, discord.Forbidden):
            pass                      # already gone, or we cannot see it

    message = await channel.send(embed=embed)
    await save({"channel_id": channel.id, "message_id": message.id})
    if not changed:
        return f"↕️ **{track}** — reordered, no change"
    if was_update:
        note = ("🏆 new best: " + ", ".join(improved)) if improved else "updated"
        return f"✅ **{track}** — {note}{held}"
    if empty:
        return f"🆕 **{track}** — board posted (no times yet)"
    return f"🆕 **{track}** — board posted{held}"


async def refresh_boards(channel, tracks, raced_by_track: dict | None = None,
                         force: bool = False) -> tuple[list[str], list[str]]:
    """Refresh several tracks. Returns (report lines, activity events).

    One track failing must not take the rest down with it, so each is caught
    and reported in place.
    """
    events: list[str] = []
    lines: list[str] = []
    for track in tracks:
        try:
            lines.append(await refresh_board(channel, track,
                                             (raced_by_track or {}).get(track), events, force))
        except discord.Forbidden:
            lines.append(f"🚫 **{track}** — no permission to post in "
                         f"{channel.mention}")
        except Exception as e:                     # keep going, name the failure
            lines.append(f"💥 **{track}** — {e.__class__.__name__}: {e}")
            traceback.print_exc()
    return lines, events


SESSION_NAME = "RVRU"      # only lobbies with this in the name are ours


def is_ours(session_name) -> bool:
    """net.rv.gl is shared with the whole community, so never touch a lobby
    that is not clearly ours."""
    return SESSION_NAME.casefold() in (session_name or "").casefold()


async def _call_coordinator(fn, *args, retries: int = 2, base_delay: float = 1.5):
    """Run a blocking coordinator call with a couple of retries on transient
    network failures, instead of surfacing a single hiccup straight to the
    user as an error. A real 404 (session closed) is not transient - fn
    already turns that into a plain None return, not an exception, so it is
    never retried here.
    """
    for attempt in range(retries + 1):
        try:
            return await asyncio.to_thread(fn, *args)
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == retries:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt) + random.random())


def live_sessions() -> list:
    """Current sessions from the coordinator's event stream (first frame)."""
    for frame in read_frames():
        return sessions_from(frame)
    return []


def check_settings(session: dict) -> list[str]:
    """Complaints about a lobby's setup, before anyone wastes a race on it."""
    game = session.get("GameData") or {}
    problems = []
    if game.get("NumLaps") != REQUIRED_LAPS:
        problems.append(f"laps are {game.get('NumLaps')}, should be {REQUIRED_LAPS}")
    if bool(game.get("Pickups")) and not ALLOW_PICKUPS:
        problems.append("pickups are ON, should be off")
    if (session.get("CarRating") or "").lower() != "pro":
        problems.append(f"car class is {session.get('CarRating')!r}, should be 'pro'")
    # Private is fine: the coordinator withholds the id, not the data, so a
    # private lobby armed with !b3l <id> reads exactly like a public one.
    return problems


async def set_active_session(session: dict, uid: int) -> None:
    await state_col.update_one(
        {"key": "active_session"},
        {"$set": {"key": "active_session", "session_id": session.get("ID"),
                  "name": session.get("Name"), "armed_by": uid,
                  "armed_at": datetime.now(timezone.utc)}},
        upsert=True)
    await remember_session(session.get("ID"), session.get("Name"))


async def active_session_id() -> str | None:
    doc = await state_col.find_one({"key": "active_session"})
    return doc.get("session_id") if doc else None


async def remember_session(session_id: str, name: str = "") -> None:
    """A private session's id is never listed by the coordinator, so the only
    way to find it again is to remember it from the first time we were given
    it - by hand with !b3l/!fetch <id>. Public sessions get remembered too,
    which is harmless - they are already found by the live listing either way.
    """
    await known_sessions_col.update_one(
        {"_id": session_id},
        {"$set": {"name": name, "last_seen": datetime.now(timezone.utc)}},
        upsert=True)


async def forget_session(session_id: str) -> None:
    await known_sessions_col.delete_one({"_id": session_id})


KNOWN_SESSION_TTL_HOURS = 12   # RVGL lobbies run for hours, not days


async def known_session_ids() -> list[str]:
    """Every remembered id, minus ones stale enough to be certainly closed.

    Without this, a private lobby armed once and never revisited sits in the
    list forever, costing one extra coordinator probe on every bare !b3l/
    !fetch until someone happens to hit its actual 404 and prune it by hand.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=KNOWN_SESSION_TTL_HOURS)
    stale_ids = [doc["_id"] async for doc in
                 known_sessions_col.find({"last_seen": {"$lt": cutoff}}, {"_id": 1})]
    if stale_ids:
        await known_sessions_col.delete_many({"_id": {"$in": stale_ids}})
    return [doc["_id"] async for doc in known_sessions_col.find({}, {"_id": 1})]


@bot.command(name="b3l")
async def b3l_cmd(ctx, session_ref: str = ""):
    """Arm a session: !b3l to find a public one, or !b3l <id> for a private one.

    Private lobbies work in full - the coordinator simply never publishes their
    id, so it has to be pasted once from the host's browser URL.
    """
    listed, session = {}, None
    armed_before = await active_session_id()

    if session_ref:
        match = SESSION_ID_RE.search(session_ref)
        if not match:
            await ctx.send("❌ That does not look like a session id. Open your lobby on "
                           "net.rv.gl and copy the 32-character id from the URL.")
            return
        session_id = match.group(1)
    else:
        # Always rediscover fresh rather than parroting back whatever is
        # currently armed - arming several sessions one after another and
        # then running a bare !b3l should show all of them, not silently
        # just the last one armed.
        try:
            async with ctx.typing():
                sessions = await _call_coordinator(live_sessions)
        except Exception as e:
            await ctx.send(f"❌ Could not reach the coordinator: "
                           f"`{e.__class__.__name__}`")
            return

        ours   = [s for s in sessions if is_ours(s.get("name"))]
        usable = [s for s in ours if s.get("id")]
        candidates = {s["id"]: s for s in usable}

        # Private sessions never publish an id in the live listing, so the
        # only way to know one is still up is to check every id we've been
        # given before by hand - confirmed-dead ones are forgotten as we go,
        # same self-pruning !fetch does.
        for kid in await known_session_ids():
            if kid in candidates:
                continue
            try:
                probed = await _call_coordinator(fetch_session, kid)
            except Exception:
                continue
            if probed is None:
                await forget_session(kid)
                continue
            if is_ours(probed.get("Name")):
                candidates[kid] = {"id": kid, "name": probed.get("Name"), "private": True}

        if not candidates:
            if ours:
                await ctx.send(
                    f"🔒 **{ours[0].get('name')}** is live but private, so the "
                    f"coordinator does not publish its id.\n"
                    f"Open it on net.rv.gl and paste the id from the URL:\n"
                    f"`!b3l <session id>` — everything else works exactly the same.")
            else:
                other = f" ({len(sessions)} other session(s) up, ignored)" if sessions else ""
                await ctx.send(f"❌ No **{SESSION_NAME}** session is live{other}. "
                               f"Host a session at <https://net.rv.gl> with "
                               f"`{SESSION_NAME}` in the lobby name, then run this again "
                               f"- or arm a private one with `!b3l <session id>`.")
            return

        if len(candidates) > 1:
            embed = discord.Embed(
                title=f"⚠️ {len(candidates)} {SESSION_NAME} sessions are live",
                description="Pick one with `!b3l <id>`:", color=0xffaa00)
            for c in list(candidates.values())[:10]:
                players = f"{c['player_count']} players" if c.get("player_count") is not None \
                    else "private"
                embed.add_field(name=c.get("name", "?"), value=f"`{c['id']}`  ·  {players}",
                                inline=False)
            await ctx.send(embed=embed)
            return

        listed = next(iter(candidates.values()))
        session_id = listed["id"]

    if session is None:
        async with ctx.typing():
            try:
                session = await _call_coordinator(fetch_session, session_id)
            except Exception as e:
                await ctx.send(f"❌ Could not read that session: `{e.__class__.__name__}`")
                return
    if session is None:
        await ctx.send("❌ No session with that id — it may have closed, or the id is wrong.")
        return
    if not is_ours(session.get("Name")):
        await ctx.send(f"❌ **{session.get('Name')}** is not an {SESSION_NAME} session.")
        return

    # "Already armed" just means this happens to be the same session that was
    # already active - not news, whichever way we arrived at its id.
    already = session_id == armed_before
    await set_active_session(session, ctx.author.id)
    problems = check_settings(session)
    host = listed.get("host") or "eu.rv.gl"
    join = f"{host}:{session.get('Port', '?')}"
    private = not session.get("Public")

    embed = discord.Embed(
        title=("🔒 " if private else "🏁 ") + str(session.get("Name", "?")),
        description=f"```{session.get('ID')}```",
        color=0xff5555 if problems else 0x00cfff)
    embed.add_field(name="Join", value=f"`{join}`", inline=True)
    embed.add_field(name="Laps", value=str((session.get("GameData") or {}).get("NumLaps")),
                    inline=True)
    embed.add_field(name="Cars", value=str(session.get("CarRating")), inline=True)
    if problems:
        embed.add_field(name="⚠️ Fix before racing",
                        value="\n".join(f"• {p}" for p in problems), inline=False)
    else:
        embed.add_field(name="✅ Settings look right",
                        value=f"{REQUIRED_LAPS} laps · no pickups · pro cars"
                              + ("  ·  *private, armed by id*" if private else ""),
                        inline=False)
    embed.set_footer(
        text=("already armed — !fetch when you are done" if already
              else "run !fetch before closing the lobby — results vanish with it"))
    await ctx.send(embed=embed)

    if already:
        return                # checking on a session is not news, do not re-announce

    # Announce regardless of settings - people still want the join address, and
    # silently posting nothing looked like the feed was broken
    warning = ("\n⚠️ *settings still need fixing: " + "; ".join(problems) + "*") if problems else ""
    await announce(ctx.guild,
                   f"🏁 **{session.get('Name','?')} is live** — join `{join}`\n"
                   f"{REQUIRED_LAPS} laps · pro cars · no pickups{warning}")

    if not await get_channel(ctx.guild, "activity"):
        await ctx.send("*(no activity channel set — nothing was announced. "
                       "`!setchannel activity #channel`)*")


@bot.command(name="refresh", aliases=["rebuildboards"])
@admin_or_dev()
async def refresh_cmd(ctx):
    """Forget where the boards were posted and put them up fresh.

    Needed after moving the times channel, or after linking someone whose
    results were being held - also recomputes the overall standings.

    Always the fixed 13-track list, Toys in the Hood 1 through Toytanic 2, not
    just tracks that happen to have results - a track nobody has raced yet
    still gets a board, saying so, in its proper place in the order.
    """
    async with board_lock:
        board_ch = await get_channel(ctx.guild, "times") or ctx.channel
        # Only forget the fingerprint, not message_id/channel_id - refresh_board
        # needs those to find and delete the old post itself. A full !resetseason
        # is what wipes the id fields for a truly clean slate.
        await boards_col.update_many({}, {"$unset": {"fingerprint": ""}})
        tracks = [scoring.TRACK_DISPLAY[k] for k in scoring.CANONICAL_TRACK_KEYS]
        async with ctx.typing():
            lines, _ = await refresh_boards(board_ch, tracks)
        await ctx.send(f"🔁 Rebuilt {len(lines)} board(s) in {board_ch.mention}.")

        # !refresh means "put it up fresh" - forget the standings fingerprint too,
        # or recompute_standings sees an unchanged score and skips reposting even
        # when the image itself changed (e.g. a layout fix, not a new time).
        await state_col.update_one({"key": "standings_board"}, {"$unset": {"fingerprint": ""}})
        async with ctx.typing():
            events = await recompute_standings(ctx.guild)
        for event in events:
            await announce(ctx.guild, event)
        await ctx.send("✅ Standings reposted.")
        await nudge_unlinked(ctx)


async def _ingest_session(session_id: str) -> tuple[dict | None, list, list, int, str | None]:
    """Fetch one coordinator session and store any new races in it.

    Returns (session, races, fresh, already, error) - error is a plain
    sentence for the user, and session/races/fresh/already are only
    meaningful when it is None. Shared by the single-session and
    fetch-everything-live paths so storing and reporting stay in sync.
    """
    try:
        session = await _call_coordinator(fetch_session, session_id)
    except Exception as e:
        return None, [], [], 0, f"could not reach the coordinator (`{e.__class__.__name__}`)"
    if session is None:
        # Gone for good, not just quiet - the coordinator drops closed sessions
        # instantly. No point remembering an id that will never resolve again.
        await forget_session(session_id)
        return None, [], [], 0, "that session is gone — the coordinator drops them once they close"
    if not is_ours(session.get("Name")):
        return None, [], [], 0, f"**{session.get('Name')}** is not an {SESSION_NAME} session"

    races = apply_race_rules(races_from(session))
    fresh, already = await store_races(session, races)
    await remember_session(session_id, session.get("Name"))
    return session, races, fresh, already, None


AUTO_FETCH_INTERVAL = 30   # seconds - matches coordinator_ingest.py's own poll default


async def _auto_session_loop() -> None:
    """Runs for the bot's whole lifetime: keeps every live RVRU session's
    results flowing in on its own (nobody has to remember !fetch), and
    announces the moment one closes - a session 404s the instant it ends,
    so noticing it just vanished from the live list is the only way to know.

    Alongside !b3l/!fetch, not instead of them - this only ever adds
    results and announcements; it never arms a session or changes what
    those commands do.
    """
    await bot.wait_until_ready()
    seen: dict[str, str] = {}   # session_id -> name, live as of the last tick

    while not bot.is_closed():
        await asyncio.sleep(AUTO_FETCH_INTERVAL)
        guild = bot.guilds[0] if bot.guilds else None
        if guild is None:
            continue

        try:
            live = await _call_coordinator(live_sessions)
            live_ids = {s["id"]: s.get("name", "?") for s in live
                       if is_ours(s.get("name")) and s.get("id")}
        except Exception:
            continue   # coordinator hiccup - just try again next tick

        all_ids = dict(live_ids)
        for kid in await known_session_ids():
            if kid in all_ids:
                continue
            try:
                probed = await _call_coordinator(fetch_session, kid)
            except Exception:
                continue
            if probed is None:
                await forget_session(kid)
            elif is_ours(probed.get("Name")):
                all_ids[kid] = probed.get("Name", "?")

        for sid, name in list(seen.items()):
            if sid not in all_ids:
                await announce(guild, f"🔒 **{name}** has closed.")
                del seen[sid]
        seen.update(all_ids)

        all_fresh = []
        for sid in all_ids:
            _session, _races, fresh, _already, error = await _ingest_session(sid)
            if error is None:
                all_fresh.extend(fresh)
        if not all_fresh:
            continue

        async with board_lock:
            raced_by_track: dict[str, set] = {}
            for r in all_fresh:
                if r.counted:
                    raced_by_track.setdefault(r.track, set()).update(
                        e.name_key for e in r.entries if e.counted)
            board_ch = await get_channel(guild, "times")
            if board_ch:
                # Every one of the 13, forced - not just the tracks that
                # changed. Discord only ever appends, so reposting just the
                # changed one would leave it stranded at the bottom, out of
                # the fixed track order every other board is still sitting in.
                all_tracks = [scoring.TRACK_DISPLAY[k] for k in scoring.CANONICAL_TRACK_KEYS]
                _lines, events = await refresh_boards(board_ch, all_tracks,
                                                       raced_by_track, force=True)
                for event in events:
                    await announce(guild, event)
            for event in await recompute_standings(guild):
                await announce(guild, event)


_auto_session_loop_started = False


@bot.command(name="fetch")
async def fetch_cmd(ctx, session_ref: str = ""):
    """Pull a coordinator session's races in: !fetch [id or url]

    With no id, pulls every live public RVRU session at once, plus every
    private session it has ever been given the id for (via !b3l/!fetch <id>
    previously) - a private lobby's id is never listed by the coordinator, so
    remembering it the first time is the only way to find it again on later
    fetches. A remembered id that has since closed for good is forgotten
    automatically.
    """
    if session_ref:
        match = SESSION_ID_RE.search(session_ref)
        if not match:
            await ctx.send("❌ Give a session id or its net.rv.gl URL.")
            return
        session_id = match.group(1)
    else:
        live_ids = []
        try:
            async with ctx.typing():
                sessions = await _call_coordinator(live_sessions)
            live_ids = [s["id"] for s in sessions if is_ours(s.get("name")) and s.get("id")]
        except Exception:
            pass                      # coordinator hiccup - known private ids still apply below

        known_ids = await known_session_ids()
        all_ids = live_ids + [sid for sid in known_ids if sid not in live_ids]

        if len(all_ids) > 1:
            await _fetch_many(ctx, all_ids)
            return

        session_id = all_ids[0] if all_ids else await active_session_id()
        if not session_id:
            await ctx.send("❌ No session armed — run `!b3l` first, or `!fetch <id>`.")
            return

    async with ctx.typing():
        session, races, fresh, already, error = await _ingest_session(session_id)
    if error:
        await ctx.send(f"❌ {error}")
        return
    running = [r for r in races if r.finished_at is None]

    # Nothing new: say so once and touch nothing, so re-running is harmless
    if not fresh:
        note = f"Nothing new — all {already} race(s) were already in." if already \
               else "No finished races yet."
        if running:
            note += f"\n⏱️ *{running[0].track} is still being raced.*"
        await ctx.send(f"📥 **{session.get('Name', '?')}** — {note}")
        return

    report = [f"📥 **{session.get('Name', '?')}**",
              f"Read **{len(fresh)} new race(s)**"
              + (f", {already} already in." if already else ".")]
    for r in running:
        report.append(f"⏱️ *{r.track} is still being raced — it will come in next time.*")
    for r in [x for x in fresh if not x.counted][:5]:
        report.append(f"❌ **{r.track}** — this race does not count: {r.reject_reason}")

    # Individual results thrown out of races that did count, so an exclusion is
    # visible the moment it happens rather than only if a whole track is empty
    # Anyone who also set a valid time on this track needs no mention - a
    # restarted race is not a failure worth reporting
    scored: dict[str, set] = {}
    for r in fresh:
        if r.counted:
            scored.setdefault(r.track, set()).update(
                e.name_key for e in r.entries if e.counted)

    dropped: dict[str, list[str]] = {}
    for r in fresh:
        if not r.counted:
            continue
        for e in r.entries:
            if e.counted or e.name_key in scored.get(r.track, set()):
                continue
            # Skip people who sat the race out - they show up in every race's
            # entry list with zeroed times and were never on the track
            if e.time_ms > 0 or e.best_lap_ms > 0:
                dropped.setdefault(r.track, []).append(f"{e.name_raw} ({e.reject_reason})")
    for track, who in list(dropped.items())[:6]:
        report.append(f"⚠️ **{track}** — not counted: " + ", ".join(who[:5]))

    await ctx.send("\n".join(report))
    await _apply_fetch_results(ctx, fresh)


async def _apply_fetch_results(ctx, fresh: list) -> None:
    """Refresh boards/standings for a batch of freshly-stored races.

    Shared tail for both the single-session and fetch-everything-live paths -
    whether `fresh` came from one session or several, it only ever needs
    applying once against the boards and standings.
    """
    async with board_lock:
        # Only tracks that actually gained a race, and only the people in them
        raced_by_track: dict[str, set] = {}
        for r in fresh:
            if r.counted:
                raced_by_track.setdefault(r.track, set()).update(
                    e.name_key for e in r.entries if e.counted)

        board_ch = await get_channel(ctx.guild, "times") or ctx.channel
        # Every one of the 13, forced - not just the tracks that changed.
        # Discord only ever appends, so reposting just the changed one would
        # leave it stranded at the bottom, out of the fixed track order
        # every other board is still sitting in.
        all_tracks = [scoring.TRACK_DISPLAY[k] for k in scoring.CANONICAL_TRACK_KEYS]
        async with ctx.typing():
            lines, events = await refresh_boards(board_ch, all_tracks,
                                                 raced_by_track, force=bool(raced_by_track))
        # The force-repost above touches every one of the 13 to keep them in
        # order, but only the tracks that actually changed are worth telling
        # the user about - a "reordered, no change" line per untouched track
        # would drown the real news in noise.
        worth_reporting = [l for l in lines if not l.startswith(("➖", "⚪", "↕️"))]
        if worth_reporting:
            await ctx.send("\n".join(worth_reporting))
        if board_ch is ctx.channel and not await get_channel(ctx.guild, "times"):
            await ctx.send("*(no times channel set — track boards are here. "
                           "`!setchannel times #channel` to move them.)*")

        # Records and podium moves are news, straight to the activity feed.
        # Import counts are bookkeeping and stay in the command channel.
        for event in events:
            await announce(ctx.guild, event)

        async with ctx.typing():
            standings_events = await recompute_standings(ctx.guild)
        for event in standings_events:
            await announce(ctx.guild, event)

        await nudge_unlinked(ctx)


async def _fetch_many(ctx, session_ids: list[str]) -> None:
    """!fetch with no id, when more than one live RVRU session was found.

    Ingests every one of them, then applies the combined result to the
    boards/standings once - a race from any of them landing on the same
    track in the same batch is reported together, not as separate reposts.
    """
    all_fresh = []
    lines = [f"📥 **{len(session_ids)} live RVRU sessions found** — pulling all of them:"]
    async with ctx.typing():
        for session_id in session_ids:
            session, races, fresh, already, error = await _ingest_session(session_id)
            if error:
                lines.append(f"❌ `{session_id}` — {error}")
                continue
            running = [r for r in races if r.finished_at is None]
            name = session.get("Name", "?")
            if not fresh:
                note = f"nothing new ({already} already in)" if already else "no finished races yet"
                lines.append(f"➖ **{name}** — {note}")
            else:
                note = f"{len(fresh)} new race(s)" + (f", {already} already in" if already else "")
                lines.append(f"✅ **{name}** — {note}")
            for r in running:
                lines.append(f"⏱️ *{r.track} still being raced in {name} — comes in next time.*")
            all_fresh.extend(fresh)

    await ctx.send("\n".join(lines))
    if not all_fresh:
        return
    await _apply_fetch_results(ctx, all_fresh)




async def nudge_unlinked(ctx) -> None:
    """Say plainly if anyone's times are being held back."""
    links = await link_map()
    skipped = {s["name_key"] async for s in skips_col.find({}, {"name_key": 1})}
    waiting = {}
    async for doc in races_col.find({"counted": True}, {"entries": 1}):
        for e in doc["entries"]:
            if e.get("counted") and e["name_key"] not in links and e["name_key"] not in skipped:
                waiting[e["name_key"]] = e["name_raw"]
    if not waiting:
        return
    names = ", ".join(f"`{n}`" for n in sorted(waiting.values(), key=str.lower))
    await ctx.send(f"⏳ **{len(waiting)} racer(s) have times but no Discord account yet:** "
                   f"{names}\nLink them with `!link <name> <user id>`, then `!refresh`.")

@bot.command(name="unlinked")
@admin_or_dev()
async def unlinked_cmd(ctx):
    """Everyone with stored results who has no Discord user attached yet."""
    links = await link_map()
    skipped = set()
    async for s in skips_col.find({}, {"name_key": 1}):
        skipped.add(s["name_key"])

    waiting: dict[str, dict] = {}
    async for doc in races_col.find({}, {"entries": 1, "track": 1, "session": 1}):
        for entry in doc["entries"]:
            key = entry["name_key"]
            if key in links or key in skipped:
                continue
            row = waiting.setdefault(key, {"raw": entry["name_raw"], "races": 0,
                                           "session": doc.get("session")})
            row["races"] += 1

    if not waiting:
        await ctx.send("✅ Everyone with results is linked.")
        return

    lines = [f"`{r['raw']}` — {r['races']} race(s), last in *{r['session']}*"
             for r in sorted(waiting.values(), key=lambda r: -r["races"])]
    embed = discord.Embed(
        title=f"⏳ Unlinked racers ({len(waiting)})",
        description="\n".join(lines[:25])[:2000],
        color=0xffcc00)
    embed.set_footer(text="!link <ingame name> <user id>  ·  then !refresh")
    await ctx.send(embed=embed)


@bot.command(name="racers")
@admin_or_dev()
async def racers_cmd(ctx):
    """Everyone seen in results, and whether they are linked yet."""
    links = await link_map()
    skipped = set()
    async for s in skips_col.find({}, {"name_key": 1}):
        skipped.add(s["name_key"])

    seen: dict[str, dict] = {}
    async for doc in races_col.find({}, {"entries": 1}):
        for entry in doc["entries"]:
            row = seen.setdefault(entry["name_key"], {"raw": entry["name_raw"], "races": 0})
            row["races"] += 1
    if not seen:
        await ctx.send("No results stored yet — run `!fetch <session id>` while a "
                       "session is live.")
        return

    done, todo = [], []
    for key, row in sorted(seen.items(), key=lambda kv: -kv[1]["races"]):
        if key in links:
            done.append(f"`{row['raw']}` → <@{links[key]}>")
        elif key in skipped:
            done.append(f"`{row['raw']}` → 🚫 skipped")
        else:
            todo.append(f"`{row['raw']}` — {row['races']} race(s)")

    embed = discord.Embed(title=f"🏎️ Racers seen in results ({len(seen)})", color=0x00cfff)
    if todo:
        embed.add_field(name=f"❗ Not linked yet ({len(todo)})",
                        value="\n".join(todo[:25])[:1000], inline=False)
    if done:
        embed.add_field(name=f"✅ Handled ({len(done)})",
                        value="\n".join(done[:25])[:1000], inline=False)
    await ctx.send(embed=embed)


@bot.command(name="whois")
async def whois_cmd(ctx, *, ingame_name: str):
    """Who does an in-game name belong to?"""
    raw = _strip_mentions(ingame_name)
    doc = await aliases_col.find_one({"name_key": name_key(raw)})
    if not doc:
        await ctx.send(f"❓ **{raw}** is not linked to anyone yet.")
        return
    await ctx.send(f"🔗 **{doc.get('name_raw', raw)}** → <@{doc['uid']}>")


# ── Link suggestions ──────────────────────────────────────────────────────────
def match_names(known: dict, member_idx: dict, linked: set) -> tuple[list, list, list]:
    """Split known in-game names into exact / ambiguous / unmatched.

    Pure so it can be tested without Discord or Mongo. `known` maps match key to
    the name as written, `member_idx` maps match key to the members whose
    display name or username normalises to it.
    """
    exact, ambiguous, unmatched = [], [], []
    for key, raw in sorted(known.items(), key=lambda kv: kv[1].lower()):
        if key in linked:
            continue
        found = member_idx.get(key, [])
        if len(found) == 1:
            exact.append((raw, key, found[0]))
        elif found:
            ambiguous.append((raw, key, found))
        else:
            unmatched.append((raw, key))
    return exact, ambiguous, unmatched


def build_member_index(guild) -> dict:
    """Match key -> members, from both display names and usernames."""
    idx: dict[str, list] = {}
    for m in guild.members:
        if m.bot:
            continue
        for candidate in {m.display_name, m.name}:
            key = name_key(candidate)
            if key and m not in idx.setdefault(key, []):
                idx[key].append(m)
    return idx


async def gather_suggestions(guild):
    """Which known in-game names line up with which Discord members.

    Names come from racers seen in real results. Until any session has been
    scanned that list is empty, so the curated roster stands in for it.

    Returns (exact, ambiguous, unmatched, skipped). Names already linked, or
    explicitly skipped with !linkskip, are left out of the matching entirely.
    """
    linked = set()
    async for doc in aliases_col.find({}, {"name_key": 1}):
        linked.add(doc["name_key"])

    skips: dict[str, str] = {}
    async for doc in skips_col.find({}, {"name_key": 1, "name_raw": 1}):
        skips[doc["name_key"]] = doc.get("name_raw", doc["name_key"])

    known: dict[str, str] = {}
    async for doc in races_col.find({}, {"entries": 1}):
        for entry in doc["entries"]:
            known.setdefault(entry["name_key"], entry["name_raw"])

    if not known:
        # Nothing scanned yet - fall back to the curated roster so this is useful
        # before the first session.
        async for r in ratings_col.find({}, {"user": 1}):
            if r.get("user"):
                known.setdefault(name_key(r["user"]), r["user"])
        for seeded, _rating in SEED_RATINGS:
            known.setdefault(name_key(seeded), seeded)

    known = {k: v for k, v in known.items() if k not in skips}
    exact, ambiguous, unmatched = match_names(known, build_member_index(guild), linked)
    return exact, ambiguous, unmatched, sorted(skips.values(), key=str.lower)


@bot.command(name="linksuggest")
@admin_or_dev()
async def linksuggest_cmd(ctx):
    """Dry run: show which in-game names would be linked automatically."""
    exact, ambiguous, unmatched, skipped = await gather_suggestions(ctx.guild)

    if not (exact or ambiguous or unmatched or skipped):
        await ctx.send("✅ Every known in-game name is already linked.")
        return

    embed = discord.Embed(
        title="🔗 Suggested links",
        description="Nothing has been saved yet.\n"
                    "• Wrong person? `!link <name> @correct-user` — it then drops off this list\n"
                    "• Should not be linked at all? `!linkskip <name>`\n"
                    "• Happy with the rest? `!linkconfirm`",
        color=0x00cfff,
    )
    if exact:
        embed.add_field(
            name=f"✅ Exact match ({len(exact)}) — these would be linked",
            value="\n".join(f"`{raw}` → {m.mention}" for raw, _k, m in exact[:25])[:1000]
                  + ("\n…" if len(exact) > 25 else ""),
            inline=False)
    if ambiguous:
        embed.add_field(
            name=f"⚠️ Several members match ({len(ambiguous)}) — link these by hand",
            value="\n".join(f"`{raw}` → " + ", ".join(m.mention for m in ms)
                            for raw, _k, ms in ambiguous[:10])[:1000],
            inline=False)
    if unmatched:
        embed.add_field(
            name=f"❓ No Discord member matches ({len(unmatched)})",
            value=", ".join(f"`{raw}`" for raw, _k in unmatched[:30])[:1000],
            inline=False)
    if skipped:
        embed.add_field(
            name=f"🚫 Skipped ({len(skipped)}) — restore with !linkunskip",
            value=", ".join(f"`{raw}`" for raw in skipped[:30])[:1000],
            inline=False)
    await ctx.send(embed=embed)


@bot.command(name="linkdebug")
@admin_or_dev()
async def linkdebug_cmd(ctx, *, query: str):
    """Explain a name's suggestion state, or list the names a user owns."""
    # Given a user rather than a name, answer the reverse question instead.
    member, leftover = split_member_and_name(ctx, query)
    if member and not leftover:
        owned = await aliases_col.find({"uid": member.id}).to_list(None)
        names = ", ".join(f"`{d.get('name_raw', d['name_key'])}`" for d in owned) or "*none*"
        embed = discord.Embed(
            title=f"🔍 Names linked to {member.display_name}",
            description=f"{member.mention} — {names}\n\n"
                        f"Add another with `!link <ingame name> {member.id}`.",
            color=0x00cfff)
        await ctx.send(embed=embed)
        return

    raw = _strip_mentions(query)
    key = name_key(raw)

    alias = await aliases_col.find_one({"name_key": key})
    skip  = await skips_col.find_one({"name_key": key})

    known: dict[str, str] = {}
    async for r in ratings_col.find({}, {"user": 1}):
        if r.get("user"):
            known.setdefault(name_key(r["user"]), r["user"])
    seeded = {name_key(n): n for n, _ in SEED_RATINGS}

    members = build_member_index(ctx.guild).get(key, [])

    lines = [
        f"**Typed:** `{raw}`",
        f"**Match key:** `{key or '(empty)'}`",
        "",
        f"**Linked?** " + (f"yes → <@{alias['uid']}> (stored as `{alias.get('name_raw', '?')}`)"
                           if alias else "no"),
        f"**Skipped?** " + ("yes" if skip else "no"),
        f"**In ratings table?** " + (f"yes, as `{known[key]}`" if key in known else "no"),
        f"**In SEED_RATINGS?** " + (f"yes, as `{seeded[key]}`" if key in seeded else "no"),
        f"**Discord members matching this key:** "
        + (", ".join(m.mention for m in members) if members else "none"),
    ]

    if alias:
        verdict = "Already linked, so it should NOT appear in `!linksuggest`."
    elif skip:
        verdict = "Skipped, so it appears only under 🚫."
    elif key not in known and key not in seeded:
        verdict = ("This exact spelling is not in the suggestion source at all. "
                   "`!linksuggest` only offers names from the ratings table and "
                   "SEED_RATINGS — a differently spelled name there is a separate entry.")
    else:
        verdict = "Not linked yet, so it will appear in `!linksuggest`."

    embed = discord.Embed(title="🔍 Link debug", description="\n".join(lines), color=0x00cfff)
    embed.add_field(name="Verdict", value=verdict, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="linkskip")
@admin_or_dev()
async def linkskip_cmd(ctx, *, ingame_name: str):
    """Leave a name out of !linksuggest and !linkconfirm."""
    raw = _strip_mentions(ingame_name)
    key = name_key(raw)
    if not key:
        await ctx.send("❌ Give the in-game name, e.g. `!linkskip AR|SANTI™`")
        return
    await skips_col.update_one(
        {"name_key": key},
        {"$set": {"name_key": key, "name_raw": raw,
                  "skipped_by": ctx.author.id, "skipped_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await ctx.send(f"🚫 **{raw}** will be left out of link suggestions.")


@bot.command(name="linkunskip")
@admin_or_dev()
async def linkunskip_cmd(ctx, *, ingame_name: str):
    raw = _strip_mentions(ingame_name)
    doc = await skips_col.find_one_and_delete({"name_key": name_key(raw)})
    if not doc:
        await ctx.send(f"❌ **{raw}** was not skipped.")
        return
    await ctx.send(f"↩️ **{doc.get('name_raw', raw)}** is back in link suggestions.")


@bot.command(name="linkconfirm")
@admin_or_dev()
async def linkconfirm_cmd(ctx):
    """Apply the exact matches from !linksuggest. Ambiguous ones are left alone."""
    exact, _ambiguous, _unmatched, _skipped = await gather_suggestions(ctx.guild)
    if not exact:
        await ctx.send("Nothing to link — run `!linksuggest` to see why.")
        return

    now = datetime.now(timezone.utc)
    for raw, key, member in exact:
        await aliases_col.update_one(
            {"name_key": key},
            {"$set": {
                "name_key":  key,
                "name_raw":  raw,
                "uid":       member.id,
                "user":      member.display_name,
                "linked_by": ctx.author.id,
                "linked_at": now,
                "auto":      True,
            }},
            upsert=True,
        )
    await ctx.send(f"✅ Linked {len(exact)} name(s). Check with `!links`, fix any with "
                   f"`!unlink <name>`.")


@bot.command(name="maketeams")
@admin_or_dev()
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


# ── Beef battles ──────────────────────────────────────────────────────────────
BEEF_ACCEPT_SECONDS = 120    # how long a challenge sits waiting to be answered
BEEF_TURN_SECONDS   = 180    # how long each racer has to type their burn
BEEF_VOTE_SECONDS   = 45     # crowd-vote window, when the AI judge sits one out
BEEF_ROUNDS         = 3
BEEF_YES, BEEF_NO, BEEF_FIRE = "✅", "❌", "🔥"

# One battle per channel at a time. Two overlapping duels in the same channel
# would both be listening for the next message anyone types, and would steal
# each other's burns.
_active_beefs: set[int] = set()


async def _beef_record(uid: int) -> dict:
    return await beef_col.find_one({"uid": uid}) or {"uid": uid, "wins": 0, "losses": 0,
                                                     "rounds_won": 0, "points": 0}


async def _beef_save(uid: int, name: str, won: bool, rounds_won: int) -> None:
    await beef_col.update_one(
        {"uid": uid},
        {"$set": {"uid": uid, "name": name},
         "$inc": {"wins": int(won), "losses": int(not won),
                  "rounds_won": rounds_won, "points": 3 if won else 0}},
        upsert=True)


async def _collect_burn(ctx, member, round_no: int):
    """Wait for that member's next message in this channel. None if they stall."""
    await ctx.send(f"🎤 Round {round_no} — {member.mention}, you're up. "
                   f"({BEEF_TURN_SECONDS // 60} min)")

    def is_their_turn(m):
        return (m.author.id == member.id and m.channel.id == ctx.channel.id
                and not m.content.startswith("!"))

    try:
        message = await bot.wait_for("message", check=is_their_turn,
                                     timeout=BEEF_TURN_SECONDS)
    except asyncio.TimeoutError:
        return None
    return message.content.strip()


async def _crowd_vote(ctx, a_member, a_burn, b_member, b_burn) -> int | None:
    """Fallback scoring: the room reacts, most 🔥 wins. None if nobody votes.

    Used whenever the AI judge has no verdict - no API key, unreachable, or it
    declined this particular exchange. The duel carries on either way, which is
    the whole point of having a fallback at all.
    """
    msg_a = await ctx.send(f"🅰️ {a_member.mention}: {a_burn}")
    msg_b = await ctx.send(f"🅱️ {b_member.mention}: {b_burn}")
    for msg in (msg_a, msg_b):
        await msg.add_reaction(BEEF_FIRE)
    await ctx.send(f"🗳️ Crowd vote — {BEEF_VOTE_SECONDS}s. React {BEEF_FIRE} to the better burn. "
                   f"*(the two of you don't get a vote)*")
    await asyncio.sleep(BEEF_VOTE_SECONDS)

    async def score(message_id) -> int:
        message = await ctx.channel.fetch_message(message_id)
        for reaction in message.reactions:
            if str(reaction.emoji) != BEEF_FIRE:
                continue
            # Voting for yourself is not a vote, and neither is the bot's own
            # seed reaction - both are filtered rather than subtracted, so a
            # tie is a real tie.
            return len([u async for u in reaction.users()
                        if u.id not in (a_member.id, b_member.id, bot.user.id)])
        return 0

    votes_a, votes_b = await score(msg_a.id), await score(msg_b.id)
    if votes_a == votes_b:
        return None
    return 0 if votes_a > votes_b else 1


@bot.command(name="beef")
async def beef_cmd(ctx, member: discord.Member = None):
    """Start a roast battle: !beef @someone

    They have to accept first - nobody gets dragged into a public roast they
    did not agree to.
    """
    if member is None:
        await ctx.send("❌ Who with? `!beef @someone`")
        return
    if member.id == ctx.author.id:
        await ctx.send("❌ You cannot have beef with yourself. Seek help.")
        return
    if member.bot:
        await ctx.send("❌ Pick someone who can type.")
        return
    if ctx.channel.id in _active_beefs:
        await ctx.send("❌ There's already a beef running in this channel. Wait your turn.")
        return

    challenge = discord.Embed(
        title="🥩 BEEF",
        description=(f"{ctx.author.mention} wants smoke with {member.mention}.\n\n"
                     f"**{BEEF_ROUNDS} rounds**, alternating. Funniest burn takes the round.\n"
                     f"{member.mention} — {BEEF_YES} to accept, {BEEF_NO} to back down."),
        color=0xff4444)
    challenge.set_footer(text=f"expires in {BEEF_ACCEPT_SECONDS // 60} minutes")
    prompt = await ctx.send(content=member.mention, embed=challenge)
    await prompt.add_reaction(BEEF_YES)
    await prompt.add_reaction(BEEF_NO)

    def answered(reaction, user):
        return (user.id == member.id and reaction.message.id == prompt.id
                and str(reaction.emoji) in (BEEF_YES, BEEF_NO))

    try:
        reaction, _user = await bot.wait_for("reaction_add", check=answered,
                                             timeout=BEEF_ACCEPT_SECONDS)
    except asyncio.TimeoutError:
        await ctx.send(f"💤 {member.mention} never answered. {ctx.author.mention} wins by default "
                       f"— no points, no glory.")
        return
    if str(reaction.emoji) == BEEF_NO:
        await ctx.send(f"🏳️ {member.mention} backed down. That's a choice.")
        return

    _active_beefs.add(ctx.channel.id)
    try:
        await _run_beef(ctx, ctx.author, member)
    finally:
        _active_beefs.discard(ctx.channel.id)


async def _run_beef(ctx, a_member, b_member) -> None:
    """The battle itself, once both sides have agreed to it."""
    await ctx.send(f"🔔 **It's on.** {a_member.mention} vs {b_member.mention} — "
                   f"{BEEF_ROUNDS} rounds. {a_member.mention} throws first.")

    score = [0, 0]
    for round_no in range(1, BEEF_ROUNDS + 1):
        burn_a = await _collect_burn(ctx, a_member, round_no)
        if burn_a is None:
            await ctx.send(f"⏳ {a_member.mention} froze up. {b_member.mention} takes it by forfeit.")
            await _finish_beef(ctx, b_member, a_member, score[1], score[0])
            return
        burn_b = await _collect_burn(ctx, b_member, round_no)
        if burn_b is None:
            await ctx.send(f"⏳ {b_member.mention} froze up. {a_member.mention} takes it by forfeit.")
            await _finish_beef(ctx, a_member, b_member, score[0], score[1])
            return

        async with ctx.typing():
            verdict = await beef_judge.judge_round(
                a_member.display_name, burn_a, b_member.display_name, burn_b, round_no)

        if verdict is not None:
            winner_idx = 0 if verdict.winner == "A" else 1
            winner = (a_member, b_member)[winner_idx]
            embed = discord.Embed(title=f"Round {round_no}: {winner.display_name} takes it",
                                  description=verdict.verdict, color=0xffaa00)
            embed.add_field(name=f"{a_member.display_name}", value=burn_a[:1000], inline=False)
            embed.add_field(name=f"{b_member.display_name}", value=burn_b[:1000], inline=False)
            await ctx.send(embed=embed)
            await ctx.send(f"🔥 {verdict.hype}")
        else:
            # No verdict from the judge - hand this round to the room rather
            # than dropping the whole battle.
            winner_idx = await _crowd_vote(ctx, a_member, burn_a, b_member, burn_b)
            if winner_idx is None:
                await ctx.send(f"🤝 Round {round_no} — dead even. Nobody scores.")
                continue
            winner = (a_member, b_member)[winner_idx]
            await ctx.send(f"🏆 Round {round_no} — **{winner.display_name}** takes it on votes.")

        score[winner_idx] += 1
        await ctx.send(f"📊 {a_member.display_name} **{score[0]}** — **{score[1]}** {b_member.display_name}")

    if score[0] == score[1]:
        await ctx.send(f"🤝 **{score[0]}–{score[1]}. Dead even.** Nobody wins, everybody loses.")
        return
    if score[0] > score[1]:
        await _finish_beef(ctx, a_member, b_member, score[0], score[1])
    else:
        await _finish_beef(ctx, b_member, a_member, score[1], score[0])


async def _finish_beef(ctx, winner, loser, winner_rounds: int, loser_rounds: int) -> None:
    await _beef_save(winner.id, winner.display_name, True, winner_rounds)
    await _beef_save(loser.id, loser.display_name, False, loser_rounds)
    record = await _beef_record(winner.id)
    await ctx.send(
        f"👑 **{winner.mention} wins the beef {winner_rounds}–{loser_rounds}.**\n"
        f"Record: **{record['wins']}W–{record['losses']}L** · **{record['points']} pts**  ·  "
        f"`!beefboard` for the standings.")


@bot.command(name="beefboard")
async def beefboard_cmd(ctx):
    """Roast battle standings - entirely separate from the racing ranks."""
    rows = await beef_col.find().sort("points", -1).to_list(20)
    if not rows:
        await ctx.send("Nobody has thrown hands yet. `!beef @someone`.")
        return
    lines = [f"`{i}.` <@{r['uid']}> — **{r.get('points', 0)} pts** "
             f"({r.get('wins', 0)}W–{r.get('losses', 0)}L)"
             for i, r in enumerate(rows, 1)]
    embed = discord.Embed(title="🥩 Beef Standings", description="\n".join(lines),
                          color=0xff4444)
    embed.set_footer(text="3 points per battle won")
    await ctx.send(embed=embed)


@bot.command(name="predict")
async def predict(ctx, member: discord.Member):
    import random
    name = member.display_name
    predictions = [
        f"{name} will either win by 10 seconds or disconnect. No in between.",
        f"The stars align for {name} tonight. Unfortunately they align directly into a wall.",
        f"My sources tell me {name} has been practicing. My sources are liars.",
        f"Bold prediction: {name} finishes exactly where nobody expected. Even {name}.",
        f"{name} is going to have one of those races. You know the ones.",
        f"Tonight {name} becomes unstoppable. Tomorrow they blame lag.",
        f"{name} will carry the team. Straight into last place.",
        f"I'm seeing a podium finish for {name}. On the other team's scoreboard.",
        f"{name} has a 73% chance of greatness tonight and a 100% chance of blaming their controller.",
        f"When {name} says 'I'm warmed up', everyone should be worried. Mostly {name}.",
        f"{name} tonight: fast in the straights, one with the walls in the corners.",
        f"The battery gods smile upon {name} this evening. It won't help.",
        f"An MVP performance from {name} is incoming. Just not sure for which team.",
        f"{name} will post a time that makes everyone go quiet. Whether impressive or painful — unknown.",
        f"My prediction: {name} will clip that one corner. Every. Single. Lap.",
        f"{name} enters tonight as the dark horse. Mainly because nobody can predict what they'll do, including {name}.",
        f"History books will remember what {name} does tonight. For various reasons.",
        f"{name} is locked in. The question is whether they're locked into the race or into a barrier.",
    ]
    await ctx.send(random.choice(predictions))


@bot.command(name="roast")
async def roast(ctx, member: discord.Member):
    import random
    name = member.display_name

    personal_roasts = {
        "d.olo": [
            "D.olo lost and then spent 10 minutes explaining why it was actually smart. Peak French.",
            "D.olo drives like someone who read a book about racing and hasn't forgiven anyone for it.",
            "D.olo will finish last and somehow make you feel bad about winning. The audacity is genuinely impressive.",
            "D.olo is French. He surrendered to the corner before he even got there.",
            "D.olo's biggest opponent isn't the other team. It's his own ego trying to fit in the car.",
        ],
        "boban": [
            "Boban plays like he's settling a blood debt with the track and the track owes him nothing.",
            "Boban disconnects, comes back, and is somehow angrier. Every single time. The man is a war.",
            "Boban is Serbian which means he won't admit he lost even when he's watching the winner's replay.",
            "Boban's race strategy is just aggression with extra steps and zero results.",
            "Serbia produced Nikola Tesla. And then Boban. One of these men changed the world. The other hits walls.",
        ],
        "t0x1c": [
            "t0x1c said something in VC that made three people go quiet. He also finished 7th. Both things matter.",
            "t0x1c has no filter and no podium. One of those is fixable.",
            "The scariest part about t0x1c isn't what he says — it's that he means it and still finishes last.",
            "t0x1c talks like he has zero fear. Drives like he has zero spatial awareness. Both confirmed.",
            "t0x1c is the only player who can lose a race and somehow make it everyone else's fault in real time.",
        ],
        "goxi": [
            "Goxi went full Anakin mid-race and took out two teammates. The prophecy was always real.",
            "Goxi has two names and zero podiums. Pick a lane. Any lane. Please.",
            "Two Serbians in one lobby is already a diplomatic incident. Goxi shows up anyway.",
            "Goxi's driving is so aggressive the Geneva Convention sent a letter.",
            "Goxi said he was built different. He is. Just not in the direction he meant.",
        ],
        "laggeerok": [
            "Laggeerok's connection is so bad the server registers his inputs as war crimes.",
            "Laggeerok finished the race 4 seconds after everyone. On his screen he won. He believes it.",
            "Ukraine has been through a lot. Laggeerok's internet is not helping the situation.",
            "Laggeerok's ping is so high it qualifies as a separate player.",
            "Laggeerok teleports through corners. Not a feature. Not fast. Somehow still loses.",
        ],
        "zipperzbieracz": [
            "ZipperZbieracz calls himself high IQ and then overcuts the same corner every lap. This is what chess players look like in real life.",
            "ZipperZbieracz spent 20 minutes post-race explaining why losing was the smart play. It was not the smart play.",
            "High IQ. Low IQ race line. The paradox that defines ZipperZbieracz continues unresolved.",
            "ZipperZbieracz analyzes every race like a scientist and drives like someone with their eyes closed.",
            "ZipperZbieracz called his own move 5D chess. It was a crash into a stationary wall.",
        ],
        "topke": [
            "Topke is Serbian which already explains everything about his driving style and nothing about why he keeps queuing.",
            "Three Serbians in one lobby. Topke is somehow the most unhinged one and that is genuinely saying something.",
            "Topke shows up every race with full confidence and a completely different result than expected. Always worse.",
            "Topke's race performance is unpredictable in the worst possible way. Could be 1st. Always 8th.",
        ],
        "azaria": [
            "Azaria is the only player whose pre-race ritual involves moisturizer.",
            "Azaria loses races but at least he looks good doing it. Small victories.",
            "Azaria drives with passion. Unfortunately passion doesn't corner well.",
            "Azaria finished last and said the vibes were off. The vibes were fine. He was not.",
        ],
    }

    name_lower = name.lower()
    specific = next((v for k, v in personal_roasts.items() if k in name_lower or name_lower in k), None)
    if specific:
        await ctx.send(random.choice(specific))
        return

    roasts = [
        f"Winning against {name} doesn't count. Losing to {name} is a psychiatric event.",
        f"We don't use {name} as a benchmark for bad. It would be unfair to bad.",
        f"The saddest part about {name}'s performance isn't the result — it's the confidence beforehand.",
        f"Opponents don't celebrate beating {name}. It's like celebrating a bye week.",
        f"{name} has been playing this game long enough to be good. Chose not to.",
        f"Teammates pick {name} last. Opponents pray they get {name}.",
        f"The moment {name} says 'I'm focused tonight' is when you start grieving as a teammate.",
        f"{name} treats team races like a solo activity and somehow still loses.",
        f"Every time {name} joins a lobby the average skill rating goes down and stays down.",
        f"{name} finished behind everyone and still found someone to blame. Respect the commitment.",
        f"Losing to {name} should come with a medical referral.",
        f"I checked {name}'s stats and the database returned a warning. Even MongoDB felt bad.",
        f"{name} is so consistently bad at this point we genuinely think it's a bit. It's not a bit.",
        f"{name} is a 50/50 player — either they cost you the race or they cost you two.",
        f"The barrier on turn 3 has {name} saved as a contact.",
        f"Nobody hypes up a race harder than {name}. Nobody disappears faster after.",
        f"{name}'s car isn't the problem. The car has been trying to escape for months.",
        f"There are players who choke under pressure and players who choke without it. {name} innovated the second category.",
        f"The opposing team specifically requested {name}. That should tell you everything.",
        f"{name} is the only player who makes the other team feel bad for winning.",
    ]
    await ctx.send(random.choice(roasts))


@bot.command(name="setvotes")
@admin_or_dev()
async def set_votes(ctx):
    ch = discord.utils.get(ctx.guild.text_channels, name=TEAM_RACE_CHANNEL)
    if not ch:
        await ctx.send(f"❌ Channel `#{TEAM_RACE_CHANNEL}` not found.")
        return

    pepeyes = discord.utils.get(ctx.guild.emojis, name="pepeyes")
    pepeno  = discord.utils.get(ctx.guild.emojis, name="pepeno")

    if not pepeyes or not pepeno:
        await ctx.send("❌ Could not find `:pepeyes:` or `:pepeno:` emojis in this server.")
        return

    msg = await ch.send("**Are you happy with the teams?**")
    await msg.add_reaction(pepeyes)
    await msg.add_reaction(pepeno)

    if ctx.channel != ch:
        await ctx.message.delete()


@bot.command(name="rvrhelp")
async def rvr_help(ctx):
    embed = discord.Embed(title="🤖 RVR Bot Commands", color=discord.Color.blurple())
    embed.add_field(name="!b3l [session id]",     value="Find (or arm by id) the live RVRU session and check its settings", inline=False)
    embed.add_field(name="!fetch [session id]",   value="Pull races in - with no id, every live public RVRU session at once; updates #times and the overall standings", inline=False)
    embed.add_field(name="!standings",            value="Recompute and repost the overall standings image", inline=False)
    embed.add_field(name="!card [@player]",       value="Show a player's time and title on every track (defaults to you)", inline=False)
    embed.add_field(name="!whois <ingame name>",  value="Show which Discord user an in-game name belongs to", inline=False)
    embed.add_field(name="!beef @someone",        value="Challenge someone to a 3-round roast battle (they have to accept)", inline=False)
    embed.add_field(name="!beefboard",            value="Roast battle standings", inline=False)
    embed.add_field(name="── Admin only ──",      value="\u200b", inline=False)
    embed.add_field(name="!setchannel <role> #chan", value="Set the leaderboard / times / activity / commands channel", inline=False)
    embed.add_field(name="!channels",                    value="Show which channel is used for what", inline=False)
    embed.add_field(name="!refresh", value="Post every track board and the standings again", inline=False)
    embed.add_field(name="!resetseason confirm", value="Wipe every stored race, board and standing - starts the season clean", inline=False)
    embed.add_field(name="!unlinked",                    value="Racers with held results and no Discord user yet", inline=False)
    embed.add_field(name="!link <ingame name> @user",    value="Link an in-game name to a Discord user so results score to them", inline=False)
    embed.add_field(name="!linksuggest",                 value="Show which in-game names auto-match a Discord member (no changes)", inline=False)
    embed.add_field(name="!linkconfirm",                 value="Apply the exact matches from !linksuggest", inline=False)
    embed.add_field(name="!linkskip <ingame name>",      value="Leave a name out of link suggestions (undo with !linkunskip)", inline=False)
    embed.add_field(name="!unlink <ingame name>",        value="Remove a name link", inline=False)
    embed.add_field(name="!links",                       value="Show every linked in-game name", inline=False)
    embed.add_field(name="!racers",                      value="Show everyone seen in results and who still needs linking", inline=False)
    embed.add_field(name="!setrating @player <1-10>",    value="Set a player's skill rating for team balancing", inline=False)
    embed.add_field(name="!maketeams",                   value="Auto-split players in Gather VC into 2 balanced teams", inline=False)
    embed.add_field(name="!ratings",                     value="Show all player ratings", inline=False)
    await ctx.send(embed=embed)

async def main():
    # One asyncio.run() for the whole process lifetime, not two separate
    # ones - Motor binds its client to the event loop of its first
    # operation, and reusing it across a closed-then-new loop (as a
    # standalone asyncio.run() for the lock check, then another inside
    # bot.run()) raises "attached to a different loop" errors.
    if await _claim_instance_lock():
        # Someone was still live. We have taken the lease; give them until
        # their next heartbeat to see that and exit, so we are never both
        # connected and answering the same command twice.
        print(f"⏳ Taking over from a running instance - waiting "
              f"{INSTANCE_HANDOVER_WAIT}s for it to stand down.", flush=True)
        await asyncio.sleep(INSTANCE_HANDOVER_WAIT)
        doc = await state_col.find_one({"key": "instance_lock"})
        if doc and doc.get("owner") != INSTANCE_ID:
            # A third process started after us and took the lease in turn -
            # it is the newest, so it wins and we step aside quietly.
            print("⛔ A newer instance claimed the lock during handover - exiting.", flush=True)
            return

    _install_shutdown_handlers()
    try:
        await bot.start(TOKEN)
    except asyncio.CancelledError:
        pass                      # SIGTERM/SIGINT - shut down quietly
    finally:
        await _release_instance_lock()
        await bot.close()


def _install_shutdown_handlers() -> None:
    """Cancel the running task on SIGTERM/SIGINT so main()'s finally block
    runs and the lease is released.

    Without this the platform's SIGTERM kills the process outright, leaving a
    lease behind that the next deploy has to wait out for no reason. Best
    effort: Windows has no SIGTERM and add_signal_handler is not implemented
    there, so this quietly does nothing locally.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for sig in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, sig, None)
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, task.cancel)
        except (NotImplementedError, RuntimeError):
            pass


if __name__ == "__main__":
    # Import-time side effects (connecting to Mongo, logging into Discord)
    # made this module unimportable for testing without patching bot.run
    # first - this guard is what actually should have prevented that.
    asyncio.run(main())
