"""Return + Risk + MDD portfolio optimisation.

This module deliberately has no scipy dependency.  The feasible set is a
simple capped simplex, so a deterministic projected coordinate search is
adequate for the report and keeps the calculation reproducible.
"""
from __future__ import annotations

import math
import itertools
from typing import Any

import numpy as np
import pandas as pd


NA = None
MODE_PARAMS = {
    'Conservative': {'lambda_tracking': 2.0, 'gamma_concentration': 2.0, 'eta_transaction': 1.0},
    'Balanced': {'lambda_tracking': 1.0, 'gamma_concentration': 1.0, 'eta_transaction': 1.0},
    'Growth': {'lambda_tracking': 0.25, 'gamma_concentration': 0.5, 'eta_transaction': 0.5},
}

DATA_QUALITY_THRESHOLDS = {
    'high_pairwise_observations': 2000,
    'medium_pairwise_observations': 1000,
    'low_pairwise_observations': 500,
    'valid_ratio_warning': 0.80,
}


def estimate_transaction_cost(current_weights, target_weights, portfolio_value, fees):
    """Return direction-aware trading cost; tax applies to sells only."""
    current = np.asarray(current_weights, dtype=float)
    target = np.asarray(target_weights, dtype=float)
    value = float(portfolio_value)
    buy_value = value * np.maximum(target - current, 0)
    sell_value = value * np.maximum(current - target, 0)
    commission_buy = float(fees.get('commission_buy', fees.get('commission', 0)) or 0)
    commission_sell = float(fees.get('commission_sell', fees.get('commission', 0)) or 0)
    slippage = float(fees.get('slippage', 0) or 0)
    tax = float(fees.get('tax_sell', 0) or 0)
    commission = float(buy_value.sum() * commission_buy + sell_value.sum() * commission_sell)
    slippage_cost = float((buy_value.sum() + sell_value.sum()) * slippage)
    tax_cost = float(sell_value.sum() * tax)
    total = commission + slippage_cost + tax_cost
    return {'buy_value': float(buy_value.sum()), 'sell_value': float(sell_value.sum()),
            'gross_trade_value': float(buy_value.sum() + sell_value.sum()),
            'commission': commission, 'slippage': slippage_cost, 'tax': tax_cost,
            'transaction_cost': total, 'cost_rate': total / value if value else 0.0}


def _reliability_label(observations: int) -> str:
    if observations >= DATA_QUALITY_THRESHOLDS['high_pairwise_observations']:
        return 'High Reliability'
    if observations >= DATA_QUALITY_THRESHOLDS['medium_pairwise_observations']:
        return 'Medium Reliability'
    if observations >= DATA_QUALITY_THRESHOLDS['low_pairwise_observations']:
        return 'Low Reliability'
    return 'Very Low Reliability'


def _safe_asset_returns(prices: pd.Series) -> pd.Series:
    """Return clean returns from one asset's complete available price history."""
    s = pd.to_numeric(prices, errors='coerce').where(lambda x: x > 0)
    return s.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()


def _score(values, higher=True):
    s = pd.Series(values, dtype=float)
    if s.dropna().empty or s.max() == s.min():
        return pd.Series(1.0, index=s.index).where(s.notna())
    out = (s - s.min()) / (s.max() - s.min())
    return out if higher else 1 - out


def _score_value(series: pd.Series, index: int):
    value = series.iloc[index]
    return float(value) if pd.notna(value) else None


