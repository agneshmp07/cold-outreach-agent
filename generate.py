"""
generate.py - the message-writing step.

Reads the triggers found for one TARGET company and writes 3 cold emails +
3 LinkedIn messages on behalf of one CLIENT company.

    python generate.py <client-domain> <target-domain>
    python generate.py mngo.in happiestminds.com

The client is a profile in clients/<client-domain>.json, written by profile.py.
Nothing about any particular company is hardcoded here: who you are, what you
sell, and above all what you must NEVER claim, all come from that file. Point it
at a different client file and the same checks run for a different company.

There is no fallback mode. If there is no trigger, this refuses to write and
says what is missing. A message with no hook is not a worse message, it is a
different thing: outreach that pretends to research it did not do.

Output:
    messages/<target-domain>.json
    messages/<target-domain>.md

Usage:
    python generate.py mngo.in happiestminds.com
    python generate.py mngo.in happiestminds.com --force
    python generate.py mngo.in happiestminds.com --no-proofread

Nothing is ever sent. This script only writes files.
"""

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from triggers import MODEL, ask_gemini, die, genai

BASE_DIR = Path(__file__).resolve().parent
CLIENTS_DIR = BASE_DIR / "clients"
TRIGGERS_DIR = BASE_DIR / "triggers"
OUT_DIR = BASE_DIR / "messages"

MAX_SUBJECT_CHARS = 60
MAX_EMAIL_WORDS = 120
MAX_LINKEDIN_CHARS = 300

# LinkedIn truncates connection-request notes hard, and free accounts get less
# room than premium ones. Stay well under whatever the current limit is.
MAX_CONNECTION_NOTE_CHARS = 180

# Below this, the triggers are not specific enough to be worth a cold email.
# This is a refusal, not a warning. --force overrides it.
MIN_TOP_SCORE = 6

# A phrase this long, repeated across this many of the six messages, means the
# batch reads like one sentence pasted repeatedly. Detected rather than listed,
# so it works for any client without knowing their pitch in advance.
BOILERPLATE_PHRASE_WORDS = 5
MAX_PHRASE_REPEATS = 2

EMAIL_ANGLES = [
    ("trigger-direct", "Lead with the target company's signal in the first "
                       "line. Say plainly what stage the client is at. Ask what "
                       "breaks for them today in the area the product touches."),
    ("problem-check", "Lead with one specific thing that is usually broken for "
                      "companies like this one, in the area the product serves. "
                      "Ask whether that matches their experience. Do not assert "
                      "that it is their problem."),
    ("conditional-ask", "Make a conditional offer, not a request for free "
                        "advice. The shape is: IF we brought you <the thing the "
                        "product produces>, would you be willing to look at it? "
                        "Make clear a yes commits them to nothing and costs them "
                        "nothing. Do NOT state numbers or dates the client "
                        "cannot back up. Anchor the opening to their trigger."),
]

LINKEDIN_ANGLES = [
    ("connection-note", f"A connection request note. Must fit in "
                        f"{MAX_CONNECTION_NOTE_CHARS} characters - LinkedIn "
                        f"truncates longer ones. Reference the trigger, no "
                        f"pitch, no link."),
    ("post-accept-dm", "A short DM sent after they accept. One specific question "
                       "about how they handle the thing the product addresses."),
    ("value-first", "Offer one concrete useful thing the client has ALREADY "
                    "made. If the verified-assets list in this prompt is empty, "
                    "you may not offer any document, checklist, teardown, guide "
                    "or template at all - offer to share what they learn as they "
                    "build instead. Never claim a result or percentage."),
]

SCHEMA_EXAMPLE = """{
  "emails": [
    {"angle": "trigger-direct", "subject": "string", "body": "string",
     "trigger_used": "string", "trigger_link": "string"}
  ],
  "linkedin_messages": [
    {"angle": "connection-note", "text": "string",
     "trigger_used": "string", "trigger_link": "string"}
  ]
}"""


# ---------------------------------------------------------------- the client

