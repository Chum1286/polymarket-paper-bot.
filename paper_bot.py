"""
Polymarket crypto paper-trading bot (no real money).

What it does every run:
  1. Finds open Polymarket markets like "Will Bitcoin reach $X in November?",
     "Will Bitcoin dip to $X ...?" and "Will the price of Bitcoin be above $X on <date>?"
  2. Works out a FAIR probability for each one from crypto options prices (Deribit).
  3. If Polymarket's price is cheaper than fair by at least MIN_EDGE (after fees),
     it records a pretend bet in data/paper_trades.csv.
  4. Settles pretend bets whose markets have ended and records profit/loss.
  5. Logs every market it looked at to data/scans.csv, so you can later check
     whether the options-based fair price beats Polymarket's price.

Run:  python paper_bot.py          (one scan)
      python report.py             (see results)
Only uses the Python standard library.
"""
import csv, json, math, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone

# ---------------- settings you can change ----------------
CONFIG = {
    "ASSETS": {"BTC": "bitcoin", "ETH": "ethereum"},
    "MIN_EDGE": 0.05,          # only bet if fair price beats market price by 5+ cents (after fees)
    "STAKE_USD": 20.0,         # pretend dollars per bet
    "MIN_PRICE": 0.05,         # skip contracts cheaper than 5c (longshots lost money in our data)
    "MAX_PRICE": 0.95,         # skip contracts above 95c ("almost sure thing" trap)
    "MAX_SPREAD": 0.05,        # skip markets where bid/ask gap is over 5c
    "MIN_LIQUIDITY": 3000,     # skip thin markets
    "MIN_HOURS_LEFT": 12,      # skip markets ending very soon
    "MAX_DAYS_LEFT": 70,       # skip markets far away (options data gets less reliable)
    "MAX_OPEN_PER_ASSET": 10,  # all BTC bets move together, so cap how many are open at once
    "MAX_NEW_PER_RUN": 4,      # take only the best few new bets each run
    # Fee assumption per share (conservative; Polymarket's exact fee formula wasn't confirmed).
    # fee = FEE_RATE * price * (1 - price), the same shape Kalshi uses.
    "FEE_RATE": 0.07,
}
GAMMA = "https://gamma-api.polymarket.com"
DERIBIT = "https://www.deribit.com/api/v2/public"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TRADES = os.path.join(DATA, "paper_trades.csv")
SCANS = os.path.join(DATA, "scans.csv")
TRADE_FIELDS = ["opened_at", "market_id", "slug", "question", "asset", "kind", "strike", "end_date",
                "side", "price", "fee", "shares", "cost", "fair", "edge", "spot", "iv",
                "status", "settled_at", "payout", "pnl"]
SCAN_FIELDS = ["ts", "market_id", "question", "asset", "kind", "strike", "end_date", "days_left",
               "spot", "iv", "fair_yes", "yes_bid", "yes_ask", "mid", "edge_yes", "edge_no", "action"]


# ---------------- helpers ----------------
def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-bot/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == tries - 1:
                raise RuntimeError(f"Could not fetch {url}: {e}")
            time.sleep(2)


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def now_utc():
    return datetime.now(timezone.utc)


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fee(price):
    return CONFIG["FEE_RATE"] * price * (1 - price)


