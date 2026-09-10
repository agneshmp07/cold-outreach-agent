"""
check_fit.py - the qualification step.

    python check_fit.py <client-domain> <target-domain>
    python check_fit.py mngo.in happiestminds.com

Says whether the target company matches the CLIENT's ICP, and whether that
client's buying signal is visible from outside, BEFORE spending a scrape, a news
search and two Gemini calls on them.

The ICP comes from the client profile in clients/<client-domain>.json, so this
works for any company that has run profile.py. Nothing here is specific to one
product.

What it can do: see what is public and judge it against an ICP.
What it cannot do: prove a company does NOT have the problem. A company with a
real problem and a tidy website comes back SKIP. That is the right trade when
you have more companies than hours, but a SKIP means "not visibly a buyer", not
"not a buyer".

Output:
    fit/<target-domain>.json
    fit/<target-domain>.md

Exit codes, so this can gate a pipeline:
    0  PROCEED
    1  SKIP
    2  UNCLEAR
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

from generate import load_client
from resolve import to_domain
from triggers import MODEL, ask_gemini, die, genai

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "fit"

SERPER_URL = "https://google.serper.dev/search"
RESULTS_PER_QUERY = 8
REQUEST_PAUSE_SECONDS = 1.0

QUERY_SHAPES = [
    ('what they do', '"{name}" company what they do'),
    ('size', '"{name}" employees headcount linkedin'),
    ('the problem area', '"{name}" {problem_terms}'),
    ('the signal', '"{name}" {signal_terms}'),
    ('careers page', 'site:{domain} careers OR hiring OR about'),
]

SCHEMA_EXAMPLE = """{
  "company_name": "string",
  "what_they_do": "string",
  "size": {"estimate": "string", "evidence": "string", "status": "VERIFIED|ASSERTED|NOT FOUND"},
  "segment": {"best_match": "string", "in_icp": true, "why": "string", "status": "VERIFIED|ASSERTED"},
  "signal": {"found": true, "what": "string", "evidence": "string", "status": "VERIFIED|ASSERTED|NOT FOUND"},
  "disqualifiers": ["string"],
  "verdict": "PROCEED|SKIP|UNCLEAR",
  "reason": "string",
  "could_not_find": ["string", "string"]
}"""


# ------------------------------------------------------------------ arguments

def parse_args(argv):
    args = list(argv[1:])
    quiet = "--quiet" in args
    positional = [a for a in args if not a.startswith("--")]
    unknown = [a for a in args if a.startswith("--") and a != "--quiet"]
    if unknown:
        sys.exit(f"Unknown option(s): {', '.join(unknown)}")
    if len(positional) != 2:
        sys.exit(f"Usage: python {Path(__file__).name} <client-domain> "
                 f"<target-domain> [--quiet]\n"
                 f"Example: python {Path(__file__).name} mngo.in happiestminds.com")
    return positional[0], positional[1], quiet


def clean_domain(arg):
    """Accept a company name or a domain. Names are resolved with one search."""
    domain = to_domain(arg)
    if not domain:
        die(f"Could not work out a domain from {arg!r}. "
            f"Try typing it directly, like  example.com")
    return domain


def load_icp(client):
    """Read the ICP markdown this client's profile points at."""
    rel = str(client.get("icp_file") or "").strip()
    if not rel:
        die(f"{client['_path']} has no \"icp_file\". Point it at the markdown "
            f"file holding this company's ICP.")

    path = BASE_DIR / rel
    if not path.exists():
        die(f"No ICP at {path} (from \"icp_file\" in {client['_path'].name}).\n"
            f"Run:  python profile.py {client['_slug']}")

    text = path.read_text(encoding="utf-8").strip()
    if len(text) < 100:
        die(f"{path} is nearly empty. Fill it in before using this.")
    return text, path


def search_terms(client):
    """
    Words to hunt for, drawn from what this client sells.

    A generic tool cannot know that one client cares about campus hiring and
    another about warehouse logistics, so the search shapes are filled from the
    client's own description rather than hardcoded.
    """
    sells = str(client.get("what_you_sell") or client.get("one_liner") or "")
    buyers = str(client.get("who_pays_for_it") or client.get("who_buys_it") or "")
    problem = " ".join(sells.split()[:12])
    signal = " ".join((sells + " " + buyers).split()[:12])
    return problem or "operations", signal or "process"


# --------------------------------------------------------------------- search

def serper_search(api_key, query, num=RESULTS_PER_QUERY):
    try:
        response = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"  ! search failed ({type(exc).__name__})")
        return {}

    if response.status_code == 403:
        die("Serper rejected the key (403). Check SERPER_API_KEY in your .env.")
    if response.status_code != 200:
        print(f"  ! Serper returned {response.status_code}")
        return {}

    try:
        return response.json()
    except ValueError:
        print("  ! Serper returned something that is not JSON")
        return {}