def load_client(client_arg):
    """
    Read the profile for the company doing the outreach.

    The reviewed flag is the important part. profile.py can work out what a
    company sells from its website, but it cannot work out what that company is
    NOT allowed to claim - a marketing site implies traction the company may not
    have yet. Refusing to run until a human has filled that in is the difference
    between a tool and a liability.
    """
    name = client_arg.strip().lower()
    for prefix in ("https://", "http://"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    if name.startswith("www."):
        name = name[4:]
    name = name.split("/")[0].strip()
    if name.endswith(".json"):
        name = name[:-5]

    path = CLIENTS_DIR / f"{name}.json"
    if not path.exists():
        available = sorted(p.stem for p in CLIENTS_DIR.glob("*.json")) \
            if CLIENTS_DIR.exists() else []
        hint = ("\nProfiles you have: " + ", ".join(available)) if available else ""
        die(f"No client profile at {path}.\n"
            f"Create one with:  python profile.py {name}{hint}")

    try:
        client = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{path} is not valid JSON: {exc.msg} (line {exc.lineno}).")

    if not client.get("reviewed"):
        die(f"{path} has not been reviewed yet.\n\n"
            f"Open it and do three things:\n"
            f'  - set "sender" to the name that signs the emails\n'
            f'  - fill "cannot_claim" with what this company must NEVER say\n'
            f'  - set "reviewed" to true\n\n'
            f"The cannot_claim list is the one thing no tool can work out for "
            f"you. A website markets the ambition; only the founder knows what "
            f"is actually signed. Until it is filled in, this script has no way "
            f"to stop itself writing a claim you cannot back up.")

    if not str(client.get("sender") or "").strip():
        die(f'{path} has no "sender". Set it to the name that signs the emails.')
    if not str(client.get("one_liner") or "").strip():
        die(f'{path} has no "one_liner". Set it to one plain sentence describing '
            f'what this company does.')

    client["_path"] = path
    client["_slug"] = name
    return client


def build_overclaim_pattern(cannot_claim):
    """
    Turn the client's forbidden claims into something checkable.

    Each entry may hold several comma-separated phrasings of the same claim -
    "our network, our colleges, we have access to" - because a model reaching
    for a claim will find whichever wording fits the sentence. Every fragment
    becomes an alternative in one case-insensitive pattern.

    Returns None when the list is empty, which is legitimate: a company with
    real traction may have nothing it must avoid saying.
    """
    fragments = []
    for entry in cannot_claim or []:
        for part in str(entry).split(","):
            part = part.strip()
            if len(part) < 3:
                continue
            fragments.append(re.escape(part).replace(r"\ ", r"\s+"))
    if not fragments:
        return None
    return re.compile("(" + "|".join(fragments) + ")", re.I)


# ---------------------------------------------------------------- input side

def parse_args(argv):
    args = list(argv[1:])
    force = "--force" in args
    proofread = "--no-proofread" not in args
    positional = [a for a in args if not a.startswith("--")]

    unknown = [a for a in args if a.startswith("--")
               and a not in ("--force", "--no-proofread")]
    if unknown:
        sys.exit(f"Unknown option(s): {', '.join(unknown)}")
    if len(positional) != 2:
        sys.exit(f"Usage: python {Path(__file__).name} <client-domain> "
                 f"<target-domain> [--force] [--no-proofread]\n"
                 f"Example: python {Path(__file__).name} mngo.in happiestminds.com")
    return positional[0], positional[1], force, proofread


def resolve_input(arg):
    candidate = Path(arg)
    if candidate.suffix == ".json":
        return candidate.stem, candidate

    domain = arg.strip().lower()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    if domain.startswith("www."):
        domain = domain[4:]
    domain = domain.split("/")[0].strip()

    if not domain:
        die(f"Could not work out a domain from {arg!r}.")
    return domain, TRIGGERS_DIR / f"{domain}.json"


def load_triggers(path):
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        die(f"No triggers file at {path}. Run:  python triggers.py {path.stem}")
    except OSError as exc:
        die(f"Could not read {path}: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"{path} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})")

    if not isinstance(data, dict) or "triggers" not in data:
        die(f"{path} does not look like a triggers file (no 'triggers' key). "
            f"Re-run triggers.py for this domain.")
    if not isinstance(data["triggers"], list):
        die(f"{path} has a 'triggers' key that isn't a list.")
    return data


