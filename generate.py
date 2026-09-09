"""
generate.py - step 3 (final) of the cold outreach generator.

Reads the triggers that triggers.py found for one company and writes 3 cold
emails + 3 LinkedIn messages.

Two modes:
  grounded  - a trigger was found; every message is built on it and checked
  fallback  - no trigger; generic messages, clearly labelled as such

Output:
    messages/<domain>.json
    messages/<domain>.md

Usage:
    python generate.py razorpay.com

Nothing is ever sent. This script only writes files.
"""

import json
import re
import sys
from pathlib import Path

from triggers import MODEL, ask_gemini, die, genai

# ---------------------------------------------------------------------------
# EDIT THIS BLOCK to describe whatever you are selling.
# ---------------------------------------------------------------------------
PRODUCT = {
    "name": "[Your Product]",
    "one_liner": (
        "a hiring and recruitment automation SaaS: it screens inbound applicants, "
        "schedules interviews automatically, and keeps the pipeline moving without "
        "a full-time coordinator"
    ),
    "sender": "[Your Name]",
}

BASE_DIR = Path(__file__).resolve().parent
TRIGGERS_DIR = BASE_DIR / "triggers"
OUT_DIR = BASE_DIR / "messages"

MAX_SUBJECT_CHARS = 60
MAX_EMAIL_WORDS = 120
MAX_LINKEDIN_CHARS = 300

EMAIL_ANGLES = [
    ("trigger-direct", "Lead with the trigger fact in the first line. State in one "
                       "sentence why that fact makes this relevant. Ask one small question."),
    ("problem-cost", "Lead with the operational cost the trigger implies (screening "
                     "load, time-to-hire, coordinator hours). Frame it as a question, "
                     "never as an invented statistic."),
    ("peer-pattern", "Lead with the pattern you see in companies at this stage, but "
                     "anchor it to this specific company in the first sentence. The "
                     "message must be unusable for any other company."),
]

LINKEDIN_ANGLES = [
    ("connection-note", "A connection request note. Must fit in 300 characters. "
                        "Reference the trigger, no pitch, no link."),
    ("post-accept-dm", "A short DM sent after they accept. One specific question "
                       "about how they handle the thing the trigger implies."),
    ("value-first", "Offer one concrete useful thing (a teardown, a checklist, a "
                    "benchmark) with no call booking attached. Describe it plainly - "
                    "never claim a result or percentage it achieves."),
]

# Used only when no trigger was found. Deliberately modest angles: with no
# hook, the honest move is a short, low-claim message, not a confident pitch.
FALLBACK_ANGLES = [
    ("category-fit", "State what the company does, then the hiring problem "
                     "companies in that category usually have. No claim that "
                     "this specific company has it."),
    ("open-question", "Ask one honest question about how they handle hiring "
                      "today. No pitch until the last line."),
    ("short-intro", "Four sentences maximum. Who you are, what you do, one "
                    "question. No preamble."),
]

SCHEMA_EXAMPLE = """{
  "emails": [
    {"angle": "trigger-direct", "subject": "string", "body": "string", "trigger_used": "string"}
  ],
  "linkedin_messages": [
    {"angle": "connection-note", "text": "string", "trigger_used": "string"}
  ]
}"""


# ---------------------------------------------------------------- input side

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


# ------------------------------------------------------------------ the model

def format_triggers(triggers):
    lines = []
    for i, trigger in enumerate(triggers, start=1):
        lines.append(
            f"{i}. [{trigger.get('relevance_score', 0)}/10] {trigger.get('fact', '')}\n"
            f"   (found on: {trigger.get('source_page', 'unknown')})"
        )
    return "\n".join(lines)


