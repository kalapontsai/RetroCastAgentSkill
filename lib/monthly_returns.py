"""
lib/monthly_returns.py
v3.0.3 N8: 計算每個 ticker 的逐月逐年報酬表,給 card ⑥ 「歷史真實績效明細表」用。

兩種輸入路徑:
- compute_monthly_returns_by_ticker: 從 daily_returns 出發(走含息 adj close pct_change)
- compute_monthly_returns_via_shares_tracking: 從 raw close + dividend/split events 出發
  (fresh-start-per-month,不被 cumulative shares 稀釋,P0 fix 用這條)

設計:純函式,不做 IO,給 web + html export 共用。
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd


def compute_monthly_returns_by_ticker(
    daily_returns_by_ticker: Dict[str, pd.Series],
) -> dict:
    """輸入:dict[ticker_canonical → pd.Series of daily returns]
    輸出:{
        'tickers': [
            {
                'ticker': '0050',
                'first_year': 2003,
                'last_year': 2026,
                'data': {
                    2003: {'1': 0.0234, '2': 0.0156, ..., 'year_avg': 0.0123},
                    ...
                },
            },
            ...
        ],
    }

    注意:
    - 每個 ticker 自己的歷史範圍(從 cache 拉到最新;不做 union)
    - 沒資料的 cell → None(渲染為 '—')
    - 年平均 = 12 個月報酬的 arithmetic mean(若有 None 跳過)
    """
    result_tickers = []

    for ticker, daily_rets in daily_returns_by_ticker.items():
        if daily_rets is None or len(daily_rets) == 0:
            continue
        # 過濾 NaN
        clean = daily_rets.dropna()
        if len(clean) == 0:
            continue

        # 轉成 (year, month) 分組,算月總報酬
        # monthly_total = (1 + r).prod() - 1
        try:
            # groupby 對 datetime index
            groups = clean.groupby([clean.index.year, clean.index.month])
            monthly_totals = groups.apply(lambda x: (1 + x).prod() - 1)
        except Exception:
            # 相容 string index
            try:
                idx = pd.to_datetime(clean.index)
                groups = clean.groupby([idx.year, idx.month])
                monthly_totals = (1 + groups).prod() - 1
            except Exception:
                continue

        # 整理成 nested dict
        data: dict[int, dict] = {}
        for (year, month), val in monthly_totals.items():
            year = int(year)
            month = int(month)
            if year not in data:
                data[year] = {}
            v = float(val) if np.isfinite(val) else None
            data[year][str(month)] = v

        # 補齊年平均 (arithmetic mean of 12 monthly returns, skip None)
        for year, months in data.items():
            valid = [v for v in months.values() if v is not None]
            if valid:
                months['year_avg'] = float(np.mean(valid))
            else:
                months['year_avg'] = None

        if not data:
            continue

        first_year = min(data.keys())
        last_year = max(data.keys())

        result_tickers.append({
            'ticker': ticker,
            'first_year': first_year,
            'last_year': last_year,
            'data': data,
        })

    # 排序:ticker 字母序
    result_tickers.sort(key=lambda x: x['ticker'])

    return {'tickers': result_tickers}


def compute_monthly_returns_via_shares_tracking(
    raw_pivot: pd.DataFrame,
    dividends_by_ticker: Optional[Dict[str, list]] = None,
    splits_by_ticker: Optional[Dict[str, list]] = None,
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
) -> dict:
    """v3.0.4 P0 fix: 從 raw close + events 直接算逐月報酬,與一.6 同源 (nav series 共享)。

    為什麼不用 daily returns 進來:
      - 如果 daily returns 來自 cumulative adj close (build_adjusted_close + pct_change)
        則 5y window 內的 dividend 貢獻會被「從 IPO 累積到今天的 shares」稀釋掉
        (2330 ~15%, 主人 2026-08-31 修過 v4 double-count bug 就是這個)
      - 本函數:走 lib.portfolio._window_shares_tracking_nav,跟一.6 per_stock_n_year_window
        共用同一個 NAV series 算法

    Args:
        raw_pivot: DataFrame index=Date, columns=Ticker, values=raw close
        dividends_by_ticker: {ticker: [{date, cash_div, stock_div_ratio}, ...]}
        splits_by_ticker: {ticker: [{date, split_ratio}, ...]}
        window_start, window_end: 限制 shares tracking 範圍
                                 (None = 整段 raw_pivot)
                                 例如 '2021-08-31' → shares 從這天 = 1.0 重數
                                 讓「5y 月報酬連乘」==「5y total return」

    Returns:
        跟 compute_monthly_returns_by_ticker 一樣的結構
        {
            'tickers': [
                {'ticker', 'first_year', 'last_year', 'data': {year: {month: ret, ..., 'year_avg': ret}}},
                ...
            ]
        }
        - 月報酬 = nav(month_end) / nav(month_start) - 1
        - year_avg = 12 個月報酬的 arithmetic mean (skip None)

    5y compound 跟一.6 對齊的關鍵:
      若 window_start = '2021-08-31'、window_end = '2026-08-31',
      shares 從 2021-08-31 = 1.0 開始,逐日吸收 window 內 div/split,
      月報酬連乘 60 個月 ≡ (nav(2026-08-31) / nav(2021-08-31)) - 1 ≡ per_stock_n_year_window.total_return
    """
    from .portfolio import _window_shares_tracking_nav  # 跟一.6 同源

    dividends_by_ticker = dividends_by_ticker or {}
    splits_by_ticker = splits_by_ticker or {}
    if raw_pivot.empty:
        return {'tickers': []}

    # 限制 shares tracking 到 window 範圍(與一.6 對齊)
    if window_start is not None:
        ws = pd.Timestamp(window_start)
    else:
        ws = None
    if window_end is not None:
        we = pd.Timestamp(window_end)
    else:
        we = None

    result_tickers = []
    for ticker in raw_pivot.columns:
        s = raw_pivot[ticker].dropna()
        s = s[s > 0]  # sentinel 過濾
        if s.empty:
            continue

        # 先限到 window 範圍(若指定)
        if ws is not None:
            s = s[s.index >= ws]
        if we is not None:
            s = s[s.index <= we]
        if len(s) < 2:
            continue

        # 收集 window 範圍內的所有事件
        index_dates_str = set(s.index.strftime('%Y-%m-%d').tolist())
        events_by_date: dict[str, list[tuple]] = {}
        for d in dividends_by_ticker.get(ticker) or []:
            base = d.get('date')
            if not base or base not in index_dates_str:
                continue
            cash = float(d.get('cash_div', 0) or 0)
            sr = float(d.get('stock_div_ratio', 0) or 0)
            if cash == 0 and sr == 0:
                continue
            events_by_date.setdefault(base, []).append(('div', cash, sr))
        for sp in splits_by_ticker.get(ticker) or []:
            base = sp.get('date')
            if not base or base not in index_dates_str:
                continue
            ratio = float(sp.get('split_ratio', 1.0) or 1.0)
            if ratio == 1.0:
                continue
            events_by_date.setdefault(base, []).append(('split', ratio))

        # 走跟一.6 一樣的 shares tracking(window 起點 shares=1)
        nav = _window_shares_tracking_nav(s, events_by_date)

        # 從 nav 抽逐月報酬:**跨月延續** = month_end_nav / prev_month_end_nav - 1
        # 這樣 5y compound = (nav_last/nav_first) - 1 跟一.6 ground truth 100% 對齊
        data: dict = {}
        try:
            groups = nav.groupby([nav.index.year, nav.index.month])
        except Exception:
            continue

        prev_month_end_nav: float | None = None  # 上個月最後交易日的 NAV
        for (year, month), month_navs in groups:
            year = int(year)
            month = int(month)
            if year not in data:
                data[year] = {}
            month_end_nav = float(month_navs.iloc[-1])
            if prev_month_end_nav is not None and prev_month_end_nav > 0:
                ret = month_end_nav / prev_month_end_nav - 1
                v = float(ret) if np.isfinite(ret) else None
            else:
                # 第一個月:用 month_end_nav / month_first_nav - 1
                # 這樣跟 nav_first_day(預設 shares=1) 的基準一致,
                # 跨月連乘時不會被 first month 「吞掉」(5y compound 會等於 nav_last/nav_first - 1)
                month_first_nav = float(month_navs.iloc[0])
                if month_first_nav > 0:
                    v = float(month_end_nav / month_first_nav - 1)
                else:
                    v = None
            data[year][str(month)] = v
            prev_month_end_nav = month_end_nav

        if not data:
            continue

        # year_avg = arithmetic mean of 12 monthly returns
        for year, months in data.items():
            valid = [v for v in months.values() if v is not None]
            if valid:
                months['year_avg'] = float(np.mean(valid))
            else:
                months['year_avg'] = None

        first_year = min(data.keys())
        last_year = max(data.keys())
        result_tickers.append({
            'ticker': ticker,
            'first_year': first_year,
            'last_year': last_year,
            'data': data,
        })

    result_tickers.sort(key=lambda x: x['ticker'])
    return {'tickers': result_tickers}
