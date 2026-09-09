"""
contacts.py - optional stage between triggers.py and generate.py.

Extracts named business contacts (name + title) from the scraped pages, so a
message can be addressed to the person who would actually own the problem
rather than sent to info@.

Only what is printed on the company's own public pages. No inference, no
guessed email addresses, no personal detail beyond name and role.

Usage:
    python contacts.py razorpay.com
"""

import json
import sys
from pathlib import Path

from triggers import (
    MODEL, ask_gemini, die, genai, resolve_input, load_scrape,
    flatten_pages, build_corpus,
)

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "contacts"

SCHEMA_EXAMPLE = """{
  "contacts": [
    {"name": "string", "title": "string", "source_page": "string",
     "relevance": "string"}
  ],
  "no_contact_found": false
}"""


def build_prompt(domain, corpus):
    return f"""Below is text scraped from the public website of {domain}.

Extract NAMED BUSINESS CONTACTS: people whose name and job title both appear
in the text. This is for routing a business message to the right desk.

Rules:
1. Both name and title must appear in the text. If you only have one, skip it.
2. Do NOT guess, infer, or recall anyone from outside this text.
3. Do NOT invent or construct email addresses, phone numbers, or social
   profiles. Name and title only.
4. Do NOT include anything about the person beyond their role at this company -
   no background, no history, no personal detail.
5. "relevance" is one short line on why this role would care about hiring and
   recruitment automation. If they plainly would not, say so.
6. Prefer people who own hiring: founders at small companies, heads of talent,
   engineering leaders. Skip board members, investors and advisors.
7. Return at most 4 contacts.
8. If no one qualifies, set "no_contact_found" to true and return an empty list.
   That is a normal, correct answer.

Reply with JSON only, matching this shape exactly:
{SCHEMA_EXAMPLE}

--- SCRAPED TEXT ---
{corpus}
--- END SCRAPED TEXT ---"""


def clean_result(data, domain):
    contacts = []
    for item in data.get("contacts") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        title = str(item.get("title") or "").strip()
        if not name or not title:
            continue
        contacts.append({
            "name": name,
            "title": title,
            "source_page": str(item.get("source_page") or "unknown").strip(),
            "relevance": str(item.get("relevance") or "").strip(),
        })

    return {
        "domain": domain,
        "contacts": contacts,
        "no_contact_found": len(contacts) == 0,
    }


def main():
    if len(sys.argv) != 2:
        sys.exit(f"Usage: python {Path(__file__).name} <domain>")

    import os
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        die("GEMINI_API_KEY is not set.")

    domain, in_path = resolve_input(sys.argv[1])
    pages = flatten_pages(load_scrape(in_path))
    if not pages:
        die(f"{in_path} contained no page text. Run scrape.py first.")

    print(f"Read {len(pages)} page(s) from {in_path}")
    print(f"Asking {MODEL} for contacts...")

    client = genai.Client(api_key=api_key)
    raw = ask_gemini(client, build_prompt(domain, build_corpus(pages)))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"Model did not return valid JSON: {exc.msg}")

    result = clean_result(data, domain)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{domain}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")

    if result["no_contact_found"]:
        print("\nNo named contacts on the public pages.")
    else:
        for c in result["contacts"]:
            print(f"\n  {c['name']} - {c['title']}")
            print(f"    source: {c['source_page']}")
            if c["relevance"]:
                print(f"    {c['relevance']}")

    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()