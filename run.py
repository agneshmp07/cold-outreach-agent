"""
run.py - one command for the whole pipeline.

    python run.py mngo.in
    python run.py mngo.in happiestminds.com

Give it YOUR domain. It works out who you are and who buys from you, finds
companies showing your buying signal, qualifies one against your ICP, researches
it, and writes 3 emails + 3 LinkedIn messages. Give it a target domain too and it
skips the target-finding step.

Everything it does is one of the smaller scripts, run in order:

    profile.py       who you are, who buys from you        (once per client)
    find_targets.py  companies showing the signal          (skipped if you name one)
    check_fit.py     is this target actually a fit         (can stop the run)
    scrape.py        read the target's site
    news.py          dated headlines about them
    triggers.py      pull out verifiable facts, ranked
    generate.py      write the messages, then check them

Any of those can be run on its own. This just saves typing them out.

The run STOPS rather than degrading when a step has nothing real to work with:
a SKIP verdict, no trigger, or a top trigger below the quality floor. That is
deliberate. A pipeline that always produces six emails is a pipeline that will
happily produce six bad ones.
"""

import json
import os
import subprocess
import sys
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

RULE = "=" * 64


def say(message):
    print(f"\n{RULE}\n{message}\n{RULE}")


def clean_domain(arg):
    """Accept a company name or a domain. Names are resolved with one search."""
    domain = to_domain(arg)
    if not domain:
        sys.exit(f"Could not work out a domain from {arg!r}. "
                 f"Try typing it directly, like  example.com")
    return domain


def step(script, *args, allow_codes=(0,)):
    """Run one script. Returns its exit code. Never hides its output."""
    path = BASE_DIR / script
    if not path.exists():
        sys.exit(f"Missing {script}. It should sit next to run.py.")
    result = subprocess.run([sys.executable, str(path), *[str(a) for a in args]])
    if result.returncode not in allow_codes:
        return result.returncode
    return result.returncode


# ------------------------------------------------------------------ the client

