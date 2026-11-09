"""
Backtest: how would the bot's rules have done on past Polymarket crypto markets?

Simplification vs. the live bot: Deribit doesn't offer old option-by-option prices for free,
so the backtest uses Deribit's volatility index (DVOL, a 30-day at-the-money implied volatility)
for every strike and date. The live bot uses the full options surface.
"""
import json, math, os, sys, re, time, calendar, collections
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import paper_bot as pb

START = datetime(2026, 6, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 9, tzinfo=timezone.utc)
STEP_H = 6                  # look at each market every 6 hours
HALF_SPREAD = 0.01          # assume we pay 1c above the mid price
DELAY_H = int(os.environ.get('DELAY_H', '0'))  # hours between signal and fill
MAX_PRICE_AGE = 2 * 3600    # ignore Polymarket prices older than 2 hours (stale)
CACHE = os.path.join(pb.HERE, "bt_cache")
os.makedirs(CACHE, exist_ok=True)
ASSETS = {"BTC": "bitcoin", "ETH": "ethereum"}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


def cached(name, fn):
    p = os.path.join(CACHE, name)
    if os.path.exists(p):
        return json.load(open(p))
    v = fn()
    json.dump(v, open(p, "w"))
    return v


def slugs():
    out = []
    for asset, tag in ASSETS.items():
        d = START
        while d < END:
            m = MONTHS[d.month - 1]
            out.append((asset, f"{tag}-above-on-{m}-{d.day}-{d.year}"))
            if d.day == 1:
                out.append((asset, f"what-price-will-{tag}-hit-in-{m}-{d.year}"))
            if d.weekday() == 0:  # Monday: weekly hit markets Mon-Sun
                e = d + timedelta(days=6)
                if e.month == d.month:
                    out.append((asset, f"what-price-will-{tag}-hit-{m}-{d.day}-{e.day}-{d.year}"))
                else:
                    out.append((asset, f"what-price-will-{tag}-hit-{m}-{d.day}-{MONTHS[e.month-1]}-{e.day}-{d.year}"))
            d += timedelta(days=1)
    return out


def fetch_event(item):
    asset, slug = item
    try:
        e = cached("ev_" + slug, lambda: pb.get_json(f"{pb.GAMMA}/events?slug={slug}"))
    except RuntimeError:
        return []
    res = []
    for ev in e or []:
        for m in ev.get("markets", []):
            kind, K = pb.classify(m.get("question", ""))
            if not kind or not m.get("outcomePrices") or not m.get("clobTokenIds"):
                continue
            yes = float(json.loads(m["outcomePrices"])[0])
            if yes not in (0.0, 1.0):
                continue
            res.append({"asset": asset, "slug": slug, "q": m["question"], "kind": kind, "K": K,
                        "start": m.get("startDate") or ev.get("startDate"), "end": m["endDate"],
                        "token": json.loads(m["clobTokenIds"])[0], "yes": yes})
    return res


def fetch_hist(mk):
    s = int(pb.parse_dt(mk["start"]).timestamp()) - 3600
    e = int(pb.parse_dt(mk["end"]).timestamp()) + 3600
    try:
        h = cached("ph_" + mk["token"][:40], lambda: pb.get_json(
            f"https://clob.polymarket.com/prices-history?market={mk['token']}&startTs={s}&endTs={e}&fidelity=60"))
    except RuntimeError:
        return []
    return [(x["t"], x["p"]) for x in h.get("history", [])]


