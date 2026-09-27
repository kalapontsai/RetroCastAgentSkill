"""
Historical N-Year Rolling Outcome Forecast
- 從 Portfolio NAV 建立所有 N-Year rolling periods
- 計算每段 CAGR → 取 P10/P25/P50/P75/P90
- 對應 Bear / Conservative / Base / Optimistic / Bull
- 計算 N 年後終值 FV = PV * (1 + r)^N

Phase 6 (Item 5): 加 P5 / P25 / P75 / P95 / 平均值 / 標準差到 scenario_percentiles
Phase 6 (Item 3): build_forecast 加 excluded_count / min_actual_years / max_actual_years
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# 終值情境名稱（與 SKILL.md 對應）
SCENARIOS = [
    ('Bear',         0.10, 'Bear (P10)'),
    ('Conservative', 0.25, 'Conservative (P25)'),
    ('Base',         0.50, 'Base (P50)'),
    ('Optimistic',   0.75, 'Optimistic (P75)'),
    ('Bull',         0.90, 'Bull (P90)'),
]

# Phase 6 (Item 5): checklist §三 要求 F1 輸出 P5/P10/P25/P50/P75/P90/P95 + 平均 + 標準差
ALL_PERCENTILES = [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]


class ForecastError(ValueError):
    pass


def rolling_n_year_cagr(
    nav: pd.Series,
    n: int,
    min_year_coverage: float = 0.95,
) -> pd.DataFrame:
    """
    從 NAV 建立所有 N-Year rolling periods。
    Returns DataFrame: [start, end, years, cagr]
    """
    if not isinstance(nav, pd.Series) or nav.empty:
        raise ForecastError('NAV 為空')
    if n < 1:
        raise ForecastError('N 必須 >= 1')
    if len(nav) < 2:
        raise ForecastError('NAV 至少需 2 個資料點')

    idx = nav.index
    out = []
    for i, d in enumerate(idx):
        target = d + pd.DateOffset(years=n)
        # 找第一個 >= target 的位置
        j = idx.searchsorted(target, side='left')
        if j >= len(idx):
            break
        end = idx[j]
        years = (end - d).days / 365.25
        if years < n * min_year_coverage:
            continue
        v0 = nav.iloc[i]
        v1 = nav.iloc[j]
        if v0 <= 0:
            continue
        cagr = (v1 / v0) ** (1 / years) - 1
        out.append((d, end, years, cagr))
    if not out:
        raise ForecastError(f'歷史資料不足以建立 N={n} 年 rolling outcome（最少 {min_year_coverage*100:.0f}% 覆蓋）')
    return pd.DataFrame(out, columns=['start', 'end', 'years', 'cagr'])


def scenario_percentiles(rolling: pd.DataFrame) -> dict[str, float]:
    """給定 [cagr] 的 rolling df，回傳 {情境: cagr}"""
    out = {}
    for name, q, _full in SCENARIOS:
        out[name] = float(rolling['cagr'].quantile(q))
    return out


def rolling_sample_stats(rolling: pd.DataFrame, target_n: int, tolerance: float = 0.5) -> dict:
    """Phase 6 (Item 3): 樣本有效性統計

    - valid_count: 滿足 actual_years >= N - tolerance 的樣本數
    - excluded_count: 未滿足的樣本數
    - min_actual_years: 實際持有年數最短值
    - max_actual_years: 實際持有年數最長值

    Returns:
        dict: {
          'valid_count': int,
          'excluded_count': int,
          'min_actual_years': float | None,
          'max_actual_years': float | None,
        }
    """
    if rolling is None or len(rolling) == 0:
        return {'valid_count': 0, 'excluded_count': 0,
                'min_actual_years': None, 'max_actual_years': None}
    years = rolling['years'].astype(float)
    threshold = target_n - tolerance
    valid_mask = years >= threshold
    valid = int(valid_mask.sum())
    excluded = int((~valid_mask).sum())
    return {
        'valid_count': valid,
        'excluded_count': excluded,
        'min_actual_years': float(years.min()) if len(years) else None,
        'max_actual_years': float(years.max()) if len(years) else None,
    }


def extended_percentiles(rolling: pd.DataFrame) -> dict[str, float]:
    """Phase 6 (Item 5): F1 完整指標 — P5/P10/P25/P50/P75/P90/P95/平均/標準差

    Returns:
        dict: {
          'P5': ..., 'P10': ..., ..., 'P95': ...,
          'mean': float, 'std': float,
        }
    """
    if rolling is None or len(rolling) == 0:
        return {f'P{int(q*100)}': None for q in ALL_PERCENTILES} | {'mean': None, 'std': None}
    cagrs = rolling['cagr'].astype(float)
    out = {f'P{int(q*100)}': float(cagrs.quantile(q)) for q in ALL_PERCENTILES}
    out['mean'] = float(cagrs.mean())
    out['std'] = float(cagrs.std(ddof=1)) if len(cagrs) > 1 else 0.0
    return out


def future_value(pv: float, r: float, n: int) -> float:
    """FV = PV * (1+r)^N"""
    if pv < 0:
        raise ForecastError('目前資產不得為負')
    return pv * (1 + r) ** n


def build_forecast(nav: pd.Series, n: int, pv: float) -> dict:
    """
    完整 N-Year forecast 結果：
    {
      'n': int,
      'pv': float,
      'rolling_count': int,
      'rolling': [{start, end, years, cagr}] (給前端畫圖，全部)
      'percentiles': {Bear: ..., P10: ..., ...},
      'scenarios': [{scenario, percentile, cagr, future_value, multiple}]
    }
    """
    rolling = rolling_n_year_cagr(nav, n)
    pct = scenario_percentiles(rolling)
    # Phase 6 (Item 5): F1 完整指標（P5/P25/P75/P95 + mean + std）
    ext_pct = extended_percentiles(rolling)
    # Phase 6 (Item 3): 樣本有效性統計（valid / excluded / 最短最長持有年數）
    sample_stats = rolling_sample_stats(rolling, target_n=n, tolerance=0.5)
    scenarios = []
    for name, q, _full_name in SCENARIOS:
        r = pct[name]
        fv = future_value(pv, r, n)
        scenarios.append({
            # 'scenario' 保留作 compatibility，但主名稱以 'label' 為準
            'scenario': f'{name} (P{int(q*100)})',
            'label': name,
            'quantile': q,
            'cagr': r,
            'fv': fv,
            'multiplier': fv / pv if pv > 0 else None,
        })
    rolling_list = [
        {
            'start': str(r.start.date()),
            'end': str(r.end.date()),
            'years': round(float(r.years), 2),
            'cagr': float(r.cagr),
        }
        for r in rolling.itertuples(index=False)
    ]
    return {
        'n': n,
        'pv': pv,
        'rolling_count': len(rolling),  # 保留舊名作 compatibility
        'r_count': len(rolling),         # 主名稱（驗收標準 #5）
        'percentiles': pct,
        'extended_percentiles': ext_pct,  # Phase 6 (Item 5)
        'sample_stats': sample_stats,      # Phase 6 (Item 3)
        'scenarios': scenarios,
        'rolling': rolling_list,
    }
