import json
import subprocess
import sys
import streamlit as st
from pathlib import Path

BASE = Path(__file__).resolve().parent
MESSAGES = BASE / "messages"

st.set_page_config(page_title="Cold Outreach Agent", layout="wide",
                   initial_sidebar_state="expanded")

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

  /* ---------------------------------------------------------- cards */
  .msg { background: #131317; border: 1px solid #22222A; border-radius: 12px;
         padding: 1.5rem 1.75rem; margin-bottom: 1rem;
         animation: rise .45s ease-out both;
         transition: border-color .25s ease, transform .25s ease,
                     box-shadow .25s ease; }
  .msg:hover { border-color: #3A3A46; transform: translateY(-3px);
               box-shadow: 0 12px 32px rgba(0,0,0,.4); }
  .msg:nth-of-type(1) { animation-delay: .04s; }
  .msg:nth-of-type(2) { animation-delay: .1s; }
  .msg:nth-of-type(3) { animation-delay: .16s; }

  .msg-head { display: flex; justify-content: space-between;
              align-items: baseline; margin-bottom: 1rem; }
  .angle { font-size: .68rem; text-transform: uppercase; letter-spacing: .14em;
           color: #E8674C; font-weight: 600; }
  .badge { font-size: .68rem; letter-spacing: .08em; padding: .2rem .6rem;
           border-radius: 20px; font-weight: 600; }
  .ok  { color: #4ADE80; background: rgba(74,222,128,.08);
         border: 1px solid rgba(74,222,128,.2); }
  .bad { color: #F87171; background: rgba(248,113,113,.08);
         border: 1px solid rgba(248,113,113,.2); }

  .subject { font-weight: 600; font-size: 1.1rem; margin-bottom: 1rem;
             letter-spacing: -0.01em; }
  .body { white-space: pre-wrap; line-height: 1.7; color: #C8C8D0;
          font-size: .95rem; }
  .meta { font-size: .72rem; color: #5E5E68; margin-top: 1.25rem;
          padding-top: .9rem; border-top: 1px solid #1F1F27; }

  .fallback-note { background: rgba(232,103,76,.06);
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

  section[data-testid="stSidebar"] { border-right: 1px solid #1F1F25; }
</style>
""", unsafe_allow_html=True)


def card(angle, subject, body, meta_bits, grounded, fallback):
    if fallback:
        badge = '<span class="badge bad">no trigger</span>'
    elif grounded:
        badge = '<span class="badge ok">grounded</span>'
    else:
        badge = '<span class="badge bad">ungrounded</span>'
    subj = f'<div class="subject">{subject}</div>' if subject else ""
    st.markdown(
        f'<div class="msg"><div class="msg-head">'
        f'<span class="angle">{angle}</span>{badge}</div>'
        f'{subj}<div class="body">{body}</div>'
        f'<div class="meta">{" · ".join(meta_bits)}</div></div>',
        unsafe_allow_html=True)


# ------------------------------------------------------------------ sidebar

with st.sidebar:
    st.markdown("### Run a company")
    new_domain = st.text_input("Domain", placeholder="razorpay.com",
                               label_visibility="collapsed")
    use_news = st.checkbox("Include news search", value=True)

    if st.button("Run pipeline", use_container_width=True) and new_domain.strip():
        d = new_domain.strip()
        steps = [("Scraping site", "scrape.py")]
        if use_news:
            steps.append(("Searching news", "news.py"))
        steps += [("Extracting triggers", "triggers.py"),
                  ("Writing messages", "generate.py")]

        for label, script in steps:
            with st.spinner(label):
                p = subprocess.run([sys.executable, str(BASE / script), d],
                                   capture_output=True, text=True)
            if p.returncode != 0:
                st.error(f"{label} failed"); break
        else:
            st.success("Done"); st.rerun()

    st.caption("2 Gemini calls + 1 news search per company.")


# --------------------------------------------------------------------- hero

st.markdown("""
<div class="hero">
  <div class="eyebrow">Trigger-grounded outreach</div>
  <h1>Cold Outreach Agent</h1>
  <div class="lede">
    Reads a company's site and recent news, extracts
    <strong>verifiable triggers</strong>, and writes three emails and three
    LinkedIn messages built on them. Every claim is checked back against what
    was actually found, and every source is linked so you can open it and
    check the work yourself.
  </div>
</div>
""", unsafe_allow_html=True)

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

fallback = [r for r in results if r[1].get("fallback_mode")]
grounded = [r for r in results if not r[1].get("fallback_mode")]

st.markdown(f"""
<div class="stats">
  <div class="stat">
    <div class="stat-label">Companies</div>
    <div class="stat-value">{len(results)}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Trigger-backed</div>
    <div class="stat-value accent">{len(grounded)}</div>
  </div>
  <div class="stat">
    <div class="stat-label">No trigger found</div>
    <div class="stat-value">{len(fallback)}</div>
  </div>
</div>
""", unsafe_allow_html=True)

ordered = [r[0] for r in grounded] + [r[0] for r in fallback]
domain = st.selectbox("Company", ordered, label_visibility="collapsed")
data = dict(results)[domain]
is_fallback = bool(data.get("fallback_mode"))

info = data["generated_from"]
st.markdown(f"### {data['company_name']}")

if is_fallback:
    st.markdown(
        '<div class="fallback-note"><strong>No trigger found.</strong> '
        'Nothing recent or specific was found for this company, so these '
        'messages are generic. They are shown for comparison — expect a far '
        'lower reply rate than trigger-backed ones.</div>',
        unsafe_allow_html=True)
else:
    st.caption(f"{info['trigger_count']} trigger(s) · top relevance "
               f"{info['top_score']}/10")
    if info.get("pain_hypothesis"):
        st.info(info["pain_hypothesis"])

for w in data.get("warnings") or []:
    if not w.startswith("NO TRIGGER FOUND"):
        st.warning(w)


# ------------------------------------------------------- evidence expanders

trigger_file = BASE / "triggers" / f"{domain}.json"
if not is_fallback and trigger_file.exists():
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
                    f'<div class="src-fact">{t["fact"]}</div>'
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


# ---------------------------------------------------------------- messages

t1, t2 = st.tabs(["Emails", "LinkedIn"])

with t1:
    for e in data["emails"]:
        meta = [f"{e['word_count']} words"]
        if e["trigger_used"]:
            meta.append(f"trigger: {e['trigger_used'][:70]}")
        if e.get("invented_number"):
            meta.insert(0, f"invented number: {e['invented_number']}")
        card(e["angle"], e["subject"], e["body"], meta, e["grounded"], is_fallback)

with t2:
    for m in data["linkedin_messages"]:
        meta = [f"{m['char_count']} chars"]
        if m["trigger_used"]:
            meta.append(f"trigger: {m['trigger_used'][:70]}")
        if m.get("invented_number"):
            meta.insert(0, f"invented number: {m['invented_number']}")
        card(m["angle"], "", m["text"], meta, m["grounded"], is_fallback)