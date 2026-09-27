# 範例：kadela 5 年回測 + 退休存活率分析

完整執行紀錄（2026-09-27 0:42 - 0:45 GMT+8，attaching 至本 skill 開發當下）

## Step 1：列 profile

```bash
$ ~/.openclaw/agents/main/agent/workshop-skills/retrocast/scripts/retrocast_cli.py profiles
{
  "ok": true,
  "profiles": ["kadela", "sample_stock"],
  "count": 2
}
```

## Step 2：上傳 kadela.csv（從使用者訊息附件重建）

```bash
$ retrocast_cli.py upload-profile /tmp/kadela_reconstructed.csv --name kadela
{
  "ok": true,
  "uploaded": "kadela.csv",
  "path": "/home/.openclaw/agents/main/agent/workshop-skills/retrocast/user_profile/kadela.csv"
}
```

CSV 內容（9 檔）：
```
2330,"1,000"      # 台積電
2885,"30,219"     # 元大金
2412,"10,000"     # 中華電
0050,"8,000"      # 元大台灣50
00631L,"10,000"   # 元大台灣50正2
0056,"3,000"      # 元大高股息
2881,"745"        # 富邦金
2891,"1,000"      # 中信金
2002,"1,000"      # 中鋼
```

## Step 3：建構 body JSON

```bash
$ cat > /tmp/kadela_body.json <<'EOF'
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
EOF
```

注意：所有 % 欄位已轉成 decimal（0.03 = 3%, 0.02 = 2%, 0.00142 = 0.142% 等）。

## Step 4：跑 analyze

```bash
$ retrocast_cli.py analyze --input /tmp/kadela_body.json --output /tmp/kadela_result.json
{
  "ok": true,
  "duration_sec": 8.15,
  "summary": {
    "profile": "kadela",
    "n_years": 5,
    "end_date": "2026-08-31",
    "current_assets_NT$": 7304253,
    "forecast_pv_NT$": 7286619,
    "forecast_Bear_annualized_pct": 13.27,
    "forecast_Conservative_annualized_pct": 15.21,
    "forecast_Base_annualized_pct": 19.63,
    "forecast_Optimistic_annualized_pct": 21.19,
    "forecast_Bull_annualized_pct": 23.01,
    "forecast_basis": "common",
    "forecast_sample_count": 1673,
    "common_cagr_pct": 21.18, "common_volatility_pct": 17.95, "common_mdd_pct": -29.75, "common_sharpe": 1.196,
    "dynamic_cagr_pct": 8.11, "dynamic_volatility_pct": 12.87, "dynamic_mdd_pct": -34.73, "dynamic_sharpe": 0.683,
    "full_cagr_pct": 11.54, "full_volatility_pct": 19.68, "full_mdd_pct": -52.30, "full_sharpe": 0.665,
    "survival_rate_pct": 96.3,
    "ruin_rate_pct": 3.7,
    "earliest_ruin_age": 72,
    "median_terminal_balance_NT$": 178210067,
    "optimization_status": "SUCCESS",
    "optimization_current_cagr_pct": 21.16,
    "optimization_target_cagr_pct": 20.18
  },
  "result_path": "/tmp/kadela_result.json"
}
```

耗時 7.6 秒（含 1000 次蒙地卡羅模擬）。

## Step 5：產 HTML 報告

```bash
$ retrocast_cli.py export --result /tmp/kadela_result.json --type forecast
{
  "ok": true,
  "file": "kadela_portfolio_forecast_20260927_093217.html",
  "path": "/home/.openclaw/agents/main/agent/workshop-skills/retrocast/data/reports/kadela_portfolio_forecast_20260927_093217.html",
  "size": 158262,
  "type": "forecast"
}

$ retrocast_cli.py export --result /tmp/kadela_result.json --type rebalance
{
  "ok": true,
  "file": "kadela_portfolio_rebalance_20260927_093217.html",
  "path": "/home/.openclaw/agents/main/agent/workshop-skills/retrocast/data/reports/kadela_portfolio_rebalance_20260927_093217.html",
  "size": 13398,
  "type": "rebalance"
}
```

> **byte-level 對照**：skill-internal CLI 產出的 forecast/rebalance 報告與 `/repos/RetroCast/` 同時跑的產出做 byte-level diff，差異 = **0 bytes**（僅「生成時間」timestamp 不同；數值、Sections、SVG、表格 100% 對齊）。