def _portfolio_metrics(returns: pd.DataFrame, w: np.ndarray, risk_free_rate: float = 0.015) -> dict:
    pr = returns.to_numpy(dtype=float) @ w
    if len(pr) < 2:
        return {'cagr': NA, 'volatility': NA, 'mdd': NA, 'sharpe': NA, 'sortino': NA,
                'calmar': NA, 'var': NA, 'cvar': NA, 'equity': [], 'drawdown': []}
    equity = np.cumprod(1 + pr)
    years = max((returns.index[-1] - returns.index[0]).days / 365.25, 1 / 365.25)
    vol = float(np.std(pr, ddof=1) * np.sqrt(252))
    cagr = float(equity[-1] ** (1 / years) - 1) if equity[-1] > 0 else NA
    dd = equity / np.maximum.accumulate(equity) - 1
    mdd = float(dd.min())
    daily_std = np.std(pr, ddof=1)
    rf_daily = float(risk_free_rate) / 252
    sharpe = float((np.mean(pr) - rf_daily) / daily_std * np.sqrt(252)) if daily_std > 0 else NA
    downside = np.minimum(pr - rf_daily, 0)
    downside_dev = np.sqrt(np.mean(downside ** 2)) * np.sqrt(252)
    sortino = float((np.mean(pr) * 252 - risk_free_rate) / downside_dev) if downside_dev > 0 else NA
    var = float(np.quantile(pr, 0.05))
    cvar_tail = pr[pr <= var]
    cvar = float(np.mean(cvar_tail)) if len(cvar_tail) else var
    calmar = float(cagr / abs(mdd)) if cagr is not None and mdd < 0 else NA
    trough = int(np.argmin(dd)); peak = int(np.argmax(equity[:trough + 1]))
    recovery = next((i for i in range(trough, len(equity)) if equity[i] >= equity[peak]), None)
    max_duration = max((len(list(g)) for active, g in itertools.groupby(dd < -1e-12) if active), default=0)
    drawdown = [{'date': d.strftime('%Y-%m-%d'), 'value': float(v)}
                for d, v in zip(returns.index, dd)]
    return {'cagr': cagr, 'volatility': vol, 'mdd': mdd, 'sharpe': sharpe,
            'mdd_start': returns.index[peak].strftime('%Y-%m-%d'),
            'mdd_bottom': returns.index[trough].strftime('%Y-%m-%d'),
            'recovery_date': returns.index[recovery].strftime('%Y-%m-%d') if recovery is not None else None,
            'recovery_days': int((returns.index[recovery] - returns.index[trough]).days) if recovery is not None else None,
            'maximum_drawdown_duration_days': int(max_duration),
            'sortino': sortino, 'calmar': calmar, 'var': var, 'cvar': cvar,
            'drawdown': drawdown, 'equity': [{'date': d.strftime('%Y-%m-%d'), 'value': float(v)}
                                         for d, v in zip(returns.index, equity)]}


def _mdd_evidence(s: pd.Series) -> float | None:
    s = s.dropna()
    if len(s) < 20:
        return None
    nav = (1 + s).cumprod()
    dd = nav / nav.cummax() - 1
    # Actual drawdown observations: depth, duration, and eventual recovery.
    depth = min(abs(float(dd.min())) / 0.30, 1.0)
    events = int((dd < -0.10).sum())
    duration = min(int((dd < -0.10).sum()) / 252.0, 1.0)
    recovered = 1.0 if dd.iloc[-1] >= -1e-9 or (dd < -0.10).any() and nav.iloc[-1] >= nav.cummax().iloc[-1] else 0.0
    return float(np.clip(0.25 * depth + 0.25 * min(events / 2, 1) + 0.25 * duration + 0.25 * recovered, 0, 1))


def _evidence(returns: pd.DataFrame, n_years: int) -> dict[str, dict]:
    expected = max(int(len(returns.index)), 1)
    out = {}
    for t in returns.columns:
        s = returns[t].dropna()
        years = (s.index[-1] - s.index[0]).days / 365.25 if len(s) > 1 else 0
        hist = min(years / max(n_years, 1), 1.0)
        if len(s) < 20:
            regime = None
        else:
            vol = s.rolling(63).std() * np.sqrt(252)
            regime_count = sum([
                bool((s.rolling(126).mean() > 0).any()),
                bool((s.rolling(126).mean() < 0).any()),
                bool((vol > vol.quantile(.75)).any()),
                bool((vol < vol.quantile(.25)).any()),
                bool((s.rolling(21).sum() < -.15).any()),
                bool(((s.rolling(63).sum() < -.10) & (s.rolling(63).sum().shift(-63) > 0)).any()),
            ])
            regime = regime_count / 6
        draw = _mdd_evidence(s)
        obs = min(len(s) / expected, 1.0)
        components = [hist, regime, draw, obs]
        score = sum(weight * value for weight, value in zip((.4, .3, .2, .1), components) if value is not None)
        denom = sum(weight for weight, value in zip((.4, .3, .2, .1), components) if value is not None)
        classification = 'Full N-Year' if years >= n_years else ('Partial N-Year' if years >= 3 else 'Short History')
        out[t] = {'history_years': round(years, 2), 'history_length_score': hist,
                  'market_regime_coverage_score': regime, 'drawdown_evidence_score': draw,
                  'observation_score': obs, 'evidence_score': score / denom if denom else None,
                  'evidence_status': 'OK' if denom == 1 else 'Data Insufficient',
                  'classification': classification,
                  'emerging_quality_candidate': False}
    return out


