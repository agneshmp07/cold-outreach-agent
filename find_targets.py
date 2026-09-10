"""
find_targets.py - sourcing, by working down the value chain.

    python find_targets.py <client-domain>
    python find_targets.py mngo.in --limit 20

Two stages, because that is how a person would do it:

  1. WHO BUYS FROM THIS COMPANY?  Not a signal, a sector. A sugarcane wholesaler
     sells to sugar mills. A placement platform sells to companies that hire
     from campuses. A coworking operator sells to firms opening a new city
     office. This comes from the client's ICP plus what they sell.

  2. WHO IS ACTUALLY IN THAT SECTOR?  Industry lists, directories, trade press
     and recent news name real companies. Those names are the candidates.

The previous version searched for a buying signal in the text of search results
and threw away everything that did not echo it. That works when the signal is
publicly visible - a job post exposing a Google Form - and finds nothing at all
when it is not, which is most industries. Naming the sector first and then
finding its companies works either way.

Nothing here decides whether a company is a good prospect. check_fit.py does
that, one company at a time, against the ICP.

Output:
    targets/candidates-<client>.json
    targets/candidates-<client>.md
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from generate import load_client
from resolve import NOT_A_COMPANY
from triggers import MODEL, ask_gemini, die, genai

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "targets"

SERPER_URL = "https://google.serper.dev/search"
SERPER_NEWS_URL = "https://google.serper.dev/news"
RESULTS_PER_QUERY = 10
REQUEST_PAUSE_SECONDS = 1.0
MAX_SECTORS = 4
QUERIES_PER_SECTOR = 3
DEFAULT_LIMIT = 20

# Never a candidate: directories, aggregators, social, infrastructure.
NOT_A_TARGET = NOT_A_COMPANY | {
    "indiamart.com", "tradeindia.com", "exportersindia.com", "justdial.com",
    "sulekha.com", "yellowpages.in", "zaubacorp.com", "tofler.in",
    "thecompanycheck.com", "instafinancials.com", "linkedin.com", "forms.gle",
    "docs.google.com", "scribd.com", "slideshare.net", "issuu.com",
    "researchgate.net", "statista.com", "ibef.org", "wikipedia.org",
}

SECTOR_SCHEMA = """{
  "sectors": [
    {"sector": "string", "why_they_buy": "string", "example_of_a_buyer": "string"}
  ]
}"""

EXTRACT_SCHEMA = """{
  "companies": [
    {"name": "string", "sector": "string", "why": "string", "seen_in": "string"}
  ]
}"""


# ------------------------------------------------------------------ arguments

def parse_args(argv):
    args = list(argv[1:])
    opts = {"limit": DEFAULT_LIMIT, "resolve": True}
    positional = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--no-resolve":
            opts["resolve"] = False
        elif arg == "--resolve":
            opts["resolve"] = True
        elif arg == "--limit":
            if i + 1 >= len(args):
                sys.exit("--limit needs a number after it.")
            try:
                opts["limit"] = int(args[i + 1])
            except ValueError:
                sys.exit(f"--limit needs a number, got {args[i + 1]!r}.")
            i += 1
        elif arg.startswith("--"):
            sys.exit(f"Unknown option {arg!r}.")
        else:
            positional.append(arg)
        i += 1

    if len(positional) != 1:
        sys.exit(f"Usage: python {Path(__file__).name} <client-domain> "
                 f"[--limit N] [--no-resolve]")
    opts["client"] = positional[0]
    return opts


def load_icp(client):
    rel = str(client.get("icp_file") or "").strip()
    path = BASE_DIR / rel if rel else None
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def load_excludes(client):
    rel = str(client.get("exclude_file") or "context/exclude.txt").strip()
    path = BASE_DIR / rel
    if not path.exists():
        return []
    return [line.strip().lower()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


# --------------------------------------------------- stage 1: who buys this

def build_sector_prompt(client, icp_text):
    return f"""Work out which kinds of company would BUY from the business below,
then say how to find real examples of them.

THE SELLER: {client['name']} - {client['one_liner']}
WHAT THEY SELL: {client.get('what_you_sell') or client['one_liner']}
WHO PAYS: {client.get('who_pays_for_it') or client.get('who_buys_it') or 'not stated'}

THEIR ICP:
---
{icp_text or "not written yet - work it out from what they sell"}
---

Think down the supply chain, not sideways. A sugarcane wholesaler's customers
are sugar mills and jaggery producers, not other wholesalers. A packaging
printer's customers are the brands that need boxes. A campus placement platform's
customers are the companies that hire graduates.

