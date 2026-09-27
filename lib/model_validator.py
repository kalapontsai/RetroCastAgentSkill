"""
lib/model_validator.py
- Phase 4.1: Model validation layer(P15)
- 在 _run_analyze 結尾、render_html_report 之前跑
- 任何 FAIL → ModelValidationError(可選 raise 或只 warn + 標 [FAIL] 在報告頂部)

設計:
- ValidationReport:聚合 checks + summary
- Check dataclass:單一驗證結果(severity / status / name / expected / actual)
- validate_all(analyze):跑所有 checklist,回 ValidationReport
- Severity 分類:
    - CRITICAL:破壞性錯誤,raise ModelValidationError
    - WARN:資料合理性,但報告仍可用,只在報告頂部標註
    - INFO:診斷訊息,純提示
- Mode:
    - 'raise':任何 CRITICAL FAIL 直接 raise,適合 CI
    - 'warn':只 log + 把 FAIL 放 report(預設,跟現有 UI 整合)
    - 'silent':只回 ValidationReport,不 log
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# ───────── Exceptions ─────────
class ModelValidationError(Exception):
    """CRITICAL check 沒過 → 整個 analyze 結果不可信"""
    def __init__(self, report: 'ValidationReport'):
        self.report = report
        fails = [c for c in report.checks if c.status == 'FAIL' and c.severity == 'CRITICAL']
        msg = f'ModelValidationError: {len(fails)} critical checks failed: ' + \
            ', '.join(c.name for c in fails)
        super().__init__(msg)


# ───────── Check dataclass ─────────
@dataclass
class Check:
    name: str
    severity: str          # 'CRITICAL' / 'WARN' / 'INFO'
    status: str            # 'PASS' / 'FAIL' / 'SKIP'
    expected: Any = None
    actual: Any = None
    message: str = ''

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationReport:
    """所有驗證結果的聚合"""
    checks: list[Check] = field(default_factory=list)
    summary: dict = field(default_factory=dict)   # counts by status / severity

    def add(self, c: Check) -> None:
        self.checks.append(c)

    def finalize(self) -> None:
        self.summary = {
            'total': len(self.checks),
            'pass': sum(1 for c in self.checks if c.status == 'PASS'),
            'fail': sum(1 for c in self.checks if c.status == 'FAIL'),
            'skip': sum(1 for c in self.checks if c.status == 'SKIP'),
            'critical_fail': sum(
                1 for c in self.checks
                if c.status == 'FAIL' and c.severity == 'CRITICAL'
            ),
            'warn_fail': sum(
                1 for c in self.checks
                if c.status == 'FAIL' and c.severity == 'WARN'
            ),
        }

    def has_critical_fail(self) -> bool:
        return any(c.status == 'FAIL' and c.severity == 'CRITICAL' for c in self.checks)

    def to_dict(self) -> dict:
        return {
            'checks': [c.to_dict() for c in self.checks],
            'summary': self.summary,
        }


# ───────── Check helpers ─────────
def _make_check(name: str, severity: str, status: str, expected=None, actual=None,
                message: str = '') -> Check:
    return Check(name=name, severity=severity, status=status,
                 expected=expected, actual=actual, message=message)


def check_eq(name: str, actual, expected, severity: str = 'WARN', tolerance: float = 0.0):
    """值相等(tolerance 允許絕對誤差)"""
    try:
        if abs(actual - expected) <= tolerance:
            return _make_check(name, severity, 'PASS', expected=expected, actual=actual)
        return _make_check(name, severity, 'FAIL', expected=expected, actual=actual,
                            message=f'{name}: expected {expected}, got {actual}')
    except (TypeError, ValueError):
        return _make_check(name, severity, 'SKIP', expected=expected, actual=actual,
                            message=f'{name}: type error')


def check_ge(name: str, actual, expected, severity: str = 'WARN'):
    """actual >= expected"""
    try:
        if actual >= expected:
            return _make_check(name, severity, 'PASS', expected=expected, actual=actual)
        return _make_check(name, severity, 'FAIL', expected=f'>= {expected}', actual=actual,
                            message=f'{name}: expected >= {expected}, got {actual}')
    except (TypeError, ValueError):
        return _make_check(name, severity, 'SKIP', expected=expected, actual=actual)


def check_le(name: str, actual, expected, severity: str = 'WARN'):
    """actual <= expected"""
    try:
        if actual <= expected:
            return _make_check(name, severity, 'PASS', expected=expected, actual=actual)
        return _make_check(name, severity, 'FAIL', expected=f'<= {expected}', actual=actual,
                            message=f'{name}: expected <= {expected}, got {actual}')
    except (TypeError, ValueError):
        return _make_check(name, severity, 'SKIP', expected=expected, actual=actual)


def check_lt(name: str, actual, expected, severity: str = 'WARN'):
    """actual < expected"""
    try:
        if actual < expected:
            return _make_check(name, severity, 'PASS', expected=expected, actual=actual)
        return _make_check(name, severity, 'FAIL', expected=f'< {expected}', actual=actual,
                            message=f'{name}: expected < {expected}, got {actual}')
    except (TypeError, ValueError):
        return _make_check(name, severity, 'SKIP', expected=expected, actual=actual)


def check_gt(name: str, actual, expected, severity: str = 'WARN'):
    """actual > expected"""
    try:
        if actual > expected:
            return _make_check(name, severity, 'PASS', expected=expected, actual=actual)
        return _make_check(name, severity, 'FAIL', expected=f'> {expected}', actual=actual,
                            message=f'{name}: expected > {expected}, got {actual}')
    except (TypeError, ValueError):
        return _make_check(name, severity, 'SKIP', expected=expected, actual=actual)


def check_positive(name: str, actual, severity: str = 'WARN'):
    """actual > 0"""
    try:
        if actual is not None and actual > 0:
            return _make_check(name, severity, 'PASS', expected='> 0', actual=actual)
        return _make_check(name, severity, 'FAIL', expected='> 0', actual=actual,
                            message=f'{name}: must be > 0')
    except (TypeError, ValueError):
        return _make_check(name, severity, 'SKIP', expected='> 0', actual=actual)


def check_non_negative(name: str, actual, severity: str = 'WARN'):
    """actual >= 0"""
    try:
        if actual is not None and actual >= 0:
            return _make_check(name, severity, 'PASS', expected='>= 0', actual=actual)
        return _make_check(name, severity, 'FAIL', expected='>= 0', actual=actual,
                            message=f'{name}: must be >= 0')
    except (TypeError, ValueError):
        return _make_check(name, severity, 'SKIP', expected='>= 0', actual=actual)


def _safe_get(d: dict, path: list[str], default=None):
    """safe nested dict access: _safe_get(analyze, ['forecast', 'n'])"""
    cur = d
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


# ───────── Main entry: validate_all ─────────
def validate_all(analyze: dict) -> ValidationReport:
    """跑所有 model validation checks

    對齊 audit 文件 §5 Model Validation Checklist:
      - N-year consistency (forecast.n == inputs.n)
      - rolling.years >= N * 0.95
      - recent_n_year.years == n (tolerance 0.1)
      - monte_carlo.horizon_years == n (若有 MC)
      - retirement_mc_horizon = sr.config.horizon_years == retirement_end_age - current_age
      - withdrawal_monthly sanity check (若有提款)
      - withdrawal_inflation 一致性
      - forecast_horizon_end <= last_date
      - mc_initial_with_cost 對齊手續費(若有 MC)
    """
    report = ValidationReport()

    # ── 1. N-year consistency ──
    fc_n = _safe_get(analyze, ['forecast', 'n'])
    in_n = _safe_get(analyze, ['inputs', 'n'])
    if fc_n is not None and in_n is not None:
        report.add(check_eq(
            'forecast.n == inputs.n',
            actual=fc_n, expected=in_n, severity='CRITICAL'
        ))

    # ── 2. Rolling window length ──
    rolling = _safe_get(analyze, ['forecast', 'rolling'], default=[])
    n = in_n
    if n is not None:
        for r in rolling:
            report.add(check_ge(
                f"rolling.years[{r.get('years', '?')}] >= N*0.95",
                actual=r.get('years', 0), expected=n * 0.95, severity='WARN'
            ))

    # ── 3. recent_n_year.years == n ──
    recent_years = _safe_get(analyze, ['recent_n_year_metrics', 'years'])
    if recent_years is not None and n is not None:
        report.add(check_eq(
            'recent_n_year.years == n',
            actual=recent_years, expected=n, severity='WARN', tolerance=0.1
        ))

    # ── 4. Monte Carlo horizon ──
    mc_horizon = _safe_get(analyze, ['monte_carlo', 'config', 'horizon_years'])
    if mc_horizon is not None and n is not None:
        report.add(check_eq(
            'monte_carlo.horizon_years == n',
            actual=mc_horizon, expected=n, severity='CRITICAL'
        ))

    # ── 5. Retirement MC horizon = retirement_end_age - current_age ──
    sr_horizon = _safe_get(analyze, ['sequence_risk', 'config', 'horizon_years'])
    cur_age = _safe_get(analyze, ['retirement_inputs', 'current_age'])
    end_age = _safe_get(analyze, ['retirement_inputs', 'retirement_end_age'])
    if sr_horizon is not None and cur_age is not None and end_age is not None:
        expected_horizon = end_age - cur_age
        report.add(check_eq(
            'retirement_mc_horizon == end_age - current_age',
            actual=sr_horizon, expected=expected_horizon, severity='CRITICAL'
        ))

    # ── 6. Withdrawal sanity ──
    wd_monthly = _safe_get(analyze, ['retirement_inputs', 'withdrawal_monthly'])
    if wd_monthly is not None and wd_monthly > 0:
        report.add(check_lt(
            'withdrawal_monthly < 1e9',
            actual=wd_monthly, expected=1e9, severity='WARN'
        ))

    # ── 7. Withdrawal inflation 一致性 ──
    sr_wd_inflation = _safe_get(analyze, ['sequence_risk', 'config', 'withdrawal_inflation'])
    ri_wd_inflation = _safe_get(analyze, ['retirement_inputs', 'withdrawal_inflation'])
    if sr_wd_inflation is not None and ri_wd_inflation is not None:
        report.add(check_eq(
            'withdrawal_inflation 一致性',
            actual=sr_wd_inflation, expected=ri_wd_inflation, severity='WARN',
            tolerance=1e-9
        ))

    # ── 8. Forecast horizon end <= last_date ──
    fc_end = _safe_get(analyze, ['forecast', 'rolling', -1, 'end'])
    last_date = _safe_get(analyze, ['common', 'metrics', 'end'])
    if fc_end is not None and last_date is not None:
        report.add(check_le(
            'forecast_horizon_end <= last_date',
            actual=str(fc_end), expected=str(last_date), severity='CRITICAL'
        ))

    # ── 9. Pension sanity ──
    pension_monthly = _safe_get(analyze, ['retirement_inputs', 'pension_monthly'])
    if pension_monthly is not None:
        report.add(check_non_negative(
            'pension_monthly >= 0',
            actual=pension_monthly, severity='WARN'
        ))

    # ── 10. PV 一致性:pv == pv_raw - cost(若有手續費) ──
    pv = _safe_get(analyze, ['inputs', 'pv'])
    pv_raw = _safe_get(analyze, ['inputs', 'pv_raw'])
    if pv is not None and pv_raw is not None:
        report.add(check_le(
            'pv <= pv_raw(手續費後應 <= raw)',
            actual=pv, expected=pv_raw, severity='WARN'
        ))

    # ── 11. Phase 6 (Item 13): current_age > 0 ──
    if cur_age is not None:
        report.add(check_positive(
            'current_age > 0',
            actual=cur_age, severity='CRITICAL'
        ))

    # ── 12. Phase 6 (Item 13): retirement_end_age > retirement_age >= current_age ──
    ret_age = _safe_get(analyze, ['retirement_inputs', 'retirement_age'])
    if cur_age is not None and ret_age is not None and end_age is not None:
        report.add(check_ge(
            'retirement_age >= current_age',
            actual=ret_age, expected=cur_age, severity='CRITICAL'
        ))
        report.add(check_gt(
            'retirement_end_age > retirement_age',
            actual=end_age, expected=ret_age, severity='CRITICAL'
        ))

    # ── 13. Phase 6 (Item 13): forecast_end_age == current_age + N ──
    fc_horizon_age = _safe_get(analyze, ['retirement_inputs', 'forecast_end_age'])
    if cur_age is not None and n is not None:
        expected_fc_end_age = cur_age + n
        if fc_horizon_age is not None:
            report.add(check_eq(
                'forecast_end_age == current_age + N',
                actual=fc_horizon_age, expected=expected_fc_end_age, severity='CRITICAL'
            ))

    # ── 14. Phase 6 (Item 13): retirement_horizon == retirement_end_age - current_age ──
    ret_horizon = _safe_get(analyze, ['retirement_inputs', 'retirement_horizon'])
    if cur_age is not None and end_age is not None and ret_horizon is not None:
        report.add(check_eq(
            'retirement_horizon == retirement_end_age - current_age',
            actual=ret_horizon, expected=end_age - cur_age, severity='CRITICAL'
        ))

    # ── 15. Phase 6 (Item 13): 滾動樣本 actual_years >= N - tolerance ──
    if rolling and n is not None:
        tolerance = 0.5  # ±半年�限
        threshold = n - tolerance
        bad = [r for r in rolling if r.get('years', 0) < threshold]
        if bad:
            worst = min(r.get('years', 0) for r in bad)
            report.add(_make_check(
                name='rolling.actual_years >= N - 0.5',
                severity='WARN', status='FAIL',
                expected=f'>= {threshold}', actual=worst,
                message=f'{len(bad)}/{len(rolling)} 滾動樣本 < N-{tolerance},worst={worst:.2f}'
            ))
        else:
            report.add(_make_check(
                name='rolling.actual_years >= N - 0.5',
                severity='WARN', status='PASS',
                expected=f'>= {threshold}', actual=f'all {len(rolling)} samples pass'
            ))

    # ── 16. Phase 6 (Item 13): Sequence Risk 年齡矩陣未超出模擬終點 ──
    sr_cfg = _safe_get(analyze, ['sequence_risk', 'config'], default={})
    sr_success_by_age = _safe_get(analyze, ['sequence_risk', 'success_rate_by_age'], default={})
    if sr_success_by_age and cur_age is not None and sr_cfg:
        sr_horizon_years = sr_cfg.get('horizon_years', 0)
        max_age_in_data = max(int(a) for a in sr_success_by_age.keys())
        expected_max_age = cur_age + sr_horizon_years
        if max_age_in_data > expected_max_age:
            report.add(_make_check(
                name='sr.success_rate_by_age 未超出模擬終點',
                severity='CRITICAL', status='FAIL',
                expected=f'<= {expected_max_age}', actual=max_age_in_data,
                message=f'SR 矩陣 max age {max_age_in_data} > 模擬終點 {expected_max_age}'
            ))
        else:
            report.add(_make_check(
                name='sr.success_rate_by_age 未超出模擬終點',
                severity='CRITICAL', status='PASS',
                expected=f'<= {expected_max_age}', actual=max_age_in_data
            ))
        ordered_rates = [sr_success_by_age[a] for a in sorted(sr_success_by_age, key=int)]
        monotonic = all(ordered_rates[i] >= ordered_rates[i + 1] for i in range(len(ordered_rates) - 1))
        report.add(_make_check(
            name='sr.success_rate_by_age 單調不上升',
            severity='CRITICAL',
            status='PASS' if monotonic else 'FAIL',
            expected='older-age survival <= younger-age survival',
            actual='PASS' if monotonic else ordered_rates,
            message='' if monotonic else '存活率隨年齡上升，可能表示破產路徑被錯誤復活',
        ))

    # ── 17. Phase 6 (Item 13): 無未來資料洩漏 (look-ahead bias) ──
    # 驗證:所有資料 end <= now,且 forecast.rolling 不超出 data end
    import datetime as _dt
    today = _dt.date.today().isoformat()
    common_end = _safe_get(analyze, ['common', 'metrics', 'end'])
    if common_end:
        report.add(check_le(
            'data.end <= today (無未來資料)',
            actual=str(common_end), expected=today, severity='WARN'
        ))
    if rolling and common_end:
        last_rolling_end = rolling[-1].get('end') if rolling else None
        if last_rolling_end:
            report.add(check_le(
                'rolling.last_end <= data.end',
                actual=str(last_rolling_end), expected=str(common_end), severity='CRITICAL'
            ))

    # ── 18. Phase 6 (Item 13): 圖表 X 軸長度與資料陣列維度一致 ──
    nav_series = _safe_get(analyze, ['nav_series'], default={})
    if nav_series:
        for mode in ('common', 'dynamic', 'full'):
            if mode not in nav_series:
                continue  # mode 不存在(合理,例如 full 資料不足)
            series = nav_series[mode]
            n_pts = len(series) if series is not None else 0
            if n_pts < 1:
                report.add(_make_check(
                    name=f'nav_series.{mode} 資料點數 >= 1',
                    severity='WARN', status='FAIL',
                    expected='>= 1', actual=n_pts
                ))
            else:
                report.add(_make_check(
                    name=f'nav_series.{mode} 資料點數 >= 1',
                    severity='WARN', status='PASS',
                    expected='>= 1', actual=n_pts
                ))

    report.finalize()
    return report


def raise_if_critical(report: ValidationReport) -> None:
    """任何 CRITICAL FAIL → raise ModelValidationError"""
    if report.has_critical_fail():
        raise ModelValidationError(report)
