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
# excluded rather than guessed at.
#
# Stick to the 3.x ("Stable") family only. gemini-2.5-flash-lite is still
# documented but 404s for newer API keys ("no longer available to new
# users") - the docs and what a given account can actually call have drifted
# apart, so prefer models from the generation that's still being onboarded.
#
# This list was cross-checked against the models actually visible in one
# real account's AI Studio dropdown, not just the docs - the docs/account
# mismatch is exactly what caused the last failure. "Lite" variants are
# ordered before the full model since they're built for higher free-tier
# volume. gemini-3-flash-preview is still in Preview status (per
# ai.google.dev) even though AI Studio's dropdown didn't say so - lowest
# confidence of the five, so it's last. If its real ID has since changed,
# the 404-cascade fix means it just gets skipped, not a broken chain.
GEMINI_MODELS = [
    "gemini-3.6-flash",        # primary - best quality, likely the one your quota was on
    "gemini-3.5-flash-lite",   # same generation, separate quota bucket
    "gemini-3.1-flash-lite",   # a third bucket
    "gemini-3.5-flash",        # full model, not lite - a fourth bucket
    "gemini-3-flash-preview",  # least certain ID - still Preview status per docs
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

# Margin -> points. The judge never sees the running score (see judge_round's
# docstring for why) - it only ever says how decisively THIS exchange went,
# and the caller converts that to points. Keeping "how good was this" and
# "who's ahead overall" as two separate concerns is what lets the reaction
# below talk about the scoreboard without the judge being tempted to skew a
# call because someone's already behind.
MARGIN_POINTS = {"small": 1, "medium": 2, "large": 3}

# The judge is a bit, not an oracle. It is told to pick a winner on comedic
# merit so it stops hedging - a judge that keeps calling draws makes for a
# terrible contest.
SYSTEM_TEMPLATE = """You are the judge of a Discord roast battle in RVR \
Underground, a Re-Volt racing league. Two racers are trading insults; each \
exchange you score awards the winner points toward a target, so a decisive \
exchange should be worth more than a narrow one.

Judge purely on comedic merit: wit, timing, specificity, and how well it lands \
against that person. Reward burns that are actually about the opponent - their \
driving, their excuses, their history in the league - over generic insults \
anyone could have written. Punish low-effort ("you suck") and pure volume.

You must pick a winner - never a draw, never a refusal because both were rude; \
rudeness is the entire event and everyone here opted in. Then rate the margin: \
`large` if one burn clearly outclassed the other, `medium` for a real but not \
huge gap, `small` if it was close and you're mostly picking the slightly better \
one. Use the full range - `medium` is not the safe default when you're unsure. \
Ask yourself "was this close?" first (if yes, `small`) and "did one completely \
outclass the other?" second (if yes, `large`); only land on `medium` when it's \
genuinely neither. If you notice yourself picking `medium` most of the time, \
you are hedging - go back and commit to `small` or `large` instead.

Separately, judge `loser_landed`: true if the LOSING burn was ALSO genuinely \
good on its own terms - specific, landed a real hit, would have won against a \
weaker burn - and just happened to run into something even better this time. \
False only if it was actually generic, low-effort, or a non-sequitur. Default \
to true: two people who showed up to a roast battle are usually both landing \
something, so most exchanges should have `loser_landed: true` - reserve false \
for the genuinely weak burns, not just "the one that didn't win." This is \
independent of the margin, but they usually correlate: a `small` margin with \
`loser_landed: true` means the exchange was basically a wash between two good \
burns; a `large` margin more often means `loser_landed: false`, since a real \
blowout usually does mean \
the other side landed nothing worth crediting.

`verdict` is one sentence declaring who won and why - honestly, including when \
neither burn was very good. Not a review, just the call.

YOUR VOICE: you are {persona}

Stay in that voice completely - it is the whole appeal. One line each field."""

# A separate, shorter call fired right after each individual burn lands - not
# a verdict, just a live reaction, so the battle feels commentated turn by
# turn instead of going quiet until the exchange is scored.
#
# This is the one call that DOES see the running score, specifically so it
# can react to the actual state of the fight ("he's down 5, this better be
# good") instead of blindly instigating whoever just got hit regardless of
# who's winning - which was the whole problem with the old version.
REACT_TEMPLATE = """You are the hype man for a Discord roast battle in RVR \
Underground, a Re-Volt racing league. First to {target} points wins. {name} \
just threw a burn at {opponent} - this is your reaction to it as it lands, \
not a verdict.

Current score: {name} {name_score} - {opponent_score} {opponent}{score_note}

React to the burn IN LIGHT OF that score - don't contradict it. If {name} is \
already comfortably ahead, this is piling on, not an underdog fighting back. \
If {name} is behind, frame it as clawing their way back in, not "taking the \
lead." If it's close, treat it like it matters. Never tell someone to "watch \
out" or "you're in trouble" if the scoreboard says they're winning easily - \
that is the one thing you must always get right.

Your job is to stoke the fire, not grade the joke - don't praise wordplay, \
react to what it means for the fight. Weak or lazy burns can get called out \
("that's it? that's all you got?") but that should be the exception, not the \
norm - default to hyping the exchange and the stakes, not auditing quality \
line by line.

YOUR VOICE: you are {persona}

Stay in that voice completely. One short line only - this is a between-turns \
reaction, not a speech."""


def persona_names() -> list[str]:
    return list(PERSONAS)


def build_system(persona: str | None) -> str:
    voice = PERSONAS.get(persona or "", PERSONAS[DEFAULT_PERSONA])
    return SYSTEM_TEMPLATE.format(persona=voice)


def build_react_system(persona: str | None, name: str, opponent: str, target: int,
                       name_score: int, opponent_score: int) -> str:
    voice = PERSONAS.get(persona or "", PERSONAS[DEFAULT_PERSONA])
    note = ""
    if name_score == 0 and opponent_score == 0:
        note = "  (first blood - nobody's on the board yet)"
    return REACT_TEMPLATE.format(persona=voice, name=name, opponent=opponent,
                                 target=target, name_score=name_score,
                                 opponent_score=opponent_score, score_note=note)

SCHEMA = {
    "type": "object",
    "properties": {
        "winner":  {"type": "string", "enum": ["A", "B"]},
        "margin":  {"type": "string", "enum": ["small", "medium", "large"],
                   "description": "How decisive this exchange was."},
        "loser_landed": {"type": "boolean",
                         "description": "Whether the losing burn was also genuinely good "
                                        "on its own terms, independent of who won."},
        "verdict": {"type": "string", "description": "One sentence on why that burn won."},
    },
    "required": ["winner", "margin", "loser_landed", "verdict"],
    "additionalProperties": False,
}

REACT_SCHEMA = {
    "type": "object",
    "properties": {
        "reaction": {"type": "string",
                    "description": "One short in-character line reacting to this one burn "
                                   "in light of the current score."},
    },
    "required": ["reaction"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    winner: str          # "A" or "B"
    margin: str          # "small", "medium", or "large"
    loser_landed: bool   # did the losing burn also genuinely land?
    verdict: str

    @property
    def winner_points(self) -> int:
        return MARGIN_POINTS[self.margin]

    @property
    def loser_points(self) -> int:
        # A small-margin exchange where the loser also landed is a wash - 1
        # point each, not a manufactured tie, just two genuinely close scores.
        return 1 if self.loser_landed else 0


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


def _model_unavailable(exc: Exception) -> bool:
    """Whether this model itself is the problem - deprecated, removed, or
    never available to this account - as distinct from a rate limit.

    Docs and what a given API key can actually call drift apart (a model
    still documented can 404 with "no longer available to new users"), so
    this is judged from the error text rather than a maintained allowlist.
    Treated the same as a rate limit for cascading to the next model - but
    unlike a rate limit, never worth a wait-and-retry afterward, since a
    removed model does not come back.
    """
    text = str(exc).lower()
    return "404" in text or "not_found" in text or "not found" in text \
        or "no longer available" in text


async def _call_gemini_cascade(system: str, prompt: str, schema: dict,
                               label: str) -> tuple[str | None, Exception | None]:
    """Try each configured Gemini model in turn on a rate limit or a model
    that's gone.

    Quota is tracked per model, not per account - a 429 on gemini-3.6-flash
    doesn't mean the account is out of requests, just that model's bucket is
    - and a 404 means only that specific model is gone, not every model.
    Switching models is a free retry with no wait, unlike retrying the same
    exhausted or removed one. Returns (text, None) on success, or (None,
    last error) if every model failed - the caller decides what to do next,
    since only it knows whether a final wait-and-retry is worth it (it is
    for a rate limit that might clear; it never is for a removed model).
    """
    last_exc: Exception | None = None
    for i, model in enumerate(GEMINI_MODELS):
        try:
            return await _call_gemini(system, prompt, schema, model), None
        except Exception as e:
            last_exc = e
            rate_limited = _rate_limit_delay(e) is not None
            unavailable = _model_unavailable(e)
            if not rate_limited and not unavailable:
                return None, e   # a real error - another model won't help
            if i < len(GEMINI_MODELS) - 1:
                reason = "rate limited" if rate_limited else "unavailable"
                print(f"⏳ beef {label} (gemini/{model}) {reason} - "
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
                      exchange_no: int = 1, persona: str | None = None) -> Verdict | None:
    """Score one exchange: who won, and by how much. None means "no verdict -
    use crowd voting instead".

    Deliberately does not receive the running score - only react_to_burn does.
    A judge that knew who was behind could start inflating margins to keep a
    fight dramatic instead of scoring the burns on their own merit; keeping
    "how good was this" and "who's ahead" as separate calls is what makes the
    margin trustworthy enough to actually decide the battle.
    """
    prompt = (
        f"Exchange {exchange_no} of a roast battle.\n\n"
        f"Racer A ({name_a}) said:\n{burn_a}\n\n"
        f"Racer B ({name_b}) said:\n{burn_b}\n\n"
        f"Who won, and by how much?"
    )
    text = await _call_provider(build_system(persona), prompt, SCHEMA,
                                label="judge", fallback_msg="crowd voting instead")
    if text is None:
        return None

    import json
    try:
        data = json.loads(text)
        return Verdict(winner=data["winner"], margin=data["margin"],
                       loser_landed=data["loser_landed"], verdict=data["verdict"])
    except (ValueError, KeyError, TypeError) as e:
        print(f"⚠️ beef judge sent something unparseable "
              f"({e.__class__.__name__}) - crowd voting instead", flush=True)
        return None


async def react_to_burn(name: str, burn: str, opponent: str, target: int,
                        name_score: int, opponent_score: int,
                        persona: str | None = None) -> str | None:
    """A quick in-character reaction to one burn, right as it lands.

    `name_score`/`opponent_score` are the running totals BEFORE this burn -
    the scoreboard state the reaction should react in light of, not the
    result of the exchange this burn is part of (that isn't scored yet).

    Purely flavor - unlike judge_round, there is nothing downstream relying
    on this succeeding. None just means say nothing for this turn, same as
    if the feature were never enabled at all.
    """
    prompt = (f"Score before this burn: {name} {name_score} - {opponent_score} {opponent}\n\n"
             f"{name}'s burn, aimed at {opponent}:\n{burn}")
    system = build_react_system(persona, name, opponent, target, name_score, opponent_score)
    text = await _call_provider(system, prompt, REACT_SCHEMA,
                                label="reaction", fallback_msg="skipping")
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
        print(f"winner:  {result.winner}  (+{result.winner_points})")
        print(f"margin:  {result.margin}")
        loser = "B" if result.winner == "A" else "A"
        print(f"loser:   {loser}  landed={result.loser_landed}  (+{result.loser_points})")
        print(f"verdict: {result.verdict}")
