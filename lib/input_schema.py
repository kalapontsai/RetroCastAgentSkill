"""
lib/input_schema.py
- Phase 4.2(P20): input schema 統一驗證
- 集中處理預設、範圍、依賴關係
- 取代各 endpoint (analyze / v2/sequence_risk / v2/monte_carlo) 散落的 parse 邏輯

設計:
- SpecialExpense:dataclass(year_offset, amount, label?)
- RetirementInputs:dataclass 完整退休輸入 + derived() 計算衍生欄位
- parse_retirement_inputs(body) -> RetirementInputs:集中 parse + 預設 + 範圍驗證
- InputSchemaError:輸入不合法時 raise(由 endpoint 統一轉成 400 Bad Request)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ───────── Exceptions ─────────
class InputSchemaError(ValueError):
    """輸入欄位不合法(由 endpoint 統一轉 400 Bad Request)"""


# ───────── Dataclasses ─────────
@dataclass
class SpecialExpense:
    """退休期間一次性大額支出(如房屋裝修、醫療)"""
    year_offset: int       # 退休後第幾年(0 = 退休當年年初)
    amount: float          # NT$
    label: str = ''        # 顯示用 label(ex: '房屋裝修')


@dataclass
class DerivedRetirement:
    """衍生欄位:由 RetirementInputs.derived() 計算"""
    years_to_retirement: int        # retirement_age - current_age
    retirement_years: int           # retirement_end_age - retirement_age
    forecast_end_age: int           # current_age + forecast_years
    retirement_horizon: int         # retirement_end_age - current_age


@dataclass
class RetirementInputs:
    """退休投資 + 提款 + 年金 + 一次性支出 + 成本設定
    對齊 audit 文件 §4「應該有的新輸入參數」"""
    # 生命週期
    current_age: int = 55           # 必填,1-120
    retirement_age: int = 65        # 必填,>= current_age,<= 120
    retirement_end_age: int = 90    # 必填,> retirement_age,<= 120
    forecast_years: int = 10        # N,1-50
    # 提款
    withdrawal_monthly: float = 0.0 # NT$,>=0(預設 0=F1 模式)
    withdrawal_inflation: float = 0.03  # 預設 0.03
    # 年金
    pension_monthly: float = 0.0    # NT$,>=0(預設 0)
    pension_inflation_adjust: float = 0.0  # 預設 0(只跟物價,不主動加)
    # 一次性支出
    special_expenses: list[SpecialExpense] = field(default_factory=list)
    # 成本
    fee_buy: float = 0.0
    fee_sell: float = 0.0
    tax_sell: float = 0.0
    slippage: float = 0.0

    def derived(self) -> DerivedRetirement:
        return DerivedRetirement(
            years_to_retirement=self.retirement_age - self.current_age,
            retirement_years=self.retirement_end_age - self.retirement_age,
            forecast_end_age=self.current_age + self.forecast_years,
            retirement_horizon=self.retirement_end_age - self.current_age,
        )


# ───────── Helpers ─────────
def _validate_age(name: str, value: Any, min_v: int = 1, max_v: int = 120) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InputSchemaError(f'{name} 必須是數字,got {type(value).__name__}')
    iv = int(value)
    if iv < min_v or iv > max_v:
        raise InputSchemaError(f'{name} 應在 [{min_v}, {max_v}],got {iv}')
    return iv


def _validate_non_negative_float(name: str, value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise InputSchemaError(f'{name} 必須是數字,got {value!r}')
    if f < 0:
        raise InputSchemaError(f'{name} 應 >= 0,got {f}')
    return f


def _validate_special_expenses(raw: Any) -> list[SpecialExpense]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InputSchemaError(f'special_expenses 應為 list,got {type(raw).__name__}')
    out: list[SpecialExpense] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InputSchemaError(f'special_expenses[{i}] 應為 dict')
        if 'year_offset' not in item or 'amount' not in item:
            raise InputSchemaError(
                f'special_expenses[{i}] 需含 year_offset + amount 欄位'
            )
        yo = _validate_age('special_expenses[%d].year_offset' % i,
                           item['year_offset'], min_v=0, max_v=80)
        amt = _validate_non_negative_float(
            'special_expenses[%d].amount' % i, item['amount']
        )
        out.append(SpecialExpense(
            year_offset=yo,
            amount=amt,
            label=str(item.get('label') or ''),
        ))
    return out


# ───────── Main parse ─────────
def parse_retirement_inputs(body: dict | None) -> RetirementInputs:
    """集中解析退休輸入 body

    body 為 None → 用全預設值(current_age=55, retirement_age=65,
    retirement_end_age=90, forecast_years=10)

    Raises:
        InputSchemaError:任何欄位不合法
    """
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise InputSchemaError(f'body 應為 dict,got {type(body).__name__}')

    # 預設值
    DEFAULT_CURRENT_AGE = 55
    DEFAULT_RETIREMENT_AGE = 65
    DEFAULT_RETIREMENT_END_AGE = 90
    DEFAULT_FORECAST_YEARS = 10

    # 生命週期
    cur = _validate_age(
        'current_age', body.get('current_age', DEFAULT_CURRENT_AGE),
    )
    ret = _validate_age(
        'retirement_age', body.get('retirement_age', DEFAULT_RETIREMENT_AGE),
        min_v=cur,   # 必須 >= current_age
    )
    end = _validate_age(
        'retirement_end_age', body.get('retirement_end_age', DEFAULT_RETIREMENT_END_AGE),
        min_v=ret + 1,   # 必須 > retirement_age
    )
    n = _validate_age(
        'forecast_years', body.get('forecast_years', DEFAULT_FORECAST_YEARS),
        min_v=1, max_v=50,
    )

    # 提款
    wd_monthly = _validate_non_negative_float(
        'withdrawal_monthly', body.get('withdrawal_monthly', 0.0)
    )
    wd_inflation = _validate_non_negative_float(
        'withdrawal_inflation', body.get('withdrawal_inflation', 0.03)
    )

    # 年金
    pension_monthly = _validate_non_negative_float(
        'pension_monthly', body.get('pension_monthly', 0.0)
    )
    pension_inflation = _validate_non_negative_float(
        'pension_inflation_adjust', body.get('pension_inflation_adjust', 0.0)
    )

    # 一次性支出
    specials = _validate_special_expenses(body.get('special_expenses'))

    # 成本
    fee_buy = _validate_non_negative_float('fee_buy', body.get('fee_buy', 0.0))
    fee_sell = _validate_non_negative_float('fee_sell', body.get('fee_sell', 0.0))
    tax_sell = _validate_non_negative_float('tax_sell', body.get('tax_sell', 0.0))
    slippage = _validate_non_negative_float('slippage', body.get('slippage', 0.0))

    return RetirementInputs(
        current_age=cur,
        retirement_age=ret,
        retirement_end_age=end,
        forecast_years=n,
        withdrawal_monthly=wd_monthly,
        withdrawal_inflation=wd_inflation,
        pension_monthly=pension_monthly,
        pension_inflation_adjust=pension_inflation,
        special_expenses=specials,
        fee_buy=fee_buy,
        fee_sell=fee_sell,
        tax_sell=tax_sell,
        slippage=slippage,
    )