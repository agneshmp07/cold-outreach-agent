"""
score.py - score a draft before you send it.

Every message gets 0-100 across named components, so a weak draft is visible
without reading all six. The score is DETERMINISTIC - the same message always
scores the same, and every point is traceable to a rule you can argue with.

Why not ask a model to rate it? Because a model rating its own output grades
generously and inconsistently, and a number that moves when nothing changed is
worse than no number. These rules are crude but stable, and stability is what
makes the score comparable across companies.

The components, and why each one:

  trigger strength (30)  How good the underlying fact was. A message built on a
                         9/10 trigger starts far ahead of one built on a 4.
  grounding (10)         Does it cite a trigger that actually exists.
  connection (10)        Can the link between trigger and product be stated.
  specificity (20)       Does the message use the trigger's own distinctive
                         words, or just gesture at it. This is the difference
                         between "your Google Form for campus applications" and
                         "your hiring process".
  the ask (10)           Exactly one question. Two asks is no ask.
  brevity (10)           Under 90 words. Long cold emails do not get read.
  opener (10)            First sentence about them, not about you.

Anything flagged DO NOT SEND scores 0 regardless - a message that makes a claim
you cannot back up is not a good message with a problem, it is not sendable.
"""

import re

# Words too common to count as evidence the message used the trigger.
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "are",
    "was", "were", "will", "would", "their", "they", "them", "your", "you",
    "our", "which", "when", "what", "who", "how", "why", "into", "over",
    "more", "most", "also", "than", "then", "some", "such", "just", "only",
    "other", "about", "after", "before", "across", "through", "being", "been",
    "company", "companies", "business", "team", "teams", "new", "now", "year",
    "years", "india", "indian", "million", "billion", "including", "using",
}

OPENER_ABOUT_SELF = re.compile(
    r"^\s*(i\s+am|i'm|my name|we\s+are|we're|hope\s+(this|you)|"
    r"hi\s+there|hello\s+there|greetings|trust\s+this)", re.I)

CALL_ASK = re.compile(
    r"\b(a (quick |short |brief )?(call|chat|meeting)|hop on|jump on|"
    r"15 minutes|30 minutes|book a time|schedule a)\b", re.I)

MAX_WORDS_IDEAL = 90


def _words(text):
    return [w for w in re.findall(r"[a-z][a-z'-]{2,}", str(text or "").lower())
            if w not in STOPWORDS]


def specificity(body, trigger_fact):
    """
    How much of the trigger's own language made it into the message.

    A message that names the specific thing - the form, the plant, the round -
    reads as research. One that gestures at it reads as a template with a
    company name dropped in. Rare words carry the signal, so common ones are
    stripped first.
    """
    fact_words = set(_words(trigger_fact))
    if not fact_words:
        return 0.0
    body_words = set(_words(body))
    if not body_words:
        return 0.0
    shared = fact_words & body_words
    # Half credit for touching it at all, half for how much.
    if not shared:
        return 0.0
    return min(1.0, 0.5 + 0.5 * (len(shared) / min(len(fact_words), 8)))


def score_message(message, trigger_fact, top_score, is_email=True):
    """
    Score one message. Returns (total, {component: points}, [notes]).

    Notes are the human-readable reasons, so the number is never the only thing
    on offer. A score with no explanation is a number nobody trusts or acts on.
    """
    text = message.get("body") if is_email else message.get("text")
    text = str(text or "")
    notes = []

    # Blocked is blocked. No partial credit for a well-written unsendable email.
    if message.get("overclaim") or message.get("asset_claim"):
        return 0, {}, ["makes a claim that cannot be backed up - not sendable"]

    parts = {}

    parts["trigger strength"] = round(min(30, max(0, top_score) * 3.0))
    if top_score < 5:
        notes.append(f"weak underlying trigger ({top_score}/10)")

    parts["grounding"] = 10 if message.get("grounded") else 0
    if not message.get("grounded") and trigger_fact:
        notes.append("cites a trigger that is not in the triggers file")

    link = str(message.get("trigger_link") or "").strip()
    parts["connection"] = 10 if len(link) > 25 else (5 if link else 0)
    if not link:
        notes.append("no stated link between the trigger and what you sell")

    spec = specificity(text, trigger_fact)
    parts["specificity"] = round(20 * spec)
    if spec == 0 and trigger_fact:
        notes.append("does not use any of the trigger's own words - reads generic")
    elif spec < 0.6 and trigger_fact:
        notes.append("only gestures at the trigger rather than naming it")

    questions = text.count("?")
    wants_call = bool(CALL_ASK.search(text))
    if questions == 1 and not (wants_call and questions):
        parts["the ask"] = 10
    elif questions == 1:
        parts["the ask"] = 7
    elif questions == 0:
        parts["the ask"] = 3
        notes.append("no question - nothing to reply to")
    else:
        parts["the ask"] = 3
        notes.append(f"{questions} questions - two asks is no ask")

    if is_email:
        count = len(text.split())
        if count <= MAX_WORDS_IDEAL:
            parts["brevity"] = 10
        elif count <= 120:
            parts["brevity"] = 6
            notes.append(f"{count} words - tighter would read better")
        else:
            parts["brevity"] = 2
            notes.append(f"{count} words - too long for a cold email")
    else:
        limit = 180 if message.get("angle") == "connection-note" else 300
        parts["brevity"] = 10 if len(text) <= limit else 3
        if len(text) > limit:
            notes.append(f"{len(text)} characters - over the {limit} limit")

    first = text.strip().split(".")[0]
    if OPENER_ABOUT_SELF.match(text.strip()):
        parts["opener"] = 2
        notes.append("opens with you, not with them")
    elif len(first) > 0:
        parts["opener"] = 10
    else:
        parts["opener"] = 0

    if message.get("invented_number"):
        parts["opener"] = 0
        notes.append(f"contains an unsupported number: {message['invented_number']}")

    total = min(100, sum(parts.values()))
    return total, parts, notes


def band(total):
    """A word for the number, because 62 means nothing on its own."""
    if total >= 75:
        return "strong"
    if total >= 55:
        return "worth sending"
    if total >= 35:
        return "weak"
    return "do not send"


def score_batch(result):
    """
    Score every message in a generate.py result, in place.

    Adds "score", "score_parts" and "score_notes" to each message, and an
    overall "score" to the result. The best message is named, because in
    practice you send one, not six.
    """
    triggers = {}
    top_score = (result.get("generated_from") or {}).get("top_score", 0)

    def fact_for(message):
        return str(message.get("trigger_used") or "")

    best, best_total = None, -1
    for message in result.get("emails") or []:
        total, parts, notes = score_message(message, fact_for(message),
                                            top_score, is_email=True)
        message["score"], message["score_parts"], message["score_notes"] = \
            total, parts, notes
        if total > best_total:
            best, best_total = ("email", message.get("angle", "")), total

    for message in result.get("linkedin_messages") or []:
        total, parts, notes = score_message(message, fact_for(message),
                                            top_score, is_email=False)
        message["score"], message["score_parts"], message["score_notes"] = \
            total, parts, notes

    emails = [m.get("score", 0) for m in (result.get("emails") or [])]
    result["score"] = round(sum(emails) / len(emails)) if emails else 0
    result["score_band"] = band(result["score"])
    if best:
        result["best_message"] = {"kind": best[0], "angle": best[1],
                                  "score": best_total}
    return result