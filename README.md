# Cold Outreach Agent

Most cold-outreach tools always produce output. Give them a company name and
they will write you three cheerful emails, whether or not there is any real
reason to contact that company.

This one checks first. It reads the company's own site and recent news, pulls
out facts that are specific, dated and verifiable, and builds every message on
one of them. Every claim is checked back against what was actually found, and
every source is linked so a human can open it. When there is no real hook, the
tool says so rather than inventing one.

Tested on 11 Indian tech companies: 9 produced trigger-backed messages, 2 had
nothing to work with.

## Pipeline

```
domain → scrape.py → news.py → triggers.py → generate.py → app.py
         (site text)  (headlines) (verified facts) (6 messages)  (demo UI)
                    ↘ contacts.py
                      (who to send it to)
```

Each stage writes JSON the next one reads, so any step can be inspected or
re-run alone.

**scrape.py** — pulls text from homepage, about, careers, team and leadership
pages, plus `careers.` / `jobs.` subdomains, which is where most Indian
companies actually post roles. Falls back to a headless browser when plain
HTTP returns an empty shell.

**news.py** — searches Google News via Serper for recent headlines about the
company. Optional, but it is what makes triggers *dated*.

**triggers.py** — extracts triggers: specific, checkable, time-bound facts.
"Appointed a new CMO in May 2026" is a trigger. "A fast-growing company" is
not. Undated facts are capped at 5/10 so news beats stale marketing copy.

**generate.py** — writes 3 emails and 3 LinkedIn messages across named angles,
each built on a specific trigger. Falls back to clearly-labelled generic
messages when no trigger exists.

**app.py** — Streamlit page over the saved results, with expanders showing
every trigger source and every article the news stage found.

## Two hallucination checks

The second exists because the first was not enough.

**Citation check** — every message copies the trigger fact it used into a
`trigger_used` field, matched against the actual triggers file. A message
citing something never extracted gets flagged `UNGROUNDED`.

**Body check** — during testing a message cited a real trigger correctly and
still claimed it would "drop candidate drop-off rates by 40%." That figure
appeared nowhere in the input. The citation check passed it, because the
citation *was* fine. The body check scans message text for numeric claims no
trigger supports.

## Four things testing taught me

**More text made extraction worse.** Raising the per-page character cap from
6,000 to 15,000 dropped Razorpay from 1 trigger to 0. The extra text was
navigation and footer boilerplate that buried the signal. The cap works as a
crude relevance filter.

**Half the "refusals" were scraper failures.** Zerodha, cred.club and
BrowserStack all declined until a headless-browser fallback was added.
cred.club then produced six messages. A tool that refuses for the wrong reason
looks principled and is just broken.

**The company's own site goes stale; news does not.** Razorpay's careers page
changed between runs and the pipeline lost its only trigger. Adding news
search brought back four — including a CMO appointment and a leadership
departure, both dated.

**Grounding checks do not make a message appropriate.** One generated email
named the executive who had just left for a competitor. Every fact in it was
true and correctly cited. It was still the wrong thing to send, so the
generator now refers to "a recent ops leadership change" without naming
anyone. Correctness and judgement are separate problems.

## Running it

```bash
pip install requests beautifulsoup4 google-genai python-dotenv streamlit playwright
playwright install chromium
```

Create a `.env` file next to the scripts:

```
GEMINI_API_KEY=your_gemini_key
SERPER_API_KEY=your_serper_key
```

Free keys: [aistudio.google.com](https://aistudio.google.com/app/apikey) and
[serper.dev](https://serper.dev).

```bash
python scrape.py razorpay.com
python news.py razorpay.com
python triggers.py razorpay.com
python generate.py razorpay.com

streamlit run app.py
```

## Known limits

- **Contact extraction almost never fires.** `contacts.py` pulls names and
  titles where they appear as plain text. Across the Indian tech companies
  tested it found none — leadership pages are images or JS widgets. The stage
  is correct; the data is not public in a form it can read. Apollo.io or
  Hunter.io would fill this gap.
- **No LinkedIn data.** Company pages and employee counts would be strong
  triggers, but every route to them needs a session cookie and violates
  LinkedIn's terms.
- **Gemini free tier caps at 20 requests/day**, which is 10 companies.
- Nothing is ever sent. This writes files only.