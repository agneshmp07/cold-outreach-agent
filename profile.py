"""
profile.py - step 0 of the pipeline, run once per company that uses this tool.

You give it YOUR OWN domain. It reads your site, works out what you sell and who
would buy it, and writes two files:

    clients/<domain>.json      who you are, what you may claim, where your ICP lives
    context/icp-<domain>.md    a draft ICP: who fits, who does not, what to look for

Every other script reads those two files, which is what makes this tool work for
any company rather than only the one it was built for.

WHAT IT CANNOT DO, and why that matters:

It cannot work out what you are NOT allowed to claim. Your website markets the
vision - the ambition, the roadmap, the "trusted by teams everywhere". A model
reading it will cheerfully write that you have hundreds of customers, because
your homepage implies it. If that is not true, the tool has just written you a
lie that a prospect will call you on.

So "cannot_claim" is left empty and the profile is marked reviewed: false. The
generator refuses to run until you fill it in and flip that flag. One minute of
your attention, once, in exchange for never sending an email you cannot back up.

Usage:
    python profile.py mngo.in
    python profile.py mngo.in --no-scrape     # reuse an earlier scrape
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from resolve import to_domain
from triggers import (MODEL, ask_gemini, build_corpus, die, flatten_pages,
                      genai, load_scrape)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CLIENTS_DIR = BASE_DIR / "clients"
CONTEXT_DIR = BASE_DIR / "context"

SCHEMA_EXAMPLE = """{
  "company_name": "string",
  "what_you_sell": "string",
  "one_liner": "string",
  "business_model": "direct|marketplace|platform",
  "who_uses_it": "string",
  "who_pays_for_it": "string",
  "who_buys_it": "string",
  "target_group": "string",
  "claims_to_check": ["string"],
  "icp": {
    "fits": [
      {"industry": "string", "size": "string", "must_be_true": "string", "why_they_care": "string"}
    ],
    "does_not_fit": [
      {"looks_similar": "string", "why_wrong": "string"}
    ],
    "signals": [
      {"signal": "string", "where_visible": "string"}
    ],
    "weakest_assumption": "string"
  }
}"""


# ------------------------------------------------------------------ arguments

def parse_args(argv):
    args = list(argv[1:])
    scrape = "--no-scrape" not in args
    positional = [a for a in args if not a.startswith("--")]
    unknown = [a for a in args if a.startswith("--") and a != "--no-scrape"]
    if unknown:
        sys.exit(f"Unknown option(s): {', '.join(unknown)}")
    if len(positional) != 1:
        sys.exit(f"Usage: python {Path(__file__).name} <your-domain> [--no-scrape]")
    return positional[0], scrape


def clean_domain(arg):
    """Accept a company name or a domain. Names are resolved with one search."""
    domain = to_domain(arg)
    if not domain:
        die(f"Could not work out a domain from {arg!r}. "
            f"Try typing it directly, like  example.com")
    return domain


def run_scrape(domain):
    """Shell out to scrape.py rather than duplicating its fallback logic."""
    script = BASE_DIR / "scrape.py"
    if not script.exists():
        die(f"No scrape.py at {script}.")
    print(f"Reading {domain}...\n")
    result = subprocess.run([sys.executable, str(script), domain])
    if result.returncode != 0:
        die(f"scrape.py failed for {domain}. Check the domain and try again.")


# ------------------------------------------------------------------ the model

def build_prompt(domain, corpus):
    return f"""You are setting up a cold-outreach tool for the company at {domain}.
Below is text scraped from their own website. Work out who they are, what they
sell, and who would buy it.

Be concrete and be strict. A vague ICP produces outreach to everyone, which is
outreach to no one.

Rules:
1. Use ONLY the scraped text below. If the site does not say what they sell, say
   so rather than guessing from the domain name.
2. "one_liner" is how the company would describe itself in one sentence to a
   stranger, in plain words. No marketing adjectives - no "leading", "innovative",
   "cutting-edge", "world-class". If the site is full of them, strip them out.
3. WHO PAYS IS NOT ALWAYS WHO USES. Decide this before anything else, because
   everything downstream depends on it.
   - "who_uses_it": the people who open the app or log in day to day.
   - "who_pays_for_it": the ones who sign a contract or hand over money.
   - "business_model": "marketplace" if the site serves two different sides
     (buyers and sellers, riders and restaurants, students and recruiters);
     "platform" if one side pays to reach another; "direct" if the user and the
     payer are the same person.
   A consumer website almost always describes the USER side, because that is who
   it is marketing to. If this is a marketplace, the ICP below must target the
   side that PAYS - the businesses - not the consumers who use the app. Getting
   this backwards produces an ICP full of the company's own customers rather
   than its prospects, and every search built on it looks for the wrong people.
   Set "who_buys_it" to the paying side.
5. The ICP is the important part, and it describes WHO PAYS. For "fits", give at
   most 3 segments. Each needs an industry, a size range, one thing that must be
   TRUE about that company for this product to matter, and why they would care.
