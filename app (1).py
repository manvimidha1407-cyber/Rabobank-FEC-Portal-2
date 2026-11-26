"""Dashboard for the cross-bank mule detection testbed. Run: streamlit run app.py"""
import pandas as pd
import streamlit as st

import testbed as tb

NAVY, ORANGE, MUTED, LINE = "#2C3696", "#F36717", "#6B7190", "#E3E6F2"
st.set_page_config(page_title="Cross-bank mule testbed", layout="wide")
st.markdown(f"""<style>
.stApp {{ background:#fff; }} h1,h2,h3 {{ color:{NAVY}; }}
section[data-testid="stSidebar"] {{ background:#F5F6FB; }}
.rule {{ height:4px; width:64px; background:{ORANGE}; border-radius:2px; margin:.4rem 0 1rem; }}
.finding {{ border-left:5px solid {ORANGE}; background:#FFF8F3; padding:.8rem 1rem; border-radius:4px; }}
div.stButton > button[kind="primary"] {{ background:{ORANGE}; border:none; color:#fff; }}
</style>""", unsafe_allow_html=True)

# ---- Sidebar: experiment settings -------------------------------------------------
with st.sidebar:
    st.header("Experiment")
    window = st.slider("Fast pass-through window (min)", 5, 180, 30, 5)
    min_banks = st.selectbox("Minimum banks in a match", [2, 3], index=0)
    hub_cap = st.slider("Popular-account cap", 1, 20, 3,
                        help="Ignore tokens linked to more triggered accounts than this "
                             "(payment providers, big landlords).")
    chains = st.slider("Planted cross-bank chains", 2, 20, 8)
    seed = st.number_input("Random seed", 0, 999, 7)
    st.caption("All data is synthetic. Change the seed to see how much results move.")

params = dict(window=window, min_banks=min_banks, hub_cap=hub_cap, n_cross_chains=chains)

@st.cache_data(show_spinner="Running the full pipeline…")
def one(seed, **kw):
    return tb.run(seed=seed, **kw)

@st.cache_data(show_spinner="Repeating over 10 seeds…")
def many(**kw):
    return tb.run_many(**kw)

r, m = one(seed, **params), many(**params)

# ---- Header ----------------------------------------------------------------------
st.title("Cross-bank mule detection testbed")
st.markdown("<div class='rule'></div>", unsafe_allow_html=True)
st.write("Three synthetic banks, planted mule chains and benign look-alikes. Each bank runs "
         "its own rule, signals are compared with a private set intersection, and the result "
         "is scored against known ground truth.")

# ---- 1. Headline: local alerts vs cross-bank confirmed ---------------------------------
st.header("1. Does cross-bank confirmation help?")
def fmt(key):
    mean, lo, hi = m[key]
    return f"{mean:.0%} ({lo:.0%} to {hi:.0%})" if "alerts" not in key else f"{mean:.0f} ({lo:.0f} to {hi:.0f})"
df = pd.DataFrame({
    "": ["Alerts for analysts", "Share that are real mules", "Share of all mules found"],
    "Single bank alone (every local trigger)": [fmt("local_alerts"), fmt("local_precision"), fmt("local_recall")],
    "Cross-bank confirmed (matched token)": [fmt("cross_alerts"), fmt("cross_precision"), fmt("cross_recall")],
}).set_index("")
st.table(df)
st.caption("Mean over 10 random seeds, range in brackets. 'Real mules' is known only because the data is synthetic.")
lp, cp = m["local_precision"][0], m["cross_precision"][0]
lr, cr = m["local_recall"][0], m["cross_recall"][0]
st.markdown(f"<div class='finding'><b>Reading this honestly:</b> cross-bank confirmation cuts the alert "
            f"pile from about {m['local_alerts'][0]:.0f} to {m['cross_alerts'][0]:.0f} and lifts the share "
            f"that are real mules from {lp:.0%} to {cp:.0%}, but finds fewer mules overall ({cr:.0%} vs {lr:.0%}). "
            f"It works as a <b>priority queue</b>, not a replacement for local alerts.</div>",
            unsafe_allow_html=True)

# ---- 2. Analyst queue ------------------------------------------------------------
st.header("2. Analyst review queue (this seed)")
st.caption(f"Cross-bank chains fully or partly caught: {r['cross_chains_caught']} of {r['cross_chains']}. "
           f"Matches ignored as too popular: {r['hub_suppressed']}. No automated action is taken.")
if r["matches"]:
    q = pd.DataFrame([{"Blind token": x["token"], "Banks": ", ".join(x["banks"]),
                       "Accounts to review": len(x["accounts"]),
                       "Ground truth (testbed only)": "Mule chain" if x["is_mule"] else "False alarm"}
                      for x in r["matches"]])
    st.dataframe(q, hide_index=True)
else:
    st.info("No matches at these settings.")

# ---- 3. What left each bank + audit log ---------------------------------------------
st.header("3. What left each bank")
st.write(f"Raw customer names: **never**. Raw IBANs: **never**. Each bank sent only blinded values "
         f"(about {r['psi_kb']:.0f} KB of PSI traffic in total). Every exchange is logged:")
st.dataframe(pd.DataFrame(r["audit"]).rename(columns={"time": "Time", "bank": "Bank", "event": "Event",
             "items": "Items", "contains": "Contents"}), hide_index=True)

# ---- 4. Sensitivity ---------------------------------------------------------------
st.header("4. How the window changes the picture")
rows = []
for w in (15, 30, 60, 120):
    s = many(**{**params, "window": w})
    rows.append({"Window (min)": w, "Cross-bank alerts": f"{s['cross_alerts'][0]:.0f}",
                 "Real mules": f"{s['cross_precision'][0]:.0%}", "Mules found": f"{s['cross_recall'][0]:.0%}"})
st.table(pd.DataFrame(rows).set_index("Window (min)"))

# ---- 5. Export the synthetic data --------------------------------------------------
st.header("5. Export the synthetic data")
@st.cache_data
def ledger_csv(seed, chains):
    ledgers, truth = tb.generate_ledgers(seed=seed, n_cross_chains=chains)
    rows = [dict(bank=b, account=t["acct"], direction=t["dir"], amount_eur=t["amt"],
                 minute_of_month=round(t["t"], 1), counterparty=t["cp"],
                 planted_mule=(b, t["acct"]) in truth["mules"])
            for b, v in ledgers.items() for t in v]
    return pd.DataFrame(rows).to_csv(index=False).encode()
st.download_button("Download all transactions (CSV)", ledger_csv(seed, chains),
                   file_name=f"synthetic_ledgers_seed{seed}.csv", mime="text/csv")
st.caption("Made-up account codes and random transactions. The planted_mule column is the ground truth.")

with st.expander("What these numbers do and don't show"):
    st.write("- The data is generated by assumptions in testbed.py. Results show the mechanics and the "
             "trade-offs, not real-world performance.\n"
             "- With few planted chains, single runs are noisy. Use the ranges.\n"
             "- Chains inside one bank cannot be matched across banks by design.\n"
             "- Tokens are still pseudonymised data under GDPR. A DPIA and supervisor notification "
             "(AMLR Art. 75) would be needed before any real pilot.\n"
             "- The PSI hides non-matching accounts, but the match counts and the number of signals "
             "per bank are visible. A production design needs a formal privacy review.")