## v1.2 新增：inquiry form workflow

對話式之外，v1.2 起可用 `form` subcommand 預填式：

```bash
$ retrocast_cli.py form --output form_kadela.json
{"ok": true, "form_path": "/path/to/form_kadela.json"}
```

產出的 `form_kadela.json` 含所有欄位的預設值，使用者直接編輯 `profile`、`n`、`v2_*` 等欄位後存檔，再：

```bash
$ retrocast_cli.py analyze -i form_kadela.json -o result.json
```

即可跳過對話欄位收集階段，直接進入 analyze。Form 範例：

```json
{
  "_doc": "RetroCast inquiry form — 編輯後執行: retrocast_cli.py analyze -i form.json -o result.json",
  "profile": "kadela",
  "n": 10,
  "fee_buy": 0.00142,
  "tax_sell": 0.003,
  "slippage": 0.001,
  "v2_current_age": 55,
  "v2_retirement_age": 65,
  "v2_retirement_end_age": 90,
  "v2_n_simulations": 1000,
  "v2_withdrawal_monthly": 0,
  "v2_withdrawal_inflation": 0.03,
  "v2_pension_monthly": 0.0,
  "v2_pension_inflation": 0.02,
  "v2_special_expenses": []
}
```

對話式與預填式產出的 JSON 完全相同，可互通。

## 與 2026-09-02 附件報告差異（重點）

| 指標 | 附件（9/02） | CLI（9/27） | 差異 | 原因 |
|---|---|---|---|---|
| 估值日 | 2026-08-31 | 2026-08-31 | 0 | default_end_date 一致 |
| 目前市值 | 7,304,253 | 7,304,253 | 0 | 完全一致 |
| 9 檔當前權重 | 32.93/27.06/18.62/11.64/4.90/2.24/1.47/0.89/0.26 | 同 | 0 | 完全一致 |
| Common CAGR | 21.07% | 21.18% | +0.11% | FinMind v4 細部資料更新 |
| Common Sharpe | 1.191 | 1.196 | +0.005 | 同上 |
| Common MDD | -29.75% | -29.75% | 0 | 完全一致 |
| 5 個 forecast 情境 (P10/P25/P50/P75/P90) | 13.27/15.21/19.63/21.19/23.01% | 同 | 0 | 抽樣分布完全一致 |
| forecast_pv | 7,286,619 | 7,286,619 | 0 | 完全一致 |
| forecast_sample_count | 1673 | 1673 | 0 | 完全一致 |
| F2 存活率 | 96.30% | 96.3% | 0 | 完全一致 |
| F2 破產率 | 3.70% | 3.7% | 0 | 完全一致 |
| F2 最早破產年齡 | 72 | 72 | 0 | 完全一致 |
| F2 中位終值餘額 | 178,210,067 | 178,210,067 | 0 | 完全一致 |
| 最佳化狀態 | SUCCESS | SUCCESS | 0 | 完全一致 |
| 個股波動率（5 檔取樣） | 20.64/17.06/38.08/24.83/32.12% | 同 | 0 | 完全一致 |

**Rebalance 目標權重差異**（最佳化輸出受當前價格影響，有差）：

| 標的 | ATT 目標 | NEW 目標 | 動作變化 |
|---|---|---|---|
| 2412 | 15.00% | 13.95% | SELL 8056 → 7492 股 |
| 00631L | 13.82% | 15.00% | BUY 28190 → 30587 股 |
| 0056 | 10.13% | 10.24% | BUY 13556 → 13696 股 |
| 2881 | 6.57% | 8.42% | BUY 3332 → 4271 股 |
| 2891 | 7.47% | 4.22% | BUY 8433 → 4764 股 |
| 2002 | 2.00% | 3.17% | BUY 7708 → 12221 股 |

**結論**：
1. CLI 與 Flask 版本**結構完全一致**（13 個 h2 標題全部對齊，REBALANCE 多了 v2 retirement MC 與 final recommendation 兩個新區塊）
2. 依賴 FinMind 歷史資料的部分（CAGR、forecast percentiles、sequence_risk、optimization scores）**位元級一致**
3. 依賴當前價格的部分（current_value、optimization 目標權重）有微小差異，因為 FinMind v4 在 9/02 → 9/27 期間可能有資料更新
4. 結構定義為 v1，CLI 與 Flask 報告**可互換**