def guess_company_name(api_key, domain):
    data = serper_search(api_key, domain, num=5)
    kg = data.get("knowledgeGraph") or {}
    if kg.get("title"):
        return kg["title"]
    for result in data.get("organic") or []:
        title = (result.get("title") or "").split("|")[0].split("-")[0].strip()
        if 2 < len(title) < 50:
            return title
    return domain.split(".")[0].replace("-", " ").title()


def gather(api_key, domain, name, client):
    problem_terms, signal_terms = search_terms(client)
    evidence = []
    for label, shape in QUERY_SHAPES:
        query = shape.format(name=name, domain=domain,
                             problem_terms=problem_terms,
                             signal_terms=signal_terms)
        print(f"searching [{label}]: {query}")
        data = serper_search(api_key, query)
        results = data.get("organic") or []
        print(f"  {len(results)} result(s)")
        for result in results:
            evidence.append({
                "asking_about": label,
                "title": result.get("title", ""),
                "snippet": result.get("snippet", ""),
                "link": result.get("link", ""),
                "date": result.get("date", ""),
            })
        time.sleep(REQUEST_PAUSE_SECONDS)
    return evidence


# ------------------------------------------------------------------ the model

def format_evidence(evidence):
    lines = []
    for i, e in enumerate(evidence, start=1):
        dated = f" ({e['date']})" if e.get("date") else ""
        lines.append(f"[{i}] asking about: {e['asking_about']}{dated}\n"
                     f"    {e['title']}\n"
                     f"    {e['snippet']}\n"
                     f"    {e['link']}")
    return "\n\n".join(lines) if lines else "NOTHING FOUND"


def build_prompt(domain, name, client, icp_text, evidence):
    return f"""You decide whether one company is worth researching, by checking it
against an ICP. You are strict. Saying "this company is not a fit" is a useful
answer, not a failure.

WHO IS SELLING: {client['name']} - {client['one_liner']}
WHAT THEY SELL: {client.get('what_you_sell') or client['one_liner']}

THEIR ICP - this is the standard, and the only standard:
---
{icp_text}
---

COMPANY UNDER TEST: {name} ({domain})

SEARCH RESULTS - the only facts you may use:
{format_evidence(evidence)}

Rules:
1. Use ONLY the search results above. If something is not in them, it is not
   known. Do not use anything you happen to know about this company from
   elsewhere - that is exactly the kind of confident guess this check exists to
   catch.
2. Every claim must be traceable to a numbered result. Put the result number in
   the evidence field, like "[3]".
3. Mark each finding VERIFIED if a result states it plainly, ASSERTED if you are
   inferring it, NOT FOUND if the results do not cover it. An inference from a
   company's industry is ASSERTED, never VERIFIED.
4. If you cannot find the company size, say NOT FOUND. Do not estimate from the
   industry.
5. Check the "Who does NOT fit" section of the ICP carefully. A match there is a
   disqualifier and forces SKIP, no matter how strong the signal looks.
6. The buying signal must be VISIBLE in the results. "They probably struggle with
   this" is not a signal, it is a wish. If no result shows it, it is NOT FOUND.
7. Do not treat a missing fact as a pass. "No disqualifier found" is only true if
   you looked and the results covered it. If the results are silent on something
   the ICP cares about, say so in could_not_find rather than assuming the best.

Verdict rules, applied in this order:
- Any disqualifier from the "Who does NOT fit" section  -> SKIP
- Not in any "Who fits" segment                          -> SKIP
- In a fits segment AND the signal is found              -> PROCEED
- In a fits segment, signal NOT FOUND, size unknown      -> UNCLEAR
- Everything else                                        -> SKIP

In "could_not_find", name exactly two things you would want to know and could not
learn from these results. Be specific.

Reply with JSON only, matching this shape exactly:
{SCHEMA_EXAMPLE}"""


def parse_verdict(raw_text, domain):
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        debug = OUT_DIR / f"{domain}.raw.txt"
        debug.write_text(raw_text, encoding="utf-8")
        die(f"Model did not return valid JSON: {exc.msg}. Raw reply saved to {debug}.")

    if not isinstance(data, dict):
        die(f"Expected a JSON object, got {type(data).__name__}.")

    verdict = str(data.get("verdict") or "").strip().upper()
    if verdict not in ("PROCEED", "SKIP", "UNCLEAR"):
        data["verdict"] = "UNCLEAR"
        data["reason"] = (f"model returned an unrecognised verdict {verdict!r}; "
                          f"treating as UNCLEAR. "
                          f"{data.get('reason', '')}").strip()
    else:
        data["verdict"] = verdict
    return data


# --------------------------------------------------------------------- output