def build_prompt(company_name, triggers, pain_hypothesis):
    email_angles = "\n".join(f'- "{name}": {brief}' for name, brief in EMAIL_ANGLES)
    linkedin_angles = "\n".join(f'- "{name}": {brief}' for name, brief in LINKEDIN_ANGLES)

    return f"""You write cold outreach that a busy Indian founder or hiring lead would
actually reply to. You are writing on behalf of {PRODUCT['sender']}, who sells
{PRODUCT['name']} - {PRODUCT['one_liner']}.

TARGET COMPANY: {company_name}

VERIFIED TRIGGERS (the only facts you may use):
{format_triggers(triggers)}

PAIN HYPOTHESIS: {pain_hypothesis or "none stated"}

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
2. Use ONLY the facts above. Do NOT invent metrics, funding, customer names,
   case studies, mutual connections, or anything you "noticed" that is not
   listed. No "I saw your post about..." unless it is a trigger.
3. NO NUMBERS that are not in the trigger list. No percentages, no multipliers,
   no "40% fewer drop-offs", no "3x faster", no "saves 10 hours a week". If you
   want to describe a benefit, describe it in words with no figure attached.
4. Spread the messages across different triggers where the trigger list allows
   it. Do not build all six on trigger 1.
5. Subject lines: under {MAX_SUBJECT_CHARS} characters, lowercase or sentence
   case, specific. No "Quick question", no "Touching base", no clickbait.
6. Email body: under {MAX_EMAIL_WORDS} words. Plain text. No "Hope this finds
   you well", no "I came across your impressive company", no flattery opener.
   Get to the trigger in the first sentence.
7. One ask per message. Prefer a question they can answer in one line. Only ONE
   of the three emails may ask for a call - the other two must end in a question.
8. LinkedIn "connection-note" must be under {MAX_LINKEDIN_CHARS} characters.
9. Sign emails as {PRODUCT['sender']}. Do not invent a phone number, a
   calendar link, or a company website.
10. Indian business context: professional and direct. No "Dear Sir/Madam", no
    "Respected Sir", no American sales slang either.
11. Each of the six messages must be genuinely different. If two would say the
    same thing, change the angle, not just the wording.
12. If a trigger has no date attached, do not imply it is recent. Write
    "you've raised from Insight Partners", not "recently closed".
13. Never name an individual employee in connection with a departure,
    resignation, or exit. A leadership vacancy can be referenced as
    "your recent ops leadership change" without naming who left.

Reply with JSON only, matching this shape exactly:
{SCHEMA_EXAMPLE}"""


def build_fallback_prompt(company_name, description):
    """
    Used only when no trigger was found.

    These messages have no hook, so they will convert far worse than a grounded
    one. The rules here are almost entirely prohibitions, because a model with
    nothing real to say will confabulate to fill the gap.
    """
    angles = "\n".join(f'- "{n}": {b}' for n, b in FALLBACK_ANGLES)
    return f"""You are writing cold outreach on behalf of {PRODUCT['sender']},
who sells {PRODUCT['name']} - {PRODUCT['one_liner']}.

TARGET COMPANY: {company_name}

There is NO verified trigger for this company. Nothing recent or specific was
found on their site. You are writing generic outreach and you must not pretend
otherwise.

What is known: {description or "only the company name"}

Write exactly 3 emails and exactly 3 LinkedIn messages.

EMAIL ANGLES - one message each, in this order:
{angles}

LINKEDIN ANGLES - one message each, in this order:
{angles}

HARD RULES:
1. Invent NOTHING. No funding, no hiring, no headcount, no news, no "I saw
   that...". You did not see anything.
2. No numbers, percentages or multipliers anywhere.
3. Do not imply you researched them. No "I've been following your work", no
   "your impressive growth", no flattery of any kind.
4. Under 80 words per email. Under 250 characters per LinkedIn message.
5. One question per message. No call asks - you have not earned one.
6. Leave "trigger_used" as an empty string. There is no trigger.
7. Sign emails as {PRODUCT['sender']}.
8. Indian business context: professional and direct. No "Dear Sir/Madam", no
   American sales slang.

Reply with JSON only, matching this shape exactly:
{SCHEMA_EXAMPLE}"""


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