# ---------------- probability math ----------------
def prob_above_at_end(S, K, sigma, T):
    """Chance price ends above K at time T (lognormal, no drift)."""
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d2 = (math.log(S / K) - 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    return ncdf(d2)


def prob_touch(S, K, sigma, T):
    """Chance price touches K at any point before T (reaches if K > S, dips if K < S)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    if K > S:   # max of log-price with drift -sigma^2/2 reaching b
        b, nu = math.log(K / S), -0.5 * sigma ** 2
    else:       # min reaching -b  ==  max of mirrored process with drift +sigma^2/2
        b, nu = math.log(S / K), 0.5 * sigma ** 2
    s = sigma * math.sqrt(T)
    p = ncdf((-b + nu * T) / s) + math.exp(2 * nu * b / sigma ** 2) * ncdf((-b - nu * T) / s)
    return min(max(p, 0.0), 1.0)


# ---------------- options data (Deribit) ----------------
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def load_options(asset):
    spot = get_json(f"{DERIBIT}/get_index_price?index_name={asset.lower()}_usd")["result"]["index_price"]
    rows = get_json(f"{DERIBIT}/get_book_summary_by_currency?currency={asset}&kind=option")["result"]
    opts = []
    for r in rows:
        m = re.match(r"[A-Z]+-(\d{1,2})([A-Z]{3})(\d{2})-(\d+)-([CP])", r["instrument_name"])
        if not m or not r.get("mark_iv"):
            continue
        exp = datetime(2000 + int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)), 8, tzinfo=timezone.utc)
        opts.append({"exp": exp, "K": float(m.group(4)), "type": m.group(5), "iv": r["mark_iv"] / 100})
    return spot, opts


def _iv_at_strike(chain, spot, K):
    """IV at strike K for one expiry, interpolating between the two nearest strikes (out-of-the-money side)."""
    side = "C" if K >= spot else "P"
    pool = sorted([o for o in chain if o["type"] == side] or chain, key=lambda o: o["K"])
    lo = [o for o in pool if o["K"] <= K]
    hi = [o for o in pool if o["K"] >= K]
    if lo and hi:
        a, b = lo[-1], hi[0]
        if b["K"] == a["K"]:
            return a["iv"]
        w = (K - a["K"]) / (b["K"] - a["K"])
        return a["iv"] + w * (b["iv"] - a["iv"])
    return (lo or hi)[-1 if lo else 0]["iv"]


def implied_vol(opts, spot, K, end):
    """IV for strike K at the market's end date: interpolate total variance between the
    option expiries just before and just after the end date."""
    now = now_utc()
    future = [o for o in opts if (o["exp"] - now).total_seconds() > 6 * 3600]
    if not future:
        return None
    exps = sorted({o["exp"] for o in future})
    before = [e for e in exps if e <= end]
    after = [e for e in exps if e >= end]
    t = lambda e: (e - now).total_seconds() / 86400 / 365.25
    T = max((end - now).total_seconds() / 86400 / 365.25, 1e-6)
    if before and after:
        e1, e2 = before[-1], after[0]
        v1 = _iv_at_strike([o for o in future if o["exp"] == e1], spot, K)
        v2 = _iv_at_strike([o for o in future if o["exp"] == e2], spot, K)
        if e1 == e2:
            return v1
        w1, w2 = v1 ** 2 * t(e1), v2 ** 2 * t(e2)
        w = w1 + (w2 - w1) * (T - t(e1)) / (t(e2) - t(e1))
        return math.sqrt(max(w, 1e-9) / T)
    e = (after or before)[0 if after else -1]
    return _iv_at_strike([o for o in future if o["exp"] == e], spot, K)


# ---------------- Polymarket markets ----------------
def classify(q):
    ql = q.lower()
    m = re.search(r"\$([\d,]+(?:\.\d+)?)", q)
    if not m:
        return None, None
    K = float(m.group(1).replace(",", ""))
    if "dip to" in ql:
        return "touch", K
    if "reach" in ql or "hit" in ql:
        return "touch", K
    if "above" in ql:
        return "above", K
    return None, None


def window_start(event_slug, end):
    """Touch markets count only from the start of their window. Returns start date if known."""
    m = re.search(r"in-([a-z]+)-(\d{4})", event_slug)
    if m and m.group(1)[:3].upper() in MONTHS:
        return datetime(int(m.group(2)), MONTHS[m.group(1)[:3].upper()], 1, tzinfo=timezone.utc)
    return None


def find_markets(asset, tag):
    events, offset = [], 0
    while True:
        page = get_json(f"{GAMMA}/events?tag_slug={tag}&active=true&closed=false&limit=100&offset={offset}")
        if not page:
            break
        events += page
        offset += 100
        if len(page) < 100 or offset > 1000:
            break
    out = []
    for e in events:
        if not re.search(rf"{tag}-above-on|what-price-will-{tag}-hit", e.get("slug", "")):
            continue
        for m in e.get("markets", []):
            if m.get("closed") or not m.get("active") or not m.get("enableOrderBook"):
                continue
            kind, K = classify(m.get("question", ""))
            if not kind:
                continue
            m["_kind"], m["_K"], m["_asset"], m["_event_slug"] = kind, K, asset, e["slug"]
            out.append(m)
    return out


# ---------------- storage ----------------
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def append_csv(path, rows, fields):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


# ---------------- main steps ----------------
def settle(trades):
    changed = 0
    for t in trades:
        if t["status"] != "open":
            continue
        try:
            m = get_json(f"{GAMMA}/markets/{t['market_id']}")
        except RuntimeError:
            continue
        prices = m.get("outcomePrices")
        if not m.get("closed") or not prices:
            continue
        yes_final = float(json.loads(prices)[0])
        if yes_final not in (0.0, 1.0):
            continue
        won = (yes_final == 1.0) if t["side"] == "YES" else (yes_final == 0.0)
        payout = float(t["shares"]) if won else 0.0
        t.update(status="won" if won else "lost", settled_at=now_utc().isoformat(timespec="seconds"),
                 payout=round(payout, 2), pnl=round(payout - float(t["cost"]), 2))
        changed += 1
    return changed


def scan(trades):
    open_ids = {t["market_id"] for t in trades if t["status"] == "open"}
    new_trades, scan_rows, candidates = [], [], []
    ts = now_utc()
    for asset, tag in CONFIG["ASSETS"].items():
        spot, opts = load_options(asset)
        for m in find_markets(asset, tag):
            end = parse_dt(m["endDate"])
            days_left = (end - ts).total_seconds() / 86400
            bid, ask = m.get("bestBid"), m.get("bestAsk")
            if bid is None or ask is None or days_left <= 0:
                continue
            K, kind = m["_K"], m["_kind"]
            iv = implied_vol(opts, spot, K, end)
            if not iv:
                continue
            T = days_left / 365.25
            if kind == "above":
                fair = prob_above_at_end(spot, K, iv, T)
            else:
                ws = window_start(m["_event_slug"], end)
                if ws and ws > ts:          # window hasn't started yet: price must touch after start
                    T = (end - ws).total_seconds() / 86400 / 365.25
                fair = prob_touch(spot, K, iv, T)
            yes_ask, no_ask = float(ask), 1 - float(bid)
            edge_yes = fair - yes_ask - fee(yes_ask)
            edge_no = (1 - fair) - no_ask - fee(no_ask)
            side, price, edge = ("YES", yes_ask, edge_yes) if edge_yes >= edge_no else ("NO", no_ask, edge_no)
            reasons = []
            if edge < CONFIG["MIN_EDGE"]: reasons.append("edge too small")
            if not CONFIG["MIN_PRICE"] <= price <= CONFIG["MAX_PRICE"]: reasons.append("price out of range")
            if float(ask) - float(bid) > CONFIG["MAX_SPREAD"]: reasons.append("spread too wide")
            if float(m.get("liquidityNum") or 0) < CONFIG["MIN_LIQUIDITY"]: reasons.append("thin market")
            if days_left * 24 < CONFIG["MIN_HOURS_LEFT"]: reasons.append("ends too soon")
            if days_left > CONFIG["MAX_DAYS_LEFT"]: reasons.append("ends too far away")
            if str(m["id"]) in open_ids: reasons.append("already holding")
            action = "BET " + side if not reasons else "skip: " + ", ".join(reasons)
            scan_rows.append({"ts": ts.isoformat(timespec="seconds"), "market_id": m["id"], "question": m["question"],
                              "asset": asset, "kind": kind, "strike": K, "end_date": m["endDate"],
                              "days_left": round(days_left, 2), "spot": round(spot, 2), "iv": round(iv, 4),
                              "fair_yes": round(fair, 4), "yes_bid": bid, "yes_ask": ask,
                              "mid": round((float(bid) + float(ask)) / 2, 4), "edge_yes": round(edge_yes, 4),
                              "edge_no": round(edge_no, 4), "action": action})
            if not reasons:
                f = fee(price)
                shares = CONFIG["STAKE_USD"] / (price + f)
                candidates.append({"opened_at": ts.isoformat(timespec="seconds"), "market_id": m["id"],
                                   "slug": m.get("slug"), "question": m["question"], "asset": asset, "kind": kind,
                                   "strike": K, "end_date": m["endDate"], "side": side, "price": round(price, 4),
                                   "fee": round(f, 4), "shares": round(shares, 4), "cost": CONFIG["STAKE_USD"],
                                   "fair": round(fair if side == "YES" else 1 - fair, 4), "edge": round(edge, 4),
                                   "spot": round(spot, 2), "iv": round(iv, 4), "status": "open",
                                   "settled_at": "", "payout": "", "pnl": ""})
                open_ids.add(str(m["id"]))
    # keep only the best bets, within the per-asset cap
    open_count = {a: sum(t["status"] == "open" and t["asset"] == a for t in trades) for a in CONFIG["ASSETS"]}
    for c in sorted(candidates, key=lambda c: -c["edge"]):
        if len(new_trades) >= CONFIG["MAX_NEW_PER_RUN"]:
            break
        if open_count[c["asset"]] >= CONFIG["MAX_OPEN_PER_ASSET"]:
            continue
        new_trades.append(c)
        open_count[c["asset"]] += 1
    taken = {c["market_id"] for c in new_trades}
    for r in scan_rows:
        if r["action"].startswith("BET") and r["market_id"] not in taken:
            r["action"] = "skip: bet limit reached"
    return new_trades, scan_rows


def main():
    os.makedirs(DATA, exist_ok=True)
    trades = read_csv(TRADES)
    settled = settle(trades)
    new_trades, scan_rows = scan(trades)
    trades += new_trades
    write_csv(TRADES, trades, TRADE_FIELDS)
    append_csv(SCANS, scan_rows, SCAN_FIELDS)
    print(f"{now_utc():%Y-%m-%d %H:%M} UTC | looked at {len(scan_rows)} markets | "
          f"new pretend bets: {len(new_trades)} | settled: {settled} | "
          f"open: {sum(t['status'] == 'open' for t in trades)}")
    for t in new_trades:
        print(f"  BET {t['side']} @ {t['price']:.3f} (fair {t['fair']:.3f}, edge {t['edge']:+.3f}) {t['question']}")


if __name__ == "__main__":
    main()
