import html
import json
import subprocess
import sys
import streamlit as st
from pathlib import Path

BASE = Path(__file__).resolve().parent
MESSAGES = BASE / "messages"
TARGETS = BASE / "targets"

st.set_page_config(page_title="Cold Outreach Agent", layout="wide",
                   initial_sidebar_state="expanded")


def esc(value):
    """Escape anything model-written before it goes into raw HTML."""
    return html.escape(str(value or ""))


st.markdown("""
<style>
  .block-container { padding-top: 2rem; max-width: 1100px; }

  @keyframes rise {
    from { opacity: 0; transform: translateY(14px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  @keyframes sweep {
    0%   { background-position: 0% 50%; }
    100% { background-position: 100% 50%; }
  }
  @keyframes fadein { from { opacity: 0; } to { opacity: 1; } }

  /* ---------------------------------------------------------- hero */
  .hero { padding: 3.5rem 0 2.5rem; border-bottom: 1px solid #1F1F25;
          margin-bottom: 2.5rem; animation: rise .5s ease-out both; }
  .eyebrow { font-size: .7rem; letter-spacing: .18em; text-transform: uppercase;
             color: #E8674C; font-weight: 600; margin-bottom: 1rem; }
  .hero h1 { font-size: 3.4rem; line-height: 1.05; font-weight: 700;
             letter-spacing: -0.035em; margin: 0 0 1.25rem;
             background: linear-gradient(100deg, #FFF 20%, #E8674C 50%, #FFF 80%);
             background-size: 200% auto;
             -webkit-background-clip: text; -webkit-text-fill-color: transparent;
             animation: sweep 6s linear infinite alternate; }
  .lede { color: #8C8C94; font-size: 1.1rem; line-height: 1.65; max-width: 640px;
          animation: rise .6s .1s ease-out both; }
  .lede strong { color: #D8D8DC; font-weight: 500; }

  /* -------------------------------------------------------- metrics */
  .stats { display: flex; gap: 1rem; margin: 2rem 0 2.5rem;
           animation: rise .6s .2s ease-out both; }
  .stat { flex: 1; background: #131317; border: 1px solid #22222A;
          border-radius: 12px; padding: 1.25rem 1.5rem;
          transition: border-color .25s ease, transform .25s ease; }
  .stat:hover { border-color: #33333D; transform: translateY(-2px); }
  .stat-label { font-size: .7rem; letter-spacing: .12em; text-transform: uppercase;
                color: #6E6E76; margin-bottom: .5rem; }
  .stat-value { font-size: 2.1rem; font-weight: 700; letter-spacing: -0.03em;
                color: #EDEDED; line-height: 1; }
  .stat-value.accent { color: #E8674C; }
  .stat-value.danger { color: #F87171; }

  /* ---------------------------------------------------------- cards */
  .msg { background: #131317; border: 1px solid #22222A; border-radius: 12px;
         padding: 1.5rem 1.75rem; margin-bottom: 1rem;
         animation: rise .45s ease-out both;
         transition: border-color .25s ease, transform .25s ease,
                     box-shadow .25s ease; }
  .msg:hover { border-color: #3A3A46; transform: translateY(-3px);
               box-shadow: 0 12px 32px rgba(0,0,0,.4); }
  .msg.blocked { border-color: rgba(248,113,113,.45);
                 background: linear-gradient(#17121400, #17101200), #131317; }
  .msg:nth-of-type(1) { animation-delay: .04s; }
  .msg:nth-of-type(2) { animation-delay: .1s; }
  .msg:nth-of-type(3) { animation-delay: .16s; }

  .msg-head { display: flex; justify-content: space-between;
              align-items: baseline; margin-bottom: 1rem; gap: .5rem; }
  .angle { font-size: .68rem; text-transform: uppercase; letter-spacing: .14em;
           color: #E8674C; font-weight: 600; }
  .badges { display: flex; gap: .4rem; flex-wrap: wrap; justify-content: flex-end; }
  .badge { font-size: .68rem; letter-spacing: .08em; padding: .2rem .6rem;
           border-radius: 20px; font-weight: 600; white-space: nowrap; }
  .ok  { color: #4ADE80; background: rgba(74,222,128,.08);
         border: 1px solid rgba(74,222,128,.2); }
  .bad { color: #F87171; background: rgba(248,113,113,.08);
         border: 1px solid rgba(248,113,113,.2); }
  .stop { color: #FFF; background: #B91C1C; border: 1px solid #F87171; }

  .subject { font-weight: 600; font-size: 1.1rem; margin-bottom: 1rem;
             letter-spacing: -0.01em; }
  .body { white-space: pre-wrap; line-height: 1.7; color: #C8C8D0;
          font-size: .95rem; }
  .meta { font-size: .72rem; color: #5E5E68; margin-top: 1.25rem;
          padding-top: .9rem; border-top: 1px solid #1F1F27; }
  .link-line { font-size: .78rem; color: #8C8C94; margin-top: .9rem;
               padding-left: .75rem; border-left: 2px solid #33333D; }
  .link-line .lbl { color: #5E5E68; text-transform: uppercase;
                    letter-spacing: .1em; font-size: .66rem; }
  .stop-line { font-size: .8rem; color: #F87171; margin-top: .9rem;
               padding: .6rem .8rem; border-radius: 8px;
               background: rgba(248,113,113,.07);
               border: 1px solid rgba(248,113,113,.22); }

  .note-block { background: rgba(232,103,76,.06);
                border: 1px solid rgba(232,103,76,.25);
                border-radius: 10px; padding: 1rem 1.25rem;
                color: #D8A090; font-size: .9rem; line-height: 1.6;
                margin-bottom: 1.5rem; animation: fadein .5s ease-out both; }

  .src { background: #101014; border-left: 2px solid #E8674C;
         border-radius: 0 8px 8px 0; padding: .9rem 1.1rem;
         margin-bottom: .75rem; }
  .src-fact { color: #D8D8DC; font-size: .92rem; line-height: 1.55;
              margin-bottom: .5rem; }
  .src-meta { font-size: .72rem; color: #5E5E68; }
  .score { display: inline-block; font-size: .68rem; font-weight: 600;
           color: #E8674C; background: rgba(232,103,76,.1);
           border: 1px solid rgba(232,103,76,.25);
           border-radius: 20px; padding: .1rem .5rem; margin-right: .5rem; }

  .person { background: #101014; border-left: 2px solid #4A5568;
            border-radius: 0 8px 8px 0; padding: .8rem 1.1rem;
            margin-bottom: .6rem; }
  .person-name { color: #D8D8DC; font-weight: 600; font-size: .95rem; }
  .person-role { color: #8C8C94; font-size: .85rem; margin-top: .2rem; }

  .cand { background: #101014; border-left: 2px solid #4ADE80;
          border-radius: 0 8px 8px 0; padding: .8rem 1.1rem;
          margin-bottom: .6rem; }
  .cand-name { color: #D8D8DC; font-weight: 600; font-size: .95rem; }
  .cand-sig { color: #8C8C94; font-size: .82rem; margin-top: .25rem; }

  .null-result { color: #7A7A84; font-size: .88rem; line-height: 1.65;
                 border-left: 2px solid #33333D; padding-left: 1rem; }

  section[data-testid="stSidebar"] { border-right: 1px solid #1F1F25; }
</style>
""", unsafe_allow_html=True)


