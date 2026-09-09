"""
find_targets.py - step 0 of the MNGO outreach pipeline.

Finds companies that show the buying signal from context/icp.md, rather than
starting from a list of company names you already thought of. The signal that
survived grading:

    a campus-hiring job post that collects resumes through a Google Form or a
    personal email address

That is visible from outside, and it is diagnostic: a company running structured
campus hiring at volume does not collect CVs on forms.gle. A company that does
is coordinating by hand.

What this does NOT do: it does not confirm the company is in the ICP. It finds a
signal and scores it. Staffing agencies and rival campus-hiring platforms show
the same signal and are not buyers - they are filtered, but read the list anyway.

Output:
    targets/candidates.json
    targets/candidates.md
    targets/rejected.md     - everything thrown away, and why

Usage:
    python find_targets.py
    python find_targets.py --min-score 6
    python find_targets.py --resolve          # look up each company's domain
    python find_targets.py --limit 40

Nothing is contacted. This script only writes files.
"""

import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import requests

from triggers import die  # also loads .env

BASE_DIR = Path(__file__).resolve().parent
CONTEXT_DIR = BASE_DIR / "context"
OUT_DIR = BASE_DIR / "targets"
EXCLUDE_PATH = CONTEXT_DIR / "exclude.txt"

SERPER_URL = "https://google.serper.dev/search"
RESULTS_PER_QUERY = 10
DEFAULT_MIN_SCORE = 5
REQUEST_PAUSE_SECONDS = 1.0

# ---------------------------------------------------------------------------
# The searches. Each one hunts the same signal from a different angle.
# Edit these freely - they are the cheapest thing in the pipeline to change.
# No company names here. Company names live in context/, per the ICP file.
# ---------------------------------------------------------------------------
QUERY_TEMPLATES = [
    '"campus recruiter" OR "campus hiring" jobs India "forms.gle"',
    '"campus hiring" India "docs.google.com/forms" resume freshers',
    '"campus recruitment" India send resume "@gmail.com" engineering freshers',
    'site:linkedin.com/jobs "campus recruiter" India',
    'site:linkedin.com/jobs "campus hiring" freshers India engineering',
    '"placement drive" 2026 batch engineering "google form" company hiring',
    '"campus drive" India IT services freshers "share your resume at"',
]

# Sites that host the posts. The company is named in the title or snippet, not
# in the URL, so these are never treated as the target themselves.
AGGREGATOR_DOMAINS = {
    "linkedin.com", "naukri.com", "indeed.com", "indeed.co.in", "glassdoor.com",
    "glassdoor.co.in", "ambitionbox.com", "shine.com", "monsterindia.com",
    "foundit.in", "timesjobs.com", "internshala.com", "hirist.com", "cutshort.io",
    "instahyre.com", "apna.co", "workindia.in", "freshersworld.com",
    "placementindia.com", "youtube.com", "facebook.com", "x.com", "twitter.com",
    "reddit.com", "quora.com", "telegram.me", "t.me", "medium.com",
}

# Words that mark a result as a recruiter or agency rather than a company that
# hires for itself. These are not your buyer.
AGENCY_MARKERS = re.compile(
    r"\b(staffing|manpower|consultanc|consultants?|recruit(ers?|ment) (agency|services|firm|partners?)"
    r"|placement (agency|consultan|services)|hr solutions|talent solutions|rpo"
    r"|outsourc|hiring partner)\b", re.I)

# Companies that SELL campus-hiring software. They are the reason this filter
# exists at all: a vendor writes about campus hiring far more than a buyer does,
# publishes blog posts stuffed with these exact keywords, and advertises sales
# roles that mention campus drives. They outrank real buyers on every query in
# the list above. They are competitors, not prospects.
VENDOR_MARKERS = re.compile(
    r"\b(placement (platform|software|portal|automation|management system)"
    r"|campus (hiring|recruitment|recruiting|placement) "
    r"(platform|software|solution|automation|suite|tool|portal|system)"
    r"|university recruit(ing|ment) platform"
    r"|recruitment (platform|software|automation|management system)"
    r"|hiring (platform|software|automation)"
    r"|applicant tracking|\bats\b|\bhrms\b|\bhcm\b"
    r"|assessment platform|coding assessment|proctoring"
    r"|job (portal|board)|talent (platform|marketplace|cloud)"
    r"|edtech|upskilling platform|hackathon platform)\b", re.I)

FORM_MARKERS = re.compile(r"forms\.gle|docs\.google\.com/forms|google form", re.I)
PERSONAL_EMAIL = re.compile(
    r"[\w.+-]+@(gmail|yahoo|outlook|hotmail|rediffmail|ymail)\.(com|co\.in|in)", re.I)
