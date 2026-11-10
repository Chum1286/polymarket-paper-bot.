# Polymarket crypto paper-trading bot

Pretend-money test of the "crypto price level" strategy. No real money, no wallet, no login.

## How it works (simple version)
1. Every 15 minutes it looks at Polymarket markets like "Will Bitcoin dip to $70,000 in November?"
2. It calculates a **fair chance** for each one using crypto options prices from Deribit
   (the biggest crypto options exchange). Options prices show how much traders expect prices to move.
3. If Polymarket's price is at least **5 cents cheaper than fair** (after fees), it records a pretend $20 bet.
4. When markets end, it records whether each pretend bet won or lost.

## Files
- `paper_bot.py`: the bot. Settings are at the top in `CONFIG`.
- `report.py`: shows results and a PASS/FAIL verdict.
- `data/paper_trades.csv`: every pretend bet.
- `data/scans.csv`: every market it checked (fair price vs. Polymarket price).

## Run it on your laptop
Needs Python 3 (no extra packages).
```
python paper_bot.py     # one scan
python report.py        # results
```

## Two versions run side by side
- `data/`: **surface** version (volatility at each market's price level, includes the extra cost traders pay for crash protection)
- `data_atm/`: **atm** version (one volatility per date, no crash premium; same as the backtest)

See results with `python report.py` (surface) or `MODEL=atm python report.py` (atm).

## How it runs
The repo must be **public** (unlimited free GitHub minutes). Each run scans every 15 minutes for
about 5h40m, saves results, then starts the next run. A backup schedule restarts it every 6 hours if the
chain breaks. If the repo is private, it only does one scan per run so free minutes aren't used up.

## Run it automatically for free (recommended): GitHub Actions
Runs every 15 minutes in the cloud, even when your laptop is off.
1. Make a free GitHub account, then create a new **private** repository.
2. Upload everything in this folder, including the `.github` folder.
3. Open the repo's **Actions** tab and enable workflows.
4. Click **paper-bot → Run workflow** once to test it.
5. After that it runs by itself. Results build up in the `data` folder.

Note: GitHub sometimes delays scheduled runs or skips a few. That's fine for this test.

## When to judge it
Wait for **100+ settled bets** (probably 3–4 weeks), then run `python report.py`.
It passes only if:
- total pretend profit is positive,
- it wins more often than the prices it paid, and
- its fair prices were more accurate than Polymarket's prices.

If it fails, don't use real money.

## Known simplifications
- Fair price uses a standard options model (lognormal, no trend). Real prices jump more than the model assumes.
- Polymarket settles on Binance prices; the bot uses Deribit's index. They're usually within a few dollars.
- Fees are an estimate (`FEE_RATE`). Change it once you know Polymarket's exact fee.
- Pretend bets assume you buy at the current asking price, which is conservative.

## Backtest (June 1 – Nov 9, 2026)
`python backtest.py` replays the bot's rules on ~4,700 past markets (set `DELAY_H=1` to assume
you buy 1 hour after the signal). Results with $20 pretend bets:

| Buy delay after signal | Bets | Profit | Return |
|---|---|---|---|
| none | 530 | +$3,871 | +36.5% |
| 1 hour | 530 | +$2,161 | +20.4% |
| 3 hours | 530 | +$769 | +7.3% |

The bot's fair prices were only slightly more accurate than Polymarket's (error score 0.0529 vs 0.0538).
Profit shrinks fast with delay, so much of it comes from reacting before Polymarket catches up.
Backtests overstate real results (old prices may not have been available to buy), so this only
says "worth paper trading," not "will make money."
