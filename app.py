"""
app.py - the whole tool as four steps in one column.

    1. Your company        - type a name
    2. Who you sell to      - the target group, in plain words
    3. Pick a company       - candidates showing your buying signal
    4. Messages             - 3 emails, 3 LinkedIn messages

Everything else the pipeline can do lives in the individual scripts. This page
does the one path people actually want, without checkboxes.
"""

import html
import json
import subprocess
import sys
from pathlib import Path

import streamlit as st

from resolve import to_domain

BASE = Path(__file__).resolve().parent
CLIENTS = BASE / "clients"
CONTEXT = BASE / "context"
TARGETS = BASE / "targets"
MESSAGES = BASE / "messages"
FIT = BASE / "fit"
CONTACTS = BASE / "contacts"

st.set_page_config(page_title="Cold Outreach Agent", layout="centered")


def esc(value):
    return html.escape(str(value or ""))


def run(script, *args):
    return subprocess.run([sys.executable, str(BASE / script),
                           *[str(a) for a in args]],
                          capture_output=True, text=True)


def fail(label, result):
    st.error(label)
    detail = (result.stdout or "").strip() or (result.stderr or "").strip()
    if detail:
        with st.expander("Details"):
            st.code(detail)


st.markdown("""
<style>
  .block-container { padding-top: 2.5rem; max-width: 780px; }
  h1 { font-size: 2.4rem !important; letter-spacing: -0.03em; margin-bottom: .3rem; }
  .sub { color: #8C8C94; font-size: 1rem; margin-bottom: 2rem; }
  .step { font-size: .68rem; letter-spacing: .16em; text-transform: uppercase;
          color: #E8674C; font-weight: 600; margin: 2rem 0 .5rem; }
  .tg { color: #D8D8DC; font-size: 1rem; line-height: 1.7;
        background: #131317; border: 1px solid #22222A; border-radius: 12px;
        padding: 1.1rem 1.3rem; margin-bottom: 1rem; }
  .row { background: #131317; border: 1px solid #22222A; border-radius: 10px;
         padding: .8rem 1rem; margin-bottom: .5rem; }
  .row-name { color: #EDEDED; font-weight: 600; }
  .row-sub { color: #8C8C94; font-size: .85rem; margin-top: .2rem; }
  .msg { background: #131317; border: 1px solid #22222A; border-radius: 12px;
         padding: 1.4rem 1.6rem; margin-bottom: 1rem; }
  .msg.blocked { border-color: rgba(248,113,113,.5); }
  .angle { font-size: .66rem; text-transform: uppercase; letter-spacing: .14em;
           color: #E8674C; font-weight: 600; }
  .subject { font-weight: 600; font-size: 1.05rem; margin: .6rem 0 .8rem; }
  .body { white-space: pre-wrap; line-height: 1.7; color: #C8C8D0; }
  .meta { font-size: .72rem; color: #5E5E68; margin-top: 1rem;
          padding-top: .8rem; border-top: 1px solid #1F1F27; }
  .stop { color: #F87171; font-size: .8rem; margin-top: .8rem;
          padding: .55rem .8rem; border-radius: 8px;
          background: rgba(248,113,113,.07);
          border: 1px solid rgba(248,113,113,.22); }
</style>
""", unsafe_allow_html=True)

st.markdown("# Cold Outreach Agent")
st.markdown('<div class="sub">Type your company. Get the people worth writing '
            'to, and what to write.</div>', unsafe_allow_html=True)

state = st.session_state
state.setdefault("client", None)
state.setdefault("target", None)


