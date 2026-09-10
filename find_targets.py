"""
find_targets.py - sourcing.

    python find_targets.py <client-domain>
    python find_targets.py mngo.in --resolve

Finds companies that show the buying signals in THAT CLIENT's ICP, rather than
starting from a list of company names you already thought of.

The searches are built from the ICP at run time, by one Gemini call. That is the
only honest way to make this general: one client's signal is a campus job post
collecting resumes on a Google Form, another's is a restaurant chain advertising
delivery roles. Hardcoded queries would find the first client's signal no matter
whose ICP was loaded - which is exactly the bug this version fixes.

What this does NOT do: it does not confirm a company is in the ICP. It finds a
signal and scores it. Run check_fit.py on anything you pick.

Output:
    targets/candidates-<client>.json
    targets/candidates-<client>.md
    targets/rejected-<client>.md     - everything thrown away, and why

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

from generate import load_client
from resolve import NOT_A_COMPANY
from triggers import MODEL, ask_gemini, die, genai

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "targets"

SERPER_URL = "https://google.serper.dev/search"
RESULTS_PER_QUERY = 10
DEFAULT_MIN_SCORE = 5
REQUEST_PAUSE_SECONDS = 1.0
MAX_QUERIES = 7

# Sites that HOST posts rather than being the employer. The company is named in
# the title, not the URL. Treating the host as the company is what produced
# candidates called "Instagram", "Forms" and "Scribd" - the domain of the thing
# the signal MENTIONS, not the company that has the problem.
AGGREGATOR_DOMAINS = {
    "linkedin.com", "naukri.com", "indeed.com", "indeed.co.in", "glassdoor.com",
    "glassdoor.co.in", "ambitionbox.com", "shine.com", "monsterindia.com",
    "foundit.in", "timesjobs.com", "internshala.com", "hirist.com", "cutshort.io",
    "instahyre.com", "apna.co", "workindia.in", "freshersworld.com",
    "placementindia.com", "youtube.com", "facebook.com", "x.com", "twitter.com",
    "reddit.com", "quora.com", "telegram.me", "t.me", "medium.com",
    "instagram.com", "scribd.com", "slideshare.net", "issuu.com", "pinterest.com",
    "jobstreet.com", "simplyhired.com", "glassdoor.co.uk", "wellfound.com",
}

# Infrastructure the signal points AT: form hosts, link shorteners, mail
# providers, generic TLD landing pages. Never a target company.
INFRA_DOMAINS = {
    "forms.gle", "docs.google.com", "google.com", "drive.google.com",
    "forms.office.com", "office.com", "microsoft.com", "typeform.com",
    "airtable.com", "jotform.com", "surveymonkey.com", "gmail.com",
    "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "bit.ly", "tinyurl.com", "lnkd.in", "wa.me", "whatsapp.com",
    "chat.whatsapp.com", "edu.in", "ac.in", "gov.in", "co.in", "org.in",
    "zoom.us", "calendly.com", "notion.so", "canva.com", "dropbox.com",
}

BLOCKED_AS_COMPANY = AGGREGATOR_DOMAINS | INFRA_DOMAINS | NOT_A_COMPANY

# Words that mark a result as a recruiter or agency rather than a company that
# hires or buys for itself.
AGENCY_MARKERS = re.compile(
    r"\b(staffing|manpower|consultanc|consultants?|recruit(ers?|ment) (agency|services|firm|partners?)"
    r"|placement (agency|consultan|services)|hr solutions|talent solutions|rpo"
    r"|outsourc|hiring partner)\b", re.I)

# Companies that SELL what the client sells. A vendor writes about the problem
# far more than a sufferer does, and outranks real buyers on every keyword.
# The category words come from the client's own description at run time.
VENDOR_SUFFIXES = (r"platform|software|solution|automation|suite|tool|portal"
                   r"|system|saas|app|service provider|vendor")

# Company names that are really page titles.
POST_MARKERS = re.compile(r"('s post\b|\bposted\b|\bshared\b|\bcomments? on\b"
                          r"|\blikes? this\b|\bon linkedin\b)", re.I)

COMPANY_FROM_TITLE = [
    re.compile(r"^(.{2,60}?)\s+hiring\s", re.I),
    re.compile(r"\bat\s+([A-Z][\w&.\- ]{2,50}?)\s*(?:\||-|,|$)"),
    re.compile(r"\bjobs?\s+in\s+([A-Z][\w&.\- ]{2,50}?)\s*(?:\||-|,|$)", re.I),
]

TITLE_NOISE = re.compile(
    r"\b(linkedin|naukri|indeed|glassdoor|jobs?|careers?|hiring|vacanc|apply|india|"
    r"forms?|google|drive|post|profile|page|home|welcome|login|sign ?in|"
    r"bengaluru|bangalore|mumbai|pune|hyderabad|chennai|delhi|noida|gurgaon|remote)\b",
    re.I)


# ------------------------------------------------------------------ arguments

def parse_args(argv):
    args = list(argv[1:])
    opts = {"min_score": DEFAULT_MIN_SCORE, "resolve": False, "limit": 0}
    positional = []

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
        elif arg.startswith("--"):
            sys.exit(f"Unknown option {arg!r}.")
        else:
            positional.append(arg)
        i += 1

    if len(positional) != 1:
        sys.exit(f"Usage: python {Path(__file__).name} <client-domain> "
                 f"[--min-score N] [--limit N] [--resolve]\n"
                 f"Example: python {Path(__file__).name} mngo.in --resolve")
    opts["client"] = positional[0]
    return opts


# --------------------------------------------------------- searches from ICP

def load_icp(client):
    rel = str(client.get("icp_file") or "").strip()
    if not rel:
        die(f"{client['_path']} has no \"icp_file\".")
    path = BASE_DIR / rel
    if not path.exists():
        die(f"No ICP at {path}. Run:  python profile.py {client['_slug']}")
    text = path.read_text(encoding="utf-8").strip()
    if len(text) < 100:
        die(f"{path} is nearly empty. Fill it in before sourcing targets.")
    return text, path


def build_query_prompt(client, icp_text):
    return f"""You write web search queries that find COMPANIES WITH A PROBLEM.