def blocking_reasons(message):
    """
    Reasons this message must not be sent, as opposed to merely reviewed.

    An overclaim about colleges MNGO does not have, or an offer of a document
    that does not exist, is not a quality note - it is a commitment you cannot
    honour. These used to be written into the JSON and never shown here, which
    meant the demo could display a fabricated claim under a green badge.
    """
    reasons = []
    if message.get("overclaim"):
        reasons.append(f"claims a college network that does not exist: "
                       f"\u201c{message['overclaim']}\u201d")
    if message.get("asset_claim"):
        reasons.append(f"offers something that has not been made: "
                       f"\u201c{message['asset_claim']}\u201d")
    return reasons


def card(angle, subject, body, meta_bits, message):
    blocked = blocking_reasons(message)

    badges = []
    if blocked:
        badges.append('<span class="badge stop">DO NOT SEND</span>')
    if message.get("invented_number"):
        badges.append('<span class="badge bad">invented number</span>')
    if message.get("grounded"):
        badges.append('<span class="badge ok">grounded</span>')
    else:
        badges.append('<span class="badge bad">ungrounded</span>')

    subj = f'<div class="subject">{esc(subject)}</div>' if subject else ""

    stop_html = ""
    for reason in blocked:
        stop_html += f'<div class="stop-line">{esc(reason)}</div>'

    link = message.get("trigger_link")
    link_html = (f'<div class="link-line"><span class="lbl">Link to campus '
                 f'hiring</span><br>{esc(link)}</div>') if link else ""

    st.markdown(
        f'<div class="msg{" blocked" if blocked else ""}"><div class="msg-head">'
        f'<span class="angle">{esc(angle)}</span>'
        f'<span class="badges">{"".join(badges)}</span></div>'
        f'{subj}<div class="body">{esc(body)}</div>'
        f'{stop_html}{link_html}'
        f'<div class="meta">{esc(" · ".join(meta_bits))}</div></div>',
        unsafe_allow_html=True)


