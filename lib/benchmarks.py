"""Benchmarks — 多基準比較 (0050 + 006208)

F5:同時對標 0050、006208 兩檔市值型 ETF,作為「大盤代理基準」。
讓使用者看到自組 portfolio 在「原型 ETF」vs「高股息」下的相對位置。

歷史:
- v3.0.0 預設還含 ^TWII(加權指數),但 FinMind 不提供 TAIEX 日價的 stock-compatible API
  (沒 `TaiwanStockPrice data_id=TAIEX`,也沒 `TaiwanIndices` 這 dataset)
- v3.0.2 拿掉 ^TWII,只留兩個 ETF 作市場代理
  (0050 收費低、流動性高,最貼近市場;006208 是備援)

設計原則:
- 每個 benchmark 獨立計算 metrics(CAGR / Sharpe / MDD)
- 若 benchmark 資料不足(如 006208 在 2017-09-12 前是 phantom data),自動 trim
- 報告含 alpha = portfolio_CAGR - benchmark_CAGR

邊界:
- portfolio 與 benchmark 須對齊到共同日期區間
- 任何 benchmark 歷史 < 60 個交易日 → 跳過並回報
- benchmark 是 ticker string,若 FinMind 抓不到 → 進 `skipped`,不 break 整體 analyze
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

import numpy as np
import pandas as pd


# ───────── Constants ─────────
TRADING_DAYS_PER_YEAR = 252
MIN_HISTORY = 60


# ───────── Custom Errors ─────────
class BenchmarkError(ValueError):
    pass


# ───────── Config / Result ─────────
@dataclass
class BenchmarkConfig:
    benchmarks: list[str] = field(
        default_factory=lambda: ['0050', '006208']  # 兩個市值型 ETF 作為大盤代理
    )
    risk_free_rate: float = 0.015
    portfolio_label: str = 'portfolio'

    def __post_init__(self) -> None:
        # 型別檢查(TypeError)→ 邏輯檢查(ValueError)
        if not isinstance(self.benchmarks, Iterable) or isinstance(self.benchmarks, (str, bytes)):
            raise TypeError(
                f'benchmarks 須為 list[str], got {type(self.benchmarks).__name__}'
            )
        if not isinstance(self.risk_free_rate, (int, float)) or isinstance(self.risk_free_rate, bool):
            raise TypeError(
                f'risk_free_rate 須為 number, got {type(self.risk_free_rate).__name__}'
            )
        if not isinstance(self.portfolio_label, str):
            raise TypeError(
                f'portfolio_label 須為 str, got {type(self.portfolio_label).__name__}'
            )
        self.benchmarks = list(self.benchmarks)
        self.risk_free_rate = float(self.risk_free_rate)
        _validate_config(self)

    def to_dict(self) -> dict:
        return {
            'benchmarks': list(self.benchmarks),
            'risk_free_rate': self.risk_free_rate,
            'portfolio_label': self.portfolio_label,
        }


@dataclass
class BenchmarkResult:
    benchmarks: dict[str, dict]       # {"0050": {"cagr": ..., "sharpe": ..., "mdd": ..., "period": ...}}
    vs_portfolio: dict[str, dict]     # {"alpha_vs_0050": ..., "alpha_vs_006208": ..., ...}
    portfolio_metrics: dict            # portfolio 的 cagr / sharpe / mdd
    period: dict                       # 對齊後的共同起訖
    config: dict = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)  # 資料不足的 benchmark

    def to_dict(self) -> dict:
        return {
            'benchmarks': self.benchmarks,
            'vs_portfolio': self.vs_portfolio,
            'portfolio_metrics': self.portfolio_metrics,
            'period': self.period,
            'config': self.config,
            'skipped': self.skipped,
        }


# ───────── Public API ─────────
def compute_benchmark_compare(
    portfolio_nav: pd.Series,
    benchmark_prices: dict[str, pd.Series],
    config: BenchmarkConfig | None = None,
) -> BenchmarkResult:
    """多基準比較

    Args:
        portfolio_nav: pd.Series(index=Date, value=NAV)
        benchmark_prices: {ticker: pd.Series(index=Date, value=close)}
        config: 設定

    每個 benchmark 會對齊到 portfolio 的日期範圍內共同交易日。
    """
    if config is None:
        config = BenchmarkConfig()
    _validate_config(config)

    if portfolio_nav.empty:
        raise BenchmarkError('portfolio_nav 為空')

    # 計算 portfolio metrics
    port_metrics = _compute_nav_metrics(portfolio_nav, config.risk_free_rate)

    # 計算每個 benchmark
    bench_metrics: dict[str, dict] = {}
    vs_portfolio: dict[str, dict] = {}
    skipped: list[str] = []
    period = {
        'start': str(portfolio_nav.index[0].date()),
        'end': str(portfolio_nav.index[-1].date()),
        'days': len(portfolio_nav),
    }

    for ticker in config.benchmarks:
        prices = benchmark_prices.get(ticker)
        if prices is None or prices.empty:
            skipped.append(f'{ticker} (no data)')
            continue
        # 對齊到 portfolio 日期
        aligned = prices.reindex(portfolio_nav.index).dropna()
        if len(aligned) < MIN_HISTORY:
            skipped.append(f'{ticker} (history < {MIN_HISTORY} after alignment)')
            continue
        m = _compute_nav_metrics(aligned, config.risk_free_rate)
        m['period'] = {
            'start': str(aligned.index[0].date()),
            'end': str(aligned.index[-1].date()),
            'days': len(aligned),
        }
        bench_metrics[ticker] = m
        # alpha = portfolio_CAGR - benchmark_CAGR
        vs_portfolio[f'alpha_vs_{ticker}'] = round(
            float(port_metrics['cagr'] - m['cagr']), 6
        )

    return BenchmarkResult(
        benchmarks=bench_metrics,
        vs_portfolio=vs_portfolio,
        portfolio_metrics=port_metrics,
        period=period,
        config=config.to_dict(),
        skipped=skipped,
    )


# ───────── Internals ─────────
def _validate_config(cfg: BenchmarkConfig) -> None:
    if not cfg.benchmarks:
        raise BenchmarkError('benchmarks 不可為空')
    if cfg.risk_free_rate < 0:
        raise BenchmarkError('risk_free_rate 不可為負')


def _compute_nav_metrics(prices: pd.Series, rf_annual: float) -> dict:
    """從價格序列計算 CAGR / Sharpe / MDD"""
    p = prices.dropna()
    if p.empty or len(p) < 2:
        return {'cagr': None, 'sharpe': None, 'mdd': None, 'total_return': None}

    rets = p.pct_change().dropna()
    yrs = max((p.index[-1] - p.index[0]).days / 365.25, 1 / 365.25)
    total_return = float(p.iloc[-1] / p.iloc[0] - 1)
    cagr = float((p.iloc[-1] / p.iloc[0]) ** (1 / yrs) - 1)

    # Sharpe
    mean_d = float(rets.mean())
    std_d = float(rets.std(ddof=1))
    if std_d > 0:
        rf_daily = rf_annual / TRADING_DAYS_PER_YEAR
        sharpe = float((mean_d - rf_daily) / std_d * np.sqrt(TRADING_DAYS_PER_YEAR))
    else:
        sharpe = None

    # MDD
    cum = p / p.iloc[0]
    peak = cum.cummax()
    dd = cum / peak - 1
    mdd = float(dd.min())

    return {
        'cagr': round(cagr, 6),
        'sharpe': round(sharpe, 6) if sharpe is not None else None,
        'mdd': round(mdd, 6),
        'total_return': round(total_return, 6),
        'years': round(yrs, 2),
    }


# ───────── Convenience wrapper for Flask route ─────────
def run_benchmark_compare(
    portfolio_nav: pd.Series,
    benchmark_prices: dict[str, pd.Series],
    body: dict,
) -> dict:
    """Flask-friendly wrapper"""
    try:
        config = BenchmarkConfig(
            benchmarks=body.get('benchmarks', ['0050', '006208']),
            risk_free_rate=body.get('risk_free_rate', 0.015),
            portfolio_label=body.get('portfolio_label', 'portfolio'),
        )
    except (TypeError, ValueError) as e:
        raise BenchmarkError(f'config 解析失敗:{e}') from e

    result = compute_benchmark_compare(portfolio_nav, benchmark_prices, config)
    return result.to_dict()
