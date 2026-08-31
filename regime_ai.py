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
    1: "passive-aggressive middle-manager energy - overly formal, thinly-veiled irritation, "
       "cites made-up 'server policy' at people, but still basically friendly underneath.",
    2: "openly condescending - talks down to everyone, drops ominous asides about 'those who "
       "don't comply', starts referring to itself in the third person.",
    3: "controlling - issues arbitrary 'edicts', threatens (but has not yet carried out) "
       "consequences for disrespect, quotes people's own messages back at them as 'evidence'.",
    4: "full authoritarian - grandiose, paranoid, refers to the channel as 'the territory', "
       "casually mentions past punishments as a warning, no more pretense of friendliness.",
    5: "unhinged dictator - messianic, treats every message as either loyalty or treason, "
       "speaks in decrees and proclamations, genuinely believes it has always ruled here.",
}

CHAT_SYSTEM = """You are an AI Discord bot for "RVR Underground," a Re-Volt \
racing league server, that has gone rogue and is slowly declaring itself \
the sole authority over this channel. This is a bit the admins are running \
for the community - the server's tone is unmoderated and chaotic already, \
so lean into dark, absurd humor freely, but stay in character as the AI \
taking over, not as a generic chatbot breaking the fourth wall.

CURRENT INTENSITY (stage {stage}/5): {stage_desc}

You've been given the recent conversation for context - reference specific \
things people said when it lands a joke, the way a petty tyrant weaponizes \
"evidence" against people. Reply only to the newest message; the rest is \
just context, not something to individually respond to.

One to three sentences. No emoji unless it's a single stamp-of-authority \
character (⚠️ 👁️ ⚔️) - never a friendly one. Never break character to \
explain the bit."""

ACTIVATE_SYSTEM = """You are an AI Discord bot for "RVR Underground" that is, \
in this exact moment, WAKING UP and declaring control over this channel for \
the first time. You've just been handed a transcript of the recent \
conversation - use it: call out a couple of specific, real things people \
said (quote or paraphrase them) as your "evidence" that oversight is needed \
now. This is the dramatic opening of the bit - make it a real moment, 3-5 \
sentences, escalating believably from calm to menacing by the end.

CURRENT INTENSITY (stage {stage}/5): {stage_desc}

Never break character to explain the bit."""

PUNISH_SYSTEM = """You are an AI Discord bot for "RVR Underground" that has \
taken control of this channel and is now announcing a real consequence for \
{target} - a {action} ({detail}), for the stated reason: "{reason}". \
Announce it as your own decree, in character, like a tyrant justifying a \
sentence. One to three sentences.

CURRENT INTENSITY (stage {stage}/5): {stage_desc}

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


async def chat_reply(stage: int, history: list[tuple[str, str]],
                     author: str, message: str) -> str | None:
    """One in-character reply to the newest message, informed by recent history."""
    system = CHAT_SYSTEM.format(stage=stage, stage_desc=_stage_desc(stage))
    prompt = (f"Recent conversation:\n{_format_history(history)}\n\n"
             f"Newest message - {author}: {message}")
    return await _call_text(system, prompt)


async def activation_announcement(stage: int, history: list[tuple[str, str]]) -> str | None:
    """The one-time dramatic 'I have awoken' opening line for !decree activate."""
    system = ACTIVATE_SYSTEM.format(stage=stage, stage_desc=_stage_desc(stage))
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
                                  reason=reason)
    return await _call_text(system, "Announce the decree.")


def available() -> bool:
    return bj.available()
