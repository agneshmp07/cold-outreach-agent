"""
generate.py - step 3 (final) of the MNGO outreach generator.

Reads the triggers that triggers.py found for one company and writes 3 cold
emails + 3 LinkedIn messages, each one built on a specific trigger.

Target: companies that hire entry-level talent from Indian campuses.
Pitch:  MNGO is early. We are onboarding our first colleges and building the
        recruiter side around what companies actually need. The ask is input
        or a conditional yes, never a promise of candidates.

There is no fallback mode. If there is no trigger, this script refuses to write
and tells you what is missing. A message with no hook is not a worse message,
it is a different thing: outreach that pretends to research it did not do. The
refusal is the useful output.

Output:
    messages/<domain>.json
    messages/<domain>.md

Usage:
    python generate.py razorpay.com
    python generate.py razorpay.com --force          # draft on weak triggers
    python generate.py razorpay.com --no-proofread   # skip the grammar pass

Nothing is ever sent. This script only writes files.
"""

import json
import os
import re
import sys
from pathlib import Path

from triggers import MODEL, ask_gemini, die, genai

# ---------------------------------------------------------------------------
# EDIT THIS BLOCK to describe whatever you are selling.
#
# NOTE: no college count, no student numbers. Nothing is signed yet, and
# overstating the network is the one mistake that cannot be walked back.
# ---------------------------------------------------------------------------
PRODUCT = {
    "name": "MNGO",
    "one_liner": (
        "campus placement infrastructure for India: we are onboarding our "
        "first colleges now and building the recruiter side around what "
        "companies actually need from campus hiring, rather than guessing"
    ),
    "sender": "Agnesh",
}

# ---------------------------------------------------------------------------
# Things you can honestly say you have made. If a draft offers to send someone
# a checklist, teardown or guide that is NOT in this list, it gets flagged as
# DO NOT SEND - because if they say yes, you owe them a document that does not
# exist. Add a string here only once the thing actually exists on disk.
# ---------------------------------------------------------------------------
VERIFIED_ASSETS = [
    # "campus drive evaluation checklist",
]

BASE_DIR = Path(__file__).resolve().parent
TRIGGERS_DIR = BASE_DIR / "triggers"
OUT_DIR = BASE_DIR / "messages"

MAX_SUBJECT_CHARS = 60
MAX_EMAIL_WORDS = 120
MAX_LINKEDIN_CHARS = 300

# LinkedIn truncates connection-request notes hard, and free accounts get less
# room than premium ones. Stay well under whatever the current limit is.
MAX_CONNECTION_NOTE_CHARS = 180

# Below this, the triggers are not specific enough to be worth a cold email.
# This is a refusal, not a warning: drafting on a 5/10 trigger burns the inbox
# for a message that was never going to land. --force overrides it.
MIN_TOP_SCORE = 6

# Stock phrases from the pitch. If the same one shows up in most of the six
# drafts, the batch reads like one sentence pasted six times.
BOILERPLATE_PHRASES = [
    "onboarding our first colleges",
    "campus placement infrastructure",
    "building the recruiter side",
    "what companies actually need",
    "what recruiters actually need",
]
MAX_PHRASE_REPEATS = 2

EMAIL_ANGLES = [
    ("trigger-direct", "Lead with the company's hiring signal in the first "
                       "line. Say plainly that MNGO is early and being built "
                       "now. Ask what breaks for them in campus hiring today."),
    ("problem-check", "Lead with one specific thing that is usually broken in "
                      "campus hiring - resumes arriving as PDFs, email chains "
                      "with placement officers, no way to compare candidates "
                      "across colleges. Ask whether that matches their "
                      "experience. Do not assert that it is their problem."),
    ("conditional-ask", "Make a conditional offer, not a request for free "
                        "advice. The shape is: we are onboarding our first "
                        "colleges; IF we brought you a pre-screened, structured "
                        "shortlist of final-year engineering candidates from "
                        "them, would you be willing to look at it? Make clear a "
                        "yes commits them to nothing and costs them nothing. Do "
                        "NOT state a number of candidates, colleges or a date - "
                        "you do not have those yet. Anchor the opening to their "
                        "trigger."),
]

