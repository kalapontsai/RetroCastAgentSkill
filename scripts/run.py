#!/usr/bin/env python3
"""scripts/run.py — 一行跑完 analyze + export，給直接 user 用。

Usage:
    python scripts/run.py kadela
    python scripts/run.py kadela --n 10 --withdrawal 80000 --pension 50000
    python scripts/run.py --list  # 列出可用 profile

不做的事（CLI 才做）：
    - JSON envelope 輸出（agent 需要）
    - health / form / upload-profile / preview 等 utility subcommands
    - stdin pipe、--output 精確指定
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# === venv auto-bootstrap + sys.path setup ===
SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
_VENV_PY = SKILL_ROOT / '.venv' / 'bin' / 'python'
if _VENV_PY.exists():
    try:
        import pandas  # noqa: F401
    except ImportError:
        os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

# scripts/ 給 app + app_config；skill root 給 lib
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SKILL_ROOT))

from app import _run_analyze  # noqa: E402
from app_config import (  # noqa: E402
    DEFAULT_N_YEARS, DEFAULT_START_DATE, REPORTS_DIR, USER_PROFILE_DIR,
)
from lib.exporter import render_html_report, render_rebalance_report  # noqa: E402


def cmd_run(args) -> None:
    """一行跑完：analyze → export forecast + rebalance → 印報告路徑。"""
    profile = args.profile
    if not profile:
        profiles = sorted(p.stem for p in USER_PROFILE_DIR.glob('*.csv'))
        if profiles:
            print('Available profiles:')
            for p in profiles:
                print(f'  - {p}')
            print()
            print(f'Usage: python scripts/run.py {profiles[0]}')
        else:
            print('No profiles in user_profile/. Upload a CSV first.')
        sys.exit(1)

    body = {
        'profile': profile,
        'n': args.n,
        'start_date': args.start_date,
        'fee_buy': 0.00142,
        'tax_sell': 0.003,
        'slippage': 0.001,
        'v2_current_age': 55,
        'v2_retirement_age': 60,
        'v2_retirement_end_age': 90,
        'v2_withdrawal_monthly': args.withdrawal,
        'v2_pension_monthly': args.pension,
        'v2_special_expenses': [],
    }

    t0 = time.time()
    print(f'[{profile}] analyze ...')
    try:
        result = _run_analyze(body)
    except Exception as e:
        print(f'ERROR: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(1)
    analyze_t = time.time() - t0
    print(f'[{profile}] analyze OK in {analyze_t:.1f}s')

    print(f'[{profile}] export forecast ...')
    t1 = time.time()
    forecast_html = render_html_report(result, profile_name=profile)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    forecast_out = REPORTS_DIR / f'{profile}_portfolio_forecast_{ts}.html'
    forecast_out.write_text(forecast_html, encoding='utf-8')
    print(f'  → {forecast_out} ({forecast_out.stat().st_size:,} bytes, {time.time()-t1:.1f}s)')

    print(f'[{profile}] export rebalance ...')
    t2 = time.time()
    rebalance_html = render_rebalance_report(result, profile_name=profile)
    rebalance_out = REPORTS_DIR / f'{profile}_portfolio_rebalance_{ts}.html'
    rebalance_out.write_text(rebalance_html, encoding='utf-8')
    print(f'  → {rebalance_out} ({rebalance_out.stat().st_size:,} bytes, {time.time()-t2:.1f}s)')

    total = time.time() - t0
    print(f'\nDone in {total:.1f}s')
    print(f'Open: file://{forecast_out}')


def main() -> None:
    p = argparse.ArgumentParser(
        prog='retrocast-run',
        description='一鍵跑完 RetroCast analyze + export（直接 user 用）',
    )
    p.add_argument('profile', nargs='?', help='Profile 名稱（user_profile/<name>.csv）')
    p.add_argument('--n', type=int, default=DEFAULT_N_YEARS,
                   help=f'回測年數（default {DEFAULT_N_YEARS}）')
    p.add_argument('--start-date', default=DEFAULT_START_DATE,
                   help=f'FinMind 抓價起點（default {DEFAULT_START_DATE}）')
    p.add_argument('--withdrawal', type=float, default=0.0,
                   help='v2 月提款 NT$（default 0）')
    p.add_argument('--pension', type=float, default=0.0,
                   help='v2 月年金 NT$（default 0）')
    args = p.parse_args()
    cmd_run(args)


if __name__ == '__main__':
    main()
