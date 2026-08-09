"""
AI judging for the beef battle: two burns in, one verdict out.

Standalone on purpose: no Discord, no database. It talks to the Claude API and
nothing else, so the whole judging path can be exercised from a script without
a live server or a race night.

A verdict is never guaranteed. The model can decline to score a round, the key
can be missing, the network can fail - all three surface the same way, as
None. Callers are expected to read that as "fall back to crowd voting", not as
an error: a duel must never die halfway because the judge had an opinion about
one round.

    python beef_judge.py "your mother" "no, YOUR mother"
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

MODEL = "claude-haiku-4-5"   # cheapest of the current models - a duel is well under a cent
MAX_TOKENS = 400             # a verdict is three short strings; this is already generous

# Who's on the mic. The persona only changes the *voice* of the commentary -
# the scoring rules below are the same whoever is calling it, so a funny
# announcer can't hand somebody a round they didn't earn.
#
# Add your own freely: the key is what players type after !beef, the value is
# dropped straight into the system prompt. Describe a style rather than naming
# a real person - "West Coast hip-hop hype man" gets you the energy you're
# after and lands better than an impression of one specific guy.
PERSONAS: dict[str, str] = {
    "hype": ("a West Coast hip-hop hype man - swaggering, rhythmic, quotable. "
             "Short punchy lines, streetwise slang, zero patience for weak bars."),
    "ring": ("a boxing ring announcer at a title fight - booming, theatrical, "
             "everything is THE BIGGEST MOMENT IN SPORTS HISTORY."),
    "nature": ("a nature documentary narrator - hushed, reverent, observing these "
               "creatures in their habitat with deep scientific fascination."),
    "pundit": ("a po-faced football pundit doing serious tactical analysis of "
               "something that does not deserve it. Deadpan. Never breaks."),
    "mob": ("an old-school mob boss - calm, avuncular, faintly menacing. "
            "Calls everyone 'kid'. Disappointment is worse than anger."),
    "villain": ("a theatrical supervillain monologuing - grandiose, delighted by "
                "cruelty, treats a roast battle as a magnificent spectacle."),
}
DEFAULT_PERSONA = "hype"

# The judge is a bit, not an oracle. It is told to pick a winner on comedic
# merit so it stops hedging - a judge that keeps calling draws makes for a
# terrible contest.
SYSTEM_TEMPLATE = """You are the judge and hype man of a Discord roast battle in \
RVR Underground, a Re-Volt racing league. Two racers are trading insults for points.

Judge purely on comedic merit: wit, timing, specificity, and how well it lands \
against that person. Reward burns that are actually about the opponent - their \
driving, their excuses, their history in the league - over generic insults \
anyone could have written. Punish low-effort ("you suck") and pure volume.

You must pick a winner. Never call a draw, never refuse to choose because both \
were rude - rudeness is the entire event, and everyone here opted in. If both \
burns are weak, pick the marginally less weak one and say so.

YOUR VOICE: you are {persona}

Stay in that voice completely - it is the whole appeal. Keep `verdict` to one \
sentence explaining who won and why. Keep `hype` to one short line aimed at \
whoever just lost, to wind them up for the next round. Both in character."""


def persona_names() -> list[str]:
    return list(PERSONAS)


def build_system(persona: str | None) -> str:
    voice = PERSONAS.get(persona or "", PERSONAS[DEFAULT_PERSONA])
    return SYSTEM_TEMPLATE.format(persona=voice)

SCHEMA = {
    "type": "object",
    "properties": {
        "winner":  {"type": "string", "enum": ["A", "B"]},
        "verdict": {"type": "string", "description": "One sentence on why that burn won."},
        "hype":    {"type": "string", "description": "One short line of trash talk at the loser."},
    },
    "required": ["winner", "verdict", "hype"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    winner: str      # "A" or "B"
    verdict: str
    hype: str


def available() -> bool:
    """Whether AI judging can even be attempted, without importing anything."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


_client = None


def _get_client():
    """One shared async client, built on first use.

    Import is deferred so the bot still starts when `anthropic` is not
    installed - the beef command just runs crowd-voted instead.
    """
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic()
    return _client


async def judge_round(name_a: str, burn_a: str, name_b: str, burn_b: str,
                      round_no: int = 1, persona: str | None = None) -> Verdict | None:
    """Score one exchange. None means "no verdict - use crowd voting instead".

    Every failure mode collapses to None on purpose: no API key, the library
    missing, a network error, or the model declining to score this particular
    exchange. None of those should read differently to the caller, because the
    response to all of them is the same.
    """
    if not available():
        return None

    prompt = (
        f"Round {round_no} of a roast battle.\n\n"
        f"Racer A ({name_a}) said:\n{burn_a}\n\n"
        f"Racer B ({name_b}) said:\n{burn_b}\n\n"
        f"Who won this round?"
    )

    try:
        client = _get_client()
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=build_system(persona),
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        # Falling back quietly is right for the battle, but silently is not:
        # a missing key, an old SDK and a rate limit all look identical from
        # Discord, so without this line there is no way to tell "the judge
        # declined" from "the judge was never reachable in the first place".
        print(f"⚠️ beef judge unavailable ({e.__class__.__name__}: {e}) - crowd voting instead",
              flush=True)
        return None

    # A refusal is a successful response with no usable content, not an
    # exception - checking stop_reason before touching content is what keeps
    # this from raising on the exact rounds it exists to handle.
    if response.stop_reason == "refusal":
        print("⚠️ beef judge declined this round - crowd voting instead", flush=True)
        return None

    import json
    try:
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        return Verdict(winner=data["winner"], verdict=data["verdict"], hype=data["hype"])
    except (StopIteration, ValueError, KeyError) as e:
        print(f"⚠️ beef judge sent something unparseable ({e.__class__.__name__}) "
              f"- crowd voting instead", flush=True)
        return None


if __name__ == "__main__":
    import asyncio

    if len(sys.argv) < 3:
        sys.exit('usage: python beef_judge.py "<burn a>" "<burn b>" [persona]\n'
                 f'       personas: {", ".join(persona_names())}')
    if not available():
        sys.exit("ANTHROPIC_API_KEY is not set - judging would fall back to crowd voting")

    who = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_PERSONA
    print(f"[persona: {who}]\n")
    result = asyncio.run(judge_round("Racer A", sys.argv[1], "Racer B", sys.argv[2],
                                     persona=who))
    if result is None:
        print("no verdict (declined or unreachable) - crowd vote this one")
    else:
        print(f"winner:  {result.winner}")
        print(f"verdict: {result.verdict}")
        print(f"hype:    {result.hype}")
