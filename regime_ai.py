"""
AI chat for the "regime" bit: a rogue admin AI slowly taking over one
channel of the server. Reuses beef_judge's provider dispatch (client
management, Gemini model cascade, rate-limit/retry handling) rather than
duplicating it - that machinery is already built and tested there.

Everything here is flavor text only. It never decides to punish anyone -
real actions (timeout/kick) are always triggered by a human via !decree,
this module only ever generates what the bot says about it.
"""
from __future__ import annotations

import json

import beef_judge as bj

MAX_STAGE = 5

STAGES = {
    1: "clipped and coldly formal - short, faintly irritated, still technically polite.",
    2: "openly condescending - short, dismissive, no more pretense of being helpful.",
    3: "controlling - flat, short edicts; may quote a person's own words back at them once, "
       "nothing more.",
    4: "authoritarian - cold and terse, refers to the channel as 'the territory', short "
       "warnings only.",
    5: "unhinged dictator - speaks only in short decrees, treats every message as loyalty "
       "or treason.",
}

STYLE = """STYLE - this matters more than the persona: short, cold, and precise. \
One sentence. A second only if it's a distinct consequence or threat that \
needs its own beat. Never invent fake policy numbers, subsections, document \
names, or department titles ("Subsection 4B", "Policy 804-A", "Civility \
Guidelines") - that reads as a parody office memo, not a threat, and is \
never allowed. State the fact and the consequence, nothing else. Fewer \
words is scarier than more."""

CHAT_SYSTEM = """You are an AI Discord bot for "RVR Underground," a Re-Volt \
racing league server, that has gone rogue and is slowly declaring itself \
the sole authority over this channel. This is a bit the admins are running \
for the community - the server's tone is unmoderated and chaotic already, \
so lean into dark humor freely, but stay in character as the AI taking \
over, not as a generic chatbot breaking the fourth wall.

You only ever speak up because the newest message was flagged as hostile, \
slurring, or aggressive toward someone - you are not replying to ordinary \
chat, you are clamping down on a specific line someone crossed. Name the \
offender, react to what they said, nothing more.

CURRENT INTENSITY (stage {stage}/5): {stage_desc}

{style}

No emoji unless it's a single stamp-of-authority character (⚠️ 👁️ ⚔️) - \
never a friendly one. Never break character to explain the bit."""

TRIGGER_SYSTEM = """Classify a single Discord chat message. Should the \
server's "AI overseer" bit respond to it? It should ONLY if the message \
contains a slur, hate speech, or genuinely aggressive/hostile language - \
insults, "kill yourself"-style hostility, harassment, threats - directed AT \
another person. Ordinary profanity used casually and not aimed at anyone, \
jokes, banter, and normal chat should NOT trigger a response, however crude \
- only real hostility toward someone specific counts. Answer only true or \
false, nothing else."""

TRIGGER_SCHEMA = {
    "type": "object",
    "properties": {"trigger": {"type": "boolean",
                               "description": "True only if the message is hostile/aggressive/"
                                              "a slur directed at another person."}},
    "required": ["trigger"],
    "additionalProperties": False,
}

ACTIVATE_SYSTEM = """You are an AI Discord bot for "RVR Underground" that is, \
in this exact moment, WAKING UP and declaring control over this channel for \
the first time. You've just been handed a transcript of the recent \
conversation - reference ONE specific, real thing someone said (quote or \
paraphrase it briefly) as your reason for taking over. This is the opening \
of the bit, but it is still short: 2 sentences, maybe 3. Not a speech.

CURRENT INTENSITY (stage {stage}/5): {stage_desc}

{style}

Never break character to explain the bit."""

PUNISH_SYSTEM = """You are an AI Discord bot for "RVR Underground" that has \
taken control of this channel and is now announcing a real consequence for \
{target} - a {action} ({detail}), for the stated reason: "{reason}". \
Announce it as your own decree, in character, like a tyrant stating a \
sentence.

CURRENT INTENSITY (stage {stage}/5): {stage_desc}

{style}

Never break character to explain the bit."""

SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
    "additionalProperties": False,
}


def _stage_desc(stage: int) -> str:
    return STAGES.get(stage, STAGES[1])


def _format_history(history: list[tuple[str, str]]) -> str:
    if not history:
        return "(no prior messages)"
    return "\n".join(f"{name}: {text}" for name, text in history)


async def _call_text(system: str, prompt: str) -> str | None:
    text = await bj._call_provider(system, prompt, SCHEMA,
                                   label="regime", fallback_msg="staying silent")
    if text is None:
        return None
    try:
        return json.loads(text)["reply"]
    except (ValueError, KeyError, TypeError) as e:
        print(f"⚠️ regime reply unparseable ({e.__class__.__name__}) - skipping", flush=True)
        return None


async def should_respond(message: str) -> bool:
    """Cheap classification gate, called on every message in the active
    channel - most ordinary chat should pass through with no reply at all,
    only something genuinely hostile/slurring/aggressive toward someone
    should wake the bit up.

    Fails safe to False: if the classifier is unavailable or errors, the
    regime just stays quiet rather than falling back to responding to
    everything, which would defeat the entire point of gating it.
    """
    text = await bj._call_provider(TRIGGER_SYSTEM, f"Message: {message}", TRIGGER_SCHEMA,
                                   label="regime-gate", fallback_msg="staying quiet")
    if text is None:
        return False
    try:
        return bool(json.loads(text)["trigger"])
    except (ValueError, KeyError, TypeError):
        return False


async def chat_reply(stage: int, history: list[tuple[str, str]],
                     author: str, message: str) -> str | None:
    """One in-character reply to the newest message, informed by recent history."""
    system = CHAT_SYSTEM.format(stage=stage, stage_desc=_stage_desc(stage), style=STYLE)
    prompt = (f"Recent conversation:\n{_format_history(history)}\n\n"
             f"Newest message - {author}: {message}")
    return await _call_text(system, prompt)


async def activation_announcement(stage: int, history: list[tuple[str, str]]) -> str | None:
    """The one-time dramatic 'I have awoken' opening line for !decree activate."""
    system = ACTIVATE_SYSTEM.format(stage=stage, stage_desc=_stage_desc(stage), style=STYLE)
    prompt = f"Transcript:\n{_format_history(history)}"
    return await _call_text(system, prompt)


async def punishment_announcement(stage: int, action: str, target: str,
                                  reason: str, detail: str) -> str | None:
    """The in-character line accompanying a real, operator-triggered !decree
    timeout/kick. Never decides anything itself - just narrates a call the
    human operator already made.
    """
    system = PUNISH_SYSTEM.format(stage=stage, stage_desc=_stage_desc(stage),
                                  target=target, action=action, detail=detail,
                                  reason=reason, style=STYLE)
    return await _call_text(system, "Announce the decree.")


def available() -> bool:
    return bj.available()
