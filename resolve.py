"""
resolve.py - turn a company NAME into a domain.

Every other script wants a domain, because a domain is what you scrape and what
you name a file after. People think in names. This bridges the two:

    from resolve import to_domain
    domain = to_domain("Happiest Minds")     -> "happiestminds.com"
    domain = to_domain("happiestminds.com")  -> "happiestminds.com"

Anything that already looks like a domain is passed straight through, so this
costs nothing when you type one.

Results are cached in context/domains.json. A name resolved once is free
afterwards, which matters because this runs at the top of several scripts and a
Serper search is not free.

It prints what it matched. A name lookup is a guess - "Apex Solutions" could be
any of forty companies - and a guess you cannot see is a guess you cannot catch.
Run it directly to check one:

    python resolve.py "Happiest Minds"
"""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "context" / "domains.json"

SERPER_URL = "https://google.serper.dev/search"

# Sites that will never be the company you meant.
NOT_A_COMPANY = {
    "linkedin.com", "wikipedia.org", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "crunchbase.com", "glassdoor.com",
    "glassdoor.co.in", "ambitionbox.com", "indeed.com", "indeed.co.in",
    "naukri.com", "zaubacorp.com", "tofler.in", "bloomberg.com", "reuters.com",
    "economictimes.indiatimes.com", "moneycontrol.com", "tracxn.com",
    "pitchbook.com", "owler.com", "zoominfo.com", "rocketreach.co",
    "apollo.io", "medium.com", "reddit.com", "quora.com", "github.com",
}

# Looks like a domain already: something.something, no spaces.
DOMAIN_SHAPE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.I)


def _clean(text):
    text = str(text or "").strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("www."):
        text = text[4:]
    return text.split("/")[0].strip()


def looks_like_domain(text):
    return bool(DOMAIN_SHAPE.match(_clean(text)))


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


def _load_cache():
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    except OSError:
        pass  # a cache that will not save is not worth stopping for


def _search(name, api_key):
    try:
        response = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": f"{name} official website", "num": 8},
            timeout=30,
        )
    except requests.RequestException:
        return {}
    if response.status_code != 200:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def to_domain(text, quiet=False):
    """
    Return a domain for whatever the user typed.

    Returns "" when a name cannot be resolved, rather than guessing a domain
    from the name. A constructed domain wastes a scrape and poisons every step
    downstream, so a clean failure is worth more than a plausible invention.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""

    if looks_like_domain(raw):
        return _clean(raw)

    key = raw.lower()
    cache = _load_cache()
    if key in cache:
        if not quiet:
            print(f'"{raw}" -> {cache[key]}  (remembered)')
        return cache[key]

    api_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not api_key:
        if not quiet:
            print(f'Cannot look up "{raw}" - SERPER_API_KEY is not set. '
                  f'Type the domain instead.')
        return ""

    data = _search(raw, api_key)

    # The knowledge panel is the most reliable answer when Google has one.
    kg = data.get("knowledgeGraph") or {}
    candidate = registrable_domain(kg.get("website") or "")
    matched_title = kg.get("title") or ""

    if not candidate or candidate in NOT_A_COMPANY:
        candidate = ""
        for result in data.get("organic") or []:
            domain = registrable_domain(result.get("link") or "")
            if domain and domain not in NOT_A_COMPANY:
                candidate = domain
                matched_title = result.get("title") or matched_title
                break

    if not candidate:
        if not quiet:
            print(f'Could not work out a domain for "{raw}". '
                  f'Type it in directly, like  example.com')
        return ""

    cache[key] = candidate
    _save_cache(cache)

    if not quiet:
        label = f" ({matched_title.split('|')[0].strip()})" if matched_title else ""
        print(f'"{raw}" -> {candidate}{label}')
        print("  If that is the wrong company, pass the domain instead.")
    return candidate


def main():
    if len(sys.argv) < 2:
        sys.exit(f'Usage: python {Path(__file__).name} "Company Name"')
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    domain = to_domain(" ".join(sys.argv[1:]))
    sys.exit(0 if domain else 1)


if __name__ == "__main__":
    main()