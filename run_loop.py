"""
Runs both bot versions every 15 minutes for up to ~5h40m (GitHub jobs max out at 6 hours),
saving results to the repo after each scan. The workflow then starts the next run.
Usage: python run_loop.py [minutes_to_run]
"""
import os, subprocess, sys, time

RUN_MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 340
EVERY = 15 * 60
stop_at = time.time() + RUN_MINUTES * 60


def sh(cmd, env=None):
    print("$", cmd, flush=True)
    return subprocess.run(cmd, shell=True, env=env).returncode


fails_in_a_row = 0
while True:
    ok = all([sh("python paper_bot.py", env=dict(os.environ, MODEL=model)) == 0 for model in ("surface", "atm")])
    fails_in_a_row = 0 if ok else fails_in_a_row + 1
    if fails_in_a_row >= 3:
        # Stop with an error so GitHub shows a red X (and the next run retries)
        sys.exit("Scans failed 3 times in a row - something is broken. See the error above.")
    sh('git add data data_atm && (git diff --cached --quiet || '
       'git commit -q -m "paper bot run $(date -u +%Y-%m-%dT%H:%MZ)")')
    for _ in range(3):  # retry push if the repo changed meanwhile
        if sh("git pull -q --rebase && git push -q") == 0:
            break
        time.sleep(10)
    next_run = (time.time() // EVERY + 1) * EVERY
    if next_run > stop_at:
        break
    time.sleep(max(0, next_run - time.time()))
