"""
Watch net.rv.gl for a named RVGL session going up or down.

The coordinator publishes a Server-Sent Events stream at /api/sessions/events.
Each frame is the full list of active sessions as JSON, so we hold one long
lived connection instead of polling - the site tells us when something changes.

Standalone: no Discord, no database, no RVGL. It just prints when the session
you named appears or disappears.

    python coordinator_probe.py --list           # show what's live right now
    python coordinator_probe.py "rvru months"    # watch for that session
"""

from __future__ import annotations

import json
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SSE_URL = "https://net.rv.gl/api/sessions/events"
USER_AGENT = "rvru-probe/0.1 (+RVR Underground league bot)"

# Reconnect backoff, mirroring the site's own live.js so we behave like a
# normal browser tab rather than hammering it.
RETRY_MIN, RETRY_MAX, RETRY_JITTER = 1.0, 30.0, 0.25


def ssl_context() -> ssl.SSLContext:
    """Verified TLS, using certifi's CA bundle when it is available.

    Python on Windows may fall back to an OS trust store that still contains an
    expired root, which fails against a perfectly valid certificate. certifi
    sidesteps that. Verification stays on either way.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def norm(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def say(*parts) -> None:
    """Print and flush - this runs for hours, output should not sit in a buffer."""
    print(*parts, flush=True)


# ── Stream ────────────────────────────────────────────────────────────────────
def read_frames(url: str = SSE_URL):
    """Yield each decoded JSON frame from the SSE stream (one connection)."""
    req = urllib.request.Request(url, headers={
        "Accept": "text/event-stream",
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=90, context=ssl_context()) as resp:
        buf: list[str] = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":                      # blank line ends an event
                if buf:
                    try:
                        yield json.loads("\n".join(buf))
                    except json.JSONDecodeError:
                        pass                    # ignore malformed frames
                    buf = []
            elif line.startswith("data:"):
                buf.append(line[5:].lstrip())
            # ':' comments, 'event:', 'id:', 'retry:' are not needed here


def sessions_from(frame) -> list[dict]:
    """The stream sends a bare list; tolerate a dict wrapper just in case."""
    if isinstance(frame, list):
        return frame
    if isinstance(frame, dict):
        for key in ("sessions", "data", "active"):
            if isinstance(frame.get(key), list):
                return frame[key]
    return []


def find(sessions: list[dict], target: str) -> dict | None:
    want = norm(target)
    for s in sessions:
        if norm(s.get("name", "")) == want:
            return s
    return None


def describe(s: dict) -> str:
    bits = [
        f"players={s.get('player_count', '?')}",
        f"region={s.get('region_name') or s.get('region', '?')}",
        f"host={s.get('host', '?')}",
        "public" if s.get("public") else "private",
    ]
    if s.get("official"):
        bits.append("official")
    return "  ".join(bits)


# ── Modes ─────────────────────────────────────────────────────────────────────
def list_once() -> None:
    """Print the first frame and exit - handy for checking names."""
    for frame in read_frames():
        sessions = sessions_from(frame)
        if not sessions:
            print("no active sessions")
            return
        print(f"{len(sessions)} active session(s):\n")
        for s in sessions:
            print(f"  {s.get('name', '?')!r}")
            print(f"      {describe(s)}   created {s.get('created_at', '?')}")
        return


def watch(target: str) -> None:
    """Report when `target` goes up or down. Runs until interrupted."""
    say(f"[{stamp()}] watching for session {target!r} ... (ctrl-c to stop)")
    live = False
    delay = RETRY_MIN

    while True:
        try:
            for frame in read_frames():
                delay = RETRY_MIN                      # a good frame resets backoff
                found = find(sessions_from(frame), target)

                if found and not live:
                    live = True
                    say(f"[{stamp()}] UP    {target!r} -> True")
                    say(f"           {describe(found)}")
                elif not found and live:
                    live = False
                    say(f"[{stamp()}] DOWN  {target!r} -> False")

        except KeyboardInterrupt:
            say(f"\n[{stamp()}] stopped")
            return
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            say(f"[{stamp()}] connection lost ({e.__class__.__name__}) "
                  f"- retrying in {delay:.0f}s")

        # Stream ended or errored: back off, then reconnect.
        time.sleep(delay + random.random() * RETRY_JITTER)
        delay = min(delay * 2, RETRY_MAX)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__.strip().splitlines()[-1].strip())
    if args[0] == "--list":
        list_once()
    else:
        watch(" ".join(args))