CAMPUS_TERMS = re.compile(r"\b(campus|fresher|final year|placement|batch \d{4}|\d{4} batch)\b", re.I)
VOLUME_TERMS = re.compile(r"\b(drive|bulk|walk-?in|multiple colleges|pan[- ]india)\b", re.I)

# Title shapes that name the employer.
COMPANY_FROM_TITLE = [
    re.compile(r"^(.{2,60}?)\s+hiring\s", re.I),                 # "Acme hiring Campus Recruiter..."
    re.compile(r"\bat\s+([A-Z][\w&.\- ]{2,50}?)\s*(?:\||-|,|$)"),  # "... at Acme | LinkedIn"
    re.compile(r"\bjobs?\s+in\s+([A-Z][\w&.\- ]{2,50}?)\s*(?:\||-|,|$)", re.I),
]

TITLE_NOISE = re.compile(
    r"\b(linkedin|naukri|indeed|glassdoor|jobs?|careers?|hiring|vacanc|apply|india|"
    r"bengaluru|bangalore|mumbai|pune|hyderabad|chennai|delhi|noida|gurgaon|remote)\b", re.I)


# ------------------------------------------------------------------ arguments

def parse_args(argv):
    args = list(argv[1:])
    opts = {"min_score": DEFAULT_MIN_SCORE, "resolve": False, "limit": 0}

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--resolve":
            opts["resolve"] = True
        elif arg in ("--min-score", "--limit"):
            if i + 1 >= len(args):
                sys.exit(f"{arg} needs a number after it.")
            try:
                value = int(args[i + 1])
            except ValueError:
                sys.exit(f"{arg} needs a number, got {args[i + 1]!r}.")
            opts["min_score" if arg == "--min-score" else "limit"] = value
            i += 1
        else:
            sys.exit(f"Unknown option {arg!r}. "
                     f"Usage: python {Path(__file__).name} "
                     f"[--min-score N] [--limit N] [--resolve]")
        i += 1
    return opts


# -------------------------------------------------------------------- excludes

def load_excludes():
    """
    Company names to skip, one per line, from context/exclude.txt.

    Kept in context/ rather than in this file on purpose. The ICP file says no
    company name belongs in a prompt or a script - the day you point this at a
    different product, a hardcoded list would quietly keep filtering the wrong
    companies without erroring.
    """
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    if not EXCLUDE_PATH.exists():
        EXCLUDE_PATH.write_text(
            "# One company name per line. Lines starting with # are ignored.\n"
            "# Seeded from the 'Who does NOT fit' table in context/icp.md,\n"
            "# plus competitors - other campus placement platforms.\n"
            "TCS\nTata Consultancy\nInfosys\nWipro\nHCL\nCognizant\nAccenture\n"
            "Capgemini\nTech Mahindra\nLTIMindtree\nRazorpay\n"
            "Superset\njoinsuperset\nGreat Learning\n",
            encoding="utf-8")
        print(f"Created {EXCLUDE_PATH} - edit it to add companies to skip.")
    names = []
    for line in EXCLUDE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line.lower())
    return names


# --------------------------------------------------------------------- search

def serper_search(api_key, query, num=RESULTS_PER_QUERY):
    """One Serper call. Network problems warn and return nothing, never crash."""
    try:
        response = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num, "gl": "in", "hl": "en"},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"  ! search failed ({type(exc).__name__}) - skipping this query")
        return []

    if response.status_code == 403:
        die("Serper rejected the key (403). Check SERPER_API_KEY in your .env.")
    if response.status_code == 429:
        print("  ! Serper rate limit (429) - skipping this query")
        return []
    if response.status_code != 200:
        print(f"  ! Serper returned {response.status_code} - skipping this query")
        return []

    try:
        return response.json().get("organic") or []
    except ValueError:
        print("  ! Serper returned something that is not JSON - skipping")
        return []


# ------------------------------------------------------------------ extraction

def registrable_domain(url):
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def clean_company(name):
    name = re.sub(r"\s+", " ", name or "").strip(" -|,·:")
    name = re.sub(r"\b(pvt\.?|private|ltd\.?|limited|inc\.?|llp)\b", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" -|,·:")
    if len(name) < 3 or len(name) > 50:
        return ""
    # A "company name" made only of job-board words is not a company name.
    residue = TITLE_NOISE.sub("", name).strip(" -|,·:")
    if len(residue) < 3:
        return ""
    return name


