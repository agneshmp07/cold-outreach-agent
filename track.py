"""
track.py - what you sent, and what came back.

    python track.py sent mngo.in happiestminds.com --angle trigger-direct \\
                    --to "Priya Sharma"
    python track.py reply happiestminds.com --kind positive --note "asked for a call"
    python track.py stats mngo.in
    python track.py list

Everything the pipeline does is a guess until this file has rows in it. The
score in score.py predicts which messages are good; this records which ones got
answered. Twenty rows and you can check whether the prediction holds.

Kept as one JSONL file - append-only, one line per event, readable in any
editor. No database, nothing to migrate, and a file you can open and correct by
hand when you inevitably log something wrong.

Output:
    sent.jsonl
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "sent.jsonl"
MESSAGES_DIR = BASE_DIR / "messages"

REPLY_KINDS = ("positive", "negative", "neutral", "bounced", "none")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_log():
    if not LOG_PATH.exists():
        return []
    rows = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append(event):
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def message_facts(domain, angle):
    """
    Pull the facts that might explain a reply, from the draft itself.

    Logged at send time rather than looked up later, because the messages file
    gets overwritten the next time you run that company - and then the row says
    a message got a reply without recording what the message was like.
    """
    path = MESSAGES_DIR / f"{domain}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    found = None
    for message in (data.get("emails") or []) + (data.get("linkedin_messages") or []):
        if not angle or message.get("angle") == angle:
            found = message
            break

    facts = {
        "client": data.get("client", ""),
        "company": data.get("company_name", domain),
        "top_trigger": (data.get("generated_from") or {}).get("top_score", 0),
        "no_trigger": bool(data.get("no_trigger")),
        "batch_score": data.get("score"),
    }
    if found:
        facts.update({
            "angle": found.get("angle", angle),
            "score": found.get("score"),
            "grounded": bool(found.get("grounded")),
            "words": found.get("word_count") or found.get("char_count"),
        })
    return facts


# ------------------------------------------------------------------ commands

def cmd_sent(args):
    facts = message_facts(args.target, args.angle)
    event = {"type": "sent", "at": now(), "client": args.client,
             "domain": args.target, "angle": args.angle or "",
             "to": args.to or "", "channel": args.channel, **facts}
    append(event)
    print(f"Logged: {event.get('company', args.target)} "
          f"({args.angle or 'angle not given'})"
          + (f" to {args.to}" if args.to else ""))
    if facts.get("score") is not None:
        print(f"  draft scored {facts['score']}/100, "
              f"trigger {facts.get('top_trigger', '?')}/10")
    print(f"  {LOG_PATH}")


def cmd_reply(args):
    if args.kind not in REPLY_KINDS:
        sys.exit(f"--kind must be one of: {', '.join(REPLY_KINDS)}")
    rows = [r for r in read_log()
            if r.get("type") == "sent" and r.get("domain") == args.target]
    if not rows:
        print(f"Nothing logged as sent to {args.target}. Recording anyway.")
    append({"type": "reply", "at": now(), "domain": args.target,
            "kind": args.kind, "note": args.note or "",
            "angle": rows[-1].get("angle", "") if rows else "",
            "score": rows[-1].get("score") if rows else None,
            "top_trigger": rows[-1].get("top_trigger") if rows else None})
    print(f"Logged a {args.kind} reply from {args.target}.")


def cmd_list(args):
    rows = read_log()
    if not rows:
        sys.exit("Nothing logged yet.")
    replies = defaultdict(list)
    for r in rows:
        if r.get("type") == "reply":
            replies[r["domain"]].append(r)

    for r in rows:
        if r.get("type") != "sent":
            continue
        got = replies.get(r["domain"], [])
        outcome = got[-1]["kind"] if got else "waiting"
        print(f"{r['at'][:10]}  {r.get('company', r['domain'])[:28]:<28} "
              f"{r.get('angle', '')[:16]:<16} "
              f"score {str(r.get('score', '-')):>4}  {outcome}")


def cmd_stats(args):
    rows = read_log()
    sent = [r for r in rows if r.get("type") == "sent"
            and (not args.client or r.get("client_domain") == args.client
                 or r.get("client") == args.client)]
    if not sent:
        sys.exit("Nothing sent yet. Nothing to learn from.")

    replies = {}
    for r in rows:
        if r.get("type") == "reply":
            replies[r["domain"]] = r

    answered = [r for r in sent if replies.get(r["domain"], {}).get("kind")
                not in (None, "none", "bounced")]

    print(f"\n{len(sent)} sent, {len(answered)} answered "
          f"({len(answered) / len(sent) * 100:.0f}%)\n")

    if len(sent) < 10:
        print("  Too few to mean much yet. Ten is where patterns start to show,")
        print("  twenty is where you can act on them.\n")

    # by angle
    by_angle = defaultdict(lambda: [0, 0])
    for r in sent:
        key = r.get("angle") or "not recorded"
        by_angle[key][0] += 1
        if r in answered:
            by_angle[key][1] += 1
    print("  By angle:")
    for angle, (total, got) in sorted(by_angle.items(),
                                      key=lambda kv: -kv[1][1]):
        print(f"    {angle:<22} {got}/{total}")

    # does the score predict anything
    scored = [r for r in sent if isinstance(r.get("score"), int)]
    if scored:
        hi = [r for r in scored if r["score"] >= 60]
        lo = [r for r in scored if r["score"] < 60]
        hi_rate = sum(1 for r in hi if r in answered) / len(hi) * 100 if hi else 0
        lo_rate = sum(1 for r in lo if r in answered) / len(lo) * 100 if lo else 0
        print(f"\n  Scored 60+ : {len(hi)} sent, {hi_rate:.0f}% answered")
        print(f"  Scored <60 : {len(lo)} sent, {lo_rate:.0f}% answered")
        if len(scored) >= 10 and hi and lo:
            if hi_rate > lo_rate + 10:
                print("  The score is predicting something. Trust it more.")
            elif lo_rate > hi_rate + 10:
                print("  Lower-scoring messages are doing better. The scoring "
                      "rules are wrong - look at what the good ones did.")
            else:
                print("  No difference yet. The score is not earning its place.")

    # with a hook versus without
    hooked = [r for r in sent if not r.get("no_trigger")]
    bare = [r for r in sent if r.get("no_trigger")]
    if hooked and bare:
        h = sum(1 for r in hooked if r in answered) / len(hooked) * 100
        b = sum(1 for r in bare if r in answered) / len(bare) * 100
        print(f"\n  With a trigger    : {len(hooked)} sent, {h:.0f}% answered")
        print(f"  Without a trigger : {len(bare)} sent, {b:.0f}% answered")

    kinds = Counter(replies[d]["kind"] for d in replies)
    if kinds:
        print("\n  Replies: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
    print()


def main():
    p = argparse.ArgumentParser(description="Track what was sent and what came back.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sent", help="log a message you sent")
    s.add_argument("client")
    s.add_argument("target")
    s.add_argument("--angle", help="which draft, e.g. trigger-direct")
    s.add_argument("--to", help="who you sent it to")
    s.add_argument("--channel", default="email", choices=("email", "linkedin"))
    s.set_defaults(func=cmd_sent)

    r = sub.add_parser("reply", help="log what came back")
    r.add_argument("target")
    r.add_argument("--kind", default="positive",
                   help=f"one of: {', '.join(REPLY_KINDS)}")
    r.add_argument("--note")
    r.set_defaults(func=cmd_reply)

    l = sub.add_parser("list", help="everything logged")
    l.set_defaults(func=cmd_list)

    t = sub.add_parser("stats", help="what is working")
    t.add_argument("client", nargs="?")
    t.set_defaults(func=cmd_stats)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()