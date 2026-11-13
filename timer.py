"""Upload-time guard.

GitHub's own schedule is "best effort": runs are often late or skipped (we saw gaps of 3-7 hours).
This small job wakes up twice an hour. If an upload time is coming up within the next hour (or was
missed), it waits until that exact minute and then starts the main AutoPoster run. So uploads happen
on time even when GitHub skips the normal runs. It makes NO Facebook or TikTok calls itself.
"""
import datetime
import json
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from autopost import parse_times  # noqa: E402

LOOKAHEAD_MIN = 65


def load(name, default):
    try:
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def next_slot(cfg, state, now):
    """Earliest upload time today that is not done yet (missed ones count as 'now')."""
    if cfg.get("paused"):
        return None
    today = now.date().isoformat()
    best = None
    for acc in cfg.get("accounts", []):
        if not acc.get("enabled", True):
            continue
        st = (state.get("accounts") or {}).get(acc.get("id"), {})
        if st.get("last_checked") is None:
            continue                       # brand-new account: engine sets it up first
        done = set((st.get("slots") or {}).get(today, []))
        for hhmm in parse_times(acc.get("times")):
            if hhmm in done:
                continue
            h, m = map(int, hhmm.split(":"))
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if t <= now + datetime.timedelta(minutes=LOOKAHEAD_MIN):
                best = t if best is None or t < best else best
    return best


def main():
    cfg = load("config.json", {})
    state = load("state.json", {})
    tz = ZoneInfo(cfg.get("timezone") or "Asia/Karachi")
    now = datetime.datetime.now(tz)
    t = next_slot(cfg, state, now)
    if not t:
        print("No upload time in the next hour - nothing to do.")
        return
    wait = (t - now).total_seconds()
    if wait > 0:
        print(f"Next upload time {t:%H:%M} - waiting {wait / 60:.1f} min")
        time.sleep(wait + 20)
    else:
        print(f"Upload time {t:%H:%M} was missed by GitHub - starting now")
    r = subprocess.run(["gh", "workflow", "run", "autopost.yml", "-f", "action=auto"],
                       capture_output=True, text=True)
    print(r.stdout, r.stderr)
    r.check_returncode()
    print("Main AutoPoster run started.")


if __name__ == "__main__":
    main()
