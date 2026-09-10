"""
find_contacts.py - who to send it to.

    python find_contacts.py <client-domain> <target-domain>
    python find_contacts.py mngo.in happiestminds.com

The pipeline writes a good email and then leaves you hunting LinkedIn by hand for
someone to send it to. That is the slowest step in the loop. This narrows it.

How it works:
  1. Work out which JOB TITLES would care, from what the client sells. A
     coworking operator wants Heads of Real Estate; a placement platform wants
     campus recruiters. Titles come from the client profile, not a hardcoded list.
  2. Search public LinkedIn profile pages for those titles at that company, and
     read the names out of the result titles.
  3. Look for the company's email pattern in public sources - not to fabricate
     an address, but to tell you what shape theirs take.

What it will not do: guess someone's email address and present it as fact. It
reports the pattern it saw and where it saw it. Constructing an address from a
pattern is your call, and a bounced first email costs more than the minute it
takes to check.

Output:
    contacts/<target-domain>.json
    contacts/<target-domain>.md
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

from generate import load_client
from resolve import to_domain
from triggers import MODEL, ask_gemini, die, genai

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "contacts"

SERPER_URL = "https://google.serper.dev/search"
RESULTS_PER_QUERY = 10
REQUEST_PAUSE_SECONDS = 1.0
MAX_TITLES = 5

TITLES_SCHEMA = """{
  "titles": ["string"],
  "why": "string"
}"""

# Google renders LinkedIn results several ways, and matching only one shape
# found nobody at all:
#   "Priya Sharma - Head of Talent Acquisition - Acme | LinkedIn"
#   "Priya Sharma – Acme Ltd | LinkedIn"
#   "Priya Sharma on LinkedIn: we're hiring..."
# So the name is taken from whichever part looks like a name, and the role from
# whatever follows. The profile URL is the fallback when the title is unusable.
NAME_SHAPE = re.compile(r"^[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){1,3}$")

# Capitalised words that pass the name shape but are page furniture, not people.
NOT_A_NAME = {"some page", "linkedin page", "company page", "home page",
              "sign in", "log in", "jobs", "people", "posts", "about us",
              "contact us", "our team", "the team", "privacy policy",
              "terms of service", "profile", "search results"}
SPLIT_ON = re.compile(r"\s*[|\u2013\u2014]\s*|\s+-\s+")
URL_SLUG = re.compile(r"linkedin\.com/in/([a-z0-9-]+)", re.I)
TRAILING_ID = re.compile(r"-[0-9a-f]{4,}$", re.I)

EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")

# Common shapes, checked against whatever real addresses turn up.
PATTERNS = [
    ("first.last", re.compile(r"^[a-z]+\.[a-z]+$")),
    ("firstlast", re.compile(r"^[a-z]{6,}$")),
    ("first", re.compile(r"^[a-z]{2,12}$")),
    ("f.last", re.compile(r"^[a-z]\.[a-z]+$")),
    ("first_last", re.compile(r"^[a-z]+_[a-z]+$")),
    ("firstl", re.compile(r"^[a-z]+[a-z]$")),
]

# Matched as a PREFIX, not an exact string: "infodelhi@" is as generic as
# "info@", and inferring a name pattern from it produced "firstlast" from a
# mailbox with no name in it at all.
GENERIC_PREFIXES = ("info", "contact", "hello", "support", "sales", "admin",
                    "career", "hr", "help", "enquir", "inquir", "media",
                    "press", "marketing", "office", "team", "noreply",
                    "no-reply", "webmaster", "privacy", "legal", "customer",
                    "service", "booking", "appointment", "feedback", "general",
                    "reception", "front", "desk", "mail", "email", "web")


def is_generic_mailbox(local):
    local = local.lower()
    return any(local.startswith(prefix) for prefix in GENERIC_PREFIXES)


def parse_args(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        sys.exit(f"Usage: python {Path(__file__).name} <client> <target>\n"
                 f"Example: python {Path(__file__).name} mngo.in happiestminds.com")
    return args[0], args[1]


def serper(api_key, query, num=RESULTS_PER_QUERY):
    try:
        response = requests.post(
            SERPER_URL,
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
        return response.json().get("organic") or []
    except ValueError:
        return []


# ------------------------------------------------------------ which titles

def build_titles_prompt(client):
    return f"""Name the job titles worth writing to.