def extract_company(result):
    """
    Work out who the employer is.

    For an aggregator URL the company is in the title. For a company's own
    careers page the domain is the company. Returns (name, source, domain) where
    source says which route was used, so you can distrust the weaker one.
    """
    link = result.get("link") or ""
    domain = registrable_domain(link)
    title = result.get("title") or ""

    if domain and domain not in AGGREGATOR_DOMAINS:
        return clean_company(domain.split(".")[0].replace("-", " ").title()) or "", "own-site", domain

    for pattern in COMPANY_FROM_TITLE:
        match = pattern.search(title)
        if match:
            name = clean_company(match.group(1))
            if name:
                return name, "title", ""
    return "", "unknown", ""


def score_result(result):
    """
    How strongly does this result show the signal?

    Weighted so that the two hard markers - a form link or a personal email -
    carry the result on their own, and the softer campus wording only tops up.
    """
    text = f"{result.get('title', '')} {result.get('snippet', '')}"
    score = 0
    evidence = []

    if FORM_MARKERS.search(text):
        score += 4
        evidence.append("collects resumes via a Google Form")
    email = PERSONAL_EMAIL.search(text)
    if email:
        score += 4
        evidence.append(f"resumes to a personal address ({email.group(0).split('@')[1]})")
    if CAMPUS_TERMS.search(text):
        score += 2
        evidence.append("campus or fresher hiring language")
    if VOLUME_TERMS.search(text):
        score += 1
        evidence.append("drive or bulk hiring language")

    return score, evidence


def classify_reject(company, text, excludes):
    """
    Decide whether to throw this result away, and say why.

    The reason matters more than the rejection. If most results come back
    'vendor', the queries are finding people who SELL campus hiring software
    rather than people who suffer from not having it, and the fix is the query
    list, not the filters.
    """
    if not company:
        return "no company name found in the result"
    if VENDOR_MARKERS.search(text):
        return "sells campus-hiring or recruitment software (competitor, not a buyer)"
    if AGENCY_MARKERS.search(text):
        return "staffing or recruitment agency (hires on behalf of others)"
    for name in excludes:
        if name in company.lower():
            return f"on the exclude list ({name})"
    return ""


# ------------------------------------------------------------------- resolving

def resolve_domain(api_key, company):
    """
    Find a company's website with one extra search.

    Returns the domain and the URL it came from, so the guess is checkable. Never
    constructs a domain from the name - a guessed domain wastes a scrape and
    poisons everything downstream.
    """
    results = serper_search(api_key, f"{company} India official website", num=5)
    for result in results:
        domain = registrable_domain(result.get("link") or "")
        if domain and domain not in AGGREGATOR_DOMAINS:
            return domain, result.get("link") or ""
    return "", ""


# --------------------------------------------------------------------- output

def collect(api_key, excludes, limit):
    found = {}
    rejected = []
    reasons = Counter()
    seen_results = 0

    for query in QUERY_TEMPLATES:
        print(f"searching: {query}")
        results = serper_search(api_key, query)
        seen_results += len(results)
        print(f"  {len(results)} result(s)")

        for result in results:
            company, source, own_domain = extract_company(result)
            text = f"{company} {result.get('title', '')} {result.get('snippet', '')}"

            reason = classify_reject(company, text, excludes)
            if reason:
                reasons[reason.split(" (")[0]] += 1
                rejected.append({
                    "company": company or "(unknown)",
                    "reason": reason,
                    "title": result.get("title", ""),
                    "link": result.get("link", ""),
                    "query": query,
                })
                continue

            score, evidence = score_result(result)
            if score == 0:
                reasons["no signal in the snippet"] += 1
                rejected.append({
                    "company": company,
                    "reason": "no signal found in the title or snippet",
                    "title": result.get("title", ""),
                    "link": result.get("link", ""),
                    "query": query,
                })
                continue

            key = company.lower()
            existing = found.get(key)
            if existing and existing["score"] >= score:
                existing["hits"] += 1
                continue

            found[key] = {
                "company": company,
                "domain": own_domain,
                "score": score,
                "name_from": source,
                "evidence": evidence,
                "title": result.get("title", ""),
                "snippet": result.get("snippet", ""),
                "link": result.get("link", ""),
                "date": result.get("date", ""),
                "query": query,
                "hits": (existing["hits"] if existing else 0) + 1,
            }
        time.sleep(REQUEST_PAUSE_SECONDS)

    candidates = sorted(found.values(), key=lambda c: (-c["score"], -c["hits"], c["company"]))
    if limit:
        candidates = candidates[:limit]
    return candidates, rejected, reasons, seen_results


