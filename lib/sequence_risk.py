"""F2 Sequence Risk — 退休提款模擬

Wraps F1 引擎 + 加入:
- 月→年提款轉換(withdrawal_monthly × 12 = annual_withdrawal)
- 通膨調整(year-y 名目 = annual × (1+inflation)^y)
- ruin age tracking(記錄每個 sim 的破產年齡)
- success_rate_by_age(每年末資產 > 0 的 sim 比例)
- scenario_examples(每年 median balance + 名目年提款)

設計:
- 跟 F1 共用 `_block_bootstrap` / `_prepare_returns` / `TRADING_DAYS_PER_YEAR`
- F2 自己做 raw simulation loop,為了追蹤 ruin_age + success_rate_by_age
  (F1 的 MonteCarloResult 只給 summary + yearly_stats,沒暴露 per-year 是否 ruin)
- 與 F1 在 withdrawal=0 時**邏輯完全一致**(T2.3 acceptance:等同 F1)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from lib.monte_carlo import (
    TRADING_DAYS_PER_YEAR,
    MonteCarloError,
    _block_bootstrap,
    _prepare_returns,
    _safe_int_round,
)


# ───────── Errors ─────────
class SequenceRiskError(ValueError):
    pass


# ───────── Config ─────────
@dataclass
class SequenceRiskConfig:
    initial_balance: float
    retirement_age: int
    horizon_years: int
    withdrawal_monthly: float              # 月提款 NT$
    withdrawal_inflation: float = 0.03
    n_simulations: int = 10_000
    block_bootstrap: bool = True
    block_size_days: int = 21
    seed: int | None = None
    # v2.1 (Phase 1.1): 年齡模型 — 區分「目前年齡」、「退休年齡」、「退休評估終點」
    # 向後相容:兩個新欄位都是 Optional,沒傳就用 retirement_age / current_age + horizon_years 反推
    current_age: int | None = None
    retirement_end_age: int | None = None
    # v2.2 (Phase 2C): 外部現金流注入 — 年金(勞保/勞退月領)
    # pension_monthly: 起始月領金額 (NT$), 預設 0 = 無
    # pension_inflation: 年調幅 (預設 2% — 勞保年金調整機制近似值)
    # pension_start_age: 開始領取年齡, 預設 retirement_age
    pension_monthly: float = 0.0
    pension_inflation: float = 0.02
    pension_start_age: int | None = None
    # v2.3 (Phase 2D): 一次性大額支出 (醫療/長照/裝修/旅遊)
    # list[dict(year_offset, amount, label?)]; year_offset 從 current_age 開始計算
    # 預設 [] = 無
    special_expenses: list = field(default_factory=list)
    _explicit_extended_horizon: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        explicit_end_age = self.retirement_end_age is not None
        # Backward-compat defaults: 沒傳 current_age → 從 retirement_age 開始(舊行為)
        if self.current_age is None:
            self.current_age = self.retirement_age
        # 沒傳 retirement_end_age → 從 current_age + horizon_years 推(讓 horizon_years 保持 single source of truth)
        if self.retirement_end_age is None:
            self.retirement_end_age = self.current_age + self.horizon_years
        self._explicit_extended_horizon = explicit_end_age and self.retirement_end_age >= 110
        # Phase 2C: pension_start_age 預設 = retirement_age
        if self.pension_start_age is None:
            self.pension_start_age = self.retirement_age
        # 跨欄位一致性:horizon_years 必須等於 retirement_end_age - current_age
        # 這條把「使用者同時亂填兩個欄位」擋下,避免默默採用錯誤語意
        derived_horizon = self.retirement_end_age - self.current_age
        if derived_horizon != self.horizon_years:
            raise SequenceRiskError(
                f'horizon_years={self.horizon_years} 與 '
                f'retirement_end_age({self.retirement_end_age}) - '
                f'current_age({self.current_age}) = {derived_horizon} 不一致;'
                f'請確認三欄位填寫一致(典型用法:current_age=55, retirement_age=60, '
                f'retirement_end_age=90 → horizon_years 自動 = 35)'
            )
        # 立即驗證 — fail-fast,讓 SequenceRiskConfig(...) 就 raise(不等到 simulate)
        _validate_config(self)


# ───────── Result ─────────
@dataclass
class SequenceRiskResult:
    survival_rate: float
    median_final_balance: int
    ruin_age_distribution: list[int] = field(default_factory=list)  # 破產年齡排序 list
    scenario_examples: list[dict] = field(default_factory=list)     # 每年 median balance + withdrawal
    success_rate_by_age: dict[str, float] = field(default_factory=dict)  # {age: rate}
    config: dict = field(default_factory=dict)
    earliest_ruin_age: int | None = None  # Phase 6 (Item 7): 最早破產年齡
    ruin_rate: float = 0.0                # Phase 6 (Item 7): 破產率
    wealth_by_age: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'survival_rate': self.survival_rate,
            'median_final_balance': self.median_final_balance,
            'ruin_age_distribution': self.ruin_age_distribution,
            'scenario_examples': self.scenario_examples,
            'success_rate_by_age': self.success_rate_by_age,
            'config': self.config,
            'earliest_ruin_age': self.earliest_ruin_age,
            'ruin_rate': self.ruin_rate,
            'wealth_by_age': self.wealth_by_age,
        }


# ───────── Public API ─────────
def simulate_sequence_risk(
    daily_returns: pd.Series | np.ndarray,
    config: SequenceRiskConfig,
) -> SequenceRiskResult:
    """退休提款模擬 — 證明「同 CAGR、不同起點 → 結果差很大」"""
    _validate_config(config)

    # 把 _prepare_returns 的 MonteCarloError 轉成 SequenceRiskError(API 一致性)
    try:
        rets = _prepare_returns(daily_returns)
    except MonteCarloError as e:
        raise SequenceRiskError(str(e)) from e
    n_sims = config.n_simulations
    horizon = config.horizon_years
    total_days = horizon * TRADING_DAYS_PER_YEAR

    rng = np.random.default_rng(config.seed)

    # 1. 抽樣(共用 F1 的 _block_bootstrap)
    if config.block_bootstrap:
        sampled = _block_bootstrap(
            rets, total_days, n_sims, config.block_size_days, rng
        )
    else:
        idx = rng.integers(0, len(rets), size=(n_sims, total_days))
        sampled = rets[idx]

    # 2. 逐日建立 NAV。提款/收入必須在每條路徑上逐年套用，不能先
    # 累積完整報酬後再減去現金流，否則已破產路徑會在後續報酬中復活。
    nav = np.full((n_sims, total_days), float(config.initial_balance))

    # 3. 提款 + 追蹤 ruin + yearly survival
    annual_withdrawal = config.withdrawal_monthly * 12

    ruin_age, yearly_survival_rates = _apply_withdrawals_and_track(
        nav=nav,
        sampled_returns=sampled,
        annual_withdrawal=annual_withdrawal,
        inflation=config.withdrawal_inflation,
        horizon_years=horizon,
        retirement_age=config.retirement_age,
        current_age=config.current_age,  # Phase 1.1: 用 current_age 當起點,不是 retirement_age
        pension_monthly=config.pension_monthly,                # Phase 2C
        pension_inflation=config.pension_inflation,
        pension_start_age=config.pension_start_age,
        special_expenses=config.special_expenses,              # Phase 2D
    )

    # 4. 統計
    final_values = nav[:, -1]
    survival_rate = float(np.mean(final_values > 0))
    # 同 monte_carlo._compute_summary 的 overflow 修法:
    # cumprod overflow 會產 inf,np.median(inf) = inf → int(inf) OverflowError。
    # 先用 np.where 把 inf 換成 NaN,再走 nanmedian。
    clean_final = np.where(np.isfinite(final_values), final_values, np.nan)
    with np.errstate(invalid='ignore'):
        median_final_balance = _safe_int_round(np.nanmedian(clean_final))

    ruined_mask = ruin_age >= 0
    ruin_age_dist = sorted(int(a) for a in ruin_age[ruined_mask])
    # Phase 6 (Item 7): earliest_ruin_age + ruin_rate
    earliest_ruin_age = int(ruin_age_dist[0]) if ruin_age_dist else None
    ruin_rate = float(ruined_mask.mean())

    scenario_examples = _build_scenario_examples(
        nav=nav,
        annual_withdrawal=annual_withdrawal,
        inflation=config.withdrawal_inflation,
        horizon_years=horizon,
        current_age=config.current_age,           # Phase 1.1
        retirement_age=config.retirement_age,     # Phase 1.1
    )

    success_rate_by_age = _build_success_rate_by_age(
        yearly_survival_rates=yearly_survival_rates,
        current_age=config.current_age,           # Phase 1.1: 改用 current_age
        retirement_age=config.retirement_age,     # 保留相容性
    )

    wealth_by_age = {}
    for y in range(horizon):
        age = config.current_age + y + 1
        idx = min((y + 1) * TRADING_DAYS_PER_YEAR - 1, nav.shape[1] - 1)
        balances = np.where(np.isfinite(nav[:, idx]), nav[:, idx], np.nan)
        wealth_by_age[str(age)] = {
            'age': age,
            'years_from_now': age - config.current_age,
            'survival_probability': float(np.mean(balances > 0)),
            'depletion_probability': float(np.mean(balances <= 0)),
            'p10': _safe_int_round(np.nanpercentile(balances, 10)),
            'p5': _safe_int_round(np.nanpercentile(balances, 5)),
            'p25': _safe_int_round(np.nanpercentile(balances, 25)),
            'p50': _safe_int_round(np.nanpercentile(balances, 50)),
            'p75': _safe_int_round(np.nanpercentile(balances, 75)),
            'p90': _safe_int_round(np.nanpercentile(balances, 90)),
            'p95': _safe_int_round(np.nanpercentile(balances, 95)),
        }

    return SequenceRiskResult(
        survival_rate=survival_rate,
        median_final_balance=median_final_balance,
        ruin_age_distribution=ruin_age_dist,
        scenario_examples=scenario_examples,
        success_rate_by_age=success_rate_by_age,
        earliest_ruin_age=earliest_ruin_age,  # Phase 6 (Item 7)
        ruin_rate=ruin_rate,                   # Phase 6 (Item 7)
        wealth_by_age=wealth_by_age,
        config={
            'initial_balance': config.initial_balance,
            'current_age': config.current_age,            # Phase 1.1
            'retirement_age': config.retirement_age,
            'retirement_end_age': config.retirement_end_age,  # Phase 1.1
            'horizon_years': config.horizon_years,
            'withdrawal_monthly': config.withdrawal_monthly,
            'withdrawal_inflation': config.withdrawal_inflation,
            'pension_monthly': config.pension_monthly,
            'pension_inflation': config.pension_inflation,
            'pension_start_age': config.pension_start_age,
            'special_expenses': config.special_expenses,
            'n_simulations': config.n_simulations,
            'block_bootstrap': config.block_bootstrap,
            'block_size_days': config.block_size_days,
            'seed': config.seed,
        },
    )


# ───────── Internals ─────────
def _validate_config(cfg: SequenceRiskConfig) -> None:
    if cfg.initial_balance <= 0:
        raise SequenceRiskError(
            f'initial_balance 必須 > 0,got {cfg.initial_balance}'
        )
    # Retirement reports may explicitly request the required age-110 endpoint
    # (e.g. current age 55 -> 55 years). Keep the legacy 1-50 guard for all
    # ordinary simulations so existing API contracts remain unchanged.
    max_horizon = 60 if getattr(cfg, '_explicit_extended_horizon', False) else 50
    if cfg.horizon_years < 1 or cfg.horizon_years > max_horizon:
        raise SequenceRiskError(
            f'horizon_years 必須 1-{max_horizon},got {cfg.horizon_years}'
        )
    if cfg.withdrawal_monthly < 0:
        raise SequenceRiskError(
            f'withdrawal_monthly 不能 < 0,got {cfg.withdrawal_monthly}'
        )
    if cfg.current_age < 0 or cfg.current_age > 120:
        raise SequenceRiskError(
            f'current_age 必須 0-120,got {cfg.current_age}'
        )
    if cfg.retirement_age < 0 or cfg.retirement_age > 120:
        raise SequenceRiskError(
            f'retirement_age 必須 0-120,got {cfg.retirement_age}'
        )
    if cfg.retirement_end_age is not None and (
        cfg.retirement_end_age <= cfg.retirement_age
        or cfg.retirement_end_age > 120
    ):
        raise SequenceRiskError(
            f'retirement_end_age({cfg.retirement_end_age}) 必須 > retirement_age({cfg.retirement_age}) 且 <= 120'
        )
    if cfg.retirement_age < cfg.current_age:
        raise SequenceRiskError(
            f'retirement_age({cfg.retirement_age}) 必須 >= current_age({cfg.current_age});'
            f'目前年齡不可能比退休年齡大'
        )


def _apply_withdrawals_and_track(
    nav: np.ndarray,
    sampled_returns: np.ndarray,
    annual_withdrawal: float,
    inflation: float,
    horizon_years: int,
    retirement_age: int,
    current_age: int,
    pension_monthly: float = 0.0,
    pension_inflation: float = 0.02,
    pension_start_age: int | None = None,
    special_expenses: list | None = None,
) -> tuple[np.ndarray, list[float]]:
    """逐年套用現金流 + 追蹤 ruin_age + yearly survival rate

    設計 cashflow sign convention:
    - withdrawal: 正值 → 從 nav 扣
    - pension:    負值 → 注入 nav
    - special_expenses: 正值 → 該年一次性扣

    Phase 1.1 改寫:
    - 區分 current_age(模擬起點)vs retirement_age(提款起點)
    - 提款只在 age >= retirement_age 時啟動

    Phase 2C: pension(年金)注入 — 抵消退休提款
    Phase 2D: special_expenses(一次性大額支出)注入

    ruin_age 仍用 current_age + y + 1 (實際年齡)。

    Returns:
        ruin_age: shape (n_sims,), -1 表示未 ruin(實際年齡)
        yearly_survival_rates: list[float], length=horizon_years
    """
    n_sims = nav.shape[0]
    ruin_age = np.full(n_sims, -1, dtype=int)
    n_ruined = np.zeros(n_sims, dtype=bool)
    yearly_survival_rates: list[float] = []

    if pension_start_age is None:
        pension_start_age = retirement_age

    total_days = horizon_years * TRADING_DAYS_PER_YEAR

    # Phase 2D: 一次性支出 map (year_offset → 累加 amount)
    expense_by_year: dict[int, float] = {}
    for exp in (special_expenses or []):
        offset = int(exp.get('year_offset', 0))
        amount = float(exp.get('amount', 0))
        if amount <= 0:
            continue
        expense_by_year[offset] = expense_by_year.get(offset, 0.0) + amount

    # Cash flow timing: start-of-year cash flow, then daily investment return.
    # Ruined paths remain at zero and can never be revived.
    for y in range(horizon_years):
        start = y * TRADING_DAYS_PER_YEAR
        end = min(start + TRADING_DAYS_PER_YEAR, total_days)
        age_at_year_start = current_age + y
        expense = 0.0
        pension = 0.0
        if annual_withdrawal > 0 and age_at_year_start >= retirement_age:
            expense = annual_withdrawal * (1 + inflation) ** (age_at_year_start - retirement_age)
        if pension_monthly > 0 and age_at_year_start >= pension_start_age:
            pension = pension_monthly * 12 * (1 + pension_inflation) ** (age_at_year_start - pension_start_age)
        cashflow = pension - expense - expense_by_year.get(y, 0.0)
        active = ~n_ruined
        # Carry the prior year's ending balance into this year's start before
        # applying the start-of-year cash flow. Without this, every year would
        # restart from the initial balance and survival would be overstated.
        if start > 0:
            nav[active, start] = nav[active, start - 1] + cashflow
        else:
            nav[active, start] = nav[active, start] + cashflow
        nav[active, start] = np.maximum(0.0, nav[active, start])
        became_ruined = active & (nav[:, start] <= 0)
        ruin_age[became_ruined] = current_age + y
        n_ruined |= became_ruined
        for day in range(start, end):
            active = ~n_ruined
            # At the first day of a year, nav[:, start] already includes the
            # start-of-year cash flow. Subsequent days carry the prior day's
            # ending balance. Do not overwrite the cash flow with nav[:,start-1].
            if day == start:
                nav[active, day] *= (1.0 + sampled_returns[active, day])
            else:
                nav[active, day] = nav[active, day - 1] * (1.0 + sampled_returns[active, day])
            nav[~active, day] = 0.0
            became_ruined = active & (nav[:, day] <= 0)
            ruin_age[became_ruined] = current_age + y + 1
            n_ruined |= became_ruined
            nav[n_ruined, day] = 0.0
        yearly_survival_rates.append(float(np.mean(~n_ruined)))
    return ruin_age, yearly_survival_rates



def _build_scenario_examples(
    nav: np.ndarray,
    annual_withdrawal: float,
    inflation: float,
    horizon_years: int,
    current_age: int = 0,                # Phase 1.1: 用於把 year → 實際年齡
    retirement_age: int = 0,             # Phase 1.1: 提款只在 age >= retirement_age
) -> list[dict]:
    """每年一筆:median balance + 名目年提款 + 該年實際年齡

    Phase 1.1:加 'age' 欄位(template 可以直接讀)、'withdrawal' 改用「退休後第幾年」推通膨
    """
    out: list[dict] = []
    for y in range(horizon_years):
        idx = min((y + 1) * TRADING_DAYS_PER_YEAR, nav.shape[1] - 1)
        vals = np.where(np.isfinite(nav[:, idx]), nav[:, idx], np.nan)
        if np.all(np.isnan(vals)):
            # 全 inf/NaN 路徑 → median 無意義,但不應噴 RuntimeWarning
            balance_p50 = None
        else:
            with np.errstate(invalid='ignore'):
                balance_p50 = _safe_int_round(np.nanmedian(vals))
        age_at_year_start = current_age + y
        if age_at_year_start < retirement_age:
            # 退休前提款 = 0
            withdrawal_year = 0
            yir = 0
        else:
            yir = age_at_year_start - retirement_age
            withdrawal_year = int(round(annual_withdrawal * (1 + inflation) ** yir))
        out.append({
            'year': y + 1,
            'age': age_at_year_start,           # Phase 1.1: 新欄位
            'balance_p50': balance_p50,
            'withdrawal': withdrawal_year,
        })
    return out


def _build_success_rate_by_age(
    yearly_survival_rates: list[float],
    current_age: int,
    retirement_age: int = 0,            # Phase 1.1: 保留參數相容性,但實際映射用 current_age
) -> dict[str, float]:
    """year-end survival → {age_str: rate}

    Phase 1.1 改寫:用 current_age 而非 retirement_age 當起點。
    - year y 結束時,使用者實際年齡 = current_age + y + 1
    - 範例:current_age=55, year=0 → age 56;year=9 → age 65

    修正舊版 off-by-one:舊版用 retirement_age + y + 1,
    當 current_age=retirement_age(預設)時兩者結果相同(向後相容)。
    """
    out: dict[str, float] = {}
    for y, rate in enumerate(yearly_survival_rates):
        out[str(current_age + y + 1)] = rate
    return out
