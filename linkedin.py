"""
linkedin.py - who to send the message to.

Uses HarvestAPI's LinkedIn Profile Search actor, which scrapes without a
session cookie - no LinkedIn account of yours is involved.

An earlier version used their company-employees actor. It had no title filter,
so it sampled employees blind: 25 profiles from Razorpay's 3,355 returned zero
recruiters. This one searches by title within a company, which is the
difference between a lottery and a lookup.

Cost: $0.10 per search page (up to 25 profiles).

Usage:
    python linkedin.py razorpay.com
    python linkedin.py razorpay.com --force
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
TRIGGERS_DIR = BASE_DIR / "triggers"
OUT_DIR = BASE_DIR / "linkedin"

ACTOR_ID = "harvestapi~linkedin-profile-search"
SEARCH_QUERY = "talent acquisition recruiter hiring"
MAX_PROFILES = 10


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def clean_domain(arg):
    d = arg.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.split("/")[0].strip()


def company_name(domain):
    """
    The name to filter on.

    triggers.py already asked Gemini for the company's real name, so reuse it -
    "Razorpay" matches better than "razorpay.com".
    """
    trig = TRIGGERS_DIR / f"{domain}.json"
    if trig.exists():
        try:
            name = json.loads(trig.read_text(encoding="utf-8")).get("company_name")
            if name:
                return name
        except Exception:
            pass
    return domain.split(".")[0].capitalize()


def run_actor(token, company):
    """Run synchronously and get the dataset back in one call."""
    url = (f"https://api.apify.com/v2/acts/{ACTOR_ID}"
           f"/run-sync-get-dataset-items?token={token}")
    payload = {
        "profileScraperMode": "Short",
        "searchQuery": SEARCH_QUERY,
        "maxItems": MAX_PROFILES,
        "currentCompanies": [company],
    }
    try:
        r = requests.post(url, json=payload, timeout=300)
    except Exception as exc:
        die(f"Apify request failed [{type(exc).__name__}]: {exc}")

    if r.status_code == 401:
        die("Apify rejected the token. Check APIFY_TOKEN in your .env file.")
    if r.status_code == 400:
        die(f"Apify rejected the input: {r.text[:400]}")
    if r.status_code == 403:
        die(f"Apify refused the run - the actor may need renting: {r.text[:300]}")
    if r.status_code not in (200, 201):
        die(f"Apify returned {r.status_code}: {r.text[:300]}")

    return r.json()


def tidy(items, company):
    """
    Name, title, tenure, URL. Nothing else.

    The actor also returns each person's self-written summary - often several
    hundred words about their career. None of it helps route a business
    message, and storing it would make this a dossier rather than a contact
    list.
    """
    people = []
    for p in items:
        name = " ".join(filter(None, [p.get("firstName"),
                                      p.get("lastName")])).strip()
        if not name:
            continue

        role, years = "", None
        for pos in p.get("currentPositions") or []:
            if (pos.get("companyName") or "").lower() == company.lower():
                role = (pos.get("title") or "").strip()
                tenure = pos.get("tenureAtCompany") or {}
                years = tenure.get("numYears")
                break
        if not role:
            continue

        people.append({
            "name": name,
            "role": role,
            "years_at_company": years,
            "linkedin_url": (p.get("linkedinUrl") or "").strip(),
            "location": ((p.get("location") or {}).get("linkedinText") or "").strip(),
        })

    # Longest tenure first: they know the org and are usually more senior.
    people.sort(key=lambda x: x["years_at_company"] or 0, reverse=True)
    return people


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    if len(args) != 1:
        sys.exit(f"Usage: python {Path(__file__).name} <domain> [--force]")

    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        die("APIFY_TOKEN is not set. Get one at "
            "https://console.apify.com/account/integrations")

    domain = clean_domain(args[0])
    company = company_name(domain)
    out_path = OUT_DIR / f"{domain}.json"

    if out_path.exists() and not force:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        print(f"Already have contacts for {domain} (nothing spent).")
        for p in existing.get("people", []):
            print(f"  {p['name']} - {p['role']}")
        print("\nRe-run with --force to search again.")
        sys.exit(0)

    print(f"\nSearching LinkedIn: \"{SEARCH_QUERY}\" at {company}")

    items = run_actor(token, company)
    total = 0
    if items:
        total = ((items[0].get("_meta") or {}).get("pagination") or {}).get(
            "totalElements", 0)
    people = tidy(items, company)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"domain": domain, "company": company,
                    "query": SEARCH_QUERY, "total_matching": total,
                    "people": people}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    if not people:
        print(f"\n  No one matching \"{SEARCH_QUERY}\" at {company}. Either the "
              f"company name is wrong or they have no in-house recruiters - "
              f"which is itself a signal.")
    else:
        print(f"\n  {len(people)} of {total} matching profiles:\n")
        for p in people:
            yrs = f" · {p['years_at_company']}y" if p["years_at_company"] else ""
            print(f"  {p['name']} - {p['role']}{yrs}")
            print(f"    {p['location']}")
            print(f"    {p['linkedin_url']}\n")

    print(f"Saved to {out_path}")


if __name__ == "__main__":
    main()