"""
linkedin.py - optional stage. Finds who to contact at a company.

Uses HarvestAPI's Apify actor, which scrapes without a session cookie, so no
LinkedIn account of yours is involved.

Cost: roughly $3 per 1,000 basic profiles.

The actor returns enormous records - recommendations, education, endorsements,
personal websites. We keep four fields. The rest is personal data we have no
business storing to route a business message.

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
OUT_DIR = BASE_DIR / "linkedin"

ACTOR_ID = "harvestapi~linkedin-company-employees"
MAX_PROFILES = 25

# Roles that would own campus hiring. Matched against the person's current
# position at THIS company, not their headline - headlines are self-written
# marketing and match everything.
WANTED = [
    "talent", "recruit", "hiring", "campus", "university",
    "human resources", "hr ", "people ", "people operations",
]

# Anything with these in the position is not an employee for our purposes.
EXCLUDE = ["partner", "consultant", "advisor", "freelance", "investor"]


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def clean_domain(arg):
    d = arg.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.split("/")[0].strip()


def run_actor(token, company_url):
    """Run synchronously and get the dataset back in one call."""
    url = (f"https://api.apify.com/v2/acts/{ACTOR_ID}"
           f"/run-sync-get-dataset-items?token={token}")
    payload = {
        "companies": [company_url],
        "maxItems": MAX_PROFILES,
        "profileScraperMode": "Short ($4 per 1k)",
    }
    try:
        r = requests.post(url, json=payload, timeout=300)
    except Exception as exc:
        die(f"Apify request failed [{type(exc).__name__}]: {exc}")

    if r.status_code == 401:
        die("Apify rejected the token. Check APIFY_TOKEN in your .env file.")
    if r.status_code == 403:
        die(f"Apify refused the run - the actor may need renting: {r.text[:300]}")
    if r.status_code not in (200, 201):
        die(f"Apify returned {r.status_code}: {r.text[:300]}")

    return r.json()


def current_role_at(person, company_slug):
    """
    Their job title AT THIS COMPANY.

    A person's headline is self-written and often lists five companies. The
    currentPosition array says what they actually do where.
    """
    for pos in person.get("currentPosition") or []:
        universal = (pos.get("companyUniversalName") or "").lower()
        if universal == company_slug:
            return (pos.get("position") or "").strip()
    return ""


def relevant(role):
    r = f" {role.lower()} "
    if any(x in r for x in EXCLUDE):
        return False
    return any(w in r for w in WANTED)


def tidy(items, company_slug):
    """
    Four fields. Nothing else.

    The actor hands back education, endorsements, recommendations and personal
    sites. None of that helps route a business message, and storing it would
    make this a dossier rather than a contact list.
    """
    people = []
    for p in items:
        role = current_role_at(p, company_slug)
        if not role or not relevant(role):
            continue
        name = " ".join(filter(None, [p.get("firstName"), p.get("lastName")])).strip()
        if not name:
            continue
        people.append({
            "name": name,
            "role": role,
            "linkedin_url": (p.get("linkedinUrl") or "").strip(),
            "location": ((p.get("location") or {}).get("parsed") or {}).get("text", ""),
        })
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
    slug = domain.split(".")[0]
    company_url = f"https://www.linkedin.com/company/{slug}"
    out_path = OUT_DIR / f"{domain}.json"

    if out_path.exists() and not force:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        print(f"Already have LinkedIn data for {domain} (nothing spent).")
        for p in existing.get("people", []):
            print(f"  {p['name']} - {p['role']}")
        print("\nRe-run with --force to query again.")
        sys.exit(0)

    print(f"\nScraping {company_url}")
    print(f"Up to {MAX_PROFILES} profiles, filtering for hiring roles...")

    items = run_actor(token, company_url)
    people = tidy(items, slug)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"domain": domain, "source": "linkedin",
                    "scanned": len(items), "people": people},
                   indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\nScanned {len(items)} profile(s), kept {len(people)}.")
    if not people:
        print("  No one in a hiring role surfaced. LinkedIn returns whoever "
              "listed this company, in no useful order - a bigger MAX_PROFILES "
              "or a different search would be needed.")
    else:
        for p in people:
            print(f"\n  {p['name']}")
            print(f"    {p['role']}")
            print(f"    {p['linkedin_url']}")

    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()