def similar(a, b):
    """Rough word-overlap ratio, used only to spot near-duplicate messages."""
    words_a, words_b = set(normalise(a).split()), set(normalise(b).split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def clean_result(data, source, warnings, fallback=False):
    """
    Force the model's reply into a fixed shape and check it against the rules.

    In fallback mode there are no triggers, so the grounding check cannot apply.
    Every message is marked ungrounded, which is accurate: nothing supports it.
    """
    known_facts = [normalise(t.get("fact")) for t in source["triggers"]]
    known_facts = [f for f in known_facts if f]

    angle_set = FALLBACK_ANGLES if fallback else EMAIL_ANGLES
    li_angle_set = FALLBACK_ANGLES if fallback else LINKEDIN_ANGLES

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
            angle_set[i][0] if i < len(angle_set) else f"angle-{i + 1}")
        trigger_used = str(item.get("trigger_used") or "").strip()
        grounded = False if fallback else is_grounded(trigger_used, known_facts)
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

        if not grounded and not fallback:
            warnings.append(f"email {i + 1} ({angle}): cites a trigger that is not in "
                            f"the triggers file - check it for invented facts")

        emails.append({
            "angle": angle,
            "subject": subject,
            "body": body,
            "trigger_used": trigger_used,
            "word_count": word_count,
            "grounded": grounded,
            "invented_number": fake,
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
            li_angle_set[i][0] if i < len(li_angle_set) else f"angle-{i + 1}")
        trigger_used = str(item.get("trigger_used") or "").strip()
        grounded = False if fallback else is_grounded(trigger_used, known_facts)

        if angle == "connection-note" and len(text) > MAX_LINKEDIN_CHARS:
            warnings.append(f"linkedin {i + 1} ({angle}): {len(text)} chars "
                            f"(LinkedIn cuts connection notes off at {MAX_LINKEDIN_CHARS})")

        fake = invented_number(text, known_facts)
        if fake:
            warnings.append(f"linkedin {i + 1} ({angle}): contains '{fake}' - no trigger "
                            f"supports this number, it was invented")

        if not grounded and not fallback:
            warnings.append(f"linkedin {i + 1} ({angle}): cites a trigger that is not in "
                            f"the triggers file - check it for invented facts")

        linkedin.append({
            "angle": angle,
            "text": text,
            "trigger_used": trigger_used,
            "char_count": len(text),
            "grounded": grounded,
            "invented_number": fake,
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

    if not fallback:
        used = {normalise(m["trigger_used"]) for m in emails + linkedin
                if m["trigger_used"]}
        if len(known_facts) > 1 and len(used) == 1:
            warnings.append("all six messages lean on the same trigger, though more "
                            "were available")

    return {
        "company_name": source.get("company_name") or "unknown",
        "fallback_mode": fallback,
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


def to_markdown(result):
    """A readable version, for screenshots and for reading the output as a human."""
    lines = [f"# Cold outreach - {result['company_name']}", ""]

    if result.get("fallback_mode"):
        lines += ["> **No trigger found.** These messages are generic - nothing in "
                  "them is specific to this company. Expect a far lower reply rate "
                  "than a trigger-backed message.", ""]
    else:
        info = result["generated_from"]
        lines += [f"_{info['trigger_count']} trigger(s) found, top relevance "
                  f"{info['top_score']}/10._", ""]
        if info.get("pain_hypothesis"):
            lines += [f"**Pain hypothesis:** {info['pain_hypothesis']}", ""]

    lines += ["## Emails", ""]
    for i, email in enumerate(result["emails"], start=1):
        flags = ""
        if not email["grounded"] and not result.get("fallback_mode"):
            flags += "  :warning: **ungrounded**"
        if email.get("invented_number"):
            flags += f"  :warning: **invented number: {email['invented_number']}**"
        lines += [f"### {i}. {email['angle']}{flags}",
                  f"**Subject:** {email['subject']}", "",
                  email["body"], ""]
        if email["trigger_used"]:
            lines += [f"> Trigger: {email['trigger_used']}  "]
        lines += [f"> {email['word_count']} words", ""]

    lines += ["## LinkedIn", ""]
    for i, msg in enumerate(result["linkedin_messages"], start=1):
        flags = ""
        if not msg["grounded"] and not result.get("fallback_mode"):
            flags += "  :warning: **ungrounded**"
        if msg.get("invented_number"):
            flags += f"  :warning: **invented number: {msg['invented_number']}**"
        lines += [f"### {i}. {msg['angle']}{flags}", "",
                  msg["text"], ""]
        if msg["trigger_used"]:
            lines += [f"> Trigger: {msg['trigger_used']}  "]
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


def print_summary(result):
    print(f"\n{result['company_name']}")
    if result.get("fallback_mode"):
        print("  MODE: fallback (no trigger - generic messages)")
    for i, email in enumerate(result["emails"], start=1):
        mark = ""
        if not email["grounded"] and not result.get("fallback_mode"):
            mark += "  [UNGROUNDED]"
        if email.get("invented_number"):
            mark += f"  [INVENTED: {email['invented_number']}]"
        print(f"  Email {i} ({email['angle']}){mark}")
        print(f"    subject: {email['subject']}")
        print(f"    {email['word_count']} words")
    for i, msg in enumerate(result["linkedin_messages"], start=1):
        mark = ""
        if not msg["grounded"] and not result.get("fallback_mode"):
            mark += "  [UNGROUNDED]"
        if msg.get("invented_number"):
            mark += f"  [INVENTED: {msg['invented_number']}]"
        print(f"  LinkedIn {i} ({msg['angle']}){mark}: {msg['char_count']} chars")

    if result["warnings"]:
        print("\nWarnings:")
        for warning in result["warnings"]:
            print(f"  - {warning}")


# ------------------------------------------------------------------------ main

def main():
    if len(sys.argv) != 2:
        sys.exit(f"Usage: python {Path(__file__).name} <domain-or-triggers-path>")

    import os
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        die("GEMINI_API_KEY is not set. Get a key from "
            "https://aistudio.google.com/app/apikey and put it in your "
            "environment or in a .env file next to this script.")

    domain, in_path = resolve_input(sys.argv[1])
    source = load_triggers(in_path)
    triggers = source["triggers"]
    client = genai.Client(api_key=api_key)

    # No trigger: fall back to generic messages, labelled as such. The label is
    # the point - the alternative is inventing a hook, which is what the two
    # grounding checks exist to prevent.
    if source.get("no_trigger_found") or not triggers:
        print(f"{source.get('company_name') or domain}: no triggers found.")
        print("Generating FALLBACK messages (generic, low reply probability)...")

        warnings = ["NO TRIGGER FOUND - these messages are generic. Nothing in "
                    "them is specific to this company, and they will convert far "
                    "worse than trigger-backed messages."]
        prompt = build_fallback_prompt(source.get("company_name") or domain,
                                       source.get("pain_hypothesis", ""))
        result = clean_result(parse_response(ask_gemini(client, prompt), domain),
                              source, warnings, fallback=True)
        json_path, md_path = save_result(domain, result)
        print_summary(result)
        print(f"\nSaved to {json_path}")
        print(f"        and {md_path}")
        print("\nNothing was sent. Review the drafts before using them.")
        sys.exit(0)

    warnings = []
    top_score = max((t.get("relevance_score", 0) for t in triggers), default=0)
    if top_score < 4:
        warnings.append(f"weak triggers (best is {top_score}/10) - these messages are "
                        f"probably not worth sending")

    print(f"Read {len(triggers)} trigger(s) from {in_path}")
    print(f"Asking {MODEL} for 3 emails + 3 LinkedIn messages...")

    prompt = build_prompt(source.get("company_name") or domain, triggers,
                          source.get("pain_hypothesis", ""))
    result = clean_result(parse_response(ask_gemini(client, prompt), domain),
                          source, warnings)
    json_path, md_path = save_result(domain, result)
    print_summary(result)

    print(f"\nSaved to {json_path}")
    print(f"        and {md_path}")
    print("\nNothing was sent. Review the drafts before using them.")


if __name__ == "__main__":
    main()