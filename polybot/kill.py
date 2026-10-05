"""Kill switch CLI.

  python -m polybot.kill          # engage: stop all new trading immediately
  python -m polybot.kill --off    # release (trading resumes on next loop tick)
  python -m polybot.kill --status
"""
from __future__ import annotations

import argparse

from .config import load_config
from .risk import RiskManager


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--off", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--reason", default="cli")
    a = ap.parse_args()
    rm = RiskManager(load_config(a.config, env_file=None).risk)
    if a.status:
        print("ENGAGED" if rm.kill_switch_active() else "off", rm.kill_path)
    elif a.off:
        rm.kill_path.unlink(missing_ok=True)
        print("Kill switch released:", rm.kill_path)
    else:
        rm.engage_kill_switch(a.reason)
        print("Kill switch ENGAGED:", rm.kill_path)


if __name__ == "__main__":
    main()
