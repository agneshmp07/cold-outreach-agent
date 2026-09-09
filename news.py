"""
news.py - optional stage between scrape.py and triggers.py.

A company's own site goes stale. News does not. This pulls recent headlines
about the company and saves them alongside the scraped pages, so triggers.py
has dated, externally-verifiable facts to work with instead of only marketing
copy.

Usage:
    python news.py razorpay.com
"""

import json
import os
import sys
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUT_DIR = BASE_DIR / "news"

# Terms that surface hiring-relevant events rather than product PR.
QUERY_SUFFIX = "hiring OR funding OR expansion OR launch OR appoints"
MAX_ARTICLES = 6


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def company_name(domain):
    """Use the name triggers.py found if available, else the bare domain."""
    trig = BASE_DIR / "triggers" / f"{domain}.json"
    if trig.exists():
        try:
            return json.loads(trig.read_text(encoding="utf-8")).get(
                "company_name") or domain
        except Exception:
            pass
    return domain.split(".")[0]


def fetch_news(name, api_key):
    """Query Serper's news endpoint. Returns [] on any failure - news is optional."""
    try:
        r = requests.post(
            "https://google.serper.dev/news",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": f'"{name}" {QUERY_SUFFIX}', "num": MAX_ARTICLES, "gl": "in"},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"  Serper returned {r.status_code}: {r.text[:200]}")
            return []
        return r.json().get("news", [])
    except Exception as exc:
        print(f"  news fetch failed [{type(exc).__name__}]: {exc}")
        return []


def main():
    if len(sys.argv) != 2:
        sys.exit(f"Usage: python {Path(__file__).name} <domain>")

    api_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not api_key:
        die("SERPER_API_KEY is not set. Get a free key at https://serper.dev "
            "and put it in your .env file.")

    domain = sys.argv[1].strip().lower()
    for prefix in ("https://", "http://", "www."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.split("/")[0]

    name = company_name(domain)
    print(f"\nSearching news for {name}")

    articles = fetch_news(name, api_key)

    cleaned = []
    for a in articles:
        title = str(a.get("title") or "").strip()
        link = str(a.get("link") or "").strip()
        if not title or not link:
            continue
        cleaned.append({
            "title": title,
            "snippet": str(a.get("snippet") or "").strip(),
            "date": str(a.get("date") or "").strip(),
            "source": str(a.get("source") or "").strip(),
            "link": link,
        })

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{domain}.json"
    out_path.write_text(
        json.dumps({"domain": domain, "company_name": name,
                    "articles": cleaned}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    if not cleaned:
        print("  No news found.")
    else:
        for a in cleaned:
            print(f"\n  {a['title']}")
            print(f"    {a['source']} · {a['date']}")
            print(f"    {a['link']}")

    print(f"\nSaved {len(cleaned)} article(s) -> {out_path}")


if __name__ == "__main__":
    main()