WHO IS SELLING: {client['name']} - {client['one_liner']}
WHAT THEY SELL: {client.get('what_you_sell') or client['one_liner']}
WHO BUYS IT: {client.get('who_buys_it') or 'not stated'}

THEIR ICP, including the buying signals that survived grading:
---
{icp_text}
---

Write up to {MAX_QUERIES} Google search queries that would surface companies
showing those buying signals RIGHT NOW.

Rules:
1. Each query hunts a SIGNAL, not a category. "companies that need X" finds
   nothing. A query that finds the visible evidence of the problem finds real
   companies.
2. Use the exact strings a sufferer would publish - the wording that appears on
   their job post, their careers page, their listing. Quote distinctive phrases.
3. Do NOT write queries that surface companies SELLING this product. Vendors
   publish far more about the problem than sufferers do and will drown the
   results. Avoid words like {VENDOR_SUFFIXES}.
4. Vary the angle across queries: a job board, the company's own site, a review
   site, a press item. Do not write seven versions of one query.
5. Include a geography only if the ICP names one.
6. If a buying signal in the ICP is not findable through a web search, skip it
   rather than writing a query that will return noise.

Reply with JSON only:
{{"queries": ["string", "string"], "note": "one sentence on what these will and will not find"}}"""


def get_queries(api, client, icp_text):
    raw = ask_gemini(api, build_query_prompt(client, icp_text))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        die("The model did not return usable search queries. Try again.")
    queries = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()]
    if not queries:
        die(f"No searchable signal could be built from {client['_slug']}'s ICP.\n"
            f"Usually this means the buying signals are not visible from outside "
            f"a company. Open the ICP and rewrite them, or name targets yourself.")
    return queries[:MAX_QUERIES], str(data.get("note") or "").strip()


def load_excludes(client):
    rel = str(client.get("exclude_file") or "context/exclude.txt").strip()
    path = BASE_DIR / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# One company name per line. Lines starting with # are "
                        "ignored.\n# Companies to never surface as targets.\n",
                        encoding="utf-8")
        print(f"Created {path} - add companies to skip, one per line.")
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line.lower())
    return names


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


def blocked_host(domain):
    """Is this domain a host, an aggregator or infrastructure rather than a company?"""
    if not domain:
        return True
    if domain in BLOCKED_AS_COMPANY:
        return True
    # forms.gle style: the registrable domain is itself a bare public suffix
    if domain.count(".") == 1 and domain.split(".")[0] in (
            "edu", "ac", "gov", "co", "org", "net", "forms", "docs", "drive"):
        return True
    return False


def clean_company(name):
    name = re.sub(r"\s+", " ", name or "").strip(" -|,·:")
    name = re.sub(r"\b(pvt\.?|private|ltd\.?|limited|inc\.?|llp)\b", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" -|,·:")
    if len(name) < 3 or len(name) > 50:
        return ""
    residue = TITLE_NOISE.sub("", name).strip(" -|,·:")
    if len(residue) < 3:
        return ""
    return name


def extract_company(result):
    """
    Work out who the employer is.

    A result hosted on a job board, a form service or a social site is never the
    company - the company is named in the title. Only a company's OWN site lets
    the domain stand in for the name.
    """
    link = result.get("link") or ""
    domain = registrable_domain(link)
    title = result.get("title") or ""

    if POST_MARKERS.search(title):
        return "", "post", ""

    if not blocked_host(domain):
        return clean_company(domain.split(".")[0].replace("-", " ").title()) or "", \
               "own-site", domain

    for pattern in COMPANY_FROM_TITLE:
        match = pattern.search(title)
        if match:
            name = clean_company(match.group(1))
            if name:
                return name, "title", ""
    return "", "unknown", ""


def build_signal_patterns(icp_text):
    """
    Pull the distinctive words out of the ICP's buying-signal rows.

    Crude on purpose: the point is to score a result higher when it echoes the
    client's own signal wording, without needing to understand the industry.
    """
    signals = []
    in_section = False
    for line in icp_text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("## buying signal"):
            in_section = True
            continue
        if in_section and stripped.startswith("##"):
            break
        if in_section and stripped.startswith("|") and "---" not in stripped:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and cells[0] and cells[0].lower() != "signal":
                signals.append(cells[0])
    return signals


def score_result(result, signals):
    """How strongly does this result echo the client's buying signals?"""
    text = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
    score = 0
    evidence = []

    for signal in signals:
        words = [w for w in re.findall(r"[a-z]{4,}", signal.lower())
                 if w not in ("that", "with", "from", "this", "their", "your",
                              "collect", "collects", "would", "where", "outside")]
        if not words:
            continue
        hits = sum(1 for w in set(words) if w in text)
        if hits >= max(2, len(set(words)) // 3):
            score += 4
            evidence.append(signal[:90])

    # A concrete artifact in the snippet is worth more than matching words.
    if re.search(r"forms\.gle|docs\.google\.com/forms|google form|spreadsheet", text):
        score += 3
        evidence.append("points at a form or spreadsheet")
    if re.search(r"[\w.+-]+@(gmail|yahoo|outlook|hotmail|rediffmail)\.", text):
        score += 3
        evidence.append("points at a personal email address")

    return min(score, 11), evidence


def classify_reject(company, text, excludes, vendor_pattern):
    if not company:
        return "not a company - a host, a form link or a social post"
    if vendor_pattern and vendor_pattern.search(text):
        return "sells this kind of product (competitor, not a buyer)"
    if AGENCY_MARKERS.search(text):
        return "staffing or recruitment agency (hires on behalf of others)"
    for name in excludes:
        if name in company.lower():
            return f"on the exclude list ({name})"
    return ""


def build_vendor_pattern(client):
    """
    Catch companies selling what this client sells.

    Built from the client's own words plus generic product nouns, so it adapts:
    a placement-software client filters placement platforms, a logistics client
    filters logistics platforms.
    """
    words = re.findall(r"[a-z]{5,}",
                       f"{client.get('what_you_sell', '')} "
                       f"{client.get('one_liner', '')}".lower())
    stop = {"their", "which", "there", "these", "those", "other", "provides",
            "company", "companies", "system", "manage", "management", "using",
            "central", "replaces", "dedicated", "single", "across", "about"}
    keywords = sorted({w for w in words if w not in stop}, key=len, reverse=True)[:8]
    if not keywords:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b"
                      r"[^.]{0,40}\b(" + VENDOR_SUFFIXES + r")\b", re.I)


