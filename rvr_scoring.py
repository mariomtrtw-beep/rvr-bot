"""
The overall RVRU ranking: 13 stock tracks, one score, one title.

Score is the sum of a driver's best time on each of the 13 tracks (Rooftops is
excluded - RVR's own "best laps" ranking never included it either). A track
with no time yet costs 1.5x that track's Street threshold, so skipping a track
is always worse than actually racing it, however slowly.

Titles are per track first: four fixed time targets per track, independent of
who else has raced. Each per-track tier is worth points (Street=1 up to
Legend=4); a driver's overall title comes from the SUM of those points across
all 13 (untouched or below-Street tracks contribute 0), compared against
OVERALL_THRESHOLDS. Deliberately not the weakest track and not gated on
touching every track - a handful of excellent tracks can outrank many
mediocre ones. See tier_for_time / score_driver.

Standalone and pure: no Discord, no database, no network. Everything here is a
plain function over dicts of milliseconds, so it can be tested and re-tuned
without touching the bot.
"""

from __future__ import annotations

from rvgl_results import name_key

# The bot's own normaliser doubles as the track normaliser: it is already
# case/punctuation/spacing insensitive, which is all a track name needs
# ("Toys in the Hood 1" and "TOYS IN THE HOOD 1" must be the same track).
track_key = name_key

TIER_ORDER = ["Street", "Hustler", "Elite", "Legend"]
TIER_EMOJI = {"Street": "🥉", "Hustler": "🥈", "Elite": "🥇", "Legend": "👑"}
TIER_COLOR = {                      # RGB, used by the image renderers
    "Street":  (190, 105, 40),
    "Hustler": (110, 200, 255),      # bright icy blue - the old silver-gray
                                     # was nearly indistinguishable from the
                                     # plain gray used for "no tier / not set"
    "Elite":   (255, 200, 0),
    "Legend":  (255, 80, 210),
}
UNRANKED = "Unranked"
UNRANKED_COLOR = (185, 195, 210)      # was too dark to read against the near-black background

LAPS = 3                            # B3L - divides the summed score for display
PENALTY_MULT = 1.5                  # a missing track costs 1.5x its Street time


def _ms(mmssms: str) -> int:
    m, s, ms = mmssms.split(":")
    return int(m) * 60_000 + int(s) * 1_000 + int(ms)


# Display name -> (Street, Hustler, Elite, Legend), each strictly faster than
# the last. 3-lap B3L targets, harder than RVR's own - see the repo history
# for the derivation from revoltrace.net's public results.
_THRESHOLDS_RAW: dict[str, tuple[str, str, str, str]] = {
    "Toys in the Hood 1": ("02:06:000", "02:05:000", "02:04:000", "02:03:500"),
    "Toys in the Hood 2": ("01:51:000", "01:50:000", "01:49:000", "01:48:500"),
    "Supermarket 1":      ("01:44:000", "01:43:000", "01:41:500", "01:40:500"),
    "Supermarket 2":      ("00:54:000", "00:53:000", "00:52:200", "00:51:900"),
    "Museum 1":           ("02:35:000", "02:34:000", "02:31:500", "02:30:200"),
    "Museum 2":           ("01:48:000", "01:47:000", "01:45:800", "01:44:600"),
    "Botanical Garden":   ("01:10:000", "01:09:500", "01:09:000", "01:08:800"),
    "Toy World 1":        ("01:10:000", "01:08:600", "01:08:000", "01:07:600"),
    "Toy World 2":        ("01:29:000", "01:28:000", "01:27:000", "01:26:700"),
    "Ghost Town 1":       ("01:08:300", "01:07:500", "01:06:500", "01:05:900"),
    "Ghost Town 2":       ("01:36:500", "01:35:500", "01:34:500", "01:34:000"),
    "Toytanic 1":         ("02:20:000", "02:19:000", "02:18:500", "02:18:000"),
    "Toytanic 2":         ("02:21:000", "02:19:500", "02:18:500", "02:17:800"),
}

TRACK_DISPLAY: dict[str, str] = {track_key(name): name for name in _THRESHOLDS_RAW}
CANONICAL_TRACK_KEYS: list[str] = list(TRACK_DISPLAY.keys())   # fixed order
THRESHOLDS_MS: dict[str, dict[str, int]] = {
    track_key(name): dict(zip(TIER_ORDER, (_ms(v) for v in vals)))
    for name, vals in _THRESHOLDS_RAW.items()
}

TIER_RANK = {None: 0, **{tier: i + 1 for i, tier in enumerate(TIER_ORDER)}}