def load_client(slug):
    path = CLIENTS / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_client(slug, data):
    (CLIENTS / f"{slug}.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ------------------------------------------------------- 1. your company

st.markdown('<div class="step">1 · Your company</div>', unsafe_allow_html=True)

known = sorted(p.stem for p in CLIENTS.glob("*.json")) if CLIENTS.exists() else []
col1, col2 = st.columns([3, 1])
with col1:
    typed = st.text_input("Your company", value=state.client or "",
                          placeholder="e.g. KPMG", label_visibility="collapsed")
with col2:
    go = st.button("Go", use_container_width=True)

if known:
    st.caption("Already set up: " + ", ".join(known))

if go and typed.strip():
    slug = to_domain(typed.strip(), quiet=True)
    if not slug:
        st.error(f"Could not find a website for {typed.strip()!r}.")
        st.stop()
    if not load_client(slug):
        with st.spinner(f"Reading {slug} to work out who you sell to"):
            result = run("profile.py", slug)
        if result.returncode != 0 or not load_client(slug):
            fail("Could not read that company's site.", result)
            st.stop()
    state.client = slug
    state.target = None
    st.rerun()

if not state.client:
    st.stop()

client = load_client(state.client)
if not client:
    state.client = None
    st.rerun()


# ------------------------------------------------------- 2. who you sell to

st.markdown('<div class="step">2 · Who you sell to</div>', unsafe_allow_html=True)
st.markdown(f"### {client.get('name', state.client)}")

target = (client.get("target_group") or client.get("who_pays_for_it")
          or client.get("who_buys_it") or "Not worked out yet.")
st.markdown(f'<div class="tg">{esc(target)}</div>', unsafe_allow_html=True)

if client.get("sells_to_businesses") is False:
    st.error("This company sells to the public, not to businesses. Cold "
             "outreach needs a person at a company to write to, so nothing "
             "below will find one.")
    if client.get("b2c_note"):
        st.caption(client["b2c_note"])
    st.stop()

icp_path = BASE / str(client.get("icp_file") or "")
if icp_path.exists():
    with st.expander("The full picture — segments, signals, who does not fit"):
        st.markdown(icp_path.read_text(encoding="utf-8"))

# The one thing no tool can work out. Asked once, inline, then never again.
if not client.get("reviewed"):
    st.markdown("**Two things before writing anything**")
    with st.form("review"):
        sender = st.text_input("Who signs the emails",
                               value=client.get("sender", ""))
        implied = client.get("_claims_to_check") or []
        if implied:
            st.caption("Your website implies these. Any that are not true yet "
                       "belong in the box below:")
            for c in implied:
                st.caption(f"• {c}")
        forbidden = st.text_area(
            "Anything these messages must never claim (one per line, "
            "leave blank if nothing)", height=90)
        if st.form_submit_button("Save", use_container_width=True):
            if not sender.strip():
                st.error("A name is needed to sign the emails.")
            else:
                client["sender"] = sender.strip()
                client["cannot_claim"] = [l.strip() for l in forbidden.splitlines()
                                          if l.strip()]
                client["reviewed"] = True
                save_client(state.client, client)
                st.rerun()
    st.stop()


# ------------------------------------------------------- 3. pick a company

st.markdown('<div class="step">3 · Pick a company to write to</div>',
            unsafe_allow_html=True)

cand_path = TARGETS / f"candidates-{state.client}.json"
candidates, cand_meta = [], {}
if cand_path.exists():
    try:
        cand_meta = json.loads(cand_path.read_text(encoding="utf-8"))
        candidates = cand_meta.get("candidates") or []
    except Exception:
        cand_meta, candidates = {}, []

col1, col2 = st.columns([3, 1])
with col1:
    manual = st.text_input("Company to write to", placeholder="e.g. Infosys",
                           label_visibility="collapsed")
with col2:
    if st.button("Write to them", use_container_width=True) and manual.strip():
        resolved = to_domain(manual.strip(), quiet=True)
        if not resolved:
            st.error(f"Could not find a website for {manual.strip()!r}.")
        else:
            state.target = resolved
            st.rerun()

if st.button("Or find companies that buy what you sell",
             use_container_width=True):
    with st.spinner("Searching. This takes a minute."):
        result = run("find_targets.py", state.client)
    if result.returncode != 0:
        fail("The search failed.", result)
    else:
        state.searched = True
    st.rerun()

sectors = (cand_meta or {}).get("sectors") or []
if sectors:
    st.caption("Who buys from you:")
    for s in sectors:
        st.markdown(
            f'<div class="row"><div class="row-name">{esc(s.get("sector", ""))}</div>'
            f'<div class="row-sub">{esc(s.get("why_they_buy", ""))}</div></div>',
            unsafe_allow_html=True)

if not candidates and cand_meta:
    # The search ran and kept nothing. Say why, because an empty page reads as a
    # crash when it is usually an honest result: the signal in the ICP is not
    # visible on the open web, or everything it found was a vendor or a host.
    seen = cand_meta.get("results_seen", 0)
    rejected = cand_meta.get("rejected_count", 0)
    st.warning(f"**Searched {seen} results and kept none.** "
               f"{rejected} were thrown out.")
    reasons = cand_meta.get("rejection_reasons") or {}
    if reasons:
        st.caption("Why:")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            st.caption(f"• {reason}: {count}")
    queries = cand_meta.get("queries") or []
    if queries:
        with st.expander("What it searched for"):
            for q in queries:
                st.code(q)
    st.caption("This usually means the buying signal in your ICP is not "
               "visible on the open web. Type a company name in the box above "
               "instead — that path does not need the search.")

if candidates:
    st.caption(f"{len(candidates)} companies in those sectors. Being here means "
               f"they are in a buying sector, not that they are a good fit.")
    for i, c in enumerate(candidates[:20]):
        left, right = st.columns([4, 1])
        with left:
            detail = c.get("why") or c.get("sector") or ""
            st.markdown(
                f'<div class="row"><div class="row-name">{esc(c["company"])}</div>'
                f'<div class="row-sub">{esc(c.get("domain") or "no website found")}'
                f'{" — " + esc(detail) if detail else ""}</div></div>',
                unsafe_allow_html=True)
        with right:
            if c.get("domain") and st.button("Write", key=f"pick{i}",
                                             use_container_width=True):
                state.target = c["domain"]
                st.rerun()

if not state.target:
    st.stop()


# ------------------------------------------------------- 4. the messages

st.markdown('<div class="step">4 · Messages</div>', unsafe_allow_html=True)
target_domain = state.target
msg_path = MESSAGES / f"{target_domain}.json"

data = None
if msg_path.exists():
    try:
        loaded = json.loads(msg_path.read_text(encoding="utf-8"))
        if loaded.get("client_domain") == state.client:
            data = loaded
    except Exception:
        data = None

if data is None:
    steps = [("Checking whether they are a fit", "check_fit.py",
              [state.client, target_domain]),
             ("Reading their site", "scrape.py", [target_domain]),
             ("Looking for recent news", "news.py", [target_domain]),
             ("Working out what is worth writing about", "triggers.py",
              [state.client, target_domain]),
             ("Writing the messages", "generate.py",
              [state.client, target_domain])]

    for label, script, args in steps:
        with st.spinner(label):
            result = run(script, *args)

        if script == "check_fit.py" and result.returncode in (1, 2):
            fit_path = FIT / f"{target_domain}.json"
            reason = ""
            try:
                reason = (json.loads(fit_path.read_text(encoding="utf-8"))
                          .get("result", {}).get("reason", ""))
            except Exception:
                pass

            # Neither SKIP nor UNCLEAR stops the run. The verdict is shown as
            # a caution and the messages get written anyway - a fit check is a
            # judgement about a company's public face, and you may know
            # something it does not. It is advice, not a gate.
            if result.returncode == 1:
                st.warning(f"**The fit check says no.** {reason}")
                st.caption("Writing the messages anyway, since you asked for "
                           "this company. Read them with that verdict in mind.")
            else:
                st.info(f"**Not an obvious match.** {reason}")
                st.caption("The ICP did not name this kind of company, but the "
                           "need looks plausible. Carrying on.")
            continue

        if script == "news.py":
            continue  # news is optional; a failure here is not fatal

        if script == "triggers.py" and result.returncode != 0:
            # Usually the scrape came back empty - a JS-only site, or one that
            # blocks scrapers. That is a thin company to write to, not a reason
            # to abandon the run: generate.py can still write from whatever is
            # known, and it labels the result as having no hook.
            st.warning("**Could not read enough about them to find a hook.** "
                       "Their site gave up little or nothing. Writing anyway, "
                       "with no trigger.")
            with st.expander("Why"):
                st.code((result.stdout or result.stderr or "").strip())
            (BASE / "triggers").mkdir(parents=True, exist_ok=True)
            (BASE / "triggers" / f"{target_domain}.json").write_text(
                json.dumps({"company_name": target_domain, "triggers": [],
                            "disqualifiers": [], "pain_hypothesis": "",
                            "no_trigger_found": True},
                           indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            continue

        if script == "scrape.py" and result.returncode != 0:
            st.warning("**Could not read their website.** Writing from what "
                       "little is known.")
            continue

        if result.returncode != 0:
            if script == "generate.py":
                out = (result.stdout or "").strip()
                err = (result.stderr or "").strip()
                # A refusal prints to stdout and says why. A crash prints a
                # traceback to stderr and nothing to stdout. Calling both
                # "crashed" made a working quality gate look broken.
                refusal = out or err
                if "below the floor" in refusal or "CANNOT WRITE" in refusal:
                    st.warning("**Stopped before writing.**")
                    st.code(refusal)
                elif out:
                    st.warning("**Nothing worth writing was found.**")
                    st.code(out)
                else:
                    st.error("**generate.py crashed.**")
                if err:
                    with st.expander("Error detail", expanded=not out):
                        st.code(err)
            else:
                fail(f"{label} failed.", result)
            state.target = None
            st.stop()

    try:
        data = json.loads(msg_path.read_text(encoding="utf-8"))
    except Exception:
        st.error("The messages were written but could not be read back.")
        st.stop()

st.markdown(f"### {client.get('name')} → {data.get('company_name', target_domain)}")

# Who to send it to. The messages are useless without a recipient, and hunting
# LinkedIn by hand is the slowest step in the loop.
contact_path = CONTACTS / f"{target_domain}.json"
contacts = {}
if contact_path.exists():
    try:
        contacts = json.loads(contact_path.read_text(encoding="utf-8"))
    except Exception:
        contacts = {}

if not contacts:
    if st.button("Find who to send this to", use_container_width=True):
        with st.spinner("Looking for the right people"):
            found = run("find_contacts.py", state.client, target_domain)
        if found.returncode != 0:
            fail("Could not look for contacts.", found)
        st.rerun()
else:
    people = contacts.get("people") or []
    with st.expander(f"Who to send it to · {len(people)} found", expanded=True):
        if contacts.get("why"):
            st.caption(contacts["why"])
        for person in people:
            st.markdown(
                f'<div class="row"><div class="row-name">{esc(person["name"])}</div>'
                f'<div class="row-sub">{esc(person["role"])}</div></div>',
                unsafe_allow_html=True)
            st.caption(person["linkedin"])
        if not people:
            st.caption("Nobody matched those titles publicly. Search LinkedIn "
                       "for the company and scan the People tab.")
        email = contacts.get("email") or {}
        if email.get("pattern"):
            st.caption(f"Addresses here look like **{email['pattern']}"
                       f"@{contacts.get('domain')}** — seen in "
                       f"{', '.join(email.get('examples', [])[:2])}. Check one "
                       f"before sending; a bounce hurts your domain.")
        else:
            st.caption("No public email addresses found. LinkedIn message "
                       "instead, or use their contact form.")

info = data.get("generated_from") or {}
if info.get("pain_hypothesis"):
    st.info(info["pain_hypothesis"])

weak = [w for w in (data.get("warnings") or []) if w.startswith("WEAK TRIGGERS")]
if weak:
    st.warning(weak[0])

if data.get("no_trigger"):
    st.warning("**No hook found.** Nothing dated or newsworthy turned up for "
               "this company, so these messages lean on what their own site "
               "says. They are honest, but they will convert far worse than "
               "messages built on a real trigger.")

for d in data.get("disqualifiers") or []:
    st.error(f"Do not send. {d.get('fact', '')} {d.get('why', '')}".strip())


def blocking(message):
    out = []
    if message.get("overclaim"):
        out.append(f"claims something you said you may never claim: "
                   f"{message['overclaim']}")
    if message.get("asset_claim"):
        out.append(f"offers something that does not exist: {message['asset_claim']}")
    return out


def show(angle, subject, body, meta, message):
    stops = blocking(message)
    subj = f'<div class="subject">{esc(subject)}</div>' if subject else ""
    stop_html = "".join(f'<div class="stop">Do not send — {esc(s)}</div>'
                        for s in stops)
    st.markdown(
        f'<div class="msg{" blocked" if stops else ""}">'
        f'<span class="angle">{esc(angle)}</span>{subj}'
        f'<div class="body">{esc(body)}</div>{stop_html}'
        f'<div class="meta">{esc(meta)}</div></div>', unsafe_allow_html=True)


tab1, tab2 = st.tabs(["Emails", "LinkedIn"])
with tab1:
    for e in data.get("emails") or []:
        show(e.get("angle", ""), e.get("subject", ""), e.get("body", ""),
             f"{e.get('word_count', 0)} words · {e.get('trigger_link', '')}", e)
with tab2:
    for m in data.get("linkedin_messages") or []:
        show(m.get("angle", ""), "", m.get("text", ""),
             f"{m.get('char_count', 0)} characters · {m.get('trigger_link', '')}", m)

warnings = data.get("warnings") or []
if warnings:
    with st.expander(f"{len(warnings)} thing(s) to check before sending"):
        for w in warnings:
            st.caption(f"• {w}")

if st.button("Write to a different company"):
    state.target = None
    st.rerun()