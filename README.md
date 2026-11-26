# Cross-bank mule detection testbed

A runnable experiment, on synthetic data only, for privacy-first cross-bank mule detection
ahead of AMLR Article 75 (applies from 10 July 2027).

## Run it
    pip install -r requirements.txt
    python testbed.py          # prints the headline comparison
    streamlit run app.py       # interactive dashboard

## What actually runs
1. `generate_ledgers`: three bank ledgers (6,000 accounts) with planted mule chains
   (split payments forwarded within minutes), single-bank chains, and benign look-alikes
   (friend reimbursements, rent, payment-provider hubs).
2. `local_triggers`: each bank's fast pass-through rule, using only its own ledger.
3. `run_psi`: private set intersection by commutative encryption (2048-bit RFC 3526 group).
   Banks compare blinded signals; non-matching accounts stay hidden.
4. Matches need `min_banks` banks and must pass a popular-account cap; they go to an analyst
   queue, with every exchange written to an audit log. Nothing is blocked automatically.
5. Scoring against the planted ground truth, repeated over 10 seeds.

## Findings with default settings (30-minute window)
| | Single bank alone | Cross-bank confirmed |
|---|---|---|
| Alerts | ~40 | ~17 |
| Share that are real mules | ~55% | ~75% |
| Share of all mules found | ~70% | ~42% |

Cross-bank confirmation acts as a **priority queue**: fewer, more reliable alerts, but it
misses chains that are slow, sit inside one bank, or trigger at only one bank.
Ranges across seeds are wide (e.g. mules found 23% to 62%) because few chains are planted.

## Limits
- Results come from the assumptions in `generate_ledgers`. They show mechanics and
  trade-offs, not real-world performance. Real pilot data is needed for that.
- Tokens remain pseudonymised personal data under GDPR. Counts per token and set sizes
  are visible to participants. A DPIA, supervisor notification and formal privacy review
  are required before any real use.
- The PSI is a teaching implementation, not audited cryptography.
