"""Risk Metrics — VaR / CVaR + Sharpe with Risk-Free Rate

F3 + F6 共用模組:
- F3: 歷史法計算 VaR / CVaR（多 horizon: 1d / 21d / 252d）
- F6: Sharpe with Risk-Free Rate（扣無風險利率後的夏普值）

設計原則:
- 向量化 numpy（單純 quantile 計算,效能不是瓶頸）
- 與 v1/v2 其他模組解耦:只吃日報酬 pd.Series
- 歷史法（直接取 percentile）— SPEC F3 §2 規範

邊界:
- 歷史 < horizon_days → 該 horizon 跳過 / 回傳 None
- confidence_levels 須在 (0, 1)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd


# ───────── Custom Errors ─────────
class RiskMetricsError(ValueError):
    pass


# ───────── Constants ─────────
TRADING_DAYS_PER_YEAR = 252
DEFAULT_RISK_FREE_RATE = 0.015  # 台灣 10Y 公債近似
DEFAULT_RF_SOURCE = 'tw_10y_bond'
SUPPORTED_HORIZONS = (1, 21, 252)  # 日 / 月 / 年


# ───────── Config / Result ─────────
@dataclass
class RiskMetricsConfig:
    confidence_levels: list[float] = field(default_factory=lambda: [0.95, 0.99])
    horizon_days: list[int] = field(default_factory=lambda: [1, 21, 252])
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE
    risk_free_source: str = DEFAULT_RF_SOURCE
    method: str = 'historical'        # SPEC §2 F3:historical / parametric(先做 historical)

    def __post_init__(self) -> None:
        # 型別檢查先做(TypeError) → 邏輯檢查(ValueError)
        if not isinstance(self.confidence_levels, Iterable) or isinstance(self.confidence_levels, (str, bytes)):
            raise TypeError(
                f'confidence_levels 須為 list[float], got {type(self.confidence_levels).__name__}'
            )
        if not isinstance(self.horizon_days, Iterable) or isinstance(self.horizon_days, (str, bytes)):
            raise TypeError(
                f'horizon_days 須為 list[int], got {type(self.horizon_days).__name__}'
            )
        if not isinstance(self.risk_free_rate, (int, float)) or isinstance(self.risk_free_rate, bool):
            raise TypeError(
                f'risk_free_rate 須為 number, got {type(self.risk_free_rate).__name__}'
            )
        # 轉成 list 以防傳入 tuple 等
        self.confidence_levels = list(self.confidence_levels)
        self.horizon_days = list(self.horizon_days)
        self.risk_free_rate = float(self.risk_free_rate)
        # 邏輯檢查(ValueError)
        _validate_config(self)

    def to_dict(self) -> dict:
        return {
            'confidence_levels': list(self.confidence_levels),
            'horizon_days': list(self.horizon_days),
            'risk_free_rate': self.risk_free_rate,
            'risk_free_source': self.risk_free_source,
            'method': self.method,
        }


@dataclass
class RiskMetricsResult:
    var_cvar: dict[str, float]        # {"var_1d_95": ..., "cvar_1d_99": ..., ...}
    sharpe: dict[str, float]          # {"sharpe_with_rf": ..., "sharpe_rf_0": ..., ...}
    config: dict = field(default_factory=dict)
    horizon_days: list[int] = field(default_factory=list)
    confidence_levels: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        # 最後一道防線:就算上游疏漏漏 NaN,在這邊統一轉 None。
        # 否則 JSON 會輸出 `NaN` 非法字串,前端 fetch() 直接崩(2026-08-27 慘案)。
        return {
            'var_cvar': _scrub_nan_inf(self.var_cvar),
            'sharpe': _scrub_nan_inf(self.sharpe),
            'config': self.config,
            'horizon_days': self.horizon_days,
            'confidence_levels': self.confidence_levels,
        }


# ───────── Public API ─────────
def compute_risk_metrics(
    daily_returns: pd.Series | np.ndarray,
    config: RiskMetricsConfig | None = None,
) -> RiskMetricsResult:
    """計算 VaR/CVaR + Sharpe with Rf

    Args:
        daily_returns: 歷史日報酬（NaN 自動剔除）
        config: 設定（None = 預設 95/99 × 1d/21d/252d, rf=1.5%）

    Returns:
        RiskMetricsResult 含 var_cvar + sharpe
    """
    if config is None:
        config = RiskMetricsConfig()
    _validate_config(config)

    rets = _prepare_returns(daily_returns)

    # F3: VaR / CVaR
    var_cvar = _compute_var_cvar(rets, config.confidence_levels, config.horizon_days)

    # F6: Sharpe with Rf
    sharpe = _compute_sharpe(rets, config.risk_free_rate)

    return RiskMetricsResult(
        var_cvar=var_cvar,
        sharpe=sharpe,
        config=config.to_dict(),
        horizon_days=list(config.horizon_days),
        confidence_levels=list(config.confidence_levels),
    )


# ───────── Internals ─────────
def _validate_config(cfg: RiskMetricsConfig) -> None:
    if cfg.method not in ('historical', 'parametric'):
        raise RiskMetricsError(f'method 須為 historical/parametric,got {cfg.method!r}')
    for cl in cfg.confidence_levels:
        if not (0 < cl < 1):
            raise RiskMetricsError(f'confidence_level 須在 (0,1),got {cl}')
    for h in cfg.horizon_days:
        if h < 1:
            raise RiskMetricsError(f'horizon_days 必須 >= 1,got {h}')


def _prepare_returns(daily_returns: pd.Series | np.ndarray) -> np.ndarray:
    if isinstance(daily_returns, pd.Series):
        rets = daily_returns.dropna().to_numpy()
    else:
        rets = np.asarray(daily_returns, dtype=float)
        rets = rets[~np.isnan(rets)]
    if len(rets) < 30:
        raise RiskMetricsError(
            f'歷史日報酬太短 ({len(rets)} 天),至少需 30 個交易日'
        )
    return rets


def _scrub_nan_inf(d: dict) -> dict:
    """走訪 dict,把所有 float NaN / ±inf 換成 None。
    JSON 不接受 NaN/Infinity 字串(`{"k":NaN}` 非法),但接受 null,
    前端 fmtFloat / fmtPct 已能顯示 '—'。
    """
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                out[k] = None
            else:
                out[k] = v
        else:
            out[k] = v
    return out


def _horizon_returns(rets: np.ndarray, horizon_days: int) -> np.ndarray | None:
    """把日報酬 roll 成 N 日合計報酬
    Returns:
        shape (n_periods,) 的 horizon-level return;若歷史太短回傳 None
    """
    n = len(rets)
    if horizon_days > n:
        return None
    # cumprod-based rolling sum:(1+r_t)...(1+r_{t+h-1}) - 1
    # Clip cumprod 避免 float64 overflow(高 sigma × long horizon 會 inf)
    # 1e15 對「年化波動 ~50% × 252 天」已遠超真實可能報酬,純安全邊界
    cum = np.cumprod(1.0 + rets)
    cum = np.clip(cum, None, 1e15)
    # horizon return over [t, t+h) = cum[t+h-1] / cum[t-1] - 1
    h_ret = cum[horizon_days - 1:] / np.concatenate(([1.0], cum[:-horizon_days])) - 1.0
    # 保險:clip 過不該再有 inf,但 nan 出現(0/0 邊界)就讓下游 nanmean 處理
    h_ret = np.where(np.isinf(h_ret), np.nan, h_ret)
    return h_ret


def _var_historical(returns: np.ndarray, confidence: float) -> float:
    """Historical VaR:loss 不超過 confidence 的最壞情境(回傳負值代表損失)
    Returns:
        負值 = 損失(例 -0.0234 = 1d 95% 最壞跌 2.34%)
    """
    alpha = 1.0 - confidence
    return float(np.percentile(returns, alpha * 100))


def _cvar_historical(returns: np.ndarray, confidence: float) -> float | None:
    """Historical CVaR:超過 VaR 部分的平均(條件風險值)
    Returns:
        負值 = 平均尾部損失(絕對值永遠 >= VaR),或 None(無法計算)
    """
    var = _var_historical(returns, confidence)
    if not math.isfinite(var):
        return None
    tail = returns[returns <= var]
    if len(tail) == 0:
        # 高 confidence + 短樣本:沒有 return 落入 VaR 之後(浮點邊界)
        # 回 var 本身(保守等於 VaR,符合 CVaR ≥ VaR 定義)
        return float(var)
    avg = float(np.nanmean(tail))
    if not math.isfinite(avg):
        return None
    return avg


def _compute_var_cvar(
    rets: np.ndarray,
    confidence_levels: list[float],
    horizon_days: list[int],
) -> dict[str, float]:
    """組合所有 horizon × confidence 的 VaR/CVaR"""
    out: dict[str, float] = {'method': 'historical'}
    h_label = {1: '1d', 21: '21d', 252: '252d'}

    for h in horizon_days:
        h_ret = _horizon_returns(rets, h)
        if h_ret is None or len(h_ret) < 10:
            # 歷史太短,標 None
            for cl in confidence_levels:
                tag_cl = f'{int(cl * 100)}'
                out[f'var_{h_label.get(h, f"{h}d")}_{tag_cl}'] = None
                out[f'cvar_{h_label.get(h, f"{h}d")}_{tag_cl}'] = None
            continue
        for cl in confidence_levels:
            tag_cl = f'{int(cl * 100)}'
            key_var = f'var_{h_label.get(h, f"{h}d")}_{tag_cl}'
            key_cvar = f'cvar_{h_label.get(h, f"{h}d")}_{tag_cl}'
            out[key_var] = round(_var_historical(h_ret, cl), 6)
            out[key_cvar] = round(_cvar_historical(h_ret, cl), 6)

    return out


def _compute_sharpe(
    rets: np.ndarray,
    rf_annual: float,
) -> dict[str, float | None]:
    """Sharpe ratio(年化)
    Sharpe_rf_0 = mean(daily) / std(daily) * sqrt(252)
    Sharpe_with_rf = (mean(daily) - rf_daily) / std(daily) * sqrt(252)

    任何欄位算到 NaN / ±inf → 全部回 None(不讓 NaN 污染 JSON response)
    """
    # nan-aware 防禦:_prepare_returns 雖已 dropna,但保險起見再過濾一次
    if len(rets) > 0:
        clean = rets[~np.isnan(rets)]
    else:
        clean = rets
    if len(clean) < 2:
        return {
            'sharpe_with_rf': None,
            'sharpe_rf_0': None,
            'rf_used': float(rf_annual),
            'rf_daily_used': float(rf_annual) / TRADING_DAYS_PER_YEAR,
        }
    mean_d = float(np.nanmean(clean))
    std_d = float(np.nanstd(clean, ddof=1))
    # std_d ≈ 0(浮點殘差 ~2e-19)或 NaN → Sharpe 沒意義
    if not (math.isfinite(mean_d) and math.isfinite(std_d)) or std_d <= 0:
        return {
            'sharpe_with_rf': None,
            'sharpe_rf_0': None,
            'rf_used': float(rf_annual),
            'rf_daily_used': float(rf_annual) / TRADING_DAYS_PER_YEAR,
        }
    sharpe_rf_0 = (mean_d / std_d) * np.sqrt(TRADING_DAYS_PER_YEAR)
    rf_daily = rf_annual / TRADING_DAYS_PER_YEAR
    sharpe_with_rf = ((mean_d - rf_daily) / std_d) * np.sqrt(TRADING_DAYS_PER_YEAR)
    # 最後一道防線(std_d 接近 0 時 mean/std 比例可能爆掉)
    if not (math.isfinite(sharpe_rf_0) and math.isfinite(sharpe_with_rf)):
        return {
            'sharpe_with_rf': None,
            'sharpe_rf_0': None,
            'rf_used': float(rf_annual),
            'rf_daily_used': float(rf_annual) / TRADING_DAYS_PER_YEAR,
        }
    return {
        'sharpe_with_rf': round(float(sharpe_with_rf), 6),
        'sharpe_rf_0': round(float(sharpe_rf_0), 6),
        'rf_used': float(rf_annual),
        'rf_daily_used': round(float(rf_daily), 8),
    }


# ───────── Convenience wrapper for Flask route ─────────
def run_risk_metrics(
    daily_returns: pd.Series | np.ndarray,
    body: dict,
) -> dict:
    """Flask-friendly wrapper:接受 request body dict,回傳 JSON-safe dict

    Body 欄位（皆 optional）:
    - confidence_levels: list[float], 預設 [0.95, 0.99]
    - horizon_days: list[int], 預設 [1, 21, 252]
    - risk_free_rate: float, 預設 0.015
    - risk_free_source: str, 預設 'tw_10y_bond'
    """
    try:
        config = RiskMetricsConfig(
            confidence_levels=body.get('confidence_levels', [0.95, 0.99]),
            horizon_days=body.get('horizon_days', [1, 21, 252]),
            risk_free_rate=body.get('risk_free_rate', DEFAULT_RISK_FREE_RATE),
            risk_free_source=body.get('risk_free_source', DEFAULT_RF_SOURCE),
            method=body.get('method', 'historical'),
        )
    except (TypeError, ValueError) as e:
        raise RiskMetricsError(f'config 解析失敗:{e}') from e

    result = compute_risk_metrics(daily_returns, config)
    return result.to_dict()