# --------------------------------------------------------------------- output

def collect(api_key, queries, signals, excludes, vendor_pattern, limit):
    found, rejected, reasons, seen_results = {}, [], Counter(), 0

    for query in queries:
        print(f"searching: {query}")
        results = serper_search(api_key, query)
        seen_results += len(results)
        print(f"  {len(results)} result(s)")

        for result in results:
            company, source, own_domain = extract_company(result)
            text = f"{company} {result.get('title', '')} {result.get('snippet', '')}"

            reason = classify_reject(company, text, excludes, vendor_pattern)
            if reason:
                reasons[reason.split(" (")[0]] += 1
                rejected.append({"company": company or "(unknown)", "reason": reason,
                                 "title": result.get("title", ""),
                                 "link": result.get("link", ""), "query": query})
                continue

            score, evidence = score_result(result, signals)
            if score == 0:
                reasons["no signal in the snippet"] += 1
                rejected.append({"company": company,
                                 "reason": "no signal found in the title or snippet",
                                 "title": result.get("title", ""),
                                 "link": result.get("link", ""), "query": query})
                continue

            key = company.lower()
            existing = found.get(key)
            if existing and existing["score"] >= score:
                existing["hits"] += 1
                continue

            found[key] = {
                "company": company, "domain": own_domain, "score": score,
                "name_from": source, "evidence": evidence,
                "title": result.get("title", ""), "snippet": result.get("snippet", ""),
                "link": result.get("link", ""), "date": result.get("date", ""),
                "query": query, "hits": (existing["hits"] if existing else 0) + 1,
            }
        time.sleep(REQUEST_PAUSE_SECONDS)

    candidates = sorted(found.values(),
                        key=lambda c: (-c["score"], -c["hits"], c["company"]))
    if limit:
        candidates = candidates[:limit]
    return candidates, rejected, reasons, seen_results