4. For "does_not_fit", name at most 4 company types that LOOK like good targets
   and are wrong. This must be genuinely different from the inverse of the fits
   list. If you cannot name any, the product description is too vague and you
   should say that in "weakest_assumption".
6. For "signals", every signal MUST be visible from outside the company - on a
   public website, a job board, a LinkedIn page, a review site or a press item.
   If you could not see it without being an employee, do not list it. At most 4.
   This rule matters more than any other: an invisible signal is useless to
   someone doing cold outreach.
7. "claims_to_check" is the important safety field. List the specific claims
   this company's website makes or implies about scale, traction, customers or
   results - the ones a cold email would be tempted to repeat and that might not
   be true yet. Examples of the shape: "implies it already has many customers",
   "implies an established partner network", "quotes a results percentage".
   These are things for the human to CONFIRM or FORBID, not facts you are
   asserting. List at most 5.
8. "target_group" is the answer to "who does this company sell to?" in two or
   three plain sentences a person could read aloud. Name the kinds of business
   or the kinds of role, say roughly how big they are, and say what makes one of
   them a good prospect rather than a bad one. No tables, no jargon, no
   marketing adjectives. If the company sells to two different sides, say so and
   name the side that pays.
9. "weakest_assumption" is the one thing in your ICP you are least sure about,
   and how the human could check it. Be specific. Do not hedge everything.

Reply with JSON only, matching this shape exactly:
{SCHEMA_EXAMPLE}