# ------------------------------------------------------------------ sidebar

with st.sidebar:
    st.markdown("### Find companies")
    st.caption("Searches for the buying signal in context/icp.md and returns "
               "companies that show it. About 7 searches, plus one per company "
               "when resolving domains.")
    resolve = st.checkbox("Resolve domains", value=True)
    if st.button("Find targets", use_container_width=True):
        cmd = [sys.executable, str(BASE / "find_targets.py")]
        if resolve:
            cmd.append("--resolve")
        with st.spinner("Searching for the signal"):
            p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0:
            st.success("Done")
        else:
            st.error("find_targets.py failed")
        if p.stdout.strip():
            with st.expander("Output"):
                st.code(p.stdout.strip())
        if p.stderr.strip():
            with st.expander("Errors"):
                st.code(p.stderr.strip())

    st.divider()
    st.markdown("### Run a company")
    new_domain = st.text_input("Domain", placeholder="example.com",
                               label_visibility="collapsed")
    use_news = st.checkbox("Include news search", value=True)
    use_linkedin = st.checkbox("Include LinkedIn scan", value=False,
                               help="Costs about $0.10 per company and rarely "
                                    "finds anyone. See the LinkedIn panel.")

    if st.button("Run pipeline", use_container_width=True) and new_domain.strip():
        d = new_domain.strip()
        steps = [("Scraping site", "scrape.py")]
        if use_news:
            steps.append(("Searching news", "news.py"))
        if use_linkedin:
            steps.append(("Scanning LinkedIn", "linkedin.py"))
        steps += [("Extracting triggers", "triggers.py"),
                  ("Writing messages", "generate.py")]

        halted = False
        for label, script in steps:
            with st.spinner(label):
                p = subprocess.run([sys.executable, str(BASE / script), d],
                                   capture_output=True, text=True)
            if p.returncode != 0:
                # generate.py exits non-zero on purpose when there is no usable
                # trigger, or when the top trigger is below the quality floor.
                # That refusal text is the most useful thing the pipeline
                # produces on a bad company, so it gets shown, not swallowed.
                output = (p.stdout or "").strip()
                errors = (p.stderr or "").strip()
                if script == "generate.py":
                    st.warning(f"{label}: nothing was written.")
                else:
                    st.error(f"{label} failed")
                if output:
                    st.code(output)
                if errors:
                    with st.expander("stderr"):
                        st.code(errors)
                halted = True
                break
        if not halted:
            st.success("Done")
            st.rerun()

    st.caption("3 Gemini calls (triggers, messages, proofread) + 1 news search "
               "per company.")


