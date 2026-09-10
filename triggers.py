"""
triggers.py - step 2 of the MNGO outreach generator.

Takes the text that scrape.py saved for one company, plus recent headlines from
news.py and the fit evidence from check_fit.py, asks Gemini to pull out
"triggers" - specific, recent, verifiable facts that justify a cold message
existing - and writes the result to triggers/<domain>.json.

Target: companies that hire entry-level talent from Indian campuses.

Three sources, in descending order of usefulness:
  fit/<domain>.json   the buying signal that qualified this company. Strongest,
                      because it is evidence of the pain itself rather than a
                      proxy for it. Optional - only present if check_fit.py ran.
  news/<domain>.json  dated and externally verifiable. Optional.
  data/<domain>.json  the company's own site. Always required.

The fit source exists because check_fit.py used to find the one fact that made a
company worth writing to - a campus job post collecting resumes on a Google Form
- and then nothing downstream could see it. The pipeline would qualify a company
on evidence it then threw away, and score the company low on corporate news that
had nothing to do with campus hiring.

Usage:
    python triggers.py happiestminds.com
"""

import datetime
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from google import genai
    from google.genai import types
except ImportError:
    sys.exit("Missing dependency. Run:  pip install google-genai")


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
NEWS_DIR = BASE_DIR / "news"
FIT_DIR = BASE_DIR / "fit"
OUT_DIR = BASE_DIR / "triggers"

MAX_TOTAL_CHARS = 60_000
MIN_CHARS_PER_PAGE = 2_000

# How many of the fit check's raw search results to pass through. The verdict
# summarises them, but the model needs the originals to cite a real URL.
MAX_FIT_RESULTS = 12

# Which Gemini model to call. gemini-2.5-flash is retired for new API keys.
MODEL = "gemini-3.6-flash"

SCHEMA_EXAMPLE = """{
  "company_name": "string",
  "triggers": [
    {"fact": "string", "source_page": "string", "relevance_score": 0}
  ],
  "pain_hypothesis": "string",
  "no_trigger_found": false
}"""


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- input side

def resolve_input(arg):
    candidate = Path(arg)
    if candidate.suffix == ".json":
        return candidate.stem, candidate

    domain = arg.strip().lower()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    if domain.startswith("www."):
        domain = domain[4:]
    domain = domain.split("/")[0].strip()

    if not domain:
        die(f"Could not work out a domain from {arg!r}.")
    return domain, DATA_DIR / f"{domain}.json"


def load_scrape(path):
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        die(f"No scraped data at {path}. Run scrape.py for this domain first.")
    except OSError as exc:
        die(f"Could not read {path}: {exc}")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"{path} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})")


def flatten_pages(scraped):
    pages = []

    def add(label, text):
        if isinstance(text, str) and text.strip():
            pages.append((str(label), text.strip()))

    def add_page_dict(page, fallback_label):
        add(
            page.get("url") or page.get("page") or page.get("title") or fallback_label,
            page.get("text") or page.get("content") or page.get("body"),
        )

    if isinstance(scraped, dict):
        if isinstance(scraped.get("pages"), list):
            for i, page in enumerate(scraped["pages"], start=1):
                if isinstance(page, dict):
                    add_page_dict(page, f"page {i}")
                else:
                    add(f"page {i}", page)
        elif isinstance(scraped.get("pages"), dict):
            for key, value in scraped["pages"].items():
                add(key, value)
        elif isinstance(scraped.get("text") or scraped.get("content"), str):
            add_page_dict(scraped, "homepage")
        else:
            for key, value in scraped.items():
                if isinstance(value, str):
                    add(key, value)
                elif isinstance(value, dict):
                    add_page_dict(value, key)
    elif isinstance(scraped, list):
        for i, page in enumerate(scraped, start=1):
            if isinstance(page, dict):
                add_page_dict(page, f"page {i}")
            else:
                add(f"page {i}", page)

    return pages


