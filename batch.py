"""
batch.py - run the pipeline over many companies at once.

    python batch.py <client-domain>
    python batch.py mngo.in --limit 10
    python batch.py mngo.in --file my-targets.txt --contacts

Takes the candidates find_targets.py found (or a file of domains you wrote
yourself), runs each one end to end, and writes a review sheet listing what came
out. Meant to be left running: ten companies is roughly fifteen minutes.

It never stops on a failure. A company that cannot be scraped, is not a fit, or
produces nothing worth sending is recorded and the run moves on - the point of a
batch is to come back to a folder of drafts, not to a process that died on the
third company at 2am.

Already-processed companies are skipped, so re-running after an interruption
picks up where it stopped. Use --redo to write over them.

Output:
    batch/<client>-<date>.md     what happened to each company
    messages/<domain>.md          the drafts themselves, as usual
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from resolve import to_domain

BASE_DIR = Path(__file__).resolve().parent
CLIENTS_DIR = BASE_DIR / "clients"
TARGETS_DIR = BASE_DIR / "targets"
MESSAGES_DIR = BASE_DIR / "messages"
FIT_DIR = BASE_DIR / "fit"
OUT_DIR = BASE_DIR / "batch"

# A pause between companies. Not politeness - Serper and Gemini both rate limit,
# and a batch that trips a limit halfway through wastes everything after it.
PAUSE_BETWEEN = 3.0


def parse_args():
    p = argparse.ArgumentParser(description="Run the pipeline over many companies.")
    p.add_argument("client", help="your domain, e.g. mngo.in")
    p.add_argument("--limit", type=int, default=10,
                   help="how many companies to do (default 10)")
    p.add_argument("--file", help="a text file of company names or domains, "
                                  "one per line, instead of the candidates list")
    p.add_argument("--contacts", action="store_true",
                   help="also look for who to send each one to (slower)")
    p.add_argument("--redo", action="store_true",
                   help="rewrite companies that already have messages")
    p.add_argument("--skip-fit", action="store_true",
                   help="do not run the fit check (faster, less accurate)")
    return p.parse_args()


def load_client(slug):
    path = CLIENTS_DIR / f"{slug}.json"
    if not path.exists():
        sys.exit(f"No client profile at {path}.\n"
                 f"Create one with:  python profile.py {slug}")
    client = json.loads(path.read_text(encoding="utf-8"))
    if not client.get("reviewed"):
        sys.exit(f"{path} has not been reviewed. Open the app and fill in the "
                 f"sender and the never-claim list before running a batch - "
                 f"a batch of unreviewed messages is a batch of claims you "
                 f"cannot back up.")
    return client


def load_targets(args):
    """Company domains to work through, from a file or the candidates list."""
    if args.file:
        path = Path(args.file)
        if not path.exists():
            sys.exit(f"No such file: {path}")
        raw = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.strip().startswith("#")]
        out = []
        for entry in raw:
            domain = to_domain(entry, quiet=True)
            if domain:
                out.append({"company": entry, "domain": domain})
            else:
                print(f"  ! could not find a website for {entry!r} - skipping")
        return out

    path = TARGETS_DIR / f"candidates-{args.client}.json"
    if not path.exists():
        sys.exit(f"No candidates at {path}.\n"
                 f"Run:  python find_targets.py {args.client}\n"
                 f"Or pass your own list with --file targets.txt")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [c for c in (data.get("candidates") or []) if c.get("domain")]


def run(script, *args):
    return subprocess.run([sys.executable, str(BASE_DIR / script),
                           *[str(a) for a in args]],
                          capture_output=True, text=True)


def already_done(client_slug, domain):
    path = MESSAGES_DIR / f"{domain}.json"
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(
            "client_domain") == client_slug
    except Exception:
        return False


def fit_verdict(domain):
    path = FIT_DIR / f"{domain}.json"
    if not path.exists():
        return "", ""
    try:
        result = json.loads(path.read_text(encoding="utf-8")).get("result") or {}
        return result.get("verdict", ""), result.get("reason", "")
    except Exception:
        return "", ""


def summarise_messages(domain):
    """Read back what was written, so the sheet can rank what is worth reading."""
    path = MESSAGES_DIR / f"{domain}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    everything = (data.get("emails") or []) + (data.get("linkedin_messages") or [])
    blocked = sum(1 for m in everything
                  if m.get("overclaim") or m.get("asset_claim"))
    return {
        "top_score": (data.get("generated_from") or {}).get("top_score", 0),
        "no_trigger": bool(data.get("no_trigger")),
        "disqualified": bool(data.get("disqualifiers")),
        "blocked": blocked,
        "warnings": len(data.get("warnings") or []),
        "emails": len(data.get("emails") or []),
    }


def process(client_slug, target, args):
    """One company, start to finish. Returns a row for the review sheet."""
    domain = target["domain"]
    name = target.get("company") or domain
    row = {"company": name, "domain": domain, "outcome": "", "detail": "",
           "score": 0, "contacts": 0}

    if not args.redo and already_done(client_slug, domain):
        row["outcome"] = "skipped"
        row["detail"] = "already written - use --redo to replace"
        return row

    if not args.skip_fit:
        result = run("check_fit.py", client_slug, domain)
        verdict, reason = fit_verdict(domain)
        row["detail"] = reason[:160]
        if result.returncode == 1:
            # A no from the fit check does not stop a batch. It is recorded and
            # the drafts are still written, because the check judges a public
            # face and you may know something it does not.
            row["outcome"] = "poor fit"
        elif result.returncode == 2:
            row["outcome"] = "unclear fit"

    if run("scrape.py", domain).returncode != 0:
        row["detail"] = "could not read their website"

    run("news.py", domain)

    if run("triggers.py", client_slug, domain).returncode != 0:
        # No triggers file means generate.py has nothing to read, so write an
        # empty one and let it fall through to its no-hook path.
        (BASE_DIR / "triggers").mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "triggers" / f"{domain}.json").write_text(
            json.dumps({"company_name": domain, "triggers": [],
                        "disqualifiers": [], "pain_hypothesis": "",
                        "no_trigger_found": True}, indent=2) + "\n",
            encoding="utf-8")

    written = run("generate.py", client_slug, domain)
    if written.returncode != 0:
        row["outcome"] = row["outcome"] or "nothing written"
        detail = (written.stdout or written.stderr or "").strip().splitlines()
        if detail and not row["detail"]:
            row["detail"] = detail[0][:160]
        return row

    summary = summarise_messages(domain)
    if summary:
        row["score"] = summary["top_score"]
        if summary["disqualified"]:
            row["outcome"] = "do not send"
            row["detail"] = "a disqualifier was found"
        elif summary["blocked"]:
            row["outcome"] = "do not send"
            row["detail"] = f"{summary['blocked']} message(s) make a blocked claim"
        elif summary["no_trigger"]:
            row["outcome"] = row["outcome"] or "written, no hook"
        else:
            row["outcome"] = row["outcome"] or "ready"

    if args.contacts:
        found = run("find_contacts.py", client_slug, domain)
        if found.returncode == 0:
            path = BASE_DIR / "contacts" / f"{domain}.json"
            if path.exists():
                try:
                    row["contacts"] = len(json.loads(
                        path.read_text(encoding="utf-8")).get("people") or [])
                except Exception:
                    pass

    return row


RANK = {"ready": 0, "written, no hook": 1, "unclear fit": 2, "poor fit": 3,
        "nothing written": 4, "do not send": 5, "skipped": 6}


def write_sheet(client, rows, started):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y-%m-%d-%H%M")
    path = OUT_DIR / f"{client['_slug'] if '_slug' in client else 'batch'}-{stamp}.md"

    ordered = sorted(rows, key=lambda r: (RANK.get(r["outcome"], 9), -r["score"]))
    ready = [r for r in ordered if r["outcome"] == "ready"]

    lines = [f"# Batch for {client.get('name', 'client')}", "",
             f"_{len(rows)} company(ies), "
             f"{started.strftime('%d %b %Y %H:%M')}._", "",
             f"**{len(ready)} ready to read.** Start at the top - the list is "
             f"sorted by how worth reading each one is.", "",
             "| Company | Outcome | Top trigger | Contacts | Note |",
             "| --- | --- | --- | --- | --- |"]
    for r in ordered:
        lines.append(f"| {r['company']} | {r['outcome']} | "
                     f"{r['score'] or '-'}/10 | {r['contacts'] or '-'} | "
                     f"{r['detail']} |")

    lines += ["", "## What the outcomes mean", "",
              "- **ready** - trigger-backed, nothing flagged. Read these first.",
              "- **written, no hook** - nothing dated or newsworthy was found, "
              "so the messages lean on what their own site says. Weaker.",
              "- **unclear fit / poor fit** - the fit check was unconvinced but "
              "the drafts were written anyway. Read the note before sending.",
              "- **do not send** - a message makes a claim you said you would "
              "never make, or a disqualifier turned up. Do not edit and send; "
              "look at why.",
              "- **nothing written** - the pipeline had nothing real to work "
              "with. Usually a thin or unreadable website.", "",
              "## The drafts", ""]
    for r in ordered:
        if r["outcome"] not in ("nothing written", "skipped"):
            lines.append(f"- {r['company']}: `messages/{r['domain']}.md`")

    path.write_text("\n".join(lines), encoding="utf-8")

    # A machine-readable twin, so the app can show the run without parsing
    # markdown back into data.
    (OUT_DIR / f"{client.get('_slug', 'batch')}-{stamp}.json").write_text(
        json.dumps({"client": client.get("name", ""),
                    "client_domain": client.get("_slug", ""),
                    "started": started.isoformat(timespec="seconds"),
                    "ready": len(ready), "rows": ordered},
                   indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return path, ready


def main():
    args = parse_args()
    client_slug = to_domain(args.client, quiet=True) or args.client
    client = load_client(client_slug)
    client["_slug"] = client_slug

    targets = load_targets(args)[:args.limit]
    if not targets:
        sys.exit("No companies to work through.")

    started = datetime.now()
    print(f"{client.get('name')} - {len(targets)} company(ies)")
    print(f"Roughly {len(targets) * 1.5:.0f} minutes"
          f"{' (longer with --contacts)' if args.contacts else ''}.\n")

    rows = []
    for i, target in enumerate(targets, start=1):
        name = target.get("company") or target["domain"]
        print(f"[{i}/{len(targets)}] {name}")
        try:
            row = process(client_slug, target, args)
        except KeyboardInterrupt:
            print("\nStopped. Writing the sheet for what finished so far.")
            break
        except Exception as exc:
            row = {"company": name, "domain": target["domain"],
                   "outcome": "nothing written",
                   "detail": f"{type(exc).__name__}: {exc}"[:160],
                   "score": 0, "contacts": 0}
        rows.append(row)
        print(f"      {row['outcome']}"
              + (f" - {row['detail']}" if row["detail"] else ""))
        time.sleep(PAUSE_BETWEEN)

    if not rows:
        sys.exit("Nothing finished.")

    path, ready = write_sheet(client, rows, started)
    minutes = (datetime.now() - started).total_seconds() / 60

    print(f"\n{len(rows)} done in {minutes:.0f} minutes. "
          f"{len(ready)} ready to read.")
    for r in ready:
        print(f"  {r['company']:<34} messages/{r['domain']}.md")
    print(f"\nReview sheet: {path}")
    print("Nothing was sent. Read before using any of it.")


if __name__ == "__main__":
    main()