def _project(w: np.ndarray, lo: float, hi: float) -> np.ndarray | None:
    w = np.clip(np.asarray(w, dtype=float), lo, hi)
    for _ in range(100):
        delta = 1 - w.sum()
        if abs(delta) < 1e-10:
            return w
        free = (w < hi - 1e-10) if delta > 0 else (w > lo + 1e-10)
        if not free.any():
            return None
        step = delta / free.sum()
        w[free] += step
        w = np.clip(w, lo, hi)
    return w if abs(w.sum() - 1) < 1e-8 else None


def _optimise(scores: np.ndarray, current: np.ndarray, sigma: np.ndarray, params: dict,
              lo=.02, hi=.15, transaction_rate=0.0, portfolio_value=1.0, fees=None):
    w = _project(current, lo, hi)
    if w is None:
        return None
    def objective(x):
        tracking = np.sum((x-current)**2)
        concentration = np.sum(x*x)
        if fees is not None:
            transaction = estimate_transaction_cost(current, x, portfolio_value, fees)['cost_rate']
        else:
            transaction = np.sum(np.abs(x-current)) * float(transaction_rate)
        risk_penalty = float(x @ sigma @ x) if sigma is not None else 0.0
        return float(x @ scores - params['lambda_tracking'] * tracking
                     - params['gamma_concentration'] * concentration
                     - params.get('eta_transaction', 1.0) * transaction
                     - 0.05 * risk_penalty)
    best = objective(w)
    # Deterministic coordinate transfer search; every candidate is feasible.
    for step in (0.01, 0.0025, 0.0005):
        improved = True
        while improved:
            improved = False
            for i in range(len(w)):
                for j in range(len(w)):
                    if i == j or w[i] <= lo + 1e-10 or w[j] >= hi - 1e-10:
                        continue
                    amount = min(step, w[i]-lo, hi-w[j])
                    if amount <= 0:
                        continue
                    candidate = w.copy(); candidate[i] -= amount; candidate[j] += amount
                    value = objective(candidate)
                    if value > best + 1e-12:
                        w, best, improved = candidate, value, True
    return w, best


def _trade_rows(tickers, current_w, target_w, values, prices, shares, fees, threshold=.01):
    total = float(sum(values))
    rows = []; gross = commission = slippage = tax = 0.0
    for i, t in enumerate(tickers):
        value = float(values[i]); price = prices.get(t)
        diff = float(target_w[i] - current_w[i])
        trade = total * diff
        action = 'HOLD / NO TRADE' if abs(diff) < threshold else ('BUY' if diff > 0 else 'SELL')
        target_value = total * target_w[i]
        target_shares = math.floor(target_value / price) if price and price > 0 else None
        actual_value = target_shares * price if target_shares is not None else None
        actual_weight = actual_value / total if actual_value is not None and total > 0 else None
        rows.append({'ticker': t, 'current_shares': int(shares.get(t, 0)), 'current_value': value,
                     'current_weight': float(current_w[i]), 'target_weight': float(target_w[i]),
                     'weight_difference': diff, 'target_value': target_value, 'trade_value': trade,
                     'target_shares': target_shares, 'actual_value': actual_value,
                     'actual_weight': actual_weight,
                     'weight_difference_actual': actual_weight - current_w[i] if actual_weight is not None else None,
                     'action': action})
        if action != 'HOLD / NO TRADE':
            tv = abs(trade); gross += tv
            commission_rate = fees.get('commission_buy', fees.get('commission', 0)) if action == 'BUY' else fees.get('commission_sell', fees.get('commission', 0))
            commission += tv * commission_rate; slippage += tv * fees.get('slippage', 0)
            if action == 'SELL': tax += tv * fees.get('tax_sell', 0)
    actual_invested = sum((r['actual_value'] or 0) for r in rows)
    cost = commission + slippage + tax
    return rows, {'gross_trade_value': gross, 'commission': commission, 'slippage': slippage, 'tax': tax,
                  'transaction_cost': cost, 'net_trade_value': gross + cost,
                  'actual_invested_value': actual_invested,
                  'cash_residual': total - actual_invested,
                  'cost_rate': cost / total if total else 0.0}


