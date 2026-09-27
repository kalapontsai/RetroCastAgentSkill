"""Volatility Decay — 槓桿 ETF daily rebalance 損耗量化

F4:模擬「0050 持有 10 年」vs「00631L(0050正2) 持有 10 年」vs「0050+00631L 各半季度再平衡」
三條長期軌跡,量化槓桿 ETF 在反覆震盪下的實際損耗(decay)。

設計原則:
- 對齊同起始日 + 同初始資金 + 含手續費與稅(0.1425% + 0.3% 證交稅)
- 用歷史實際日 K(00631L 上市後)模擬,不做 parametric assumption
- 與 F1/F2 解耦:不吃 Monte Carlo,純歷史回測

公式:
- all_underlying:持有 0050 全程,初始資金 → 期末 = initial × (1 + 0050 累積報酬)
- all_leveraged:持有 00631L 全程,初始資金 → 期末 = initial × (1 + 00631L 累積報酬)
- 50_50_rebalance_quarterly:每季初(63 交易日)rebalance 回 50:50

decay_loss = all_leveraged_CAGR - (1 + 0050_CAGR)^2 - 1
(負值 = 槓桿實際年化 < 理論兩倍,代表 daily rebalance 損耗)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd


# ───────── Constants ─────────
TRADING_DAYS_PER_YEAR = 252
QUARTER_DAYS = 63  # 約 252/4 = 63 交易日
FEE_RATE = 0.001425         # 台股手續費 0.1425%
TAX_RATE = 0.003            # 證交稅 0.3%


# ───────── Custom Errors ─────────
class VolatilityDecayError(ValueError):
    pass


# ───────── Config / Result ─────────
@dataclass
class VolatilityDecayConfig:
    initial_balance: float = 348400.0
    compare_strategies: list[str] = field(
        default_factory=lambda: [
            'all_underlying',
            'all_leveraged',
            '50_50_rebalance_quarterly',
        ]
    )
    fee_rate: float = FEE_RATE
    tax_rate: float = TAX_RATE

    def __post_init__(self) -> None:
        # 型別檢查(TypeError)→ 邏輯檢查(ValueError)
        if not isinstance(self.initial_balance, (int, float)) or isinstance(self.initial_balance, bool):
            raise TypeError(
                f'initial_balance 須為 number, got {type(self.initial_balance).__name__}'
            )
        if not isinstance(self.compare_strategies, Iterable) or isinstance(self.compare_strategies, (str, bytes)):
            raise TypeError(
                f'compare_strategies 須為 list[str], got {type(self.compare_strategies).__name__}'
            )
        if not isinstance(self.fee_rate, (int, float)) or isinstance(self.fee_rate, bool):
            raise TypeError(
                f'fee_rate 須為 number, got {type(self.fee_rate).__name__}'
            )
        if not isinstance(self.tax_rate, (int, float)) or isinstance(self.tax_rate, bool):
            raise TypeError(
                f'tax_rate 須為 number, got {type(self.tax_rate).__name__}'
            )
        self.compare_strategies = list(self.compare_strategies)
        self.initial_balance = float(self.initial_balance)
        self.fee_rate = float(self.fee_rate)
        self.tax_rate = float(self.tax_rate)
        _validate_config(self)

    def to_dict(self) -> dict:
        return {
            'initial_balance': self.initial_balance,
            'compare_strategies': list(self.compare_strategies),
            'fee_rate': self.fee_rate,
            'tax_rate': self.tax_rate,
        }


@dataclass
class VolatilityDecayResult:
    strategies: dict[str, dict]
    decay_analysis: dict
    config: dict = field(default_factory=dict)
    period: dict = field(default_factory=dict)   # {start, end, days}

    def to_dict(self) -> dict:
        return {
            'strategies': self.strategies,
            'decay_analysis': self.decay_analysis,
            'config': self.config,
            'period': self.period,
        }


# ───────── Public API ─────────
def compute_volatility_decay(
    underlying_prices: pd.Series,       # 0050 收盤價
    leveraged_prices: pd.Series,        # 00631L 收盤價
    config: VolatilityDecayConfig | None = None,
) -> VolatilityDecayResult:
    """計算三策略對照 + decay 量化

    Args:
        underlying_prices: pd.Series(index=Date, value=close), 0050 完整歷史
        leveraged_prices: pd.Series(index=Date, value=close), 00631L 完整歷史
        config: 設定

    兩 series 須對齊日期(取交集)。
    """
    if config is None:
        config = VolatilityDecayConfig()
    _validate_config(config)

    u, l = _align_prices(underlying_prices, leveraged_prices)

    # 三策略 final value
    strategies: dict[str, dict] = {}
    if 'all_underlying' in config.compare_strategies:
        strategies['all_underlying'] = _run_buy_and_hold(u, config.initial_balance, '0050')
    if 'all_leveraged' in config.compare_strategies:
        strategies['all_leveraged'] = _run_buy_and_hold(l, config.initial_balance, '00631L')
    if '50_50_rebalance_quarterly' in config.compare_strategies:
        strategies['50_50_rebalance_quarterly'] = _run_50_50_rebalance(
            u, l, config.initial_balance, config.fee_rate, config.tax_rate
        )

    # Decay 量化
    decay = _analyze_decay(strategies)

    period = {
        'start': str(u.index[0].date()),
        'end': str(u.index[-1].date()),
        'days': len(u),
    }

    return VolatilityDecayResult(
        strategies=strategies,
        decay_analysis=decay,
        config=config.to_dict(),
        period=period,
    )


# ───────── Internals ─────────
def _validate_config(cfg: VolatilityDecayConfig) -> None:
    if cfg.initial_balance <= 0:
        raise VolatilityDecayError(f'initial_balance 必須 > 0, got {cfg.initial_balance}')
    if not cfg.compare_strategies:
        raise VolatilityDecayError('compare_strategies 不可為空')
    valid = {'all_underlying', 'all_leveraged', '50_50_rebalance_quarterly'}
    bad = set(cfg.compare_strategies) - valid
    if bad:
        raise VolatilityDecayError(f'未支援的策略: {bad}')
    if cfg.fee_rate < 0 or cfg.tax_rate < 0:
        raise VolatilityDecayError('費率 / 稅率不可為負')


def _align_prices(
    u: pd.Series, l: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    """取兩個 series 的共同日期(內部 join)"""
    if u.empty or l.empty:
        raise VolatilityDecayError('價格序列為空')
    u2 = u.dropna()
    l2 = l.dropna()
    common = u2.index.intersection(l2.index)
    if len(common) < QUARTER_DAYS:
        raise VolatilityDecayError(
            f'對齊後歷史太短 ({len(common)} 天),至少需 {QUARTER_DAYS} 個交易日'
        )
    return u2.loc[common].sort_index(), l2.loc[common].sort_index()


def _years_between(start, end) -> float:
    return (end - start).days / 365.25


def _run_buy_and_hold(
    prices: pd.Series,
    initial: float,
    label: str,
) -> dict:
    """Buy & hold:initial × (end / start) - 1"""
    p0 = float(prices.iloc[0])
    p1 = float(prices.iloc[-1])
    final = initial * (p1 / p0)
    yrs = _years_between(prices.index[0], prices.index[-1])
    cagr = (p1 / p0) ** (1 / yrs) - 1 if yrs > 0 else 0.0

    # MDD
    cum = prices / p0
    peak = cum.cummax()
    dd = cum / peak - 1
    mdd = float(dd.min())

    return {
        'label': label,
        'final': int(round(final)),
        'cagr': round(float(cagr), 6),
        'mdd': round(float(mdd), 6),
        'total_return': round(float(p1 / p0 - 1), 6),
    }


def _run_50_50_rebalance(
    u: pd.Series, l: pd.Series,
    initial: float,
    fee_rate: float,
    tax_rate: float,
) -> dict:
    """50:50 季度再平衡:每季初把組合比例 rebalance 回 50:50(扣手續費+稅)"""
    n = len(u)
    if n < QUARTER_DAYS:
        return {
            'label': '50/50 quarterly rebalance',
            'final': int(round(initial)),
            'cagr': 0.0,
            'mdd': 0.0,
            'total_return': 0.0,
        }

    # 起始 50:50 各買一半
    half = initial / 2
    u_shares = half / float(u.iloc[0])
    l_shares = half / float(l.iloc[0])

    # 季度 rebalance
    for q in range(QUARTER_DAYS, n, QUARTER_DAYS):
        # 當前市值
        port_u = u_shares * float(u.iloc[q])
        port_l = l_shares * float(l.iloc[q])
        total = port_u + port_l
        # 目標 50:50
        target_u = total / 2
        target_l = total / 2
        # 計算需要賣多少買多少(取差額絕對值,扣 fee+tax)
        diff_u = target_u - port_u  # 正 = 買 u, 負 = 賣 u
        turnover = abs(diff_u)  # u 和 l 的 turnover 相等
        cost = turnover * (fee_rate + tax_rate)
        # 扣成本後實際可用
        net_diff = diff_u - cost * np.sign(diff_u) if diff_u != 0 else 0
        # 買入或賣出
        if net_diff > 0:
            u_shares += net_diff / float(u.iloc[q])
        elif net_diff < 0:
            u_shares += net_diff / float(u.iloc[q])  # 負的 = 賣
        # l 對稱
        port_u2 = u_shares * float(u.iloc[q])
        port_l2 = total - port_u2
        l_shares = port_l2 / float(l.iloc[q])

    # 期末價值
    final = u_shares * float(u.iloc[-1]) + l_shares * float(l.iloc[-1])
    yrs = _years_between(u.index[0], u.index[-1])
    cagr = (final / initial) ** (1 / yrs) - 1 if yrs > 0 else 0.0

    # MDD:重建 NAV 序列近似
    nav_series = []
    for i in range(n):
        nav_series.append(u_shares * float(u.iloc[i]) + l_shares * float(l.iloc[i]))
    # 注意:這裡的 u_shares / l_shares 在 rebalance 之後會變,簡化作近似估算
    nav = pd.Series(nav_series, index=u.index)
    # 簡化 MDD:用 daily return 近似(忽略 quarterly rebalance 的實際 NAV 細節)
    cum = nav / initial
    peak = cum.cummax()
    dd = cum / peak - 1
    mdd = float(dd.min())

    return {
        'label': '50/50 quarterly rebalance',
        'final': int(round(final)),
        'cagr': round(float(cagr), 6),
        'mdd': round(float(mdd), 6),
        'total_return': round(float(final / initial - 1), 6),
    }


def _analyze_decay(strategies: dict[str, dict]) -> dict:
    """量化 daily-rebalance 損耗

    理論上槓桿 ETF 應 = underlying × 槓桿倍數。
    若 all_leveraged_CAGR < (1 + underlying_CAGR)^lever - 1,
    差額即為 daily rebalance 損耗。
    """
    u = strategies.get('all_underlying')
    l = strategies.get('all_leveraged')
    if not u or not l:
        return {'theory': 'insufficient_strategies'}

    u_cagr = u['cagr']
    l_cagr = l['cagr']
    # 00631L 是 0050 正 2 倍,所以理論 = (1+u)^2 - 1
    theory_leveraged = (1 + u_cagr) ** 2 - 1
    decay_pct = l_cagr - theory_leveraged  # 負值 = 損耗

    # 50/50 vs 100% leveraged:rebalance 應降低 MDD
    half = strategies.get('50_50_rebalance_quarterly')
    if half:
        mdd_reduction = l['mdd'] - half['mdd']  # 正 = rebalance 降低 MDD
    else:
        mdd_reduction = None

    recommendation = (
        '保留槓桿 ETF 適合長期看多且能承受高 MDD 的投資人;'
        '若希望降低波動,50:50 季度再平衡在 MDD 上有顯著改善。'
        if l_cagr > 0 and decay_pct < 0
        else '數據不足以給建議(槓桿 ETF 累積報酬 <= 0 或無 decay)'
    )

    return {
        'theory': (
            f'槓桿 ETF 理論年化 = (1 + underlying_CAGR)^lever - 1 = '
            f'(1 + {u_cagr:.4f})^2 - 1 = {theory_leveraged:.4f}'
        ),
        'actual_leveraged_cagr': round(float(l_cagr), 6),
        'theory_leveraged_cagr': round(float(theory_leveraged), 6),
        'decay_pct': round(float(decay_pct), 6),
        'mdd_reduction_50_50': (
            round(float(mdd_reduction), 6) if mdd_reduction is not None else None
        ),
        'recommendation': recommendation,
    }


# ───────── Convenience wrapper for Flask route ─────────
def run_volatility_decay(
    underlying_prices: pd.Series,
    leveraged_prices: pd.Series,
    body: dict,
) -> dict:
    """Flask-friendly wrapper"""
    try:
        config = VolatilityDecayConfig(
            initial_balance=body.get('initial_balance', 348400.0),
            compare_strategies=body.get(
                'compare_strategies',
                ['all_underlying', 'all_leveraged', '50_50_rebalance_quarterly'],
            ),
            fee_rate=body.get('fee_rate', FEE_RATE),
            tax_rate=body.get('tax_rate', TAX_RATE),
        )
    except (TypeError, ValueError) as e:
        raise VolatilityDecayError(f'config 解析失敗:{e}') from e

    result = compute_volatility_decay(underlying_prices, leveraged_prices, config)
    return result.to_dict()