def resolve_domain(api_key, company):
    results = serper_search(api_key, f"{company} official website", num=5)
    for result in results:
        domain = registrable_domain(result.get("link") or "")
        if not blocked_host(domain):
            return domain, result.get("link") or ""
    return "", ""


def to_markdown(client, candidates, min_score, queries, note):
    lines = [f"# Target candidates for {client['name']}", "",
             f"_{len(candidates)} candidate(s) at or above score {min_score}._", "",
             "> Sourced from the buying signals in this client's ICP. A high score "
             "means the signal is present, not that the company fits the ICP. Run "
             "check_fit.py before researching one.", ""]
    if note:
        lines += [f"_{note}_", ""]
    lines += ["## Searches used", ""] + [f"- `{q}`" for q in queries] + [""]

    for i, c in enumerate(candidates, start=1):
        lines += [f"## {i}. {c['company']}  ·  {c['score']}/11",
                  f"- Domain: {c['domain'] or '_not resolved_'}",
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
    lines = ["# Rejected results", "",
             f"_{len(rejected)} of {seen_results} result(s) thrown away._", "",
             "## Why", ""]
    for reason, count in reasons.most_common():
        share = (count / seen_results * 100) if seen_results else 0
        lines.append(f"- {reason}: {count} ({share:.0f}%)")
    lines += ["",
              "> If 'sells this kind of product' is the biggest bucket, the "
              "queries are finding vendors rather than sufferers. If 'not a "
              "company' is, the signal points at infrastructure - a form host, a "
              "social post - and the query needs to name the employer's context "
              "instead.", "",
              "## What was thrown away", ""]
    for r in rejected:
        lines += [f"- **{r['company']}** — {r['reason']}",
                  f"  - {r['title']}", f"  - {r['link']}"]
    return "\n".join(lines)


def save(client, candidates, rejected, reasons, seen_results, min_score,
         resolved, queries, note):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = client["_slug"]
    payload = {
        "client": client["name"], "client_domain": slug,
        "min_score": min_score, "resolved": resolved,
        "queries": queries, "note": note,
        "results_seen": seen_results, "rejected_count": len(rejected),
        "rejection_reasons": dict(reasons), "candidates": candidates,
    }
    json_path = OUT_DIR / f"candidates-{slug}.json"
    md_path = OUT_DIR / f"candidates-{slug}.md"
    rejected_path = OUT_DIR / f"rejected-{slug}.md"
    try:
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        md_path.write_text(to_markdown(client, candidates, min_score, queries, note),
                           encoding="utf-8")
        rejected_path.write_text(rejected_markdown(rejected, reasons, seen_results),
                                 encoding="utf-8")
    except OSError as exc:
        die(f"Could not write to {OUT_DIR}: {exc}")
    return json_path, md_path, rejected_path


# ------------------------------------------------------------------------ main

def main():
    opts = parse_args(sys.argv)

    serper_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not serper_key:
        die("SERPER_API_KEY is not set. Put it in your .env next to this script.")
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        die("GEMINI_API_KEY is not set. Put it in your .env next to this script.")

    client = load_client(opts["client"])
    icp_text, icp_path = load_icp(client)
    excludes = load_excludes(client)

    print(f"Client: {client['name']}  ·  ICP: {icp_path.name}")
    print(f"{len(excludes)} name(s) excluded\n")
    print(f"Asking {MODEL} to turn the ICP's buying signals into searches...")

    api = genai.Client(api_key=gemini_key)
    queries, note = get_queries(api, client, icp_text)
    if note:
        print(f"  {note}")
    print()

    signals = build_signal_patterns(icp_text)
    vendor_pattern = build_vendor_pattern(client)

    candidates, rejected, reasons, seen_results = collect(
        serper_key, queries, signals, excludes, vendor_pattern, opts["limit"])
    kept = [c for c in candidates if c["score"] >= opts["min_score"]]

    if opts["resolve"]:
        print("\nresolving domains...")
        for c in kept:
            if c["domain"]:
                continue
            domain, source = resolve_domain(serper_key, c["company"])
            c["domain"] = domain
            c["domain_source"] = source
            print(f"  {c['company']}: {domain or 'not found'}")
            time.sleep(REQUEST_PAUSE_SECONDS)

    json_path, md_path, rejected_path = save(
        client, kept, rejected, reasons, seen_results, opts["min_score"],
        opts["resolve"], queries, note)

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
    print(f"Next:  python check_fit.py {client['_slug']} <their-domain>")


if __name__ == "__main__":
    main()