def to_markdown(domain, client, result, evidence):
    lines = [f"# Fit check - {result.get('company_name', domain)}", "",
             f"_Checked for {client['name']}._", "",
             f"## {result['verdict']}", "",
             result.get("reason", ""), "",
             f"**Domain:** {domain}", "",
             f"**What they do:** {result.get('what_they_do', 'not found')}", ""]

    size = result.get("size") or {}
    lines += ["## Size", "",
              f"- {size.get('estimate', 'not found')}  "
              f"[{size.get('status', 'NOT FOUND')}]",
              f"- Evidence: {size.get('evidence', '-')}", ""]

    seg = result.get("segment") or {}
    lines += ["## ICP segment", "",
              f"- Best match: {seg.get('best_match', '-')}",
              f"- In the ICP: {'yes' if seg.get('in_icp') else 'no'}  "
              f"[{seg.get('status', 'ASSERTED')}]",
              f"- Why: {seg.get('why', '-')}", ""]

    sig = result.get("signal") or {}
    lines += ["## Buying signal", "",
              f"- Found: {'yes' if sig.get('found') else 'no'}  "
              f"[{sig.get('status', 'NOT FOUND')}]",
              f"- What: {sig.get('what', '-')}",
              f"- Evidence: {sig.get('evidence', '-')}", ""]

    disq = result.get("disqualifiers") or []
    if disq:
        lines += ["## Disqualifiers", ""] + [f"- {d}" for d in disq] + [""]

    missing = result.get("could_not_find") or []
    if missing:
        lines += ["## Could not find out", ""] + [f"- {m}" for m in missing] + [""]

    lines += ["## Search results used", ""]
    for i, e in enumerate(evidence, start=1):
        dated = f" ({e['date']})" if e.get("date") else ""
        lines += [f"[{i}] _{e['asking_about']}_{dated} - {e['title']}",
                  f"  {e['link']}", ""]

    return "\n".join(lines)


def save(domain, client, result, evidence, icp_path):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"domain": domain, "client": client["name"],
               "client_domain": client["_slug"],
               "checked_against": str(icp_path.name),
               "result": result, "evidence": evidence}
    json_path = OUT_DIR / f"{domain}.json"
    md_path = OUT_DIR / f"{domain}.md"
    try:
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        md_path.write_text(to_markdown(domain, client, result, evidence),
                           encoding="utf-8")
    except OSError as exc:
        die(f"Could not write to {OUT_DIR}: {exc}")
    return json_path, md_path


def print_result(domain, client, result, quiet):
    verdict = result["verdict"]
    if quiet:
        print(verdict)
        return

    seg = result.get("segment") or {}
    sig = result.get("signal") or {}
    size = result.get("size") or {}

    print(f"\n{'=' * 60}")
    print(f"{verdict}  -  {result.get('company_name', domain)}")
    print(f"{'=' * 60}")
    print(f"\n{result.get('reason', '')}\n")
    print(f"  Checked for  : {client['name']}")
    print(f"  What they do : {result.get('what_they_do', 'not found')}")
    print(f"  Size         : {size.get('estimate', 'not found')} "
          f"[{size.get('status', 'NOT FOUND')}]")
    print(f"  ICP segment  : {seg.get('best_match', '-')} "
          f"({'in ICP' if seg.get('in_icp') else 'NOT in ICP'})")
    print(f"  Signal       : {'found' if sig.get('found') else 'not found'} "
          f"[{sig.get('status', 'NOT FOUND')}]")
    if sig.get("what"):
        print(f"                 {sig['what']}")

    for d in result.get("disqualifiers") or []:
        print(f"  DISQUALIFIER : {d}")

    missing = result.get("could_not_find") or []
    if missing:
        print("\n  Could not find out:")
        for m in missing:
            print(f"    - {m}")

    if verdict == "PROCEED":
        print(f"\nNext:  python scrape.py {domain}")
    elif verdict == "SKIP":
        print("\nDo not research this one. A SKIP means 'not visibly a buyer',")
        print("not 'has no problem' - but you have more companies than hours.")
    else:
        print("\nNot enough public information to decide. Either find the size")
        print("and signal by hand, or move on - unclear is cheap to skip.")


# ------------------------------------------------------------------------ main

def main():
    client_arg, target, quiet = parse_args(sys.argv)
    domain = clean_domain(target)

    serper_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not serper_key:
        die("SERPER_API_KEY is not set. Put it in your .env next to this script.")
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        die("GEMINI_API_KEY is not set. Put it in your .env next to this script.")

    client = load_client(client_arg)
    icp_text, icp_path = load_icp(client)

    name = guess_company_name(serper_key, domain)
    print(f"Checking {name} ({domain}) against {icp_path.name} "
          f"for {client['name']}\n")

    evidence = gather(serper_key, domain, name, client)
    if not evidence:
        print("\nNo search results at all. Either the domain is wrong or Serper "
              "is failing.")
        sys.exit(2)

    print(f"\nAsking {MODEL} for a verdict on {len(evidence)} result(s)...")
    api = genai.Client(api_key=gemini_key)
    raw = ask_gemini(api, build_prompt(domain, name, client, icp_text, evidence))
    result = parse_verdict(raw, domain)

    json_path, md_path = save(domain, client, result, evidence, icp_path)
    print_result(domain, client, result, quiet)

    if not quiet:
        print(f"\nSaved to {json_path}")
        print(f"        and {md_path}")

    sys.exit({"PROCEED": 0, "SKIP": 1, "UNCLEAR": 2}[result["verdict"]])


if __name__ == "__main__":
    main()