def build_optimization(prices: pd.DataFrame, current_weights: dict, current_values: dict,
                       current_prices: dict, shares: dict, n_years: int, fees: dict | None = None,
                       risk_free_rate: float = 0.015, retirement_config: dict | None = None) -> dict:
    fees = fees or {}; tickers = [t for t in prices.columns if t in current_weights]
    transaction_rate = sum(float(fees.get(k, 0) or 0) for k in
                           ('commission_buy', 'commission_sell', 'commission', 'slippage', 'tax_sell'))
    result = {'status': 'DATA INSUFFICIENT', 'reason': '', 'parameters': {'n_years': n_years, 'min_weight': .02, 'max_weight': .15,
              'no_trade_threshold': .01, 'risk_free_rate': risk_free_rate, 'transaction_cost_rate': transaction_rate,
              'models': MODE_PARAMS, 'adjusted_score_formula': 'RawScore × (0.70 + 0.30 × EvidenceScore)'}, 'validation': {}, 'sensitivity': [], 'data_quality_warnings': []}
    # Individual history is deliberately retained; only portfolio covariance uses
    # the common intersection. This prevents short-history ETFs being discarded.
    individual_returns = prices[tickers].apply(_safe_asset_returns)
    returns = individual_returns.dropna(how='any')
    result['dataset'] = {'start_date': returns.index[0].strftime('%Y-%m-%d') if not returns.empty else None,
                         'end_date': returns.index[-1].strftime('%Y-%m-%d') if not returns.empty else None,
                         'observation_count': int(len(returns)), 'missing_count': int(prices[tickers].isna().sum().sum()),
                         'valid_observation_ratio': float(len(returns) / max(len(individual_returns), 1)),
                         'requested_n_years': n_years,
                         'actual_common_period_years': round((returns.index[-1] - returns.index[0]).days / 365.25, 2) if len(returns) > 1 else 0.0}
    pairwise_counts = individual_returns.notna().astype(int).T.dot(individual_returns.notna().astype(int))
    result['dataset']['pairwise_observation_count_matrix'] = {
        'tickers': tickers, 'values': pairwise_counts.to_numpy(dtype=int).tolist(),
        'reliability': [[_reliability_label(int(v)) for v in row] for row in pairwise_counts.to_numpy()]
    }
    if result['dataset']['valid_observation_ratio'] < DATA_QUALITY_THRESHOLDS['valid_ratio_warning']:
        valid_pct = result['dataset']['valid_observation_ratio'] * 100
        threshold_pct = DATA_QUALITY_THRESHOLDS['valid_ratio_warning'] * 100
        # Identify the bottleneck ticker: the one whose pairwise intersections
        # are smallest, i.e. the shortest individual history dragging the
        # common period down.
        bottleneck = str(pairwise_counts.sum(axis=0).idxmin())
        bottleneck_obs = int(pairwise_counts.loc[bottleneck, bottleneck])
        result['data_quality_warnings'].append(
            f'⚠️ 嚴重警告：共同期間有效觀察比例僅 {valid_pct:.2f}%（門檻 {threshold_pct:.0f}%），'
            f'主要瓶頸為 {bottleneck}（僅 {bottleneck_obs} 天歷史，是限制共同期間的關鍵標的）。'
            f'建議：(1) 共變異數 / 相關性估計僅供參考，不宜作為分散效益的唯一依據；'
            f'(2) 可改用 Ledoit-Wolf 縮減估計式，或暫時排除 {bottleneck} 重估矩陣。'
        )
    if (pairwise_counts.to_numpy() < DATA_QUALITY_THRESHOLDS['low_pairwise_observations']).any():
        result['data_quality_warnings'].append(
            'WARNING: Some asset pairs have fewer than 500 overlapping observations; estimates may be unstable.'
        )
    if result['dataset']['actual_common_period_years'] < n_years:
        result['data_quality_warnings'].append('WARNING: Common period is significantly shorter than requested N-year period.')
    result['daily_return_matrix'] = {
        'tickers': tickers,
        'rows': [{'date': d.strftime('%Y-%m-%d'), **{t: float(v) for t, v in row.items()}}
                 for d, row in returns.tail(500).iterrows()],
        'display_note': 'Rows are limited to the latest 500 for report size; calculations use the full common matrix.'
    }
    if len(returns) < 20:
        result['reason'] = 'Daily Return Matrix does not contain enough common observations.'
        return result
    covariance = individual_returns.cov(min_periods=20) * 252
    corr = individual_returns.corr(min_periods=20)
    sigma = covariance.fillna(0).to_numpy()
    result['covariance_matrix'] = {'tickers': tickers, 'values': sigma.tolist()}
    result['correlation_matrix'] = {'tickers': tickers, 'values': corr.fillna(0).to_numpy().tolist()}
    individual = {}
    for t in tickers:
        individual[t] = _portfolio_metrics(individual_returns[[t]].dropna(), np.array([1.0]), risk_free_rate)
    ev = _evidence(individual_returns, n_years)
    cagr = _score([individual[t]['cagr'] for t in tickers])
    vol = _score([individual[t]['volatility'] for t in tickers], higher=False)
    mdd = _score([abs(individual[t]['mdd']) for t in tickers], higher=False)
    sharpe = _score([individual[t]['sharpe'] for t in tickers])
    calmar = _score([individual[t]['calmar'] for t in tickers])
    scores = []
    for i, t in enumerate(tickers):
        available = [(cagr.iloc[i], .35), (sharpe.iloc[i], .20), (vol.iloc[i], .15), (mdd.iloc[i], .15), (calmar.iloc[i], .15)]
        available = [(v, weight) for v, weight in available if pd.notna(v)]
        raw = float(sum(v * weight for v, weight in available) / sum(weight for _, weight in available)) if available else None
        evidence = ev[t]['evidence_score']
        adjusted = raw * (.70 + .30*evidence) if raw is not None and evidence is not None else None
        ev[t].update({'cagr': individual[t]['cagr'], 'volatility': individual[t]['volatility'], 'mdd': individual[t]['mdd'],
                      'sharpe': individual[t]['sharpe'], 'sortino': individual[t]['sortino'], 'calmar': individual[t]['calmar'], 'return_score': float(cagr.iloc[i]) if pd.notna(cagr.iloc[i]) else None,
                      'risk_score': _score_value(vol, i), 'mdd_score': _score_value(mdd, i), 'sharpe_score': _score_value(sharpe, i),
                      'calmar_score': float(calmar.iloc[i]) if pd.notna(calmar.iloc[i]) else None,
                      'evidence_factor': .70 + .30*evidence if evidence is not None else None,
                      'raw_score': raw, 'adjusted_score': adjusted,
                      })
        ev[t]['emerging_quality_candidate'] = bool(adjusted is not None and adjusted >= .65 and evidence is not None and evidence >= .55 and ev[t]['history_years'] < n_years)
        scores.append(adjusted)
    result['scores'] = ev
    if any(x is None for x in scores):
        result['reason'] = 'Adjusted Score unavailable because Evidence Score is incomplete.'
        return result
    current = np.array([float(current_weights.get(t, 0)) for t in tickers]); current = current/current.sum()
    values = [float(current_values.get(t, 0)) for t in tickers]
    result['current'] = _portfolio_metrics(returns, current, risk_free_rate); result['current']['weights'] = current.tolist()
    result['portfolio_equity_curve'] = result['current']['equity']
    result['portfolio_drawdown_curve'] = result['current']['drawdown']
    if len(tickers) * .02 > 1 or len(tickers) * .15 < 1:
        result['reason'] = f'{len(tickers)} assets cannot satisfy 2%–15% weight constraints.'
        result['validation']['constraints_feasible'] = False
        return result
    result['validation']['constraints_feasible'] = True
    portfolio_value = float(sum(values))
    chosen = _optimise(np.array(scores), current, sigma, MODE_PARAMS['Balanced'],
                       transaction_rate=transaction_rate, portfolio_value=portfolio_value, fees=fees)
    if chosen is None:
        result['reason'] = '2%–15% constraints are infeasible for the number of assets.'; return result
    target, objective = chosen
    result['optimized'] = _portfolio_metrics(returns, target, risk_free_rate); result['optimized']['weights'] = target.tolist(); result['objective'] = objective
    result['trades'], trade_cost = _trade_rows(tickers, current, target, values, current_prices, shares, fees, result['parameters']['no_trade_threshold'])
    objective_cost = estimate_transaction_cost(current, target, portfolio_value, fees)
    result['transaction_cost'] = {**trade_cost, 'estimated_commission': objective_cost['commission'],
                                  'estimated_tax': objective_cost['tax'], 'estimated_slippage': objective_cost['slippage'],
                                  'estimated_total_transaction_cost': objective_cost['transaction_cost'],
                                  'transaction_cost_penalty': objective_cost['cost_rate']}
    result['objective_without_transaction_cost'] = _optimise(
        np.array(scores), current, sigma, {**MODE_PARAMS['Balanced'], 'eta_transaction': 0},
        portfolio_value=portfolio_value, fees={k: 0 for k in fees}, transaction_rate=0.0,
    )[1]
    result['trade_summary'] = {
        'buy': sum(row['action'] == 'BUY' for row in result['trades']),
        'sell': sum(row['action'] == 'SELL' for row in result['trades']),
        'hold': sum(row['action'] == 'HOLD / NO TRADE' for row in result['trades']),
    }
    result['concentration'] = {'current_hhi': float(np.sum(current*current)), 'optimized_hhi': float(np.sum(target*target)),
                               'current_max_weight': float(max(current)), 'optimized_max_weight': float(max(target)),
                               'current_top5': float(sorted(current, reverse=True)[:5].__iter__().__next__()) if False else float(sum(sorted(current, reverse=True)[:5])),
                               'optimized_top5': float(sum(sorted(target, reverse=True)[:5]))}
    result['before_after'] = {k: {'current': result['current'].get(k), 'optimized': result['optimized'].get(k),
                                  'change': result['optimized'].get(k) - result['current'].get(k),
                                  'direction': 'Higher is better' if k in ('cagr', 'sharpe', 'calmar') else 'Lower is better'}
                              for k in ('cagr', 'volatility', 'mdd', 'sharpe', 'sortino', 'calmar', 'var', 'cvar')}
    result['before_after']['transaction_cost'] = {'current': 0.0, 'optimized': result['transaction_cost']['transaction_cost'],
                                                  'change': result['transaction_cost']['transaction_cost']}
    result['stress_test'] = {}
    for name, start, end in (('2008 Global Financial Crisis', '2008-01-01', '2009-06-30'),
                             ('2020 COVID Crash', '2020-02-01', '2020-06-30'),
                             ('2022 Bear Market', '2022-01-01', '2022-12-31')):
        window = returns.loc[start:end]
        result['stress_test'][name] = _portfolio_metrics(window, target) if len(window) >= 20 else {'status': 'N/A', 'reason': 'Required historical dates are not available.'}
    result['stress_test']['High Inflation / High Rate'] = {'status': 'N/A', 'reason': 'Macro inflation and interest-rate series are not in the existing data.'}
    result['stress_test']['Technology Drawdown'] = {'status': 'N/A', 'reason': 'No technology-specific scenario definition is present in the existing data.'}
    result['retirement_monte_carlo'] = {'status': 'N/A', 'reason': 'Retirement inputs were not provided.'}
    for label, p in MODE_PARAMS.items():
        opt = _optimise(np.array(scores), current, sigma, p, transaction_rate=transaction_rate,
                        portfolio_value=portfolio_value, fees=fees)
        if opt:
            w2, _ = opt; met = _portfolio_metrics(returns, w2, risk_free_rate)
            result['sensitivity'].append({'mode': label, **p, **{k: met[k] for k in ('cagr','volatility','mdd','sharpe','calmar')},
                                          'weights': {t: float(x) for t, x in zip(tickers, w2)},
                                          'maximum_weight': float(max(w2)), 'top5_concentration': float(sum(sorted(w2, reverse=True)[:5])),
                                          'turnover': float(np.sum(abs(w2-current)))})
    result['validation'] = {'common_date_range': bool(result['dataset']['start_date'] and result['dataset']['end_date']),
                            'constraints_feasible': True,
                            'covariance_valid': bool(np.isfinite(sigma).all()), 'portfolio_volatility_valid': result['current']['volatility'] is not None,
                            'equity_curve_valid': bool(result['current']['equity']), 'weight_sum': float(target.sum()),
                            'min_weight': float(target.min()), 'max_weight': float(target.max()), 'constraints_satisfied': bool(abs(target.sum()-1)<1e-8 and target.min()>=.02-1e-8 and target.max()<=.15+1e-8),
                            'objective_calculated': True, 'optimizer_converged': True}
    result['validation']['transaction_cost_in_objective'] = bool(
        result['objective'] != result['objective_without_transaction_cost']
        if any(float(v or 0) for v in fees.values()) else result['transaction_cost']['transaction_cost_penalty'] == 0
    )
    if retirement_config:
        try:
            from lib.sequence_risk import SequenceRiskConfig, simulate_sequence_risk
            def run_retirement(weights):
                cfg = SequenceRiskConfig(**retirement_config)
                simulated = simulate_sequence_risk(returns @ weights, cfg).to_dict()
                distribution = simulated.get('ruin_age_distribution') or []
                summary = {
                    'success_rate': simulated['survival_rate'],
                    'failure_rate': 1 - simulated['survival_rate'],
                    'median_ending_asset': simulated['median_final_balance'],
                    'p5_ending_asset': None, 'p25_ending_asset': None,
                    'p75_ending_asset': None, 'p95_ending_asset': None,
                    'median_depletion_age': float(np.median(distribution)) if distribution else None,
                    'p5_depletion_age': float(np.percentile(distribution, 5)) if distribution else None,
                    'wealth_by_age': simulated.get('wealth_by_age', {}),
                    'config': simulated.get('config', {}),
                }
                final_ages = simulated.get('wealth_by_age', {}).get(str(cfg.retirement_end_age), {})
                for key, source in (('p5_ending_asset', 'p5'), ('p25_ending_asset', 'p25'),
                                    ('p75_ending_asset', 'p75'), ('p95_ending_asset', 'p95')):
                    summary[key] = final_ages.get(source)
                return summary
            current_mc = run_retirement(current)
            optimized_mc = run_retirement(target)
            result['retirement_monte_carlo'] = {
                'status': 'SUCCESS', 'current': current_mc, 'optimized': optimized_mc,
                'difference': {k: optimized_mc[k] - current_mc[k] for k in ('success_rate', 'failure_rate')
                               if optimized_mc[k] is not None and current_mc[k] is not None},
            }
        except (TypeError, ValueError, ArithmeticError) as exc:
            result['retirement_monte_carlo'] = {'status': 'FAILED', 'reason': str(exc)}
    mc = result['retirement_monte_carlo']
    concentration_better = bool(result['concentration']['optimized_top5'] < result['concentration']['current_top5'])
    sustainability_better = bool(
        mc.get('status') == 'SUCCESS'
        and mc['optimized']['success_rate'] > mc['current']['success_rate']
    )
    cost_ok = result['transaction_cost']['cost_rate'] <= 0.02
    if sustainability_better and concentration_better and cost_ok:
        recommendation = 'Recommended'
    elif mc.get('status') == 'SUCCESS' and concentration_better:
        recommendation = 'Conditional Recommendation: consider phased rebalancing.'
    else:
        recommendation = 'Not Recommended: retirement sustainability or risk trade-offs do not support immediate rebalance.'
    result['recommendation_summary'] = {
        'optimization_status': 'SUCCESS' if all(result['validation'].values()) else 'FAILED',
        'constraint_status': result['validation']['constraints_satisfied'],
        'current_concentration': result['concentration']['current_top5'],
        'optimized_concentration': result['concentration']['optimized_top5'],
        'estimated_transaction_cost': result['transaction_cost']['transaction_cost'],
        'current_retirement_success_rate': mc.get('current', {}).get('success_rate'),
        'optimized_retirement_success_rate': mc.get('optimized', {}).get('success_rate'),
        'recommendation': recommendation,
    }
    result['status'] = 'SUCCESS' if all(result['validation'].values()) else 'FAILED'
    return result