# --------------------------------------------------------------------- hero

st.markdown("""
<div class="hero">
  <div class="eyebrow">Trigger-grounded outreach</div>
  <h1>Cold Outreach Agent</h1>
  <div class="lede">
    Finds companies showing a <strong>visible buying signal</strong>, reads
    their site and recent news, extracts <strong>verifiable triggers</strong>,
    and writes three emails and three LinkedIn messages built on them. Every
    claim is checked back against what was actually found. When nothing
    verifiable turns up, it writes nothing and says why.
  </div>
</div>
""", unsafe_allow_html=True)


# ------------------------------------------------------------- target panel

candidates_file = TARGETS / "candidates.json"
if candidates_file.exists():
    try:
        cdata = json.loads(candidates_file.read_text(encoding="utf-8"))
    except Exception:
        cdata = {}
    cands = cdata.get("candidates", [])
    if cands:
        with st.expander(f"Sourced targets · {len(cands)} company(ies) showing "
                         f"the signal"):
            st.caption(cdata.get("signal", ""))
            seen = cdata.get("results_seen", 0)
            rejected = cdata.get("rejected_count", 0)
            if seen:
                st.caption(f"{rejected} of {seen} search results rejected "
                           f"(vendors, agencies, no signal).")
            for c in cands:
                sig = "; ".join(c.get("evidence", []))
                st.markdown(
                    f'<div class="cand">'
                    f'<div class="cand-name">{esc(c["company"])} '
                    f'<span class="score">{c.get("score", 0)}/11</span></div>'
                    f'<div class="cand-sig">{esc(c.get("domain") or "domain not resolved")}'
                    f'{" — " + esc(sig) if sig else ""}</div>'
                    f'</div>', unsafe_allow_html=True)
            st.caption("A score means the signal is present, not that the "
                       "company fits the ICP. Read the list before running one.")


files = sorted(MESSAGES.glob("*.json"))
if not files:
    st.warning("No results yet. Run a company from the sidebar.")
    st.stop()

results = []
for f in files:
    try:
        results.append((f.stem, json.loads(f.read_text(encoding="utf-8"))))
    except Exception:
        continue

if not results:
    st.warning("No readable results in messages/.")
    st.stop()


def has_blocked(payload):
    everything = (payload.get("emails") or []) + (payload.get("linkedin_messages") or [])
    return any(blocking_reasons(m) for m in everything)


blocked_count = sum(1 for _, payload in results if has_blocked(payload))
clean_count = len(results) - blocked_count

st.markdown(f"""
<div class="stats">
  <div class="stat">
    <div class="stat-label">Companies</div>
    <div class="stat-value">{len(results)}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Clean to review</div>
    <div class="stat-value accent">{clean_count}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Blocked · do not send</div>
    <div class="stat-value danger">{blocked_count}</div>
  </div>
</div>
""", unsafe_allow_html=True)

# Blocked companies first - they are the ones that need a human.
ordered = ([d for d, payload in results if has_blocked(payload)]
           + [d for d, payload in results if not has_blocked(payload)])
domain = st.selectbox("Company", ordered, label_visibility="collapsed")
data = dict(results)[domain]

info = data.get("generated_from") or {}
st.markdown(f"### {data.get('company_name', domain)}")

if info.get("forced"):
    st.markdown(
        '<div class="note-block"><strong>Drafted below the quality floor.</strong> '
        'This company was run with --force. The triggers were judged too weak '
        'to be worth sending on.</div>', unsafe_allow_html=True)

st.caption(f"{info.get('trigger_count', 0)} trigger(s) · top relevance "
           f"{info.get('top_score', 0)}/10")
if info.get("pain_hypothesis"):
    st.info(info["pain_hypothesis"])

if has_blocked(data):
    st.error("One or more messages here must not be sent. See the red cards below.")

for w in data.get("warnings") or []:
    st.warning(w)


# ------------------------------------------------------- evidence expanders

