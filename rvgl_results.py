"""
Parse RVGL session result CSVs into structured race data.

Standalone on purpose: no Discord, no database, no network. Hand it a session
CSV and it hands back the races inside, with every player row marked as counted
or rejected (and why). That makes it testable against saved files without a
live race night.

    python rvgl_results.py <session.csv>
"""

from __future__ import annotations

import csv
import hashlib
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

# ── Rules ─────────────────────────────────────────────────────────────────────
ALLOWED_CARS  = {"toyeca", "humma", "cougar"}   # pro cars only
REQUIRED_LAPS = 3                               # B3L
SESSION_TS_FMT = "%m/%d/%y %H:%M:%S"


# ── Helpers ───────────────────────────────────────────────────────────────────
def name_key(name: str) -> str:
    """Normalise an in-game name so it can be matched to a Discord user.

    NFKC folds stylised unicode (𝙆𝙤𝙩𝙞𝙠_𝙓𝙋 -> Kotik_XP) and stripping
    non-alphanumerics handles case and punctuation (DOLO -> D.olo).
    """
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", name).lower())


def time_to_seconds(t: str) -> float | None:
    """mm:ss:ms -> seconds. Returns None instead of raising on junk input."""
    try:
        parts = t.strip().split(":")
        if len(parts) == 3:
            m, s, ms = parts
            return int(m) * 60 + int(s) + int(ms) / 1000
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(t)
    except (ValueError, AttributeError):
        return None


def _flag(v: str) -> bool:
    return str(v).strip().strip('"').lower() == "true"


# ── Data ──────────────────────────────────────────────────────────────────────
@dataclass
class PlayerResult:
    position:   int
    name_raw:   str
    name_key:   str
    car:        str
    total_time: str            # total time for all 3 laps - this is what scores
    total_s:    float | None
    best_lap:   str            # side stat only, not used for ranking
    best_lap_s: float | None
    finished:   bool
    cheating:   bool
    counted:       bool = False
    reject_reason: str | None = None


@dataclass
class RaceResult:
    block_index: int
    track:       str
    race_key:    str
    players:     list[PlayerResult] = field(default_factory=list)
    counted:       bool = False
    reject_reason: str | None = None

    @property
    def ranked(self) -> list[PlayerResult]:
        """Counted players, fastest total 3-lap time first."""
        return sorted((p for p in self.players if p.counted),
                      key=lambda p: p.total_s)


@dataclass
class Session:
    version:     str
    connection:  str
    host:        str
    started_at:  datetime | None
    started_raw: str
    mode:        str
    laps:        int | None
    races:       list[RaceResult] = field(default_factory=list)
    source_file: str = ""


# ── Parsing ───────────────────────────────────────────────────────────────────
def parse_session(path: str) -> Session:
    """Read a session CSV into a Session. Pure reading - no rules applied."""
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = [r for r in csv.reader(fh) if r and any(c.strip() for c in r)]

    session = Session("", "", "", None, "", "", None, source_file=path)
    block_index = 0
    current: RaceResult | None = None

    for row in rows:
        tag = row[0].strip()

        if tag == "Version":
            session.version    = row[1] if len(row) > 1 else ""
            session.connection = row[2] if len(row) > 2 else ""

        elif tag == "Session":
            session.started_raw = row[1] if len(row) > 1 else ""
            session.host        = row[2] if len(row) > 2 else ""
            session.mode        = row[3] if len(row) > 3 else ""
            try:
                session.laps = int(row[4])
            except (IndexError, ValueError):
                session.laps = None
            try:
                session.started_at = datetime.strptime(session.started_raw, SESSION_TS_FMT)
            except ValueError:
                session.started_at = None

        elif tag == "Results":
            # A new track block. One session file can hold several.
            track = row[1] if len(row) > 1 else "?"
            current = RaceResult(
                block_index=block_index,
                track=track,
                race_key=make_race_key(session, block_index, track),
            )
            session.races.append(current)
            block_index += 1

        elif tag == "#":
            continue  # column header inside a block

        elif current is not None and tag.isdigit():
            current.players.append(_parse_player(row))

    return session


def _parse_player(row: list[str]) -> PlayerResult:
    def cell(i: int) -> str:
        return row[i].strip() if len(row) > i else ""

    total, best_lap = cell(3), cell(4)
    return PlayerResult(
        position   = int(cell(0)),
        name_raw   = cell(1),
        name_key   = name_key(cell(1)),
        car        = cell(2),
        total_time = total,
        total_s    = time_to_seconds(total),
        best_lap   = best_lap,
        best_lap_s = time_to_seconds(best_lap),
        finished   = _flag(cell(5)),
        cheating   = _flag(cell(6)),
    )


def make_race_key(session: Session, block_index: int, track: str) -> str:
    """Stable id for one track within one session - the dedup key."""
    raw = f"{session.started_raw}|{session.host}|{block_index}|{track}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ── Rules ─────────────────────────────────────────────────────────────────────
def apply_rules(session: Session) -> Session:
    """Mark what counts. Kept separate from parsing so rules can change freely."""
    for race in session.races:
        if session.laps != REQUIRED_LAPS:
            race.reject_reason = f"not a B3L session ({session.laps} laps)"
            continue
        race.counted = True

        for p in race.players:
            if p.cheating:
                p.reject_reason = "flagged as cheating"
            elif not p.finished:
                p.reject_reason = "did not finish"
            elif p.car.strip().lower() not in ALLOWED_CARS:
                p.reject_reason = f"car not allowed ({p.car})"
            elif p.total_s is None or p.total_s <= 0:
                p.reject_reason = f"unreadable time ({p.total_time!r})"
            else:
                p.counted = True

    return session


# ── Report ────────────────────────────────────────────────────────────────────
def report(session: Session) -> None:
    print(f"file     {session.source_file}")
    print(f"host     {session.host}")
    print(f"started  {session.started_raw}")
    print(f"mode     {session.mode}   laps: {session.laps}   ({session.connection})")
    print(f"races    {len(session.races)}")

    for race in session.races:
        print(f"\n─ {race.track}   [{race.race_key}]")
        if not race.counted:
            print(f"  SKIPPED - {race.reject_reason}")
            continue

        for i, p in enumerate(race.ranked, 1):
            print(f"  {i}. {p.name_raw:<16} {p.total_time}   (best lap {p.best_lap})"
                  f"   {p.car:<8} key={p.name_key}")

        for p in race.players:
            if not p.counted:
                print(f"  x  {p.name_raw:<16} {p.total_time}   -> {p.reject_reason}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python rvgl_results.py <session.csv>")
    report(apply_rules(parse_session(sys.argv[1])))
