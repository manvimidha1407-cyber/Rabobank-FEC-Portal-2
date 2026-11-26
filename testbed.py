"""
Cross-bank mule detection testbed (synthetic data only).

Pipeline, all executed for real:
  1. Generate three synthetic bank ledgers with planted mule chains plus benign
     look-alikes (friends reimbursing each other, payment-provider hubs, landlords).
  2. Each bank runs a local fast pass-through rule on its OWN ledger.
  3. Banks compare signals with a private set intersection (PSI) based on commutative
     encryption, so no bank sees another bank's raw account numbers.
  4. Matches are filtered (min banks, hub cap) and routed to an analyst queue.
     Every exchange is written to an audit log.
  5. Results are scored against the planted ground truth.

Results depend entirely on the assumptions in generate_ledgers(). They show the
mechanics work and how the knobs trade off. They are NOT real-world performance.
"""
import hashlib
import random
import secrets
import time
from collections import defaultdict

BANKS = ["Rabobank", "Bank B", "Bank C"]
DAY = 1440  # minutes per day

# --------------------------------------------------------------------------
# PSI group: the 2048-bit safe prime from RFC 3526 (group 14), derived from pi.
# --------------------------------------------------------------------------
def _arccot(x, one):
    n = one // x; s = n; x2 = x * x; k = 1; sign = -1
    while n:
        n //= x2; k += 2; s += sign * (n // k); sign = -sign
    return s

def _pi_bits(bits):
    one = 1 << (bits + 64)
    return (4 * (4 * _arccot(5, one) - _arccot(239, one))) >> 64

P = 2**2048 - 2**1984 - 1 + 2**64 * (_pi_bits(1918) + 124476)

def hash_to_group(token: str) -> int:
    """Map a token into the prime-order subgroup (squares mod P)."""
    h = int.from_bytes(hashlib.shake_256(b"psi|" + token.encode()).digest(256), "big") % P
    return pow(h, 2, P)

# --------------------------------------------------------------------------
# 1. Synthetic ledgers
# --------------------------------------------------------------------------
def generate_ledgers(seed=7, accounts_per_bank=2000, n_cross_chains=8, n_single_chains=3,
                     benign_rate=0.012, slow_share=0.2):
    """Return (ledgers, truth). ledgers[bank] = list of tx dicts; truth = mule info."""
    rng = random.Random(seed)
    ledgers = {b: [] for b in BANKS}
    accts = {b: [f"{b[:2].upper()}{i:05d}" for i in range(accounts_per_bank)] for b in BANKS}
    hubs = [f"HUB{i}" for i in range(3)]                  # payment providers / marketplaces
    landlords = [f"LND{i:03d}" for i in range(300)]
    merchants = [f"MER{i:03d}" for i in range(60)]
    mules, chains = {}, []

    def tx(bank, acct, direction, amt, t, cp):
        ledgers[bank].append(dict(acct=acct, dir=direction, amt=round(amt, 2), t=t, cp=cp))

    # Normal activity: salary in, a handful of small payments out
    for b in BANKS:
        for a in accts[b]:
            tx(b, a, "in", rng.uniform(1800, 3500), rng.randrange(0, 30 * DAY), f"EMP{rng.randrange(400)}")
            for _ in range(rng.randrange(3, 9)):
                tx(b, a, "out", rng.uniform(5, 250), rng.randrange(0, 30 * DAY), rng.choice(merchants))

    # Benign fast pass-throughs: reimbursing a friend, paying rent, using a payment hub
    for b in BANKS:
        for a in accts[b]:
            if rng.random() < benign_rate:
                amt, t0 = rng.uniform(1000, 4000), rng.randrange(0, 29 * DAY)
                dest = rng.choice(hubs) if rng.random() < 0.35 else rng.choice(landlords)
                wait = rng.expovariate(1 / 45) if rng.random() < 0.6 else rng.uniform(120, 900)
                tx(b, a, "in", amt, t0, f"FRIEND{rng.randrange(10**6)}")
                tx(b, a, "out", amt * rng.uniform(0.92, 1.0), t0 + wait, dest)

    def plant(chain_id, legs_banks):
        total = rng.uniform(6000, 15000)
        victim, cashout = f"VICTIM{chain_id}", f"CASHOUT{chain_id}"
        t0 = rng.randrange(0, 28 * DAY)
        members = []
        for i, b in enumerate(legs_banks):
            a = rng.choice(accts[b])
            amt = total / len(legs_banks)
            wait = rng.uniform(120, 360) if rng.random() < slow_share else rng.uniform(5, 35)
            tx(b, a, "in", amt, t0 + i * 3, victim)
            tx(b, a, "out", amt * rng.uniform(0.97, 1.0), t0 + i * 3 + wait, cashout)
            mules[(b, a)] = chain_id
            members.append((b, a))
        chains.append(dict(id=chain_id, banks=sorted(set(legs_banks)), accounts=members))

    for c in range(n_cross_chains):   # chains spanning 2 or 3 banks
        plant(c, rng.sample(BANKS, 3) if rng.random() < 0.7 else rng.sample(BANKS, 2))
    for c in range(n_single_chains):  # chains inside one bank: cross-bank matching can't help
        plant(100 + c, [rng.choice(BANKS)] * 3)
    return ledgers, dict(mules=mules, chains=chains)