def ask_review(client_path):
    """
    Fill in the two things no tool can work out, without making anyone edit JSON.

    profile.py can read a website and say what a company sells. It cannot say
    what that company is not allowed to claim, because a marketing site implies
    traction the company may not have. Asking here, once, is the difference
    between a tool and a liability.
    """
    try:
        client = json.loads(client_path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.exit(f"Could not read {client_path}: {exc}")

    say(f"One-time setup for {client.get('name', client_path.stem)}")

    print("\nWhat the tool read from your site:")
    print(f"  Sells  : {client.get('what_you_sell', '-')}")
    print(f"  Buyers : {client.get('who_buys_it', '-')}")
    print(f"\n  One-liner: {client.get('one_liner', '-')}")

    implied = client.get("_claims_to_check") or []
    if implied:
        print("\nYour website implies these. Any that are not true yet should go")
        print("in the list below, so the tool never writes them:")
        for c in implied:
            print(f"  - {c}")

    sender = ""
    while not sender:
        sender = input("\nName that signs the emails: ").strip()

    print("\nNow the important one. What must these messages NEVER claim?")
    print("Things that are not true yet: a customer count, a partner network,")
    print("results you have not measured. One per line. Blank line when done.")
    print("If there is genuinely nothing, press Enter straight away.\n")

    cannot_claim = []
    while True:
        line = input("  never claim: ").strip()
        if not line:
            break
        cannot_claim.append(line)

    if not cannot_claim:
        print("\n  Nothing listed. The tool will still refuse to invent facts,")
        print("  numbers or customers - but it cannot catch an overstatement")
        print("  about your own traction if you have not told it what that is.")

    client["sender"] = sender
    client["cannot_claim"] = cannot_claim
    client["reviewed"] = True

    try:
        client_path.write_text(json.dumps(client, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
    except OSError as exc:
        sys.exit(f"Could not write {client_path}: {exc}")

    print(f"\nSaved to {client_path}")
    icp = client.get("icp_file")
    if icp:
        print(f"Your draft ICP is at {icp} - worth reading and cutting before "
              f"you trust it.")


def ensure_client(client_domain):
    client_path = CLIENTS_DIR / f"{client_domain}.json"

    if not client_path.exists():
        say(f"Step 1 of 6 - reading {client_domain} to work out who buys from you")
        code = step("profile.py", client_domain)
        if code != 0 or not client_path.exists():
            sys.exit("\nCould not build a profile. Check the domain and try again.")
    else:
        print(f"Using the existing profile at {client_path}")

    try:
        client = json.loads(client_path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.exit(f"Could not read {client_path}: {exc}")

    if not client.get("reviewed"):
        ask_review(client_path)
        client = json.loads(client_path.read_text(encoding="utf-8"))

    return client


# ----------------------------------------------------------------- the target

def choose_target(client_domain):
    """Run the target finder and let the human pick from what it found."""
    say("Step 2 of 6 - finding companies that show your buying signal")
    step("find_targets.py", client_domain, "--resolve")

    path = TARGETS_DIR / f"candidates-{client_domain}.json"
    if not path.exists():
        sys.exit("\nNo candidates file was written. Name a target domain "
                 "yourself:  python run.py <your-domain> <their-domain>")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.exit(f"Could not read {path}: {exc}")

    candidates = [c for c in data.get("candidates", []) if c.get("domain")]
    if not candidates:
        sys.exit("\nNo candidate had a resolvable domain. The searches in "
                 "find_targets.py may need rewording for your market, or you can "
                 "name a target yourself:\n"
                 "  python run.py <your-domain> <their-domain>")

    say("Pick one")
    print("\nA score means the signal is present, not that the company fits your")
    print("ICP. Read the evidence before picking.\n")
    for i, c in enumerate(candidates[:10], start=1):
        evidence = "; ".join(c.get("evidence", []))
        print(f"  {i:>2}. {c['company']}  ({c['domain']})  {c.get('score', 0)}/11")
        if evidence:
            print(f"      {evidence}")

    while True:
        raw = input("\nNumber, or a domain to use instead: ").strip()
        if not raw:
            continue
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= min(10, len(candidates)):
                return candidates[index - 1]["domain"]
            print("  Out of range.")
            continue
        return clean_domain(raw)


# ------------------------------------------------------------------------ main

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv[1:]

    if not args or len(args) > 2:
        sys.exit("Usage: python run.py <your-domain> [their-domain] [--force]\n"
                 "  python run.py mngo.in\n"
                 "  python run.py mngo.in happiestminds.com")

    for key in ("GEMINI_API_KEY", "SERPER_API_KEY"):
        if not os.environ.get(key, "").strip():
            sys.exit(f"{key} is not set. Put it in your .env next to run.py.")

    client_domain = clean_domain(args[0])
    client = ensure_client(client_domain)

    target = (clean_domain(args[1]) if len(args) == 2
          else choose_target(client_domain))

    say(f"Step 3 of 6 - is {target} actually a fit for {client['name']}?")
    verdict_code = step("check_fit.py", client_domain, target,
                        allow_codes=(0, 1, 2))

    if verdict_code == 1 and not force:
        print("\nStopping here. That company is not visibly a buyer, so anything")
        print("written to them would be a guess dressed up as research.")
        print(f"Run another:  python run.py {client_domain}")
        print(f"Override:     python run.py {client_domain} {target} --force")
        sys.exit(1)
    if verdict_code == 2 and not force:
        print("\nStopping here. Not enough public information to judge the fit.")
        print("Check it by hand, or try a different company.")
        print(f"Override:     python run.py {client_domain} {target} --force")
        sys.exit(2)
    if verdict_code not in (0, 1, 2):
        sys.exit("\ncheck_fit.py failed. See the error above.")

    say(f"Step 4 of 6 - reading {target}")
    if step("scrape.py", target) != 0:
        sys.exit("\nThe scrape failed. Check the domain resolves.")
    step("news.py", target)

    say(f"Step 5 of 6 - pulling out what is worth writing about")
    if step("triggers.py", client_domain, target) != 0:
        sys.exit("\ntriggers.py failed. See the error above.")

    say(f"Step 6 of 6 - writing the messages")
    generate_args = [client_domain, target]
    if force:
        generate_args.append("--force")
    code = step("generate.py", *generate_args, allow_codes=(0, 1))

    if code != 0:
        print("\nNo messages were written. The reason is above - usually no "
              "trigger, or triggers too weak to be worth sending on.")
        print(f"Try another company:  python run.py {client_domain}")
        sys.exit(code)

    md_path = MESSAGES_DIR / f"{target}.md"
    say("Done")
    print(f"\n  {md_path}\n")
    print("  Read it before sending anything. Look for three things:")
    print("    - any DO NOT SEND flag (a claim you cannot back up)")
    print("    - whether each message's 'why it connects' line is real")
    print("    - whether you would say every sentence out loud to a stranger")
    print("\n  Nothing was sent. This tool only writes files.")


if __name__ == "__main__":
    main()