Give at most {MAX_SECTORS} sectors, most promising first. For each:
- "sector": the industry, named the way an industry list would name it. Be
  concrete: "sugar mills and distilleries", not "food and beverage".
- "why_they_buy": one sentence on what makes a company in this sector need what
  the seller offers.
- "example_of_a_buyer": the kind of company inside that sector that would need
  it most - a size, a stage, a situation.

Do not list the seller's own competitors. Do not list consultancies, agencies or
software vendors serving the sector unless they are genuinely the buyer.

Reply with JSON only:
{SECTOR_SCHEMA}"""


def get_sectors(api, client, icp_text):
    raw = ask_gemini(api, build_sector_prompt(client, icp_text))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        die("Could not work out who buys from this company. Try again.")
    sectors = [s for s in (data.get("sectors") or []) if s.get("sector")]
    if not sectors:
        die(f"Could not work out which sectors buy from {client['name']}.\n"
            f"Usually this means the ICP is too vague. Open "
            f"{client.get('icp_file')} and make the segments concrete.")
    return sectors[:MAX_SECTORS]


# ------------------------------------------------ stage 2: name the companies

def search(api_key, query, news=False, num=RESULTS_PER_QUERY):
    url = SERPER_NEWS_URL if news else SERPER_URL
    try:
        response = requests.post(
            url,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"  ! search failed ({type(exc).__name__})")
        return []
    if response.status_code == 403:
        die("Serper rejected the key (403). Check SERPER_API_KEY in your .env.")
    if response.status_code != 200:
        print(f"  ! Serper returned {response.status_code}")
        return []
    try:
        body = response.json()
    except ValueError:
        return []
    return body.get("news" if news else "organic") or []


def sector_queries(sector):
    """
    Three angles on the same sector.

    A directory list names many companies at once. A news search names the ones
    doing something right now, which is also where a trigger would come from.
    A regional cut catches the mid-sized firms the national lists leave out.
    """
    name = sector["sector"]
    return [
        (f"list of {name} companies India", False),
        (f"top {name} companies India", False),
        (f"{name} India expansion OR investment OR new plant", True),
    ][:QUERIES_PER_SECTOR]


def build_extract_prompt(client, results_block):
    return f"""Pull real COMPANY NAMES out of the search results below.

You are building a prospect list for {client['name']}, which sells:
{client.get('what_you_sell') or client['one_liner']}

Rules:
1. Only companies that are named in the results. Do not add companies you happen
   to know. If a result is a directory page listing twelve companies and the
   snippet names four of them, take those four.
2. Skip anything that is not an operating company: directories, marketplaces,
   news outlets, government bodies, industry associations, research firms,
   consultancies and software vendors serving the sector.
3. Skip {client['name']} itself and any company that sells the same thing.
4. Give the company's name as it would appear on its own website. No taglines,
   no "Ltd" unless it is part of the name, no city suffixes.
5. "why" is one short line on why this company plausibly buys what is sold - the
   sector it is in and what it does. Do not invent facts about it.
6. "seen_in" is the exact title of the result you took it from, so a human can
   check.
7. At most 25 companies. Prefer ones that appear in more than one result.

Reply with JSON only:
{EXTRACT_SCHEMA}

