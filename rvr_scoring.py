"""
The overall RVRU ranking: 13 stock tracks, one score, one title.

Score is the sum of a driver's best time on each of the 13 tracks (Rooftops is
excluded - RVR's own "best laps" ranking never included it either). A track
with no time yet costs 1.5x that track's Street threshold, so skipping a track
is always worse than actually racing it, however slowly.

Titles are per track first: four fixed time targets per track, independent of
who else has raced. A driver's overall title is the WEAKEST tier they hold
across all 13 - not an average, not a majority. One slow track holds the whole
title down, on purpose: the title should only ever say something about a
driver's worst driving, never their best. See tier_for_time / overall_tier.

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
RANK_TIER = {v: k for k, v in TIER_RANK.items() if k is not None}


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

    overall_rank = min(TIER_RANK[per_track_tier[k]] for k in CANONICAL_TRACK_KEYS)
    return {
        "score_ms":       score_ms,
        "coverage":       coverage,
        "total_tracks":   len(CANONICAL_TRACK_KEYS),
        "per_track_tier": per_track_tier,
        "overall_tier":   RANK_TIER.get(overall_rank, UNRANKED),
        "overall_rank":   overall_rank,
    }


def format_score(score_ms: int) -> str:
    """RVR-style display: summed time / laps, three decimals of seconds."""
    return f"{score_ms / LAPS / 1000:.3f}"


def ms_to_time(ms: int) -> str:
    minutes, rest = divmod(int(ms), 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{minutes:02d}:{seconds:02d}:{millis:03d}"


if __name__ == "__main__":
    # Quick self-check: a driver strong everywhere except one weak track.
    times = {k: THRESHOLDS_MS[k]["Legend"] for k in CANONICAL_TRACK_KEYS}
    weak = CANONICAL_TRACK_KEYS[0]
    times[weak] = THRESHOLDS_MS[weak]["Street"]        # only clears Street here

    result = score_driver(times)
    print(f"score: {format_score(result['score_ms'])}  "
          f"({result['coverage']}/{result['total_tracks']} tracks)")
    print(f"overall title: {result['overall_tier']}  "
          f"(should be Street - {TRACK_DISPLAY[weak]} is the weak link)")
    assert result["overall_tier"] == "Street"

    missing = dict(times)
    del missing[weak]
    penalized = score_driver(missing)
    assert penalized["score_ms"] > result["score_ms"], "missing a track must cost more than a bad time on it"
    assert penalized["overall_tier"] == UNRANKED
    print("missing-track penalty and Unranked gating: OK")
