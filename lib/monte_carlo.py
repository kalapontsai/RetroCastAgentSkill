"""Monte Carlo Simulation Engine — block bootstrap from historical returns

共用引擎:
- F1 純成長模擬（annual_withdrawal = 0）
- F2 退休提款模擬（annual_withdrawal > 0）

設計原則:
- 向量化 numpy（單 thread）— 10,000 sims × 30 years 應 < 60s
- Block bootstrap 保留序列相關性（避免 iid 假設,Politis & Romano 1994）
- 可重現:seed 控制 → 同 inputs 結果變動 < 0.5%
- 與 v1 lib 解耦:只吃日報酬 pd.Series,輸出 dict / dataclass

邊界:
- initial_balance <= 0 → raise
- horizon_years > 50 → raise（SPEC F1 §2 規範）
- 歷史 < 252 交易日 → raise
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd


# ───────── Constants ─────────
TRADING_DAYS_PER_YEAR = 252
MIN_HISTORY_DAYS = 252  # 至少 1 年歷史


# ───────── Types ─────────
RebalanceMode = Literal['buy_and_hold', 'annual', 'quarterly']


# ───────── Custom Errors ─────────
class MonteCarloError(ValueError):
    pass


# ───────── Config / Result ─────────
@dataclass
class MonteCarloConfig:
    initial_balance: float
    horizon_years: int
    n_simulations: int = 10_000
    annual_withdrawal: float = 0.0           # F1 = 0；F2 填月提款 × 12
    withdrawal_inflation: float = 0.03       # 3% 通膨調整（年提款逐年成長）
    rebalance: RebalanceMode = 'buy_and_hold'
    block_bootstrap: bool = True             # True = 保留序列結構（推薦）
    block_size_days: int = 21                # 區塊大小（≈ 1 個月交易日）
    seed: int | None = None                  # None = 隨機

    def to_dict(self) -> dict:
        return {
            'initial_balance': self.initial_balance,
            'horizon_years': self.horizon_years,
            'n_simulations': self.n_simulations,
            'annual_withdrawal': self.annual_withdrawal,
            'withdrawal_inflation': self.withdrawal_inflation,
            'rebalance': self.rebalance,
            'block_bootstrap': self.block_bootstrap,
            'block_size_days': self.block_size_days,
            'seed': self.seed,
        }


@dataclass
class MonteCarloResult:
    summary: dict
    yearly_stats: list[dict] = field(default_factory=list)
    percentile_bands: list[dict] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    n_simulations: int = 0
    horizon_years: int = 0

    def to_dict(self) -> dict:
        return {
            'summary': self.summary,
            'yearly_stats': self.yearly_stats,
            'percentile_bands': self.percentile_bands,
            'config': self.config,
            'n_simulations': self.n_simulations,
            'horizon_years': self.horizon_years,
        }


# ───────── Public API ─────────
def simulate_monte_carlo(
    daily_returns: pd.Series | np.ndarray,
    config: MonteCarloConfig,
) -> MonteCarloResult:
    """核心引擎:從歷史日報酬抽樣,模擬 N 年資產軌跡

    Args:
        daily_returns: 歷史日報酬序列（NaN 自動剔除）
        config: 模擬設定

    Returns:
        MonteCarloResult 含 summary + yearly_stats + percentile_bands
    """
    _validate_config(config)
    rets = _prepare_returns(daily_returns)

    n_sims = config.n_simulations
    horizon_years = config.horizon_years
    total_days = horizon_years * TRADING_DAYS_PER_YEAR

    rng = np.random.default_rng(config.seed)

    # 1. 抽樣日報酬:shape (n_sims, total_days)
    if config.block_bootstrap:
        sampled = _block_bootstrap(
            rets, total_days, n_sims, config.block_size_days, rng
        )
    else:
        # iid bootstrap（保留作 baseline 對照,T1.2 用）
        idx = rng.integers(0, len(rets), size=(n_sims, total_days))
        sampled = rets[idx]

    # 2. 累積 NAV（不扣提款的 baseline path）
    cumret = np.cumprod(1.0 + sampled, axis=1)
    nav = config.initial_balance * cumret  # shape (n_sims, total_days)

    # 3. 套用提款（F2 用,F1 預設跳過）
    if config.annual_withdrawal > 0:
        _apply_withdrawals(
            nav,
            annual_withdrawal=config.annual_withdrawal,
            inflation=config.withdrawal_inflation,
            horizon_years=horizon_years,
        )

    # 4. 統計
    final_values = nav[:, -1]
    summary = _compute_summary(final_values, config.initial_balance)
    yearly_stats, percentile_bands = _compute_yearly_stats(nav, horizon_years)

    return MonteCarloResult(
        summary=summary,
        yearly_stats=yearly_stats,
        percentile_bands=percentile_bands,
        config=config.to_dict(),
        n_simulations=n_sims,
        horizon_years=horizon_years,
    )


# ───────── Internals ─────────
def _validate_config(cfg: MonteCarloConfig) -> None:
    if cfg.initial_balance <= 0:
        raise MonteCarloError(
            f'initial_balance 必須 > 0,got {cfg.initial_balance}'
        )
    if cfg.horizon_years < 1:
        raise MonteCarloError(
            f'horizon_years 必須 >= 1,got {cfg.horizon_years}'
        )
    if cfg.horizon_years > 50:
        raise MonteCarloError(
            f'horizon_years 不能 > 50 (SPEC §2 F1 規範),got {cfg.horizon_years}'
        )
    if cfg.n_simulations < 100:
        raise MonteCarloError(
            f'n_simulations 太低 (至少 100),got {cfg.n_simulations}'
        )
    if cfg.block_size_days < 1:
        raise MonteCarloError(
            f'block_size_days 必須 >= 1,got {cfg.block_size_days}'
        )
    if cfg.annual_withdrawal < 0:
        raise MonteCarloError(
            f'annual_withdrawal 不能 < 0,got {cfg.annual_withdrawal}'
        )


def _prepare_returns(
    daily_returns: pd.Series | np.ndarray,
) -> np.ndarray:
    if isinstance(daily_returns, pd.Series):
        rets = daily_returns.dropna().to_numpy()
    else:
        rets = np.asarray(daily_returns, dtype=float)
        rets = rets[~np.isnan(rets)]

    if len(rets) < MIN_HISTORY_DAYS:
        raise MonteCarloError(
            f'歷史日報酬太短 ({len(rets)} 天),至少需 {MIN_HISTORY_DAYS} 個交易日'
        )
    return rets


def _block_bootstrap(
    rets: np.ndarray,
    total_days: int,
    n_sims: int,
    block_size: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """區塊 bootstrap:每次抽 block_size 連續天數,保留序列相關性

    Politis & Romano (1994) stationary block bootstrap:
    每次抽一個起始點,取 block_size 連續天數;若超過歷史長度則繞回 (modulo)

    Returns:
        shape (n_sims, total_days)
    """
    n = len(rets)
    n_blocks = int(np.ceil(total_days / block_size))

    # 起始位置:每個 sim 抽 n_blocks 個
    starts = rng.integers(0, n, size=(n_sims, n_blocks))
    # offsets:0..block_size-1
    offsets = np.arange(block_size)
    # idx[i, j, k] = (starts[i, j] + k) % n   → 形狀 (n_sims, n_blocks, block_size)
    idx = (starts[:, :, None] + offsets[None, None, :]) % n

    sampled = rets[idx]                     # (n_sims, n_blocks, block_size)
    sampled = sampled.reshape(n_sims, n_blocks * block_size)
    return sampled[:, :total_days]


def _apply_withdrawals(
    nav: np.ndarray,
    annual_withdrawal: float,
    inflation: float,
    horizon_years: int,
) -> None:
    """每年初扣一筆（依通膨逐年調整）— in-place 修改 nav

    annual_withdrawal 是 year-1 名目金額;
    year-y 名目 = annual_withdrawal * (1 + inflation) ** (y-1)。
    每日扣 amount = year_withdrawal / TRADING_DAYS_PER_YEAR。

    不允許負值 — 破產後該路徑固定為 0（後續 summary prob_zero_or_negative 用）。
    """
    n_sims, _ = nav.shape

    for y in range(horizon_years):
        w_y = annual_withdrawal * (1 + inflation) ** y
        per_day = w_y / TRADING_DAYS_PER_YEAR
        start = y * TRADING_DAYS_PER_YEAR
        end = min(start + TRADING_DAYS_PER_YEAR, nav.shape[1])
        nav[:, start:end] -= per_day
        np.maximum(nav[:, start:end], 0.0, out=nav[:, start:end])


def _safe_int_round(value) -> int | None:
    """float → int(round()); NaN/inf → None。

    背景:cumprod 遇到極端日報酬會 overflow 成 +inf;np.percentile 在 inf 端做線性內插
    會產 NaN。直接 int(round(inf)) / int(round(nan)) 會 raise,這裡轉成 None 讓 JSON
    序列化時是 null,前端可選擇顯示 '—' 或 'overflow'。
    """
    f = float(value)
    if not np.isfinite(f):
        return None
    return int(round(f))


def _compute_summary(final_values: np.ndarray, initial: float) -> dict:
    # 把 inf/-inf 換成 NaN(避免 median/percentile/mean 直接 inf → OverflowError)
    clean = np.where(np.isfinite(final_values), final_values, np.nan)
    if np.all(np.isnan(clean)):
        # 全 inf / 全 NaN(罕見,代表所有路徑都 overflow)→ int 統計全部 None
        int_stats = {'median_final': None, 'p5_final': None, 'p10_final': None,
                     'p25_final': None, 'p50_final': None, 'p75_final': None,
                     'p90_final': None, 'p95_final': None, 'mean_final': None,
                     'std_final': None}
    else:
        # np.nanpercentile 內部線性內插遇到 NaN 會噴 RuntimeWarning(invalid value),
        # 但結果是正確的(nan-aware),這裡壓掉避免 log 噪音。
        with np.errstate(invalid='ignore'):
            int_stats = {
                'median_final': _safe_int_round(np.nanmedian(clean)),
                # Phase 6 (Item 5): F1 完整分位數 P5/P10/P25/P50/P75/P90/P95 + 標準差
                'p5_final': _safe_int_round(np.nanpercentile(clean, 5)),
                'p10_final': _safe_int_round(np.nanpercentile(clean, 10)),
                'p25_final': _safe_int_round(np.nanpercentile(clean, 25)),
                'p50_final': _safe_int_round(np.nanpercentile(clean, 50)),
                'p75_final': _safe_int_round(np.nanpercentile(clean, 75)),
                'p90_final': _safe_int_round(np.nanpercentile(clean, 90)),
                'p95_final': _safe_int_round(np.nanpercentile(clean, 95)),
                'mean_final': _safe_int_round(np.nanmean(clean)),
                'std_final': _safe_int_round(np.nanstd(clean, ddof=1)),
            }
    # prob 欄位:用原始 final_values,因為 inf > initial 是 True(沒破產)、inf <= 0 是 False
    # 這些 NaN/inf 自然被當作「未破產」處理,語意正確。
    return {
        **int_stats,
        'prob_above_initial': float(np.mean(final_values > initial)),
        'prob_zero_or_negative': float(np.mean(final_values <= 0)),
        'survival_to_horizon': float(np.mean(final_values > 0)),
    }


def _compute_yearly_stats(
    nav: np.ndarray,
    horizon_years: int,
) -> tuple[list[dict], list[dict]]:
    """yearly_stats:每年一筆 median/p10/p90
    percentile_bands:每年 5 條（P5/P25/P50/P75/P95）for chart
    """
    yearly_stats: list[dict] = []
    percentile_bands: list[dict] = []

    for y in range(1, horizon_years + 1):
        idx = min(y * TRADING_DAYS_PER_YEAR, nav.shape[1] - 1)
        vals = np.where(np.isfinite(nav[:, idx]), nav[:, idx], np.nan)
        with np.errstate(invalid='ignore'):
            yearly_stats.append({
                'year': y,
                'median': _safe_int_round(np.nanmedian(vals)),
                'p10': _safe_int_round(np.nanpercentile(vals, 10)),
                'p90': _safe_int_round(np.nanpercentile(vals, 90)),
            })

    for y in range(1, horizon_years + 1):
        idx = min(y * TRADING_DAYS_PER_YEAR, nav.shape[1] - 1)
        vals = np.where(np.isfinite(nav[:, idx]), nav[:, idx], np.nan)
        for p in (5, 25, 50, 75, 95):
            with np.errstate(invalid='ignore'):
                value = _safe_int_round(np.nanpercentile(vals, p))
            percentile_bands.append({
                'percentile': p,
                'year': y,
                'value': value,
            })

    return yearly_stats, percentile_bands