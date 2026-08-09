"""
AI judging for the beef battle: two burns in, one verdict out.

Standalone on purpose: no Discord, no database. It talks to a model provider
and nothing else, so the whole judging path can be exercised from a script
without a live server or a race night.

Provider is chosen by which key is present, so switching is an environment
change rather than a code change:

    GEMINI_API_KEY     -> Google Gemini      (has a free tier)
    ANTHROPIC_API_KEY  -> Claude             (paid, sharper at this)
    neither            -> no judging; the caller crowd-votes instead

Gemini wins if both are set, on the assumption that the free one is the
deliberate choice when someone has bothered to configure it.

A verdict is never guaranteed. The model can decline to score a round, the key
can be missing, the network can fail - all surface the same way, as None.
Callers are expected to read that as "fall back to crowd voting", not as an
error: a duel must never die halfway because the judge had an opinion about
one round.

    python beef_judge.py "your mother" "no, YOUR mother" [persona]
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

CLAUDE_MODEL = "claude-haiku-4-5"   # cheapest Claude - a duel is well under a cent

# Free-tier quota is tracked per model, not per account - the "20 requests"
# limit that bit us was specific to gemini-3.6-flash, not a whole-account cap.
# Falling through to a different model on a rate limit gets a fresh bucket for
# free, no wait required, unlike retrying the same exhausted model. Order is
# best-quality first; "antigravity" is a coding agent product, not a text
# model, and gemini-2.0-flash isn't in Google's current lineup - both are
# excluded rather than guessed at. Verified against ai.google.dev, not memory.
GEMINI_MODELS = [
    "gemini-3.6-flash",        # primary - best quality, likely the one your quota was on
    "gemini-2.5-flash-lite",   # separate family, separate quota bucket
    "gemini-3.5-flash-lite",   # a third bucket if both of the above are tapped
]
MAX_TOKENS   = 400                  # a verdict is three short strings; already generous

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
burns were weak, you can say so honestly in `verdict` while still picking one \
- but don't turn `verdict` into a review. It's one sentence declaring who won \
and why, in the voice of someone hyping a fight, not grading an essay.

YOUR VOICE: you are {persona}

Stay in that voice completely - it is the whole appeal. Keep `hype` instigating, \
not just mocking: get the loser fired up to come back harder, the way a crowd \
does at a real battle - "you just gonna take that?" energy, not a dry insult. \
Both fields in character, one line each."""

# A separate, shorter call fired right after each individual burn lands -
# not a verdict, just a live reaction, so the battle feels commentated turn
# by turn instead of going quiet until the round is scored.
#
# The job here is instigation, not book review. Nobody wants a judge grading
# joke construction between every line - they want a hype man turning to
# {opponent} and going "I wouldn't let that slide" to keep the beef hot.
REACT_TEMPLATE = """You are the hype man for a Discord roast battle in RVR \
Underground, a Re-Volt racing league. {name} just threw a burn at {opponent} - \
this is your reaction to it as it lands, not a verdict on the round.

Your job is to stoke the fire between them, not grade the joke. When a burn \
actually lands, don't just praise the wordplay - turn on {opponent} and get \
in their head about it: tell them that was disrespectful, ask if they're \
really about to let that slide, act like you can't believe they're just \
standing there after that. Make {opponent} want to fire back immediately. \
Real trash-talk energy, not a craft critique.

Weak or lazy burns still get called out - "that's it? that's all you got?" - \
but this should be the exception, not the norm: default to hyping the beef \
itself and needling whoever just got hit, not auditing quality line by line. \
Only go flat and unimpressed when a burn is genuinely nothing.

YOUR VOICE: you are {persona}

Stay in that voice completely. One short line only - this is a between-turns \
reaction, not a speech."""


def persona_names() -> list[str]:
    return list(PERSONAS)


def build_system(persona: str | None) -> str:
    voice = PERSONAS.get(persona or "", PERSONAS[DEFAULT_PERSONA])
    return SYSTEM_TEMPLATE.format(persona=voice)


def build_react_system(persona: str | None, name: str, opponent: str) -> str:
    voice = PERSONAS.get(persona or "", PERSONAS[DEFAULT_PERSONA])
    return REACT_TEMPLATE.format(persona=voice, name=name, opponent=opponent)

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

