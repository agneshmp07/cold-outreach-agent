import requests, json, os, sys
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

PATHS = ["", "/about", "/about-us", "/careers", "/jobs", "/company",
         "/team", "/leadership", "/people"]

# Many Indian companies host jobs on a subdomain, not a path:
# careers.zerodha.com, jobs.example.com.
SUBDOMAINS = ["careers", "jobs", "work"]

# Deliberately low. Raising this to 15000 made trigger extraction WORSE:
# the extra text was nav menus, footers and legal boilerplate, which buried
# the real signal. The cap acts as a crude relevance filter.
MAX_CHARS = 6000
MIN_USEFUL = 300

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def extract(html):
    """Strip HTML down to readable text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "svg"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())[:MAX_CHARS]


def get_text_fast(url):
    """Plain HTTP fetch. Fast, but returns nothing on JavaScript-rendered sites."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        return extract(r.text)
    except Exception as e:
        print(f"    error: {e}")
        return None


def get_text_browser(url):
    """
    Render the page in a headless browser, then read it.

    Needed because most modern careers pages build their content with
    JavaScript - requests downloads the empty shell and nothing else.
    Slower, so this only runs when the fast path came back empty.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=20000, wait_until="networkidle")
            html = page.content()
            browser.close()
        return extract(html)
    except Exception as e:
        print(f"    browser error: {type(e).__name__}")
        return None


def try_url(url, pages):
    """
    Fetch one page and store it under its full URL.

    The URL is the key, not a label like "/careers", so every trigger extracted
    downstream carries a clickable source a human can open and check. That is
    what makes the grounding claim verifiable rather than asserted.
    """
    print(f"  trying {url}")

    t = get_text_fast(url)
    if t and len(t) > MIN_USEFUL:
        pages[url] = t
        print(f"    ok  ({len(t)} chars)")
        return

    # Fast path failed. The page may exist but be JavaScript-rendered.
    print("    empty - retrying with browser")
    t = get_text_browser(url)
    if t and len(t) > MIN_USEFUL:
        pages[url] = t
        print(f"    ok via browser  ({len(t)} chars)")
    else:
        print("    skip (nothing usable)")


def scrape(domain):
    pages = {}

    for p in PATHS:
        try_url(f"https://{domain}{p}", pages)

    for sub in SUBDOMAINS:
        try_url(f"https://{sub}.{domain}", pages)

    return {"domain": domain, "pages": pages}


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    domain = sys.argv[1] if len(sys.argv) > 1 else "zoho.com"
    print(f"\nScraping {domain}")
    out = scrape(domain)
    path = os.path.join(DATA, domain + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {len(out['pages'])} pages -> {path}")