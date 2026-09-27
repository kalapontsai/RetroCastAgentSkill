# RetroCast CLI Input Schema 參考

CLI 接受 JSON body（透過 `--input` 或 stdin），shape 對齊 Flask `POST /api/analyze`。

## 完整欄位

### 必填

| 欄位 | 型別 | 預設 | 範圍 | 單位 | 備註 |
|---|---|---|---|---|---|
| `profile` | string | — | — | — | `user_profile/<name>.csv` 的 stem |

### 常用選填

| 欄位 | 型別 | 預設 | 範圍 | 單位 | 備註 |
|---|---|---|---|---|---|
| `n` | int | 10 | 1–50 | 年 | N 年回測區間 |
| `pv` | float \| null | null (自動用市值) | ≥ 0 | NT$ | 不填 = market_value |
| `weights` | string \| null | null (市值加權) | — | `2330:0.3,2317:0.7` | 不填用市值權重 |
| `benchmark` | string \| null | null | — | ticker | 0050 / 006208 / 2330 |
| `start_date` | string | "2000-01-01" | — | ISO | FinMind 抓價起點 |
| `end_date` | string | default_end_date() | — | ISO | 不填 = 最近一個交易日 |

### 交易成本（必填或用預設）

| 欄位 | 型別 | 預設 | 範圍 | 單位 | 備註 |
|---|---|---|---|---|---|
| `fee_buy` | float | 0 | 0–0.1 | 小數 | 0.00142 = 0.142% |
| `fee_sell` | float | 0 | 0–0.1 | 小數 | |
| `tax_sell` | float | 0 | 0–0.1 | 小數 | 0.003 = 0.3% (台股賣出證交稅) |
| `slippage` | float | 0 | 0–0.1 | 小數 | |

### v2 退休參數

| 欄位 | 型別 | 預設 | 範圍 | 單位 | 備註 |
|---|---|---|---|---|---|
| `v2_current_age` | int | 55 | 1–120 | 歲 | |
| `v2_retirement_age` | int | 65 | ≥ current_age | 歲 | |
| `v2_retirement_end_age` | int | 90 | > retirement_age | 歲 | |
| `v2_n_simulations` | int | 1000 | ≥ 100 | 次 | Monte Carlo 模擬次數 |
| `v2_withdrawal_monthly` | float | 0 | ≥ 0 | NT$ | 月提款 |
| `v2_withdrawal_inflation` | float | 0.03 | ≥ 0 | **小數** | 提款通膨率 |
| `v2_pension_monthly` | float | 0 | ≥ 0 | NT$ | 月年金 |
| `v2_pension_inflation` | float | 0.02 | ≥ 0 | **小數** | 年金調整率 |
| `v2_pension_start_age` | int | (=v2_retirement_age) | ≥ current_age | 歲 | |
| `v2_special_expenses` | list | `[]` | — | — | `[{year_offset, amount, label}]` |
| `v2_horizon_years` | int | (=retirement_end - current) | ≥ 1 | 年 | 可顯式覆寫 |
| `v2_risk_free_rate` | float | 0.015 | 0–0.2 | 小數 | Sharpe Rf，預設台灣 10Y 公債 |

## 單位換算（agent 必看）

**網頁表單 vs CLI body 單位差異**：

| 欄位 | 表單（user-facing） | CLI body（傳給 retrocast_cli.py） |
|---|---|---|
| `v2_withdrawal_inflation` | `3` (%) | `0.03` (decimal) |
| `v2_pension_inflation` | `2` (%) | `0.02` (decimal) |
| `fee_buy` | `0.142` (%) | `0.00142` (decimal) |
| `tax_sell` | `0.3` (%) | `0.003` (decimal) |
| `slippage` | `0.1` (%) | `0.001` (decimal) |

**agent 唯一允許的算術**：上述單位換算。其他一律交給 CLI。

## 範例 body

```json
{
  "profile": "kadela",
  "n": 5,
  "fee_buy": 0.00142,
  "tax_sell": 0.003,
  "slippage": 0.001,
  "v2_current_age": 55,
  "v2_retirement_age": 60,
  "v2_retirement_end_age": 90,
  "v2_n_simulations": 1000,
  "v2_withdrawal_monthly": 80000,
  "v2_withdrawal_inflation": 0.03,
  "v2_pension_monthly": 50000,
  "v2_pension_inflation": 0.02,
  "v2_special_expenses": []
}
```

## 範例特殊支出

```json
"v2_special_expenses": [
  {"year_offset": 5, "amount": 1000000, "label": "房屋裝修"},
  {"year_offset": 15, "amount": 500000, "label": "醫療"}
]
```

`year_offset` = 退休後第幾年（0 = 退休當年年初）