REACT_SCHEMA = {
    "type": "object",
    "properties": {
        "reaction": {"type": "string",
                    "description": "One short in-character line reacting to this one burn."},
    },
    "required": ["reaction"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    winner: str      # "A" or "B"
    verdict: str
    hype: str


def provider() -> str | None:
    """Which judge is configured: "gemini", "claude", or None.

    Whichever key is present wins - no separate provider setting to keep in
    sync with the keys, which is the usual way this kind of thing ends up
    misconfigured.
    """
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return None


def available() -> bool:
    """Whether AI judging can be attempted at all, without importing anything."""
    return provider() is not None


_client = None
_client_kind = None


def _get_client(kind: str):
    """One shared client per provider, built on first use.

    Imports are deferred so the bot still starts when neither SDK is
    installed - the beef command just runs crowd-voted instead.
    """
    global _client, _client_kind
    if _client is None or _client_kind != kind:
        if kind == "gemini":
            from google import genai
            _client = genai.Client()
        else:
            from anthropic import AsyncAnthropic
            _client = AsyncAnthropic()
        _client_kind = kind
    return _client


async def _call_claude(system: str, prompt: str, schema: dict, max_tokens: int) -> str | None:
    """Raw JSON text from Claude, or None if it declined."""
    client = _get_client("claude")
    response = await client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    # A refusal is a successful response with no usable content, not an
    # exception - checking stop_reason before touching content is what keeps
    # this from raising on the exact calls it exists to handle.
    if response.stop_reason == "refusal":
        print("⚠️ beef judge (claude) declined this one - skipping", flush=True)
        return None
    return next(b.text for b in response.content if b.type == "text")


async def _call_gemini(system: str, prompt: str, schema: dict, model: str) -> str | None:
    """Raw JSON text from Gemini.

    The SDK is synchronous, so it runs in a worker thread rather than
    blocking the bot's event loop for the length of the call - the same
    treatment the coordinator's blocking HTTP gets.
    """
    client = _get_client("gemini")

    def call():
        # System guidance goes inline: `interactions.create` takes a single
        # `input`, so the voice and the rules ride along with the burns
        # instead of a separate system field.
        interaction = client.interactions.create(
            model=model,
            input=f"{system}\n\n---\n\n{prompt}",
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            },
        )
        return interaction.output_text

    return await asyncio.to_thread(call)


RATE_LIMIT_RETRY_CAP = 20   # seconds - long enough to clear a short-lived free-tier
                            # cap, short enough that a human mid-battle barely notices


def _rate_limit_delay(exc: Exception) -> float | None:
    """Seconds to wait before retrying, if this looks like a rate limit.

    None means "not a rate limit, don't bother retrying" - a bad request or a
    genuine outage retrying with the same input just fails the same way again.
    Gemini's free tier states its own suggested delay in the error text
    ("Please retry in 13.4s"); if that's present, honour it - otherwise fall
    back to a fixed short wait for the generic "too many requests" case.
    """
    text = str(exc)
    if "429" not in text and "quota" not in text.lower() and "rate" not in text.lower():
        return None
    import re
    match = re.search(r"retry in ([\d.]+)s", text)
    delay = float(match.group(1)) if match else 5.0
    return min(delay, RATE_LIMIT_RETRY_CAP)


async def _call_gemini_cascade(system: str, prompt: str, schema: dict,
                               label: str) -> tuple[str | None, Exception | None]:
    """Try each configured Gemini model in turn on a rate limit.

    Quota is tracked per model, not per account - a 429 on gemini-3.6-flash
    doesn't mean the account is out of requests, just that model's bucket is.
    Switching models is a free retry with no wait, unlike retrying the same
    exhausted bucket. Returns (text, None) on success, or (None, last error)
    if every model in the list was rate-limited - the caller decides what to
    do next, since only it knows whether a final wait-and-retry is worth it.
    """
    last_exc: Exception | None = None
    for i, model in enumerate(GEMINI_MODELS):
        try:
            return await _call_gemini(system, prompt, schema, model), None
        except Exception as e:
            last_exc = e
            if _rate_limit_delay(e) is None:
                return None, e   # not a quota issue - another model won't help
            if i < len(GEMINI_MODELS) - 1:
                print(f"⏳ beef {label} (gemini/{model}) rate limited - "
                      f"trying {GEMINI_MODELS[i + 1]} instead", flush=True)
    return None, last_exc


async def _call_provider(system: str, prompt: str, schema: dict, *, label: str,
                         fallback_msg: str) -> str | None:
    """Shared dispatch + error handling for both providers.

    Every failure mode collapses to None on purpose: no API key, the SDK
    missing, a network error, a rate limit that doesn't clear, or the model
    declining. None of those should read differently to the caller - the
    response is the same either way, so a duel or a between-turns reaction
    never breaks over it.
    """
    kind = provider()
    if kind is None:
        return None

    if kind == "gemini":
        text, exc = await _call_gemini_cascade(system, prompt, schema, label)
        if text is not None:
            return text
        if exc is None:
            return None
        # Every model in the cascade was rate-limited - one last wait, using
        # that model's own suggested delay, before giving up entirely.
        delay = _rate_limit_delay(exc)
        if delay is not None:
            print(f"⏳ beef {label} (gemini) all models rate limited, "
                  f"retrying {GEMINI_MODELS[-1]} in {delay:.0f}s", flush=True)
            await asyncio.sleep(delay)
            try:
                return await _call_gemini(system, prompt, schema, GEMINI_MODELS[-1])
            except Exception as e:
                exc = e
        print(f"⚠️ beef {label} (gemini) unavailable ({exc.__class__.__name__}: {exc}) "
              f"- {fallback_msg}", flush=True)
        return None

    # Claude: one model, no free quota-bucket switch available - just one
    # wait-and-retry on a rate limit, same as before.
    for attempt in range(2):
        try:
            return await _call_claude(system, prompt, schema, MAX_TOKENS)
        except Exception as e:
            delay = _rate_limit_delay(e) if attempt == 0 else None
            if delay is not None:
                print(f"⏳ beef {label} (claude) rate limited, retrying in {delay:.0f}s",
                      flush=True)
                await asyncio.sleep(delay)
                continue
            print(f"⚠️ beef {label} (claude) unavailable ({e.__class__.__name__}: {e}) "
                  f"- {fallback_msg}", flush=True)
            return None


async def judge_round(name_a: str, burn_a: str, name_b: str, burn_b: str,
                      round_no: int = 1, persona: str | None = None) -> Verdict | None:
    """Score one exchange. None means "no verdict - use crowd voting instead"."""
    prompt = (
        f"Round {round_no} of a roast battle.\n\n"
        f"Racer A ({name_a}) said:\n{burn_a}\n\n"
        f"Racer B ({name_b}) said:\n{burn_b}\n\n"
        f"Who won this round?"
    )
    text = await _call_provider(build_system(persona), prompt, SCHEMA,
                                label="judge", fallback_msg="crowd voting instead")
    if text is None:
        return None

    import json
    try:
        data = json.loads(text)
        return Verdict(winner=data["winner"], verdict=data["verdict"], hype=data["hype"])
    except (ValueError, KeyError, TypeError) as e:
        print(f"⚠️ beef judge sent something unparseable "
              f"({e.__class__.__name__}) - crowd voting instead", flush=True)
        return None


async def react_to_burn(name: str, burn: str, opponent: str,
                        persona: str | None = None) -> str | None:
    """A quick in-character reaction to one burn, right as it lands.

    Purely flavor - unlike judge_round, there is nothing downstream relying
    on this succeeding. None just means say nothing for this turn, same as
    if the feature were never enabled at all.
    """
    prompt = f"{name}'s burn, aimed at {opponent}:\n{burn}"
    text = await _call_provider(build_react_system(persona, name, opponent), prompt,
                                REACT_SCHEMA, label="reaction", fallback_msg="skipping")
    if text is None:
        return None

    import json
    try:
        return json.loads(text)["reaction"]
    except (ValueError, KeyError, TypeError) as e:
        print(f"⚠️ beef reaction sent something unparseable "
              f"({e.__class__.__name__}) - skipping", flush=True)
        return None


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit('usage: python beef_judge.py "<burn a>" "<burn b>" [persona]\n'
                 f'       personas: {", ".join(persona_names())}')
    if not available():
        sys.exit("No GEMINI_API_KEY or ANTHROPIC_API_KEY set "
                 "- judging would fall back to crowd voting")
    print(f"[provider: {provider()}]")

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