# Overall title: sum these per-track points across all 13 (untouched or
# below-Street = 0) and compare against OVERALL_THRESHOLDS - not the weakest
# track anymore. Even 12-point spacing, tied to the 4-point per-track scale:
# roughly "a tier's worth of points across the board" to advance. Max possible
# is 52 (13 x Legend); Legend overall needs 48, so there is a little slack for
# one so-so track but not several. No coverage floor - a few brilliant tracks
# can outweigh many mediocre ones, deliberately, since the old system's "one
# weak track caps everything" is exactly what this replaces.
TIER_POINTS = {"Street": 1, "Hustler": 2, "Elite": 3, "Legend": 4}
OVERALL_THRESHOLDS = [("Legend", 48), ("Elite", 36), ("Hustler", 24), ("Street", 12)]


def overall_tier_from_points(points: int) -> str:
    for tier, threshold in OVERALL_THRESHOLDS:
        if points >= threshold:
            return tier
    return UNRANKED


def is_canonical_track(track_name: str) -> bool:
    return track_key(track_name) in THRESHOLDS_MS


def tier_for_time(time_ms: int, track: str) -> str | None:
    """Highest tier this time clears on `track`, or None if it misses Street."""
    th = THRESHOLDS_MS[track_key(track)]
    for tier in reversed(TIER_ORDER):          # Legend first - it is the hardest
        if time_ms <= th[tier]:
            return tier
    return None


def penalty_ms(track: str) -> int:
    """What a missing track costs: always worse than any real finish."""
    return int(THRESHOLDS_MS[track_key(track)]["Street"] * PENALTY_MULT)


def score_driver(best_times: dict[str, int]) -> dict:
    """Score one driver from their best time per track key.

    `best_times` maps a canonical track key to their best milliseconds on it -
    tracks they have not raced are simply absent, penalty applied automatically.
    """
    per_track_tier: dict[str, str | None] = {}
    score_ms = 0
    coverage = 0

    for key in CANONICAL_TRACK_KEYS:
        if key in best_times:
            coverage += 1
            score_ms += best_times[key]
            per_track_tier[key] = tier_for_time(best_times[key], key)
        else:
            score_ms += penalty_ms(key)
            per_track_tier[key] = None

    overall_points = sum(TIER_POINTS.get(t, 0) for t in per_track_tier.values())
    return {
        "score_ms":       score_ms,
        "coverage":       coverage,
        "total_tracks":   len(CANONICAL_TRACK_KEYS),
        "per_track_tier": per_track_tier,
        "overall_tier":   overall_tier_from_points(overall_points),
        "overall_points": overall_points,
    }


def format_score(score_ms: int) -> str:
    """RVR-style display: summed time / laps, three decimals of seconds."""
    return f"{score_ms / LAPS / 1000:.3f}"


def ms_to_time(ms: int) -> str:
    minutes, rest = divmod(int(ms), 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{minutes:02d}:{seconds:02d}:{millis:03d}"


if __name__ == "__main__":
    # A driver strong everywhere except one weak track: overall title is no
    # longer capped by the weak track, since the other 12 Legend tracks (48
    # points) alone already clear the Legend threshold.
    times = {k: THRESHOLDS_MS[k]["Legend"] for k in CANONICAL_TRACK_KEYS}
    weak = CANONICAL_TRACK_KEYS[0]
    times[weak] = THRESHOLDS_MS[weak]["Street"]
    result = score_driver(times)
    print(f"12 Legend + 1 Street -> {result['overall_points']} pts, "
          f"{result['overall_tier']} (expected Legend, not capped by {TRACK_DISPLAY[weak]})")
    assert result["overall_tier"] == "Legend"

    # The trade-off this system is built for: a handful of excellent tracks
    # can outrank touching every track at a mediocre level.
    few_but_legend = {k: THRESHOLDS_MS[k]["Legend"] for k in CANONICAL_TRACK_KEYS[:6]}
    r1 = score_driver(few_but_legend)
    print(f"6 Legend, 7 untouched -> {r1['overall_points']} pts, {r1['overall_tier']}")
    assert r1["overall_tier"] == "Hustler"                # 6*4 = 24

    all_but_street = {k: THRESHOLDS_MS[k]["Street"] for k in CANONICAL_TRACK_KEYS}
    r2 = score_driver(all_but_street)
    print(f"all 13 at Street -> {r2['overall_points']} pts, {r2['overall_tier']}")
    assert r2["overall_tier"] == "Street"                 # 13*1 = 13, still under Hustler's 24

    # Missing a track still costs more on the time-based Score than a bad
    # time on it does - that mechanic is unrelated to the title change above.
    missing = dict(times)
    del missing[weak]
    penalized = score_driver(missing)
    assert penalized["score_ms"] > result["score_ms"], "missing a track must cost more than a bad time on it"

    assert score_driver({})["overall_tier"] == UNRANKED   # nothing raced at all
    print("score penalty and title thresholds: OK")