--- SEARCH RESULTS ---
{results_block}
--- END SEARCH RESULTS ---"""


def gather(api_key, sectors):
    blocks, seen_links = [], set()
    for sector in sectors:
        print(f"\nsector: {sector['sector']}")
        print(f"  ({sector.get('why_they_buy', '')})")
        for query, is_news in sector_queries(sector):
            kind = "news" if is_news else "web"
            print(f"  searching [{kind}]: {query}")
            results = search(api_key, query, news=is_news)
            print(f"    {len(results)} result(s)")
            for r in results:
                link = r.get("link") or ""
                if link in seen_links:
                    continue
                seen_links.add(link)
                blocks.append(
                    f"[sector: {sector['sector']}] {r.get('title', '')}\n"
                    f"  {r.get('snippet', '')}\n  {link}"
                    + (f"\n  ({r.get('date', '')})" if r.get("date") else ""))
            time.sleep(REQUEST_PAUSE_SECONDS)
    return "\n\n".join(blocks)


def extract_companies(api, client, results_block, excludes):
    if not results_block.strip():
        return []
    raw = ask_gemini(api, build_extract_prompt(client, results_block))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("  ! could not read the company list back")
        return []

    out, seen = [], set()
    client_name = str(client.get("name", "")).lower()
    for item in data.get("companies") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or len(name) < 3 or len(name) > 60:
            continue
        key = name.lower()
        if key in seen or key == client_name:
            continue
        if any(x in key for x in excludes):
            continue
        seen.add(key)
        out.append({
            "company": name,
            "sector": str(item.get("sector") or "").strip(),
            "why": str(item.get("why") or "").strip(),
            "seen_in": str(item.get("seen_in") or "").strip(),
            "domain": "",
        })
    return out


# -------------------------------------------------------------- domain lookup

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


def resolve_domain(api_key, company):
    for result in search(api_key, f"{company} official website", num=5):
        domain = registrable_domain(result.get("link") or "")
        if domain and domain not in NOT_A_TARGET:
            return domain
    return ""


# --------------------------------------------------------------------- output

def to_markdown(client, sectors, candidates):
    lines = [f"# Prospects for {client['name']}", "",
             f"_{len(candidates)} company(ies) found._", "",
             "> Named from industry lists and recent news in the sectors that "
             "buy from this company. Being on this list means the company is in "
             "a buying sector, not that it is a good prospect - run check_fit.py "
             "on any you like.", "",
             "## Who buys from this company", ""]
    for s in sectors:
        lines += [f"**{s['sector']}** — {s.get('why_they_buy', '')}"]
        if s.get("example_of_a_buyer"):
            lines.append(f"  Best fit: {s['example_of_a_buyer']}")
        lines.append("")

    lines += ["## Companies", ""]
    for i, c in enumerate(candidates, start=1):
        lines += [f"### {i}. {c['company']}",
                  f"- Sector: {c.get('sector', '-')}",
                  f"- Domain: {c.get('domain') or '_not resolved_'}",
                  f"- Why: {c.get('why', '-')}"]
        if c.get("seen_in"):
            lines.append(f"- Found in: {c['seen_in']}")
        lines.append("")
    return "\n".join(lines)


def save(client, sectors, candidates):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = client["_slug"]
    payload = {"client": client["name"], "client_domain": slug,
               "method": "sector", "sectors": sectors, "candidates": candidates}
    json_path = OUT_DIR / f"candidates-{slug}.json"
    md_path = OUT_DIR / f"candidates-{slug}.md"
    try:
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        md_path.write_text(to_markdown(client, sectors, candidates),
                           encoding="utf-8")
    except OSError as exc:
        die(f"Could not write to {OUT_DIR}: {exc}")
    return json_path, md_path


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
    icp_text = load_icp(client)
    excludes = load_excludes(client)
    api = genai.Client(api_key=gemini_key)

    print(f"Client: {client['name']}")
    print(f"Asking {MODEL} which sectors buy from them...\n")
    sectors = get_sectors(api, client, icp_text)

    print(f"{client['name']} sells to:")
    for s in sectors:
        print(f"  - {s['sector']}")
        print(f"    {s.get('why_they_buy', '')}")

    results_block = gather(serper_key, sectors)
    if not results_block.strip():
        die("The searches returned nothing at all. Check SERPER_API_KEY.")

    print(f"\nAsking {MODEL} to pull the company names out...")
    candidates = extract_companies(api, client, results_block, excludes)
    candidates = candidates[:opts["limit"]]

    if not candidates:
        print("\nNo company names could be pulled from the results.")
        print("The sectors may be too broad, or the searches returned only "
              "directories. Try naming a company yourself instead.")
        save(client, sectors, [])
        sys.exit(1)

    if opts["resolve"]:
        print(f"\nfinding websites for {len(candidates)} company(ies)...")
        for c in candidates:
            c["domain"] = resolve_domain(serper_key, c["company"])
            print(f"  {c['company']}: {c['domain'] or 'not found'}")
            time.sleep(REQUEST_PAUSE_SECONDS)

    json_path, md_path = save(client, sectors, candidates)

    print(f"\n{len(candidates)} company(ies) found\n")
    for c in candidates:
        print(f"  {c['company']:<38} {c.get('domain') or '-'}")
        print(f"    {c.get('sector', '')}")

    print(f"\nSaved to {json_path}")
    print(f"        {md_path}")
    print("\nBeing on this list means the company is in a buying sector, not "
          "that it is a good prospect.")
    print(f"Next:  python check_fit.py {client['_slug']} <their-domain>")


if __name__ == "__main__":
    main()