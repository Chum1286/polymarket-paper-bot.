"""
Shows how the paper-trading bot is doing.

Run:  python report.py

Two checks:
  1. Pretend bets: profit/loss, win rate vs. price paid.
  2. Model vs. market: for every market the bot looked at that has now ended,
     was the options-based "fair" price closer to the real result than Polymarket's price?
     (Brier score: lower = better. If the bot's score isn't lower, there's no edge.)
"""
import csv, json, os
from paper_bot import TRADES, SCANS, DATA, GAMMA, get_json, read_csv

OUTCOMES = os.path.join(DATA, "outcomes.json")
MIN_BETS_FOR_VERDICT = 100


def outcomes_for(ids):
    cache = json.load(open(OUTCOMES)) if os.path.exists(OUTCOMES) else {}
    for i in ids:
        if i in cache:
            continue
        try:
            m = get_json(f"{GAMMA}/markets/{i}")
        except RuntimeError:
            continue
        if m.get("closed") and m.get("outcomePrices"):
            y = float(json.loads(m["outcomePrices"])[0])
            if y in (0.0, 1.0):
                cache[i] = y
    json.dump(cache, open(OUTCOMES, "w"))
    return cache


def main():
    trades = read_csv(TRADES)
    done = [t for t in trades if t["status"] in ("won", "lost")]
    open_ = [t for t in trades if t["status"] == "open"]
    print("=" * 60)
    print("PRETEND BETS")
    print(f"  total: {len(trades)} | settled: {len(done)} | still open: {len(open_)}")
    if done:
        pnl = sum(float(t["pnl"]) for t in done)
        cost = sum(float(t["cost"]) for t in done)
        wins = sum(t["status"] == "won" for t in done)
        avg_paid = sum(float(t["price"]) + float(t["fee"]) for t in done) / len(done)
        avg_fair = sum(float(t["fair"]) for t in done) / len(done)
        print(f"  profit/loss: ${pnl:,.2f} on ${cost:,.0f} bet  ({100 * pnl / cost:+.1f}%)")
        print(f"  win rate: {wins / len(done):.1%}  | avg price paid incl. fee: {avg_paid:.1%}  | avg fair: {avg_fair:.1%}")
        by = {}
        for t in done:
            k = f"{t['asset']} {t['kind']} {t['side']}"
            b = by.setdefault(k, [0, 0.0])
            b[0] += 1; b[1] += float(t["pnl"])
        for k, (n, p) in sorted(by.items()):
            print(f"    {k:18} bets={n:4}  pnl=${p:,.2f}")

    scans = read_csv(SCANS)
    ids = sorted({s["market_id"] for s in scans})
    res = outcomes_for(ids)
    scored = [s for s in scans if s["market_id"] in res]
    print("=" * 60)
    print("MODEL vs MARKET (all markets scanned that have ended)")
    if scored:
        b_model = sum((float(s["fair_yes"]) - res[s["market_id"]]) ** 2 for s in scored) / len(scored)
        b_mkt = sum((float(s["mid"]) - res[s["market_id"]]) ** 2 for s in scored) / len(scored)
        print(f"  snapshots scored: {len(scored)} across {len({s['market_id'] for s in scored})} markets")
        print(f"  error score  -> bot's fair price: {b_model:.4f} | Polymarket price: {b_mkt:.4f}  (lower is better)")
        print("  ->", "bot's prices were MORE accurate" if b_model < b_mkt else "Polymarket's prices were more accurate")
    else:
        print("  nothing has ended yet")

    print("=" * 60)
    print("VERDICT")
    if len(done) < MIN_BETS_FOR_VERDICT:
        print(f"  Too early: {len(done)}/{MIN_BETS_FOR_VERDICT} settled bets. Luck still dominates. Keep running.")
    else:
        ok = pnl > 0 and wins / len(done) > avg_paid and scored and b_model < b_mkt
        print("  PASS: worth trying with a small real amount." if ok else
              "  FAIL: no reliable edge. Don't put real money in.")


if __name__ == "__main__":
    main()