def to_markdown(candidates, min_score, resolved):
    lines = ["# Target candidates", "",
             f"_{len(candidates)} candidate(s) at or above score {min_score}._", "",
             "> Sourced from the campus-hiring signal in `context/icp.md`. A high score "
             "means the signal is present, not that the company is in the ICP. Check "
             "size and segment yourself before running `scrape.py`.", ""]
    for i, c in enumerate(candidates, start=1):
        lines += [f"## {i}. {c['company']}  ·  {c['score']}/11",
                  f"- Domain: {c['domain'] or '_not resolved_'}"
                  + ("" if resolved or c["domain"] else "  (run with --resolve)"),
                  f"- Name taken from: {c['name_from']}",
                  f"- Seen in {c['hits']} result(s)"]
        for item in c["evidence"]:
            lines.append(f"- Signal: {item}")
        if c.get("date"):
            lines.append(f"- Posted: {c['date']}")
        lines += [f"- Source: {c['link']}", ""]
        if c.get("snippet"):
            lines += [f"> {c['snippet']}", ""]
    return "\n".join(lines)


def rejected_markdown(rejected, reasons, seen_results):
    """
    Everything thrown away, and why.

    Read the counts before the list. They tell you whether the queries are
    working: mostly 'vendor' means you are searching the words that vendors
    market with, and no filter will fix that - the queries have to change.
    """
    lines = ["# Rejected results", "",
             f"_{len(rejected)} of {seen_results} result(s) thrown away._", "",
             "## Why", ""]
    for reason, count in reasons.most_common():
        share = (count / seen_results * 100) if seen_results else 0
        lines.append(f"- {reason}: {count} ({share:.0f}%)")
    lines += ["",
              "> If 'sells campus-hiring or recruitment software' is the biggest "
              "bucket, the query list is the problem, not the filters. Vendors "
              "publish far more campus-hiring content than buyers do, so they win "
              "on any keyword a buyer would also use. Rewrite QUERY_TEMPLATES "
              "toward wording only a hiring company would produce.", "",
              "## What was thrown away", ""]
    for r in rejected:
        lines += [f"- **{r['company']}** — {r['reason']}",
                  f"  - {r['title']}",
                  f"  - {r['link']}"]
    return "\n".join(lines)


def save(candidates, rejected, reasons, seen_results, min_score, resolved):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "min_score": min_score,
        "resolved": resolved,
        "signal": "campus-hiring post collecting resumes via Google Form or personal email",
        "results_seen": seen_results,
        "rejected_count": len(rejected),
        "rejection_reasons": dict(reasons),
        "candidates": candidates,
    }
    json_path = OUT_DIR / "candidates.json"
    md_path = OUT_DIR / "candidates.md"
    rejected_path = OUT_DIR / "rejected.md"
    try:
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        md_path.write_text(to_markdown(candidates, min_score, resolved), encoding="utf-8")
        rejected_path.write_text(rejected_markdown(rejected, reasons, seen_results),
                                 encoding="utf-8")
    except OSError as exc:
        die(f"Could not write to {OUT_DIR}: {exc}")
    return json_path, md_path, rejected_path


# ------------------------------------------------------------------------ main

def main():
    opts = parse_args(sys.argv)

    api_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not api_key:
        die("SERPER_API_KEY is not set. Put it in your .env next to this script.")

    excludes = load_excludes()
    print(f"{len(excludes)} name(s) excluded from context/exclude.txt\n")

    candidates, rejected, reasons, seen_results = collect(api_key, excludes, opts["limit"])
    kept = [c for c in candidates if c["score"] >= opts["min_score"]]

    if opts["resolve"]:
        print("\nresolving domains...")
        for c in kept:
            if c["domain"]:
                continue
            domain, source = resolve_domain(api_key, c["company"])
            c["domain"] = domain
            c["domain_source"] = source
            print(f"  {c['company']}: {domain or 'not found'}")
            time.sleep(REQUEST_PAUSE_SECONDS)

    json_path, md_path, rejected_path = save(
        kept, rejected, reasons, seen_results, opts["min_score"], opts["resolve"])

    print(f"\n{seen_results} result(s) seen, {len(rejected)} rejected, "
          f"{len(candidates)} candidate(s), {len(kept)} at or above "
          f"score {opts['min_score']}")

    if reasons:
        print("\nWhy results were rejected:")
        for reason, count in reasons.most_common():
            print(f"  {count:>3}  {reason}")

    print()
    for c in kept[:15]:
        print(f"  {c['score']:>2}  {c['company']:<35} {c['domain'] or '-'}")

    print(f"\nSaved to {json_path}")
    print(f"        {md_path}")
    print(f"        {rejected_path}")
    print("\nA score means the signal is there, not that the company fits the ICP.")
    print("Read rejected.md first - the reason counts tell you if the queries work.")
    print("Then:  python scrape.py <domain>")


if __name__ == "__main__":
    main()