LINKEDIN_ANGLES = [
    ("connection-note", f"A connection request note. Must fit in "
                        f"{MAX_CONNECTION_NOTE_CHARS} characters - LinkedIn "
                        f"truncates longer ones. Reference the trigger, no "
                        f"pitch, no link."),
    ("post-accept-dm", "A short DM sent after they accept. One specific question "
                       "about how they run campus hiring today."),
    ("value-first", "Offer one concrete useful thing that Agnesh has ALREADY "
                    "made. If the verified-assets list in this prompt is empty, "
                    "you may not offer any document, checklist, teardown, guide "
                    "or template at all - instead offer to share what you learn "
                    "from the colleges you are onboarding, once you have it. "
                    "Never claim a result or percentage."),
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


# ---------------------------------------------------------------- input side

def parse_args(argv):
    """Pull flags out of argv, return (target, force, proofread)."""
    args = list(argv[1:])
    force = "--force" in args
    proofread = "--no-proofread" not in args
    positional = [a for a in args if not a.startswith("--")]

    unknown = [a for a in args if a.startswith("--")
               and a not in ("--force", "--no-proofread")]
    if unknown:
        sys.exit(f"Unknown option(s): {', '.join(unknown)}")
    if len(positional) != 1:
        sys.exit(f"Usage: python {Path(__file__).name} <domain-or-triggers-path> "
                 f"[--force] [--no-proofread]")
    return positional[0], force, proofread


def resolve_input(arg):
    """Accept a bare domain, a URL, or a direct path to a triggers JSON file."""
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
    """Read the file triggers.py wrote and check it has the shape we expect."""
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
    """
    No trigger, no message. Say what is missing and stop.

    This used to generate six generic messages and warn you afterwards. That was
    backwards: it filled a research gap with fluent text, which is the exact
    failure the checks elsewhere in this file exist to catch. A refusal you can
    read is worth more than six messages you should not send.
    """
    company = source.get("company_name") or domain

    print(f"\nCANNOT WRITE THESE MESSAGES — {company}")
    print("\nWhat is missing:")
    print("  - No verifiable trigger was found for this company.")
    print("    triggers.py returned no_trigger_found, or an empty trigger list.")

    if not source.get("pain_hypothesis"):
        print("  - No pain hypothesis either, so there is nothing to build an "
              "angle on.")

    print("\nWhere to look, in order:")
    print(f"  1. Open {in_path} and check whether it is empty or just weak.")
    print(f"  2. Check the scrape: does data/{domain}.* actually contain page text?")
    print("     A JS-heavy site with no playwright fallback returns almost nothing.")
    print("  3. Check news.py found anything dated for this company.")
    print("  4. Ask whether this company belongs in the ICP at all. Read")
    print("     context/icp.md. No findable campus-hiring signal is itself an")
    print("     answer: it usually means they are not a buyer.")

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


def format_assets():
    if not VERIFIED_ASSETS:
        return ("NONE. Agnesh has not made any shareable document yet. You may "
                "not offer, describe or promise one.")
    return "\n".join(f"- {a}" for a in VERIFIED_ASSETS)


def build_prompt(company_name, triggers, pain_hypothesis):
    email_angles = "\n".join(f'- "{name}": {brief}' for name, brief in EMAIL_ANGLES)
    linkedin_angles = "\n".join(f'- "{name}": {brief}' for name, brief in LINKEDIN_ANGLES)

    return f"""You write cold outreach that a busy Indian hiring lead or founder would
actually reply to. You are writing on behalf of {PRODUCT['sender']}, who is
building {PRODUCT['name']} - {PRODUCT['one_liner']}.

That description is CONTEXT for you, not copy. Do not paste it into the
messages.

TARGET COMPANY: {company_name}

VERIFIED TRIGGERS (the only facts you may use):
{format_triggers(triggers)}

PAIN HYPOTHESIS: {pain_hypothesis or "none stated"}

THINGS AGNESH HAS ACTUALLY MADE AND CAN SEND SOMEONE:
{format_assets()}

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
   that trigger connects to campus hiring. If you cannot write that sentence
   without a leap - if the honest answer is "it doesn't really" - pick a
   different trigger. Do not write a message whose link you cannot state.
3. Use ONLY the facts above. Do NOT invent metrics, funding, customer names,
   case studies, mutual connections, or anything you "noticed" that is not
   listed. No "I saw your post about..." unless it is a trigger.
4. NO NUMBERS that are not in the trigger list. No percentages, no multipliers,
   no "40% fewer drop-offs", no "3x faster", no "saves 10 hours a week". If you
   want to describe a benefit, describe it in words with no figure attached.
5. MNGO HAS NO CONFIRMED COLLEGE PARTNERS YET. Never state or imply a number
   of colleges, campuses or students. Never say "our network", "our colleges",
   or "we have access to". Say "we are onboarding our first colleges" and
   nothing more specific. Overstating this is the one mistake that cannot be
   walked back.
6. THE ASK. Two of the three emails ask for input - what breaks for them today.
   The third makes a CONDITIONAL offer: if we brought you a shortlist from the
   colleges we are onboarding, would you look at it? A conditional is allowed
   and wanted. An unconditional promise is not: never say candidates, hires or
   shortlists ARE coming, or give a date, or give a number.
7. Do NOT offer, describe or promise any document, checklist, teardown, guide,
   template, report or framework that is not in the verified-assets list above.
   If that list says NONE, you may not offer one at all. Claiming an artifact
   that does not exist is worse than a weak message, because a yes creates a
   debt Agnesh cannot pay.
8. Spread the messages across different triggers where the trigger list allows
   it. Do not build all six on trigger 1.
9. Subject lines: under {MAX_SUBJECT_CHARS} characters, lowercase or sentence
   case, specific. No "Quick question", no "Touching base", no clickbait.
10. Email body: under {MAX_EMAIL_WORDS} words. Plain text. No "Hope this finds
    you well", no "I came across your impressive company", no flattery opener.
    Get to the trigger in the first sentence.
11. One ask per message. Prefer a question they can answer in one line. Only ONE
    of the three emails may ask for a call - the other two must end in a question.
12. LinkedIn "connection-note" must be under {MAX_CONNECTION_NOTE_CHARS}
    characters. The other two must be under {MAX_LINKEDIN_CHARS}.
13. Sign emails as {PRODUCT['sender']}. Do not invent a phone number, a
    calendar link, or a company website.
14. Indian business context: professional and direct. No "Dear Sir/Madam", no
    "Respected Sir", no American sales slang either.
15. Each of the six messages must be genuinely different. If two would say the
    same thing, change the angle, not just the wording. In particular, the
    phrase "onboarding our first colleges" and any restatement of what MNGO is
    may appear in at most TWO of the six messages. The others must convey it
    differently or not at all.
16. If a trigger has no date attached, do not imply it is recent.
17. Never name an individual employee in connection with a departure,
    resignation, or exit. A leadership vacancy can be referenced as "your
    recent leadership change" without naming who left.
18. Being early is not a weakness to hide. A founder saying "we are building
    this, here is what we do not know yet" gets replies. A founder pretending
    to have scale gets deleted. Write like the former.
19. Write correct English. Every sentence must parse. Check verb forms before
    you answer: "requires streamlined campus hiring", never "requires
    streamline campus hiring".

Reply with JSON only, matching this shape exactly:
{SCHEMA_EXAMPLE}"""


def build_proofread_prompt(items):
    """Second pass: grammar and fluency only, nothing about strategy."""
    numbered = "\n\n".join(f"[{i}] {text}" for i, text in items)
    return f"""You are proofreading short business messages for grammar and fluency ONLY.

Do NOT comment on strategy, tone, persuasiveness, length or content. Only flag a
message if it contains an actual error that a fluent English writer would not
produce: a broken verb form, a missing word, a mangled clause, subject-verb
disagreement, a wrong preposition, or a phrase that does not parse.

Do NOT flag deliberate sentence fragments, informal phrasing, or correct Indian
English usage.

MESSAGES:
{numbered}

Reply with JSON only, in this shape:
{{"issues": [{{"index": 1, "quote": "the exact broken phrase", "problem": "what is wrong with it"}}]}}

If every message is clean, reply with exactly: {{"issues": []}}"""


# ----------------------------------------------------------------- output side

def parse_response(raw_text, domain):
    """Parse the model's JSON, saving the raw reply if it will not parse."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        debug_path = OUT_DIR / f"{domain}.raw.txt"
        debug_path.write_text(raw_text, encoding="utf-8")
        die(f"Model did not return valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno}). "
            f"The raw reply was saved to {debug_path} so you can look at it.")

    if not isinstance(data, dict):
        die(f"Expected a JSON object from the model, got {type(data).__name__}.")
    return data


def normalise(text):
    """Lowercase and collapse whitespace, so two facts can be compared fairly."""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def is_grounded(trigger_used, known_facts):
    """
    Did this message cite a trigger that actually exists?

    This is the citation check. The model is told to copy the fact verbatim; if
    what comes back doesn't match any real trigger, the message is very likely
    built on something invented, and we flag it rather than quietly shipping it.
    """
    cited = normalise(trigger_used)
    if not cited:
        return False
    return any(cited == fact or cited in fact or fact in cited for fact in known_facts)


NUMBER_PATTERN = re.compile(r"\d+\s*(?:%|x\b|percent|times)", re.I)

# Phrases that claim a network MNGO does not have yet. Caught in the body
# because the model will reach for them under sales pressure.
OVERCLAIM_PATTERN = re.compile(
    r"\b(our (network|colleges|campuses|partner colleges|student pool)"
    r"|\d+\s*(colleges|campuses|institutions)"
    r"|access to (our|a) (network|pool)"
    r"|we (have|work with) \d+)\b", re.I)

# An offer of a document that may not exist. The invented-number check catches
# fake metrics; this catches fake artifacts, which are worse - a yes leaves you
# owing someone a file you never wrote.
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
    """
    Find a numeric claim in the BODY that no trigger supports.

    is_grounded() checks the citation. This checks the message itself - the
    model can cite a real trigger and still invent "reduces drop-off by 40%"
    inside the text. That happened in testing; this is the check that catches it.
    """
    joined = " ".join(known_facts).lower()
    for match in NUMBER_PATTERN.finditer(text):
        if match.group(0).lower() not in joined:
            return match.group(0)
    return None


def overclaim(text):
    """
    Find language claiming a college network that does not exist yet.

    This is the check that matters most commercially. A company that hears
    "our network of colleges" will ask which ones, and there is no answer.
    """
    match = OVERCLAIM_PATTERN.search(text)
    return match.group(0) if match else None


def asset_claim(text):
    """
    Find an offer of a document that is not in VERIFIED_ASSETS.

    Returns the offending phrase, or None. If the phrase names something on the
    verified list, it is allowed through.
    """
    known = " ".join(VERIFIED_ASSETS).lower()
    for pattern in (ASSET_CLAIM_PATTERN, ASSET_OFFER_PATTERN):
        match = pattern.search(text)
        if not match:
            continue
        phrase = match.group(0)
        if known and any(asset.lower() in phrase.lower() for asset in VERIFIED_ASSETS):
            continue
        return phrase.strip()
    return None


def similar(a, b):
    """Rough word-overlap ratio, used only to spot near-duplicate messages."""
    words_a, words_b = set(normalise(a).split()), set(normalise(b).split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def check_boilerplate(texts):
    """
    Count stock pitch phrases across the whole batch.

    One message saying "we are onboarding our first colleges" is honest. Four
    saying it reads like a bot with a single sentence, and if they land in the
    same inbox as a sequence, that is exactly how it will be read.
    """
    findings = []
    joined = [normalise(t) for t in texts]
    for phrase in BOILERPLATE_PHRASES:
        needle = normalise(phrase)
        count = sum(1 for t in joined if needle in t)
        if count > MAX_PHRASE_REPEATS:
            findings.append(f"'{phrase}' appears in {count} of {len(joined)} messages "
                            f"(limit {MAX_PHRASE_REPEATS}) - the batch reads like one "
                            f"sentence pasted repeatedly")
    return findings


def run_proofread(client, result, warnings):
    """
    Ask the model to check its own output for grammar errors only.

    Costs one extra call. Failure here is never fatal - a proofread that does
    not come back cleanly just means no grammar warnings this run.
    """
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
        raw = ask_gemini(client, build_proofread_prompt(items))
        data = json.loads(raw)
        issues = data.get("issues") or []
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        warnings.append("proofread pass did not return usable JSON - grammar was "
                        "not checked this run")
        return
    except Exception as exc:  # network, quota, whatever - never fatal
        warnings.append(f"proofread pass failed ({type(exc).__name__}) - grammar was "
                        f"not checked this run")
        return

    if not isinstance(issues, list):
        return

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        idx = issue.get("index")
        label = labels.get(idx, f"message {idx}")
        quote = str(issue.get("quote") or "").strip()
        problem = str(issue.get("problem") or "").strip()
        warnings.append(f"{label}: grammar - '{quote}' ({problem})")


def clean_result(data, source, warnings):
    """Force the model's reply into a fixed shape and check it against the rules."""
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
        trigger_used = str(item.get("trigger_used") or "").strip()
        trigger_link = str(item.get("trigger_link") or "").strip()
        grounded = is_grounded(trigger_used, known_facts)
        word_count = len(body.split())

        if len(subject) > MAX_SUBJECT_CHARS:
            warnings.append(f"email {i + 1} ({angle}): subject is {len(subject)} chars "
                            f"(limit {MAX_SUBJECT_CHARS})")
        if word_count > MAX_EMAIL_WORDS:
            warnings.append(f"email {i + 1} ({angle}): body is {word_count} words "
                            f"(limit {MAX_EMAIL_WORDS})")

        fake = invented_number(body, known_facts)
        if fake:
            warnings.append(f"email {i + 1} ({angle}): contains '{fake}' - no trigger "
                            f"supports this number, it was invented")

        claim = overclaim(body)
        if claim:
            warnings.append(f"email {i + 1} ({angle}): says '{claim}' - MNGO has no "
                            f"confirmed colleges yet. DO NOT SEND.")

        asset = asset_claim(body)
        if asset:
            warnings.append(f"email {i + 1} ({angle}): offers '{asset}' - that is not "
                            f"in VERIFIED_ASSETS, so it does not exist. DO NOT SEND.")

        if not grounded:
            warnings.append(f"email {i + 1} ({angle}): cites a trigger that is not in "
                            f"the triggers file - check it for invented facts")

        if not trigger_link:
            warnings.append(f"email {i + 1} ({angle}): no trigger_link - the model "
                            f"could not say how the trigger relates to campus hiring")

        emails.append({
            "angle": angle,
            "subject": subject,
            "body": body,
            "trigger_used": trigger_used,
            "trigger_link": trigger_link,
            "word_count": word_count,
            "grounded": grounded,
            "invented_number": fake,
            "overclaim": claim,
            "asset_claim": asset,
        })

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
        trigger_used = str(item.get("trigger_used") or "").strip()
        trigger_link = str(item.get("trigger_link") or "").strip()
        grounded = is_grounded(trigger_used, known_facts)

        limit = MAX_CONNECTION_NOTE_CHARS if angle == "connection-note" else MAX_LINKEDIN_CHARS
        if len(text) > limit:
            warnings.append(f"linkedin {i + 1} ({angle}): {len(text)} chars "
                            f"(limit {limit}) - LinkedIn will truncate or reject it")

        fake = invented_number(text, known_facts)
        if fake:
            warnings.append(f"linkedin {i + 1} ({angle}): contains '{fake}' - no trigger "
                            f"supports this number, it was invented")

        claim = overclaim(text)
        if claim:
            warnings.append(f"linkedin {i + 1} ({angle}): says '{claim}' - MNGO has no "
                            f"confirmed colleges yet. DO NOT SEND.")

        asset = asset_claim(text)
        if asset:
            warnings.append(f"linkedin {i + 1} ({angle}): offers '{asset}' - that is not "
                            f"in VERIFIED_ASSETS, so it does not exist. DO NOT SEND.")

        if not grounded:
            warnings.append(f"linkedin {i + 1} ({angle}): cites a trigger that is not in "
                            f"the triggers file - check it for invented facts")

        if not trigger_link:
            warnings.append(f"linkedin {i + 1} ({angle}): no trigger_link - the model "
                            f"could not say how the trigger relates to campus hiring")

        linkedin.append({
            "angle": angle,
            "text": text,
            "trigger_used": trigger_used,
            "trigger_link": trigger_link,
            "char_count": len(text),
            "grounded": grounded,
            "invented_number": fake,
            "overclaim": claim,
            "asset_claim": asset,
        })

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
                        f"{label} {a + 1} and {label} {b + 1} are {score:.0%} similar - "
                        f"they are basically the same message")

    all_texts = [e["body"] for e in emails] + [m["text"] for m in linkedin]
    warnings.extend(check_boilerplate(all_texts))

    used = {normalise(m["trigger_used"]) for m in emails + linkedin if m["trigger_used"]}
    if len(known_facts) > 1 and len(used) == 1:
        warnings.append("all six messages lean on the same trigger, though more "
                        "were available")

    return {
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
        flags += f"  :rotating_light: **OVERCLAIM: {message['overclaim']}**"
    if message.get("asset_claim"):
        flags += f"  :rotating_light: **ASSET DOES NOT EXIST: {message['asset_claim']}**"
    return flags


def to_markdown(result):
    """A readable version, for screenshots and for reading the output as a human."""
    info = result["generated_from"]
    lines = [f"# Cold outreach - {result['company_name']}", "",
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
            lines += [f"> Link to campus hiring: {email['trigger_link']}  "]
        lines += [f"> {email['word_count']} words", ""]

    lines += ["## LinkedIn", ""]
    for i, msg in enumerate(result["linkedin_messages"], start=1):
        lines += [f"### {i}. {msg['angle']}{flags_for(msg)}", "",
                  msg["text"], ""]
        if msg["trigger_used"]:
            lines += [f"> Trigger: {msg['trigger_used']}  "]
        if msg.get("trigger_link"):
            lines += [f"> Link to campus hiring: {msg['trigger_link']}  "]
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
        mark += f"  [OVERCLAIM: {message['overclaim']}]"
    if message.get("asset_claim"):
        mark += f"  [FAKE ASSET: {message['asset_claim']}]"
    return mark


def print_summary(result):
    print(f"\n{result['company_name']}")
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
    target, force, do_proofread = parse_args(sys.argv)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        die("GEMINI_API_KEY is not set. Get a key from "
            "https://aistudio.google.com/app/apikey and put it in your "
            "environment or in a .env file next to this script.")

    domain, in_path = resolve_input(target)
    source = load_triggers(in_path)
    triggers = source["triggers"]

    # No trigger, no message. This is the whole of what used to be fallback mode.
    if source.get("no_trigger_found") or not triggers:
        refuse_no_trigger(domain, source, in_path)

    warnings = []
    top_score = max((t.get("relevance_score", 0) for t in triggers), default=0)

    # The quality gate. A 5/10 trigger produces a message that reads as generic
    # to the recipient and burns the address for nothing. Refuse, do not warn.
    if top_score < MIN_TOP_SCORE:
        if not force:
            die(f"Top trigger for {source.get('company_name') or domain} scores "
                f"{top_score}/10, below the floor of {MIN_TOP_SCORE}.\n"
                f"Nothing was generated.\n\n"
                f"This usually means the company is a poor fit rather than that the "
                f"scraper failed. Pick a company whose campus-hiring pain is obvious, "
                f"or run again with --force if you want to see the drafts anyway.")
        warnings.append(f"FORCED: top trigger is {top_score}/10, below the floor of "
                        f"{MIN_TOP_SCORE}. These drafts are probably not worth sending.")

    client = genai.Client(api_key=api_key)

    print(f"Read {len(triggers)} trigger(s) from {in_path}")
    print(f"Asking {MODEL} for 3 emails + 3 LinkedIn messages...")

    prompt = build_prompt(source.get("company_name") or domain, triggers,
                          source.get("pain_hypothesis", ""))
    result = clean_result(parse_response(ask_gemini(client, prompt), domain),
                          source, warnings)
    result["generated_from"]["forced"] = force and top_score < MIN_TOP_SCORE

    if do_proofread:
        print("Proofreading...")
        run_proofread(client, result, result["warnings"])

    json_path, md_path = save_result(domain, result)
    print_summary(result)

    print(f"\nSaved to {json_path}")
    print(f"        and {md_path}")
    print("\nNothing was sent. Review the drafts before using them.")


if __name__ == "__main__":
    main()