def refuse_no_trigger(domain, source, in_path):
    """No trigger, no message. Say what is missing and stop."""
    company = source.get("company_name") or domain

    print(f"\nCANNOT WRITE THESE MESSAGES - {company}")
    print("\nWhat is missing:")
    print("  - No verifiable trigger was found for this company.")
    print("    triggers.py returned no_trigger_found, or an empty trigger list.")

    if not source.get("pain_hypothesis"):
        print("  - No pain hypothesis either, so there is nothing to build an "
              "angle on.")

    print("\nWhere to look, in order:")
    print(f"  1. Open {in_path} and check whether it is empty or just weak.")
    print(f"  2. Check the scrape: does data/{domain}.json contain page text?")
    print("     A JS-heavy site with no playwright fallback returns almost nothing.")
    print("  3. Check news.py found anything dated for this company.")
    print("  4. Ask whether this company belongs in the ICP at all. No findable")
    print("     signal is itself an answer: it usually means they are not a buyer.")

    print("\nNothing was written. Pick a different company, or fix the step above")
    print("that failed. Generic outreach is not the fallback - not writing is.")
    sys.exit(1)


# ------------------------------------------------------------------ the model

def format_triggers(triggers):
    lines = []
    for i, trigger in enumerate(triggers, start=1):
        lines.append(
            f"{i}. [{trigger.get('relevance_score', 0)}/10] {trigger.get('fact', '')}\n"
            f"   (found on: {trigger.get('source_page', 'unknown')})"
        )
    return "\n".join(lines)


def format_assets(client):
    assets = client.get("verified_assets") or []
    if not assets:
        return (f"NONE. {client['sender']} has not made any shareable document "
                f"yet. You may not offer, describe or promise one.")
    return "\n".join(f"- {a}" for a in assets)


def format_cannot_claim(client):
    forbidden = client.get("cannot_claim") or []
    if not forbidden:
        return "Nothing specific. Still never invent facts, numbers or customers."
    return "\n".join(f"- NEVER say or imply: {c}" for c in forbidden)