# --------------------------------------------------------------------------
# 2. Local rule: runs inside each bank on its own data only
# --------------------------------------------------------------------------
def local_triggers(ledger, window, min_amount=1000):
    """An incoming payment >= min_amount, with >=90% sent onward within `window` minutes."""
    by_acct = defaultdict(list)
    for t in ledger:
        by_acct[t["acct"]].append(t)
    hits = []
    for acct, txs in by_acct.items():
        txs.sort(key=lambda x: x["t"])
        for i in (x for x in txs if x["dir"] == "in" and x["amt"] >= min_amount):
            for o in txs:
                if o["dir"] == "out" and 0 <= o["t"] - i["t"] <= window and o["amt"] >= 0.9 * i["amt"]:
                    hits.append(dict(acct=acct, amt=i["amt"], minutes=round(o["t"] - i["t"], 1),
                                     src=i["cp"], dest=o["cp"]))
                    break
    return hits

# --------------------------------------------------------------------------
# 3. Private set intersection (commutative encryption, ring of banks)
# --------------------------------------------------------------------------
def run_psi(signals, audit):
    """signals[bank] = {token: count}. Returns {final_value: {bank: count}} and bytes sent.
    Each bank blinds its tokens with its own secret exponent, then every other bank
    blinds them too. Equal tokens end up with equal values; nothing else is comparable."""
    keys = {b: secrets.randbits(256) | 1 for b in BANKS}
    finals, sent = {}, 0
    for b in BANKS:
        blinded = {t: pow(hash_to_group(t), keys[b], P) for t in signals[b]}
        audit.append(dict(time=time.strftime("%H:%M:%S"), bank=b, event="blinded set sent",
                          items=len(blinded), contains="blinded values only"))
        for other in BANKS:
            if other != b:
                blinded = {t: pow(v, keys[other], P) for t, v in blinded.items()}
                sent += 256 * len(blinded)
        finals[b] = blinded
    shared = defaultdict(dict)
    for b in BANKS:
        for t, v in finals[b].items():
            shared[v][b] = (signals[b][t], t)   # each bank can map back only its OWN tokens
    return shared, sent

# --------------------------------------------------------------------------
# 4 + 5. Full run and scoring
# --------------------------------------------------------------------------
def run(seed=7, window=30, min_banks=2, hub_cap=3, accounts_per_bank=2000,
        n_cross_chains=8, n_single_chains=3):
    ledgers, truth = generate_ledgers(seed, accounts_per_bank, n_cross_chains, n_single_chains)
    mules = truth["mules"]
    audit, trig = [], {b: local_triggers(ledgers[b], window) for b in BANKS}

    signals = {b: defaultdict(int) for b in BANKS}   # token -> number of triggered accounts
    for b in BANKS:
        for h in trig[b]:
            signals[b][f"out:{h['dest']}"] += 1
            signals[b][f"in:{h['src']}"] += 1
    shared, sent = run_psi(signals, audit)

    matched = {}   # final value -> banks involved; apply min_banks and hub cap
    for v, per_bank in shared.items():
        total = sum(c for c, _ in per_bank.values())
        if len(per_bank) >= min_banks and total <= hub_cap:
            matched[v] = per_bank
    hub_suppressed = sum(1 for v, pb in shared.items()
                         if len(pb) >= min_banks and sum(c for c, _ in pb.values()) > hub_cap)

    # Each bank learns which of its own triggered accounts matched elsewhere
    flagged, match_rows = set(), []
    for v, per_bank in matched.items():
        row_accts = []
        for b, (_, token) in per_bank.items():
            for h in trig[b]:
                if token in (f"out:{h['dest']}", f"in:{h['src']}"):
                    flagged.add((b, h["acct"])); row_accts.append((b, h["acct"]))
        match_rows.append(dict(token=hex(v)[2:10] + "…" + hex(v)[-6:], banks=sorted(per_bank),
                               accounts=sorted(set(row_accts)),
                               is_mule=any(a in mules for a in row_accts)))
        audit.append(dict(time=time.strftime("%H:%M:%S"), bank="all", event="match routed to analysts",
                          items=len(per_bank), contains="blinded token + bank list"))

    local_all = {(b, h["acct"]) for b in BANKS for h in trig[b]}
    def score(flag):
        tp = len(flag & set(mules)); fp = len(flag) - tp
        return dict(alerts=len(flag), true=tp, false=fp,
                    precision=tp / len(flag) if flag else 0.0, recall=tp / len(mules))
    cross_chains = [c for c in truth["chains"] if len(c["banks"]) > 1]
    caught = sum(1 for c in cross_chains if sum(a in flagged for a in c["accounts"]) >= 2)
    return dict(local=score(local_all), cross=score(flagged), matches=match_rows, audit=audit,
                psi_kb=sent / 1024, hub_suppressed=hub_suppressed, n_mules=len(mules),
                cross_chains=len(cross_chains), cross_chains_caught=caught,
                accounts=accounts_per_bank * len(BANKS), truth=truth)

def run_many(seeds=range(10), **kw):
    """Repeat the experiment over several random seeds; report mean and range.
    With only a handful of planted chains, single runs are noisy, so look at this."""
    rs = [run(seed=k, **kw) for k in seeds]
    out = {}
    for name in ("local", "cross"):
        for m in ("alerts", "precision", "recall"):
            v = [r[name][m] for r in rs]
            out[f"{name}_{m}"] = (sum(v) / len(v), min(v), max(v))
    return out

if __name__ == "__main__":
    r = run()
    print(f"Accounts: {r['accounts']}, planted mule accounts: {r['n_mules']}")
    for name in ("local", "cross"):
        s = r[name]
        print(f"{name:>5}: alerts={s['alerts']:3d} true={s['true']:2d} false={s['false']:3d} "
              f"precision={s['precision']:.0%} recall={s['recall']:.0%}")
    print(f"Cross-bank chains caught: {r['cross_chains_caught']}/{r['cross_chains']}  "
          f"hub matches suppressed: {r['hub_suppressed']}  PSI traffic: {r['psi_kb']:.0f} KB")