--- SCRAPED TEXT ---
{corpus}
--- END SCRAPED TEXT ---"""


def parse_response(raw_text, domain):
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
        debug = CLIENTS_DIR / f"{domain}.raw.txt"
        debug.write_text(raw_text, encoding="utf-8")
        die(f"Model did not return valid JSON: {exc.msg}. Raw reply saved to {debug}.")
    if not isinstance(data, dict):
        die(f"Expected a JSON object, got {type(data).__name__}.")
    return data


# --------------------------------------------------------------------- output

def icp_markdown(domain, data):
    icp = data.get("icp") or {}
    lines = [f"# ICP - {data.get('company_name', domain)}", "",
             "> Drafted by profile.py from the company's own website. Read it and "
             "cut what is wrong before trusting it. A generated ICP that nobody "
             "graded is a guess with formatting.", "",
             (f"## Who this company sells to\n\n{data.get('target_group')}\n"
              if data.get("target_group") else ""), "",
             f"**This describes who PAYS:** {data.get('who_pays_for_it') or data.get('who_buys_it', 'not stated')}", "",
             f"_Business model: {data.get('business_model', 'not stated')}. "
             f"Day-to-day users: {data.get('who_uses_it', 'not stated')}._", "",
             "## Who fits", "",
             "| Industry | Company size | What must be true | Why they would care |",
             "| --- | --- | --- | --- |"]

    for row in (icp.get("fits") or [])[:3]:
        lines.append(f"| {row.get('industry', '-')} | {row.get('size', '-')} | "
                     f"{row.get('must_be_true', '-')} | {row.get('why_they_care', '-')} |")

    lines += ["", "## Who does NOT fit", "",
              "| Looks similar but is wrong | Why |", "| --- | --- |"]
    for row in (icp.get("does_not_fit") or [])[:4]:
        lines.append(f"| {row.get('looks_similar', '-')} | {row.get('why_wrong', '-')} |")

    lines += ["", "## Buying signals", "",
              "| Signal | Where I would see it from outside |", "| --- | --- |"]
    for row in (icp.get("signals") or [])[:4]:
        lines.append(f"| {row.get('signal', '-')} | {row.get('where_visible', '-')} |")

    lines += ["", "## Weakest assumption", "",
              icp.get("weakest_assumption", "not stated"), "",
              "## How to grade this", "",
              "Before you use it, answer these four. They take five minutes and "
              "they are the difference between an ICP and a wish list.", "",
              "1. Could you actually SEE every buying signal from outside? Cross "
              "out the ones you could not.",
              "2. Is the does-not-fit table genuinely different from the inverse "
              "of the fits table? If not, the description of your product was too "
              "vague - rewrite it and re-run.",
              "3. Does this ICP rule out any real company you were considering? "
              "If it rules out nobody, it is useless.",
              "4. Do you agree with the weakest assumption it named? If not, name "
              "the one you think is actually weakest and write it in.", "",
              "## Rule", "",
              "No company name from this file goes into any prompt file. Prompts "
              "read this; they do not contain it."]

    return "\n".join(lines)


def client_json(domain, data, icp_path):
    return {
        "domain": domain,
        "name": data.get("company_name", domain),
        "one_liner": data.get("one_liner", ""),
        "what_you_sell": data.get("what_you_sell", ""),
        "target_group": data.get("target_group", ""),
        "business_model": data.get("business_model", ""),
        "who_uses_it": data.get("who_uses_it", ""),
        "who_pays_for_it": data.get("who_pays_for_it", ""),
        "who_buys_it": data.get("who_buys_it") or data.get("who_pays_for_it", ""),
        "sender": "",
        "cannot_claim": [],
        "verified_assets": [],
        "icp_file": str(icp_path.relative_to(BASE_DIR)).replace("\\", "/"),
        "exclude_file": "context/exclude.txt",
        "reviewed": False,
        "_claims_to_check": data.get("claims_to_check") or [],
        "_note": ("Fill in sender and cannot_claim, then set reviewed to true. "
                  "Nothing downstream will run until you do. See _claims_to_check "
                  "for what the tool noticed your site implies."),
    }


def save(domain, data):
    CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)

    icp_path = CONTEXT_DIR / f"icp-{domain}.md"
    client_path = CLIENTS_DIR / f"{domain}.json"

    if client_path.exists():
        print(f"\n{client_path} already exists - not overwriting it.")
        print("Delete it first if you want a fresh profile.")
        sys.exit(1)

    try:
        icp_path.write_text(icp_markdown(domain, data), encoding="utf-8")
        client_path.write_text(
            json.dumps(client_json(domain, data, icp_path), indent=2,
                       ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        die(f"Could not write the profile: {exc}")
    return client_path, icp_path


def wrap(text, width=68, indent="  "):
    """Wrap a paragraph for the terminal."""
    words, line, lines = str(text or "").split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(indent + line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(indent + line)
    return "\n".join(lines)


def print_summary(domain, data, client_path, icp_path):
    icp = data.get("icp") or {}
    name = data.get("company_name", domain)

    # The target group comes first and in plain words. It is the question people
    # actually ask - who does this company sell to - and burying it under a
    # scrape log and three tables is how it gets missed.
    print(f"\n{'=' * 60}")
    print(f"{name.upper()} SELLS TO")
    print(f"{'=' * 60}\n")
    print(wrap(data.get("target_group")
               or data.get("who_pays_for_it")
               or data.get("who_buys_it") or "not stated"))

    print(f"\n{'-' * 60}")
    print("Detail")
    print(f"{'-' * 60}")
    print(f"\n  Sells   : {data.get('what_you_sell', '-')}")
    print(f"  Model   : {data.get('business_model', '-')}")
    print(f"  Users   : {data.get('who_uses_it', '-')}")
    print(f"  PAYERS  : {data.get('who_pays_for_it') or data.get('who_buys_it', '-')}")
    if str(data.get("business_model", "")).lower() in ("marketplace", "platform"):
        print("            ^ two-sided. The ICP targets the PAYING side. If that")
        print("              line names consumers rather than businesses, the ICP")
        print("              is aimed at your own customers - re-run or edit it.")
    print(f"\n  One-liner:\n    {data.get('one_liner', '-')}")

    fits = icp.get("fits") or []
    print(f"\n  ICP segments ({len(fits)}):")
    for row in fits:
        print(f"    - {row.get('industry', '-')} ({row.get('size', '-')})")

    signals = icp.get("signals") or []
    print(f"\n  Buying signals ({len(signals)}):")
    for row in signals:
        print(f"    - {row.get('signal', '-')}")
        print(f"      seen at: {row.get('where_visible', '-')}")

    checks = data.get("claims_to_check") or []
    if checks:
        print("\n  Your site implies these. Decide which you can actually back up:")
        for c in checks:
            print(f"    - {c}")

    print(f"\n{'-' * 60}")
    print("Two things before you source targets:")
    print(f"{'-' * 60}")
    print(f"\n1. Open {client_path}")
    print('   - set "sender" to the name that signs the emails')
    print('   - fill "cannot_claim" with what you must NEVER say')
    print("     This is the one thing the tool cannot work out for you. Your")
    print("     website markets the ambition; only you know what is signed.")
    print('     Example: "a number of customers", "our partner network"')
    print('   - set "reviewed" to true')
    print(f"\n2. Open {icp_path} and grade it using the four questions at the")
    print("   bottom. Cross out any signal you could not actually see from")
    print("   outside the company. That is usually one or two of them.")
    print("\nThen:  python find_targets.py")


# ------------------------------------------------------------------------ main

def main():
    target, do_scrape = parse_args(sys.argv)
    domain = clean_domain(target)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        die("GEMINI_API_KEY is not set. Put it in your .env next to this script.")

    scrape_path = DATA_DIR / f"{domain}.json"
    if do_scrape:
        run_scrape(domain)
    elif not scrape_path.exists():
        die(f"No scrape at {scrape_path} and --no-scrape was passed. "
            f"Drop the flag to read the site.")

    pages = flatten_pages(load_scrape(scrape_path))
    if not pages:
        die(f"{scrape_path} contained no page text. The site may be entirely "
            f"JavaScript, or the domain may be wrong.")

    print(f"Read {len(pages)} page(s) from your site")
    print(f"Asking {MODEL} who you are and who buys from you...")

    client = genai.Client(api_key=api_key)
    raw = ask_gemini(client, build_prompt(domain, build_corpus(pages)))
    data = parse_response(raw, domain)

    client_path, icp_path = save(domain, data)
    print_summary(domain, data, client_path, icp_path)


if __name__ == "__main__":
    main()