def build_prompt(client, company_name, triggers, pain_hypothesis):
    email_angles = "\n".join(f'- "{name}": {brief}' for name, brief in EMAIL_ANGLES)
    linkedin_angles = "\n".join(f'- "{name}": {brief}' for name, brief in LINKEDIN_ANGLES)

    return f"""You write cold outreach that a busy person would actually reply to.
You are writing on behalf of {client['sender']}, who runs {client['name']} -
{client['one_liner']}.

That description is CONTEXT for you, not copy. Do not paste it into the messages.

WHAT {client['name'].upper()} SELLS: {client.get('what_you_sell') or client['one_liner']}
WHO BUYS IT: {client.get('who_buys_it') or 'not stated'}

TARGET COMPANY: {company_name}

VERIFIED TRIGGERS (the only facts about the target you may use):
{format_triggers(triggers)}

PAIN HYPOTHESIS: {pain_hypothesis or "none stated"}

THINGS {client['sender'].upper()} HAS ACTUALLY MADE AND CAN SEND SOMEONE:
{format_assets(client)}

WHAT {client['name'].upper()} MAY NOT CLAIM - this list overrides everything else:
{format_cannot_claim(client)}

Write exactly 3 emails and exactly 3 LinkedIn messages.

EMAIL ANGLES - one message each, in this order:
{email_angles}

LINKEDIN ANGLES - one message each, in this order:
{linkedin_angles}

HARD RULES:
1. Every message must be built on ONE trigger from the list above. Copy that
   trigger's fact verbatim into "trigger_used" so a human can check your work.
   The message BODY must NOT quote the trigger text. Reference the fact in your
   own words, as briefly as a human would: "after your Series D", not the full
   round with every investor named. Never list more than one investor.
2. For every message, fill "trigger_link" with ONE plain sentence explaining how
   that trigger connects to what {client['name']} sells. If you cannot write
   that sentence without a leap - if the honest answer is "it doesn't really" -
   pick a different trigger. Do not write a message whose link you cannot state.
3. Use ONLY the facts above. Do NOT invent metrics, funding, customer names,
   case studies, mutual connections, or anything you "noticed" that is not
   listed. No "I saw your post about..." unless it is a trigger.
4. NO NUMBERS that are not in the trigger list. No percentages, no multipliers,
   no "40% fewer drop-offs", no "3x faster", no "saves 10 hours a week". Describe
   a benefit in words with no figure attached.
5. The MAY NOT CLAIM list above is absolute. Do not state those things, imply
   them, or work around them with softer wording. Overstating traction is the
   one mistake that cannot be walked back, because the prospect will ask for
   specifics and there are none.
6. THE ASK. Two of the three emails ask for input - what breaks for them today.
   The third makes a CONDITIONAL offer: if we brought you X, would you look at
   it? A conditional is allowed and wanted. An unconditional promise is not.
7. Do NOT offer, describe or promise any document, checklist, teardown, guide,
   template, report or framework that is not in the verified-assets list above.
   If that list says NONE, you may not offer one at all. Claiming an artifact
   that does not exist is worse than a weak message, because a yes creates a
   debt the sender cannot pay.
8. Spread the messages across different triggers where the trigger list allows
   it. Do not build all six on trigger 1.
9. Subject lines: under {MAX_SUBJECT_CHARS} characters, lowercase or sentence
   case, specific. No "Quick question", no "Touching base", no clickbait.
10. Email body: under {MAX_EMAIL_WORDS} words. Plain text. No "Hope this finds
    you well", no flattery opener. Get to the trigger in the first sentence.
11. One ask per message. Prefer a question they can answer in one line. Only ONE
    of the three emails may ask for a call - the other two end in a question.
12. LinkedIn "connection-note" must be under {MAX_CONNECTION_NOTE_CHARS}
    characters. The other two must be under {MAX_LINKEDIN_CHARS}.
13. Sign emails as {client['sender']}. Do not invent a phone number, a calendar
    link, or a website.
14. Professional and direct. No "Dear Sir/Madam", no American sales slang.
15. Each of the six messages must be genuinely different. If two would say the
    same thing, change the angle, not just the wording. Any single description
    of what {client['name']} does may appear in at most TWO of the six messages.
16. If a trigger has no date attached, do not imply it is recent.
17. Never name an individual in connection with a departure, resignation or
    exit. A leadership vacancy may be referenced without naming who left.
18. Being early is not a weakness to hide. A founder saying "we are building
    this, here is what we do not know yet" gets replies. A founder pretending
    to have scale gets deleted. Write like the former.
19. Write correct English. Every sentence must parse. Check verb forms.

Reply with JSON only, matching this shape exactly:
{SCHEMA_EXAMPLE}"""


def build_proofread_prompt(items):
    numbered = "\n\n".join(f"[{i}] {text}" for i, text in items)
    return f"""You are proofreading short business messages for grammar and fluency ONLY.

Do NOT comment on strategy, tone, persuasiveness, length or content. Only flag a
message if it contains an actual error that a fluent English writer would not
produce: a broken verb form, a missing word, a mangled clause, subject-verb
disagreement, a wrong preposition, or a phrase that does not parse.

Do NOT flag deliberate sentence fragments or informal phrasing.

MESSAGES:
{numbered}

Reply with JSON only, in this shape:
{{"issues": [{{"index": 1, "quote": "the exact broken phrase", "problem": "what is wrong with it"}}]}}

If every message is clean, reply with exactly: {{"issues": []}}"""