def deribit_series(asset):
    def get():
        spot, vol, t = {}, {}, int(START.timestamp() * 1000) - 86400000 * 3
        stop = int(END.timestamp() * 1000)
        while t < stop:
            t2 = min(t + 86400000 * 30, stop)
            c = pb.get_json(f"{pb.DERIBIT}/get_tradingview_chart_data?instrument_name={asset}-PERPETUAL"
                            f"&start_timestamp={t}&end_timestamp={t2}&resolution=60")["result"]
            for ts, cl, hi, lo in zip(c["ticks"], c["close"], c["high"], c["low"]):
                spot[ts // 1000] = (cl, hi, lo)
            v = pb.get_json(f"{pb.DERIBIT}/get_volatility_index_data?currency={asset}"
                            f"&start_timestamp={t}&end_timestamp={t2}&resolution=3600")["result"]["data"]
            for row in v:
                vol[row[0] // 1000] = row[4] / 100
            t = t2
        return {"spot": spot, "vol": vol}
    d = cached(f"deribit_{asset}", get)
    return ({int(k): v for k, v in d["spot"].items()}, {int(k): v for k, v in d["vol"].items()})


def last_before(series_keys, series, ts, bar_secs=0, max_age=None):
    """Latest value known at time ts. For bars keyed by START time, a bar only counts once it has
    finished (start + bar_secs <= ts), so we never use future prices."""
    import bisect
    i = bisect.bisect_right(series_keys, ts - bar_secs) - 1
    if i < 0:
        return None
    if max_age is not None and ts - series_keys[i] > max_age:
        return None
    return series[series_keys[i]]


def main():
    C = pb.CONFIG
    print("finding past markets...", flush=True)
    with ThreadPoolExecutor(12) as ex:
        markets = [m for r in ex.map(fetch_event, slugs()) for m in r]
    print(f"  {len(markets)} resolved markets", flush=True)
    with ThreadPoolExecutor(12) as ex:
        hists = list(ex.map(fetch_hist, markets))
    D = {a: deribit_series(a) for a in ASSETS}
    keys = {a: (sorted(D[a][0]), sorted(D[a][1])) for a in ASSETS}

    snaps, signals = [], []
    for mk, hist in zip(markets, hists):
        # drop the placeholder 50c price shown before a new market's first real trade
        while hist and abs(hist[0][1] - 0.5) < 1e-9:
            hist = hist[1:]
        if len(hist) < 3:
            continue
        hk = [t for t, _ in hist]; hv = {t: p for t, p in hist}
        spot_s, vol_s = D[mk["asset"]]
        sk, vk = keys[mk["asset"]]
        start, end = pb.parse_dt(mk["start"]), pb.parse_dt(mk["end"])
        t = max(start, START) + timedelta(hours=1)
        # running high/low along hourly bars since the window started (for touch markets)
        bars = [(k, spot_s[k]) for k in sk if int(start.timestamp()) <= k <= int(end.timestamp())]
        bi, run_hi, run_lo = 0, -1.0, 1e18
        while t < end - timedelta(hours=C["MIN_HOURS_LEFT"]):
            ts = int(t.timestamp())
            while bi < len(bars) and bars[bi][0] + 3600 <= ts:
                run_hi = max(run_hi, bars[bi][1][1]); run_lo = min(run_lo, bars[bi][1][2]); bi += 1
            p = last_before(hk, hv, ts, 0, MAX_PRICE_AGE)
            sp = last_before(sk, spot_s, ts, 3600)
            iv = last_before(vk, vol_s, ts, 3600)
            days_left = (end - t).total_seconds() / 86400
            if p is None or sp is None or iv is None or days_left > C["MAX_DAYS_LEFT"]:
                t += timedelta(hours=STEP_H); continue
            S, K, T = sp[0], mk["K"], days_left / 365.25
            if mk["kind"] == "touch":
                if (K > S and run_hi >= K) or (K < S and run_lo <= K):
                    break  # already touched: market is decided
                fair = pb.prob_touch(S, K, iv, T)
            else:
                fair = pb.prob_above_at_end(S, K, iv, T)
            snaps.append((mk, fair, p))
            yes_px, no_px = p + HALF_SPREAD, (1 - p) + HALF_SPREAD
            ey = fair - yes_px - pb.fee(yes_px)
            en = (1 - fair) - no_px - pb.fee(no_px)
            side, px, edge = ("YES", yes_px, ey) if ey >= en else ("NO", no_px, en)
            if edge >= C["MIN_EDGE"] and C["MIN_PRICE"] <= px <= C["MAX_PRICE"]:
                # execution delay test: fill at the Polymarket price DELAY_H hours later
                fill_ts = ts + DELAY_H * 3600
                pf = last_before(hk, hv, fill_ts, 0, MAX_PRICE_AGE + DELAY_H * 3600) if DELAY_H else p
                if pf is None or fill_ts > end.timestamp():
                    break
                fpx = (pf + HALF_SPREAD) if side == "YES" else (1 - pf) + HALF_SPREAD
                signals.append({"ts": ts, "mk": mk, "side": side, "px": min(fpx, 0.999), "edge": edge,
                                "fair": fair if side == "YES" else 1 - fair})
                break  # one bet per market (first signal)
            t += timedelta(hours=STEP_H)

    # apply the per-asset open-bet cap in time order
    signals.sort(key=lambda s: s["ts"])
    open_until = collections.defaultdict(list)
    bets = []
    for s in signals:
        a = s["mk"]["asset"]
        open_until[a] = [e for e in open_until[a] if e > s["ts"]]
        if len(open_until[a]) >= C["MAX_OPEN_PER_ASSET"]:
            continue
        open_until[a].append(pb.parse_dt(s["mk"]["end"]).timestamp())
        won = (s["mk"]["yes"] == 1.0) == (s["side"] == "YES")
        shares = C["STAKE_USD"] / (s["px"] + pb.fee(s["px"]))
        s["won"], s["pnl"] = won, (shares if won else 0) - C["STAKE_USD"]
        bets.append(s)

    out = {"markets": len(markets), "snapshots": len(snaps), "bets": len(bets)}
    if snaps:
        out["brier_model"] = sum((f - m["yes"]) ** 2 for m, f, _ in snaps) / len(snaps)
        out["brier_market"] = sum((p - m["yes"]) ** 2 for m, _, p in snaps) / len(snaps)
    json.dump({"summary": out, "bets": [{k: (v if k != "mk" else {kk: v[kk] for kk in ("asset", "q", "kind", "K", "end", "yes")})
                                          for k, v in b.items()} for b in bets]},
              open(os.path.join(pb.HERE, "backtest_results.json"), "w"), indent=1, default=str)
    print(json.dumps(out, indent=1))
    if bets:
        pnl = sum(b["pnl"] for b in bets); n = len(bets)
        print(f"BETS {n} | PnL ${pnl:,.0f} on ${n * C['STAKE_USD']:,.0f} ({100 * pnl / (n * C['STAKE_USD']):+.1f}%) | "
              f"win {sum(b['won'] for b in bets) / n:.1%} | avg paid {sum(b['px'] for b in bets) / n:.1%} | avg fair {sum(b['fair'] for b in bets) / n:.1%}")
        G = collections.defaultdict(lambda: [0, 0.0, 0, 0.0])
        for b in bets:
            for key in (f"{b['mk']['asset']} {b['mk']['kind']} {b['side']}", "month " + datetime.fromtimestamp(b['ts'], timezone.utc).strftime('%Y-%m')):
                g = G[key]; g[0] += 1; g[1] += b["pnl"]; g[2] += b["won"]; g[3] += b["px"]
        for k in sorted(G):
            n_, p_, w_, px_ = G[k]
            print(f"  {k:22} bets={n_:4} pnl=${p_:8,.0f} win={w_ / n_:.0%} paid={px_ / n_:.0%}")


if __name__ == "__main__":
    main()
