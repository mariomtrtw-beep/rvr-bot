"""
Read finished races straight from the coordinator's session API.

`GET /api/sessions/<id>` returns a live session, and its History array holds one
entry per completed race with integer millisecond times and a unique GameID.
That is a better source than the CSV export: no string time parsing, a real
dedup key, and the canonical track name on every race.

The export at /api/sessions/<id>/export/session stays useful as an audit copy,
since everything 404s once the session closes.

    python coordinator_ingest.py <session-id>
    python coordinator_ingest.py --watch <session-id>
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from coordinator_probe import USER_AGENT, say, ssl_context
from rvgl_results import ALLOWED_CARS, REQUIRED_LAPS, name_key

BASE = "https://net.rv.gl"
POLL_SECONDS = 30
ALLOW_PICKUPS = False          # "no picks" - weapons off


# ── Helpers ───────────────────────────────────────────────────────────────────
def ms_to_time(ms: int) -> str:
    """148155 -> '02:28:155', the mm:ss:ms form already stored in the bot's DB."""
    minutes, rest = divmod(int(ms), 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{minutes:02d}:{seconds:02d}:{millis:03d}"


def epoch_ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


# ── Data ──────────────────────────────────────────────────────────────────────
@dataclass
class Entry:
    position:    int
    player_id:   int
    name_raw:    str
    name_key:    str
    car:         str
    time_ms:     int
    best_lap_ms: int
    finished:    bool
    cheating:    bool
    counted:       bool = False
    reject_reason: str | None = None

    @property
    def time(self) -> str:
        return ms_to_time(self.time_ms)

    @property
    def best_lap(self) -> str:
        return ms_to_time(self.best_lap_ms)


@dataclass
class Race:
    game_id:     int          # unique per race - the dedup key
    number:      int
    track_dir:   str
    track:       str
    laps:        int
    pickups:     bool
    custom:      bool
    finished_at: datetime | None
    entries:     list[Entry] = field(default_factory=list)
    counted:       bool = False
    reject_reason: str | None = None

    @property
    def ranked(self) -> list[Entry]:
        return sorted((e for e in self.entries if e.counted), key=lambda e: e.time_ms)


# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_session(session_id: str) -> dict | None:
    """Session JSON, or None if it has closed (everything 404s then)."""
    req = urllib.request.Request(
        f"{BASE}/api/sessions/{session_id}",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl_context()) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def races_from(session: dict) -> list[Race]:
    races = []
    for h in session.get("History") or []:
        game_id = h.get("GameID")
        if not game_id:
            # GameID is the only dedup key store_races has - a race without
            # one would look identical to any other GameID-less race to that
            # dedup check, silently swallowing every one after the first
            # instead of erroring. Loud and skipped beats quiet and wrong.
            print(f"⚠️ coordinator sent a race with no GameID, skipping: {h!r}"[:500])
            continue
        race = Race(
            game_id     = game_id,
            number      = h.get("Number", 0),
            track_dir   = h.get("TrackDir", ""),
            track       = h.get("TrackName", "?"),
            laps        = h.get("Laps", 0),
            pickups     = bool(h.get("Pickups")),
            custom      = bool(h.get("CustomTrack")),
            finished_at = epoch_ms_to_dt(h["FinishedAt"]) if h.get("FinishedAt") else None,
        )
        for e in h.get("Entries") or []:
            race.entries.append(Entry(
                position    = e.get("Position", 0),
                player_id   = e.get("PlayerID", 0),
                name_raw    = e.get("Name", ""),
                name_key    = name_key(e.get("Name", "")),
                car         = e.get("CarName", ""),
                time_ms     = e.get("TimeMS", 0),
                best_lap_ms = e.get("BestLapMS", 0),
                finished    = bool(e.get("Finished")),
                cheating    = bool(e.get("Cheating")),
            ))
        races.append(race)
    return races


# ── Rules ─────────────────────────────────────────────────────────────────────
def apply_rules(races: list[Race]) -> list[Race]:
    for race in races:
        # History includes the race currently being played; it has no FinishedAt
        # and zeroed times. Never score it - and never mark it seen, or the real
        # result is skipped as a duplicate once it lands.
        if race.finished_at is None:
            race.reject_reason = "still in progress"
            continue
        if race.laps != REQUIRED_LAPS:
            race.reject_reason = f"not a B3L race ({race.laps} laps)"
            continue
        if race.pickups and not ALLOW_PICKUPS:
            race.reject_reason = "pickups were enabled"
            continue
        race.counted = True

        for e in race.entries:
            if e.cheating:
                e.reject_reason = "flagged as cheating"
            elif not e.finished:
                e.reject_reason = "did not finish"
            elif e.car.strip().lower() not in ALLOWED_CARS:
                e.reject_reason = f"car not allowed ({e.car})"
            elif e.time_ms <= 0:
                e.reject_reason = f"bad time ({e.time_ms}ms)"
            else:
                e.counted = True
    return races


# ── Report ────────────────────────────────────────────────────────────────────
def report(session: dict, races: list[Race]) -> None:
    say(f"session  {session.get('Name')!r}   state={session.get('State')}   "
        f"port={session.get('Port')}   cars={session.get('CarRating')}")
    say(f"races    {len(races)} in History")

    for r in races:
        say(f"\n─ race {r.number}: {r.track}   [GameID {r.game_id}]  dir={r.track_dir}")
        if not r.counted:
            say(f"    SKIPPED - {r.reject_reason}")
            continue
        for i, e in enumerate(r.ranked, 1):
            say(f"  {i}. {e.name_raw:<16} {e.time}   (best lap {e.best_lap})"
                f"   {e.car:<8} key={e.name_key}")
        for e in r.entries:
            if not e.counted:
                say(f"  x  {e.name_raw:<16} {e.time}   -> {e.reject_reason}")


# ── Ladder ────────────────────────────────────────────────────────────────────
@dataclass
class Best:
    name_key:  str
    name_raw:  str
    time_ms:   int
    best_lap_ms: int
    car:       str
    game_id:   int
    attempts:  int = 1


def build_ladder(races: list[Race]) -> dict[str, list[Best]]:
    """Collapse every counted race into one best time per player per track.

    Same rule the bot already applies on approval: a repeat only replaces the
    stored time if it is faster. Racing a track four times leaves one row.
    """
    ladder: dict[str, dict[str, Best]] = {}
    for race in races:
        if not race.counted:
            continue
        per_track = ladder.setdefault(race.track, {})
        for e in race.ranked:
            prev = per_track.get(e.name_key)
            if prev is None:
                per_track[e.name_key] = Best(e.name_key, e.name_raw, e.time_ms,
                                             e.best_lap_ms, e.car, race.game_id)
            else:
                prev.attempts += 1
                if e.time_ms < prev.time_ms:      # keep only if faster
                    prev.time_ms = e.time_ms
                    prev.car = e.car
                    prev.game_id = race.game_id
                prev.best_lap_ms = min(prev.best_lap_ms, e.best_lap_ms)
    return {t: sorted(v.values(), key=lambda b: b.time_ms) for t, v in ladder.items()}


def report_ladder(races: list[Race]) -> None:
    ladder = build_ladder(races)
    if not ladder:
        say("\nno counted races - nothing on the ladder")
        return
    say("\n══ ladder (best per player per track) ══")
    for track, bests in ladder.items():
        say(f"\n🏁 {track}")
        for i, b in enumerate(bests, 1):
            extra = f"  [best of {b.attempts} runs]" if b.attempts > 1 else ""
            say(f"  {i}. {b.name_raw:<16} {ms_to_time(b.time_ms)}"
                f"   (best lap {ms_to_time(b.best_lap_ms)}){extra}")


def watch(session_id: str) -> None:
    """Poll while the session lives, reporting each new race once."""
    seen: set[int] = set()
    say(f"watching session {session_id} (ctrl-c to stop)")
    while True:
        session = fetch_session(session_id)
        if session is None:
            say("session closed - nothing more to collect")
            return
        for r in apply_rules(races_from(session)):
            if r.finished_at is None:
                continue                       # come back once it has finished
            if r.game_id in seen:
                continue                       # dedup on GameID
            seen.add(r.game_id)
            report_race(r)
        time.sleep(POLL_SECONDS)


def report_race(r: Race) -> None:
    say(f"\n[new] race {r.number}: {r.track}   [GameID {r.game_id}]")
    if not r.counted:
        say(f"    SKIPPED - {r.reject_reason}")
        return
    for i, e in enumerate(r.ranked, 1):
        say(f"  {i}. {e.name_raw:<16} {e.time}   {e.car}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit("usage: python coordinator_ingest.py [--watch] <session-id>")
    if args[0] == "--watch":
        watch(args[1])
    else:
        if args[0].endswith(".json"):
            # Saved fixture - sessions 404 once closed, so keep copies to test on.
            with open(args[0], encoding="utf-8") as fh:
                s = json.load(fh)
        else:
            s = fetch_session(args[0])
        if s is None:
            sys.exit("session not found (already closed?)")
        races = apply_rules(races_from(s))
        report(s, races)
        report_ladder(races)