# ----------------------------------------------------------------- output side

def parse_response(raw_text, domain):
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        debug_path = OUT_DIR / f"{domain}.raw.txt"
        debug_path.write_text(raw_text, encoding="utf-8")
        die(f"Model did not return valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno}). "
            f"The raw reply was saved to {debug_path}.")

    if not isinstance(data, dict):
        die(f"Expected a JSON object from the model, got {type(data).__name__}.")
    return data


def normalise(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def is_grounded(trigger_used, known_facts):
    cited = normalise(trigger_used)
    if not cited:
        return False
    return any(cited == fact or cited in fact or fact in cited for fact in known_facts)


NUMBER_PATTERN = re.compile(r"\d+\s*(?:%|x\b|percent|times)", re.I)

ASSET_NOUNS = (r"checklist|teardown|guide|template|report|breakdown|comparison"
               r"|playbook|framework|benchmark|one-pager|write-?up|doc|document"
               r"|deck|spreadsheet|study|audit")

ASSET_CLAIM_PATTERN = re.compile(
    r"\b(?:i|we)\s+(?:have\s+)?"
    r"(?:created|made|built|put together|wrote|written|prepared|compiled|"
    r"drafted|assembled|pulled together)\b[^.?!]{0,60}?\b(?:" + ASSET_NOUNS + r")\b",
    re.I)

ASSET_OFFER_PATTERN = re.compile(
    r"\b(?:send|share|forward)\s+(?:you\s+)?(?:it|this|the|a|an|our|my)\b"
    r"[^.?!]{0,40}?\b(?:" + ASSET_NOUNS + r")\b", re.I)


def invented_number(text, known_facts):
    joined = " ".join(known_facts).lower()
    for match in NUMBER_PATTERN.finditer(text):
        if match.group(0).lower() not in joined:
            return match.group(0)
    return None


def overclaim(text, pattern):
    """Find language the client said it must never use."""
    if pattern is None:
        return None
    match = pattern.search(text)
    return match.group(0) if match else None


def asset_claim(text, verified_assets):
    known = [a.lower() for a in verified_assets or []]
    for pattern in (ASSET_CLAIM_PATTERN, ASSET_OFFER_PATTERN):
        match = pattern.search(text)
        if not match:
            continue
        phrase = match.group(0)
        if known and any(asset in phrase.lower() for asset in known):
            continue
        return phrase.strip()
    return None


def similar(a, b):
    words_a, words_b = set(normalise(a).split()), set(normalise(b).split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def check_boilerplate(texts):
    """
    Find any long phrase repeated across too many of the six messages.

    Detected rather than listed, so it needs no knowledge of the client's pitch.
    One message describing what the company does is honest; four identical
    descriptions read like a bot with a single sentence.
    """
    findings = []
    seen = Counter()

    for text in texts:
        words = normalise(text).split()
        phrases = set()
        for i in range(len(words) - BOILERPLATE_PHRASE_WORDS + 1):
            phrases.add(" ".join(words[i:i + BOILERPLATE_PHRASE_WORDS]))
        for phrase in phrases:
            seen[phrase] += 1

    reported = []
    for phrase, count in seen.most_common():
        if count <= MAX_PHRASE_REPEATS:
            break
        if any(phrase in longer for longer in reported):
            continue
        reported.append(phrase)
        findings.append(f"'{phrase}' appears in {count} of {len(texts)} messages "
                        f"(limit {MAX_PHRASE_REPEATS}) - the batch reads like one "
                        f"sentence pasted repeatedly")
    return findings


def run_proofread(client_api, result, warnings):
    items = []
    labels = {}
    n = 0
    for i, email in enumerate(result["emails"], start=1):
        n += 1
        items.append((n, f"{email['subject']}\n{email['body']}"))
        labels[n] = f"email {i} ({email['angle']})"
    for i, msg in enumerate(result["linkedin_messages"], start=1):
        n += 1
        items.append((n, msg["text"]))
        labels[n] = f"linkedin {i} ({msg['angle']})"

    if not items:
        return

    try:
        raw = ask_gemini(client_api, build_proofread_prompt(items))
        data = json.loads(raw)
        issues = data.get("issues") or []
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        warnings.append("proofread pass did not return usable JSON - grammar was "
                        "not checked this run")
        return
    except Exception as exc:
        warnings.append(f"proofread pass failed ({type(exc).__name__}) - grammar "
                        f"was not checked this run")
        return

    if not isinstance(issues, list):
        return

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        label = labels.get(issue.get("index"), f"message {issue.get('index')}")
        quote = str(issue.get("quote") or "").strip()
        problem = str(issue.get("problem") or "").strip()
        warnings.append(f"{label}: grammar - '{quote}' ({problem})")


def check_message(message, text, label, known_facts, client, overclaim_pattern,
                  warnings):
    """Run every content check against one message. Returns the flags found."""
    fake = invented_number(text, known_facts)
    if fake:
        warnings.append(f"{label}: contains '{fake}' - no trigger supports this "
                        f"number, it was invented")

    claim = overclaim(text, overclaim_pattern)
    if claim:
        warnings.append(f"{label}: says '{claim}' - {client['name']} may not "
                        f"claim this. DO NOT SEND.")

    asset = asset_claim(text, client.get("verified_assets"))
    if asset:
        warnings.append(f"{label}: offers '{asset}' - that is not in "
                        f"verified_assets, so it does not exist. DO NOT SEND.")

    if not message["grounded"]:
        warnings.append(f"{label}: cites a trigger that is not in the triggers "
                        f"file - check it for invented facts")

    if not message["trigger_link"]:
        warnings.append(f"{label}: no trigger_link - the model could not say how "
                        f"the trigger relates to what {client['name']} sells")

    return fake, claim, asset


def clean_result(data, source, client, overclaim_pattern, warnings):
    known_facts = [normalise(t.get("fact")) for t in source["triggers"]]
    known_facts = [f for f in known_facts if f]

    emails = []
    for i, item in enumerate(data.get("emails") or []):
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()
        body = str(item.get("body") or "").strip()
        if not subject or not body:
            warnings.append(f"email {i + 1}: dropped (missing subject or body)")
            continue

        angle = str(item.get("angle") or "").strip() or (
            EMAIL_ANGLES[i][0] if i < len(EMAIL_ANGLES) else f"angle-{i + 1}")
        label = f"email {i + 1} ({angle})"

        message = {
            "angle": angle,
            "subject": subject,
            "body": body,
            "trigger_used": str(item.get("trigger_used") or "").strip(),
            "trigger_link": str(item.get("trigger_link") or "").strip(),
            "word_count": len(body.split()),
            "grounded": is_grounded(item.get("trigger_used"), known_facts),
        }

        if len(subject) > MAX_SUBJECT_CHARS:
            warnings.append(f"{label}: subject is {len(subject)} chars "
                            f"(limit {MAX_SUBJECT_CHARS})")
        if message["word_count"] > MAX_EMAIL_WORDS:
            warnings.append(f"{label}: body is {message['word_count']} words "
                            f"(limit {MAX_EMAIL_WORDS})")

        fake, claim, asset = check_message(message, body, label, known_facts,
                                           client, overclaim_pattern, warnings)
        message.update({"invented_number": fake, "overclaim": claim,
                        "asset_claim": asset})
        emails.append(message)

    linkedin = []
    for i, item in enumerate(data.get("linkedin_messages") or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("body") or "").strip()
        if not text:
            warnings.append(f"linkedin {i + 1}: dropped (no text)")
            continue

        angle = str(item.get("angle") or "").strip() or (
            LINKEDIN_ANGLES[i][0] if i < len(LINKEDIN_ANGLES) else f"angle-{i + 1}")
        label = f"linkedin {i + 1} ({angle})"

        message = {
            "angle": angle,
            "text": text,
            "trigger_used": str(item.get("trigger_used") or "").strip(),
            "trigger_link": str(item.get("trigger_link") or "").strip(),
            "char_count": len(text),
            "grounded": is_grounded(item.get("trigger_used"), known_facts),
        }

        limit = MAX_CONNECTION_NOTE_CHARS if angle == "connection-note" else MAX_LINKEDIN_CHARS
        if len(text) > limit:
            warnings.append(f"{label}: {len(text)} chars (limit {limit}) - "
                            f"LinkedIn will truncate or reject it")

        fake, claim, asset = check_message(message, text, label, known_facts,
                                           client, overclaim_pattern, warnings)
        message.update({"invented_number": fake, "overclaim": claim,
                        "asset_claim": asset})
        linkedin.append(message)

    if len(emails) != 3:
        warnings.append(f"expected 3 emails, got {len(emails)}")
    if len(linkedin) != 3:
        warnings.append(f"expected 3 LinkedIn messages, got {len(linkedin)}")

    for group, label, key in ((emails, "email", "body"), (linkedin, "linkedin", "text")):
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                score = similar(group[a][key], group[b][key])
                if score > 0.7:
                    warnings.append(
                        f"{label} {a + 1} and {label} {b + 1} are {score:.0%} "
                        f"similar - they are basically the same message")

    all_texts = [e["body"] for e in emails] + [m["text"] for m in linkedin]
    if all_texts:
        warnings.extend(check_boilerplate(all_texts))

    used = {normalise(m["trigger_used"]) for m in emails + linkedin if m["trigger_used"]}
    if len(known_facts) > 1 and len(used) == 1:
        warnings.append("all six messages lean on the same trigger, though more "
                        "were available")

    return {
        "client": client["name"],
        "client_domain": client["_slug"],
        "company_name": source.get("company_name") or "unknown",
        "generated_from": {
            "trigger_count": len(source["triggers"]),
            "top_score": max((t.get("relevance_score", 0) for t in source["triggers"]),
                             default=0),
            "pain_hypothesis": source.get("pain_hypothesis", ""),
        },
        "emails": emails,
        "linkedin_messages": linkedin,
        "warnings": warnings,
    }


def flags_for(message):
    flags = ""
    if not message["grounded"]:
        flags += "  :warning: **ungrounded**"
    if message.get("invented_number"):
        flags += f"  :warning: **invented number: {message['invented_number']}**"
    if message.get("overclaim"):
        flags += f"  :rotating_light: **MAY NOT CLAIM: {message['overclaim']}**"
    if message.get("asset_claim"):
        flags += f"  :rotating_light: **ASSET DOES NOT EXIST: {message['asset_claim']}**"
    return flags


def to_markdown(result):
    info = result["generated_from"]
    lines = [f"# {result['client']} -> {result['company_name']}", "",
             f"_{info['trigger_count']} trigger(s) found, top relevance "
             f"{info['top_score']}/10._", ""]

    if info.get("forced"):
        lines += ["> **Drafted with --force below the quality floor.** The "
                  "triggers were judged too weak to be worth sending on.", ""]
    if info.get("pain_hypothesis"):
        lines += [f"**Pain hypothesis:** {info['pain_hypothesis']}", ""]

    lines += ["## Emails", ""]
    for i, email in enumerate(result["emails"], start=1):
        lines += [f"### {i}. {email['angle']}{flags_for(email)}",
                  f"**Subject:** {email['subject']}", "",
                  email["body"], ""]
        if email["trigger_used"]:
            lines += [f"> Trigger: {email['trigger_used']}  "]
        if email.get("trigger_link"):
            lines += [f"> Why it connects: {email['trigger_link']}  "]
        lines += [f"> {email['word_count']} words", ""]

    lines += ["## LinkedIn", ""]
    for i, msg in enumerate(result["linkedin_messages"], start=1):
        lines += [f"### {i}. {msg['angle']}{flags_for(msg)}", "",
                  msg["text"], ""]
        if msg["trigger_used"]:
            lines += [f"> Trigger: {msg['trigger_used']}  "]
        if msg.get("trigger_link"):
            lines += [f"> Why it connects: {msg['trigger_link']}  "]
        lines += [f"> {msg['char_count']} characters", ""]

    if result["warnings"]:
        lines += ["## Warnings", ""] + [f"- {w}" for w in result["warnings"]] + [""]

    return "\n".join(lines)


def save_result(domain, result):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"{domain}.json"
    md_path = OUT_DIR / f"{domain}.md"
    try:
        json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        md_path.write_text(to_markdown(result), encoding="utf-8")
    except OSError as exc:
        die(f"Could not write to {OUT_DIR}: {exc}")
    return json_path, md_path


def console_marks(message):
    mark = ""
    if not message["grounded"]:
        mark += "  [UNGROUNDED]"
    if message.get("invented_number"):
        mark += f"  [INVENTED: {message['invented_number']}]"
    if message.get("overclaim"):
        mark += f"  [MAY NOT CLAIM: {message['overclaim']}]"
    if message.get("asset_claim"):
        mark += f"  [FAKE ASSET: {message['asset_claim']}]"
    return mark


def print_summary(result):
    print(f"\n{result['client']} -> {result['company_name']}")
    for i, email in enumerate(result["emails"], start=1):
        print(f"  Email {i} ({email['angle']}){console_marks(email)}")
        print(f"    subject: {email['subject']}")
        print(f"    {email['word_count']} words")
    for i, msg in enumerate(result["linkedin_messages"], start=1):
        print(f"  LinkedIn {i} ({msg['angle']})"
              f"{console_marks(msg)}: {msg['char_count']} chars")

    if result["warnings"]:
        print("\nWarnings:")
        for warning in result["warnings"]:
            print(f"  - {warning}")


# ------------------------------------------------------------------------ main

def main():
    client_arg, target, force, do_proofread = parse_args(sys.argv)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        die("GEMINI_API_KEY is not set. Put it in your .env next to this script.")

    client = load_client(client_arg)
    overclaim_pattern = build_overclaim_pattern(client.get("cannot_claim"))

    domain, in_path = resolve_input(target)
    source = load_triggers(in_path)
    triggers = source["triggers"]

    if source.get("no_trigger_found") or not triggers:
        refuse_no_trigger(domain, source, in_path)

    warnings = []
    top_score = max((t.get("relevance_score", 0) for t in triggers), default=0)

    if top_score < MIN_TOP_SCORE:
        if not force:
            die(f"Top trigger for {source.get('company_name') or domain} scores "
                f"{top_score}/10, below the floor of {MIN_TOP_SCORE}.\n"
                f"Nothing was generated.\n\n"
                f"This usually means the company is a poor fit rather than that "
                f"the scraper failed. Pick a better-fit company, or run again "
                f"with --force to see the drafts anyway.")
        warnings.append(f"FORCED: top trigger is {top_score}/10, below the floor "
                        f"of {MIN_TOP_SCORE}. These drafts are probably not worth "
                        f"sending.")

    api = genai.Client(api_key=api_key)

    print(f"Client: {client['name']} ({client['_path'].name})")
    print(f"Read {len(triggers)} trigger(s) from {in_path}")
    print(f"Asking {MODEL} for 3 emails + 3 LinkedIn messages...")

    prompt = build_prompt(client, source.get("company_name") or domain, triggers,
                          source.get("pain_hypothesis", ""))
    result = clean_result(parse_response(ask_gemini(api, prompt), domain),
                          source, client, overclaim_pattern, warnings)
    result["generated_from"]["forced"] = force and top_score < MIN_TOP_SCORE

    if do_proofread:
        print("Proofreading...")
        run_proofread(api, result, result["warnings"])

    json_path, md_path = save_result(domain, result)
    print_summary(result)

    print(f"\nSaved to {json_path}")
    print(f"        and {md_path}")
    print("\nNothing was sent. Review the drafts before using them.")


if __name__ == "__main__":
    main()