def build_corpus(pages):
    per_page = max(MIN_CHARS_PER_PAGE, MAX_TOTAL_CHARS // len(pages))
    chunks = []
    used = 0

    for label, text in pages:
        if used >= MAX_TOTAL_CHARS:
            break
        room = min(per_page, MAX_TOTAL_CHARS - used)
        body = text[:room]
        if len(text) > room:
            body += "\n[...truncated...]"
        chunks.append(f"=== PAGE: {label} ===\n{body}")
        used += len(body)

    return "\n\n".join(chunks)


def load_news(domain):
    """
    Read the articles news.py saved, formatted for the prompt.

    A company's own site goes stale; news is dated and externally verifiable,
    which makes it a better trigger source. Returns "" when no news file exists,
    so this stage stays entirely optional.
    """
    path = NEWS_DIR / f"{domain}.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    lines = []
    for a in data.get("articles", []):
        title = a.get("title", "").strip()
        link = a.get("link", "").strip()
        if not title or not link:
            continue
        lines.append(
            f"- {title}\n"
            f"  ({a.get('source', 'unknown source')}, {a.get('date', 'undated')})\n"
            f"  {link}"
            + (f"\n  {a['snippet']}" if a.get("snippet") else "")
        )
    return "\n".join(lines)


def load_fit(domain):
    """
    Read the verdict and evidence check_fit.py saved for this company.

    This is the highest-value source in the pipeline and the only one that
    touches the pain directly. A scrape shows what a company says about itself;
    news shows what happened to it; the fit check shows a campus job post routing
    resumes into a Google Form, which IS the problem MNGO solves.

    Returns "" when no fit file exists, so the stage stays optional and the
    pipeline still runs for a company you never fit-checked.
    """
    path = FIT_DIR / f"{domain}.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    result = data.get("result") or {}
    signal = result.get("signal") or {}
    evidence = data.get("evidence") or []

    lines = []

    verdict = str(result.get("verdict") or "").strip()
    if verdict:
        lines.append(f"Fit verdict: {verdict}")
    reason = str(result.get("reason") or "").strip()
    if reason:
        lines.append(f"Reason: {reason}")

    if signal.get("found"):
        lines.append("")
        lines.append("BUYING SIGNAL FOUND "
                     f"[{signal.get('status', 'ASSERTED')}]:")
        if signal.get("what"):
            lines.append(f"  What: {signal['what']}")
        if signal.get("evidence"):
            lines.append(f"  Cited: {signal['evidence']}")
    else:
        lines.append("")
        lines.append("No buying signal was found for this company.")

    kept = [e for e in evidence if isinstance(e, dict)][:MAX_FIT_RESULTS]
    if kept:
        lines.append("")
        lines.append("Search results the fit check used:")
        for e in kept:
            dated = f", {e['date']}" if e.get("date") else ""
            lines.append(
                f"- {e.get('title', '').strip()}"
                f" (asking about: {e.get('asking_about', 'unknown')}{dated})\n"
                f"  {e.get('link', '').strip()}"
                + (f"\n  {e['snippet'].strip()}" if e.get("snippet") else "")
            )

    return "\n".join(lines)


# ------------------------------------------------------------------ the model

def build_prompt(domain, corpus, news_block="", fit_block=""):
    today = datetime.date.today().isoformat()
    return f"""You are a research analyst preparing cold outreach for MNGO, campus
placement infrastructure being built for Indian colleges. The message goes to
a company that hires entry-level talent from campuses. Today's date is {today}.

Below are up to three sources about a company at {domain}: the fit check that
qualified this company, recent news, and text scraped from their own website.

Your job: extract TRIGGERS. A trigger is a specific, recent, verifiable fact
about this company that would justify a message about campus hiring existing
right now.

Good triggers (specific, checkable, time-bound):
- "A campus hiring post asks candidates to submit resumes through a Google Form"
- "Hiring 4 backend engineers and 2 SDET roles, posted on their careers page"
- "Raised a Series B in March 2026, led by Accel"
- "Announced a graduate trainee programme for the 2026 batch"
- "Careers page lists 18 open roles across engineering and operations"

Not triggers (generic, undated, or marketing fluff):
- "They are a fast-growing company"
- "They value their people"
- "They work in fintech"
- Anything you inferred, guessed, or would have to look up elsewhere

Hard rules:
1. Every fact must be supported by the sources below. Do NOT invent, infer, or
   embellish. If the text does not say it, it is not a trigger.
2. "source_page" must be either the exact PAGE label from the scraped text, or
   the exact article URL from the news section, or the exact result URL from the
   fit-check section - whichever the fact came from. A reader must be able to
   open that source and check the claim.
3. "relevance_score" is 0-10: how strongly this fact signals ENTRY-LEVEL OR
   VOLUME HIRING, or manual coordination of it, which is what campus
   recruitment serves.
   - Highest (8-10): DIRECT EVIDENCE OF THE PROBLEM. A campus or fresher job
     post that collects resumes through a Google Form, a personal email address
     or a spreadsheet; a placement-cell contact chain; a manual drive schedule.
     This is a company visibly doing by hand the thing MNGO automates, and it
     is the strongest trigger available. Score it here whenever the fit check
     found it.
   - High (7-8): open entry-level roles at volume, graduate or trainee
     programmes, a funding round, a new office or region.
   - Medium (4-6): senior leadership appointments, a stated growth strategy,
     mergers, new product lines that imply team building.
   - Low (0-3): awards, partnerships, product features, general PR, AI product
     launches. A company launching a platform is not a campus-hiring signal.
4. A trigger you cannot connect to campus hiring in ONE plain sentence is not a
   trigger for this message. A merger or an AI product launch is real news and
   still scores low here, because the link to campus hiring runs through two or
   three assumptions. Do not stretch.
5. Prefer dated facts. A news item from the last few months beats an undated
   claim on a marketing page. If a fact has no date anywhere in the source,
   cap its score at 5 - EXCEPT direct evidence of manual campus hiring under
   rule 3, which is about a visible process rather than an event, and keeps its
   score without a date.
6. Return at most 5 triggers, best first.
7. If there is no genuine trigger, set "no_trigger_found" to true and return an
   empty "triggers" list. Returning nothing is the correct, expected answer for
   a company with a thin website and no news. An empty list is a success, not
   a failure.
8. "pain_hypothesis" is one sentence on the campus-hiring pain this company
   plausibly has, based only on the evidence below - reaching colleges,
   coordinating drives, comparing candidates across campuses. When the fit
   check found a signal, build the hypothesis on THAT, not on corporate news.
   If there are no triggers, say plainly that there is no evidence to support
   a hypothesis.
9. Never name an individual in connection with a departure, resignation or
   exit - not in a trigger, not in the pain hypothesis. An appointment may name
   the person, because that is public and positive; an exit may not.

Reply with JSON only, matching this shape exactly:
{SCHEMA_EXAMPLE}

--- FIT CHECK ---
{fit_block or "not run for this company"}
--- END FIT CHECK ---

--- RECENT NEWS ---
{news_block or "none found"}
--- END NEWS ---

--- SCRAPED TEXT ---
{corpus}
--- END SCRAPED TEXT ---"""


def ask_gemini(client, prompt):
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
    except Exception as exc:
        die(f"Gemini API call failed [{type(exc).__name__}]: {exc}")

    text = (response.text or "").strip()
    if not text:
        die(
            "Gemini returned an empty response. This usually means the content "
            "was blocked or the input was too long. Try lowering MAX_TOTAL_CHARS."
        )
    return text


# ----------------------------------------------------------------- output side

def parse_response(raw_text, domain):
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        debug_path = OUT_DIR / f"{domain}.raw.txt"
        debug_path.write_text(raw_text, encoding="utf-8")
        die(
            f"Model did not return valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno}). "
            f"The raw reply was saved to {debug_path} so you can look at it."
        )

    if not isinstance(data, dict):
        die(f"Expected a JSON object from the model, got {type(data).__name__}.")
    return data


def clean_result(data, domain):
    triggers = []
    for item in data.get("triggers") or []:
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact") or "").strip()
        if not fact:
            continue

        try:
            score = int(round(float(item.get("relevance_score", 0))))
        except (TypeError, ValueError):
            score = 0

        triggers.append({
            "fact": fact,
            "source_page": str(item.get("source_page") or "unknown").strip(),
            "relevance_score": max(0, min(10, score)),
        })

    triggers.sort(key=lambda t: t["relevance_score"], reverse=True)

    return {
        "company_name": str(data.get("company_name") or domain).strip(),
        "triggers": triggers,
        "pain_hypothesis": str(data.get("pain_hypothesis") or "").strip(),
        "no_trigger_found": len(triggers) == 0,
    }


def save_result(domain, result):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{domain}.json"
    try:
        out_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        die(f"Could not write {out_path}: {exc}")
    return out_path


# ------------------------------------------------------------------------ main

def main():
    if len(sys.argv) != 2:
        sys.exit(f"Usage: python {Path(__file__).name} <domain-or-json-path>")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        die(
            "GEMINI_API_KEY is not set. Get a key from "
            "https://aistudio.google.com/app/apikey and put it in your "
            "environment or in a .env file next to this script."
        )

    domain, in_path = resolve_input(sys.argv[1])

    pages = flatten_pages(load_scrape(in_path))
    if not pages:
        die(
            f"{in_path} parsed fine but contained no page text. Check what "
            f"scrape.py is saving - flatten_pages() may need a new shape added."
        )

    news_block = load_news(domain)
    news_count = news_block.count("\n- ") + (1 if news_block else 0)
    fit_block = load_fit(domain)

    print(f"Read {len(pages)} page(s) from {in_path}")
    if news_block:
        print(f"Read {news_count} news article(s) from {NEWS_DIR / (domain + '.json')}")
    else:
        print("No news file - run news.py for dated triggers.")
    if fit_block:
        print(f"Read the fit check from {FIT_DIR / (domain + '.json')}")
    else:
        print("No fit file - run check_fit.py first if you want the buying "
              "signal to count as a trigger.")
    print(f"Asking {MODEL} for triggers...")

    client = genai.Client(api_key=api_key)
    prompt = build_prompt(domain, build_corpus(pages), news_block, fit_block)
    result = clean_result(parse_response(ask_gemini(client, prompt), domain), domain)
    out_path = save_result(domain, result)

    print(f"\n{result['company_name']}")
    if result["no_trigger_found"]:
        print("  No genuine triggers found - not worth a cold message.")
    else:
        for trigger in result["triggers"]:
            print(f"  [{trigger['relevance_score']}/10] {trigger['fact']}")
            print(f"          source: {trigger['source_page']}")
    if result["pain_hypothesis"]:
        print(f"\n  Pain hypothesis: {result['pain_hypothesis']}")
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()