WHAT IS BEING SOLD: {client.get('what_you_sell') or client['one_liner']}
WHO PAYS FOR IT: {client.get('who_pays_for_it') or client.get('who_buys_it') or 'not stated'}

Give at most {MAX_TITLES} job titles, best first. Rules:
1. Real titles as they appear on LinkedIn profiles, not descriptions. "Head of
   Talent Acquisition", not "the person in charge of hiring".
2. Prefer the person who FEELS the problem daily over the one who signs the
   cheque. A manager who lives with the mess replies; a CFO does not.
3. Vary the seniority a little - a director, a manager, a lead - because small
   companies and large ones title the same job differently.
4. No C-suite unless the buyer really is the founder or the CEO, which is only
   true for very small companies.
5. Include one Indian-market variant if the phrasing differs there.

"why" is one line on who actually owns this problem inside a company.

Reply with JSON only:
{TITLES_SCHEMA}"""


def get_titles(api, client):
    raw = ask_gemini(api, build_titles_prompt(client))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        die("Could not work out which job titles to look for.")
    titles = [str(x).strip() for x in (data.get("titles") or []) if str(x).strip()]
    if not titles:
        die("No job titles could be worked out from the client profile.")
    return titles[:MAX_TITLES], str(data.get("why") or "").strip()


# ------------------------------------------------------------- find people

def clean_role(role):
    role = re.sub(r"\s+", " ", role or "").strip(" -\u2013|\u00b7,")
    role = re.sub(r"\bLinkedIn\b", "", role, flags=re.I).strip(" -\u2013|\u00b7,")
    return role


def name_from_url(link):
    """Read a name out of the profile slug when the title will not give one."""
    match = URL_SLUG.search(link or "")
    if not match:
        return ""
    slug = TRAILING_ID.sub("", match.group(1))
    words = [w for w in slug.split("-") if w and not w.isdigit()]
    if not 2 <= len(words) <= 4:
        return ""
    return " ".join(w.capitalize() for w in words)


def parse_profile(result):
    """
    Pull a name and a role out of one search result.

    Returns (name, role) or ("", ""). The role is best-effort - it is whatever
    the person wrote on their own profile, so it sometimes reads oddly.
    """
    title = (result.get("title") or "").strip()
    link = result.get("link") or ""
    snippet = (result.get("snippet") or "").strip()

    parts = [p.strip() for p in SPLIT_ON.split(title) if p.strip()]
    parts = [p for p in parts if p.lower() not in ("linkedin", "linkedin.com")]

    name, role = "", ""
    for i, part in enumerate(parts):
        cleaned = re.sub(r"\s+on LinkedIn.*$", "", part, flags=re.I).strip()
        if NAME_SHAPE.match(cleaned) and cleaned.lower() not in NOT_A_NAME:
            name = cleaned
            role = " - ".join(parts[i + 1:])
            break

    if not name:
        # The title gave no usable name, so read it from the profile URL and
        # keep only title fragments that are not page furniture.
        name = name_from_url(link)
        useful = [p for p in parts if p.lower() not in NOT_A_NAME]
        role = " - ".join(useful) if useful else ""

    if not name:
        return "", ""

    role = clean_role(role)
    if not role:
        # The snippet usually opens with the headline when the title did not.
        first = snippet.split(".")[0].strip()
        role = clean_role(first[:90]) if 3 < len(first) < 120 else ""

    # The snippet often repeats the person's name before their title. Strip it,
    # or every card reads "Anita Rao — Anita Rao, Talent Lead".
    if role.lower().startswith(name.lower()):
        role = clean_role(role[len(name):].lstrip(" ,-\u2013"))

    return name, role or "role not stated"


def company_name_for(api_key, domain):
    """
    Name the company from its OWN pages.

    A loose search for a domain returns whatever Google feels like:
    apollohospitals.com came back as "Payonline" once, and every search after
    that looked for the wrong company entirely. So a title is only trusted when
    the result actually lives on the domain, and the domain stem is the fallback.
    """
    stem = domain.split(".")[0].replace("-", " ")
    fallback = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem).title()

    for r in serper(api_key, f"site:{domain}", num=5):
        link = (r.get("link") or "").lower()
        if domain not in link:
            continue
        title = r.get("title") or ""
        for sep in ("|", " - ", "\u2013", "\u2014", ":"):
            title = title.split(sep)[0]
        title = title.strip()
        # The name should look related to the domain, or it is not the company.
        squashed = re.sub(r"[^a-z]", "", title.lower())
        if 2 < len(title) < 50 and (squashed[:6] in stem.lower().replace(" ", "")
                                    or stem.lower().replace(" ", "")[:6] in squashed):
            return title
    return fallback


def mentions_company(result, company_name):
    """Does this result actually concern the company we asked about?"""
    text = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
    words = [w for w in re.split(r"\W+", company_name.lower())
             if len(w) > 3 and w not in ("ltd", "limited", "group", "india",
                                         "private", "company", "the")]
    if not words:
        return True
    return any(w in text for w in words)


def find_people(api_key, company_name, titles, domain=""):
    """
    Read names off public LinkedIn profile pages.

    Nothing is scraped and no login is used - these are the same public result
    titles Google shows anyone. Each title is tried two ways, quoted and not,
    because an exact-phrase match on a job title finds nobody at companies that
    word it slightly differently.
    """
    people, seen = [], set()
    for title in titles:
        queries = [f'site:linkedin.com/in "{company_name}" "{title}"',
                   f'site:linkedin.com/in {company_name} {title}']
        found_this_title = 0
        for query in queries:
            if found_this_title:
                break
            print(f"  searching: {title}")
            results = serper(api_key, query)
            for r in results:
                link = (r.get("link") or "")
                if "linkedin.com/in" not in link:
                    continue
                # The unquoted fallback query has no company filter, so without
                # this it returns anyone on earth holding that job title. That
                # is how a search for one hospital returned 42 payments people
                # across nine countries.
                if not mentions_company(r, company_name):
                    continue
                name, role = parse_profile(r)
                if not name:
                    continue
                key = link.split("?")[0].lower()
                if key in seen:
                    continue
                seen.add(key)
                found_this_title += 1
                people.append({
                    "name": name,
                    "role": role,
                    "linkedin": link.split("?")[0],
                    "matched_title": title,
                    "snippet": (r.get("snippet") or "")[:200],
                })
            print(f"    {found_this_title} so far")
            time.sleep(REQUEST_PAUSE_SECONDS)

    # Last resort: the company's own site often names its leadership.
    if not people and domain:
        print("  nobody on LinkedIn - checking their own site")
        for r in serper(api_key, f"site:{domain} team OR leadership OR about us"):
            snippet = r.get("snippet") or ""
            for m in re.finditer(r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b", snippet):
                candidate = m.group(1)
                if candidate.lower() in seen:
                    continue
                seen.add(candidate.lower())
                people.append({"name": candidate, "role": "named on their site",
                               "linkedin": r.get("link", ""),
                               "matched_title": "from their website",
                               "snippet": snippet[:200]})
        time.sleep(REQUEST_PAUSE_SECONDS)

    return people


# ------------------------------------------------------- email pattern only

def find_email_pattern(api_key, domain):
    """
    Work out the shape of this company's addresses from ones already public.

    Returns the pattern and the real addresses it was inferred from, so the
    guess is checkable. Never returns a constructed address for a named person -
    a fabricated address bounces, and a bounce burns the domain reputation you
    need for every later email.
    """
    found, sources = [], []
    for query in (f'"@{domain}" email contact',
                  f'site:{domain} "@{domain}"'):
        for r in serper(api_key, query, num=8):
            text = f"{r.get('title', '')} {r.get('snippet', '')}"
            for m in EMAIL_IN_TEXT.finditer(text):
                address = m.group(0).lower()
                if m.group(1).lower() != domain:
                    continue
                local = address.split("@")[0]
                if is_generic_mailbox(local):
                    continue
                if address not in found:
                    found.append(address)
                    sources.append(r.get("link", ""))
        time.sleep(REQUEST_PAUSE_SECONDS)

    # One address is not a pattern. Two matching the same shape is weak
    # evidence; one is a coincidence, and a wrong pattern means every address
    # you build from it bounces.
    pattern = ""
    if len(found) >= 2:
        for name, rx in PATTERNS:
            matches = [a for a in found if rx.match(a.split("@")[0])]
            if len(matches) >= 2:
                pattern = name
                break

    return {"pattern": pattern, "examples": found[:5], "sources": sources[:5],
            "confidence": "low" if len(found) < 3 else "reasonable"}


# --------------------------------------------------------------------- output

def to_markdown(client, company, domain, titles, why, people, email):
    lines = [f"# Who to write to at {company}", "",
             f"_For {client['name']}._", ""]
    if why:
        lines += [f"> {why}", ""]

    lines += ["## Titles searched", ""] + [f"- {t}" for t in titles] + [""]

    lines += [f"## People found ({len(people)})", ""]
    if not people:
        lines += ["Nobody matched. Either the company is small enough that "
                  "nobody holds that exact title, or their profiles are not "
                  "public. Search LinkedIn by hand for the company and scan "
                  "the People tab.", ""]
    for p in people:
        lines += [f"### {p['name']}",
                  f"- {p['role']}",
                  f"- {p['linkedin']}",
                  f"- matched on: {p['matched_title']}", ""]

    lines += ["## Email pattern", ""]
    if email.get("pattern"):
        lines += [f"Addresses at {domain} look like **{email['pattern']}"
                  f"@{domain}** ({email.get('confidence', 'low')} confidence).", "",
                  "Inferred from real addresses found publicly:"]
        lines += [f"- {a}" for a in email["examples"]]
        lines += ["", "Verify before sending. A bounced first email hurts your "
                  "domain reputation for everything after it.", ""]
    else:
        lines += [f"No public addresses at {domain} were found, so there is no "
                  f"pattern to infer. LinkedIn message instead, or use the "
                  f"contact form.", ""]
    return "\n".join(lines)


def main():
    client_arg, target_arg = parse_args(sys.argv)

    serper_key = os.environ.get("SERPER_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not serper_key:
        die("SERPER_API_KEY is not set.")
    if not gemini_key:
        die("GEMINI_API_KEY is not set.")

    client = load_client(client_arg)
    domain = to_domain(target_arg, quiet=True)
    if not domain:
        die(f"Could not work out a domain from {target_arg!r}.")

    api = genai.Client(api_key=gemini_key)
    print(f"Asking {MODEL} which job titles would care...")
    titles, why = get_titles(api, client)
    print(f"  {', '.join(titles)}")
    if why:
        print(f"  {why}\n")

    company = company_name_for(serper_key, domain)

    print(f"Looking for people at {company}:")
    people = find_people(serper_key, company, titles, domain)

    print(f"\nLooking for the email pattern at {domain}...")
    email = find_email_pattern(serper_key, domain)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"client": client["name"], "client_domain": client["_slug"],
               "company": company, "domain": domain, "titles": titles,
               "why": why, "people": people, "email": email}
    json_path = OUT_DIR / f"{domain}.json"
    md_path = OUT_DIR / f"{domain}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    md_path.write_text(to_markdown(client, company, domain, titles, why, people,
                                   email), encoding="utf-8")

    print(f"\n{len(people)} person/people found\n")
    for p in people:
        print(f"  {p['name']:<28} {p['role']}")
        print(f"    {p['linkedin']}")
    if email.get("pattern"):
        print(f"\n  Email pattern: {email['pattern']}@{domain} "
              f"({email.get('confidence', 'low')} confidence)")
        print(f"  Seen in: {', '.join(email['examples'][:3])}")
    else:
        print(f"\n  No usable email pattern at {domain}. Only generic "
              f"mailboxes, or nothing public at all.")

    print(f"\nSaved to {json_path}")
    print(f"        {md_path}")


if __name__ == "__main__":
    main()