trigger_file = BASE / "triggers" / f"{domain}.json"
if trigger_file.exists():
    try:
        tdata = json.loads(trigger_file.read_text(encoding="utf-8"))
    except Exception:
        tdata = {}
    trigs = tdata.get("triggers", [])
    if trigs:
        with st.expander(f"Verify the sources · {len(trigs)} triggers"):
            st.caption("Every message above is built on one of these. Open the "
                       "source and check it.")
            for t in trigs:
                src = t.get("source_page", "")
                st.markdown(
                    f'<div class="src">'
                    f'<div class="src-fact">{esc(t.get("fact"))}</div>'
                    f'<div class="src-meta">'
                    f'<span class="score">{t.get("relevance_score", 0)}/10</span>'
                    f'</div></div>',
                    unsafe_allow_html=True)
                if src.startswith("http"):
                    st.markdown(f"[{src}]({src})")
                else:
                    st.caption(f"source: {src}")

news_file = BASE / "news" / f"{domain}.json"
if news_file.exists():
    try:
        ndata = json.loads(news_file.read_text(encoding="utf-8"))
    except Exception:
        ndata = {}
    arts = ndata.get("articles", [])
    if arts:
        with st.expander(f"News searched · {len(arts)} articles"):
            st.caption("Everything the news stage found. Only some of it "
                       "became a trigger.")
            for a in arts:
                st.markdown(f"[{a['title']}]({a['link']})")
                st.caption(f"{a.get('source', 'unknown')} · "
                           f"{a.get('date', 'undated')}")

# LinkedIn: shown even when it finds nobody, because the null result is the
# honest finding. The actor has no title filter, so it samples blind.
li_file = BASE / "linkedin" / f"{domain}.json"
if li_file.exists():
    try:
        ldata = json.loads(li_file.read_text(encoding="utf-8"))
    except Exception:
        ldata = {}
    scanned = ldata.get("scanned", 0)
    people = ldata.get("people", [])
    if scanned:
        with st.expander(f"LinkedIn scan · {len(people)} of {scanned} in a "
                         f"hiring role"):
            if people:
                st.caption("Who to send this to.")
                for p in people:
                    st.markdown(
                        f'<div class="person">'
                        f'<div class="person-name">{esc(p["name"])}</div>'
                        f'<div class="person-role">{esc(p["role"])}</div>'
                        f'</div>', unsafe_allow_html=True)
                    if p.get("linkedin_url"):
                        st.caption(p["linkedin_url"])
            else:
                st.markdown(
                    f'<div class="null-result">'
                    f'Scanned {scanned} employees, none in a hiring role.<br><br>'
                    f'The scraper has no title filter, so this samples blind. A '
                    f'company with 3,000 people on LinkedIn has perhaps 1% in '
                    f'talent acquisition; 25 random profiles will usually miss '
                    f'all of them. Finding them reliably would mean pulling '
                    f'thousands of profiles and storing personal data for '
                    f'thousands of people to reach three.<br><br>'
                    f'Kept in the demo because the null result is the finding: '
                    f'at this volume, searching LinkedIn by hand is faster and '
                    f'more accurate than automating it.'
                    f'</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- messages

t1, t2 = st.tabs(["Emails", "LinkedIn"])

with t1:
    for e in data.get("emails") or []:
        meta = [f"{e.get('word_count', 0)} words"]
        if e.get("trigger_used"):
            meta.append(f"trigger: {e['trigger_used'][:70]}")
        if e.get("invented_number"):
            meta.insert(0, f"invented number: {e['invented_number']}")
        card(e.get("angle", ""), e.get("subject", ""), e.get("body", ""), meta, e)

with t2:
    for m in data.get("linkedin_messages") or []:
        meta = [f"{m.get('char_count', 0)} chars"]
        if m.get("trigger_used"):
            meta.append(f"trigger: {m['trigger_used'][:70]}")
        if m.get("invented_number"):
            meta.insert(0, f"invented number: {m['invented_number']}")
        card(m.get("angle", ""), "", m.get("text", ""), meta, m)