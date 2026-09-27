---
name: retrocast
description: 對話式驅動 RetroCast 投資組合歷史回測與退休存活率分析。當使用者提到 投資組合 / 退休 / portfolio / RetroCast / backtest / 回測 / rebalance / 蒙地卡羅 / Monte Carlo / 提款 / 存活率 / 台股回測 / 0050 等關鍵字時觸發。所有計算與判斷由 scripts/retrocast_cli.py 執行；agent 僅負責欄位收集、結果 relay 與錯誤處理，不做數值解讀或投資建議。
---

# RetroCast Agent Skill v1.2

對話式 CLI 介面取代 Flask 網頁 UI（v1.2 起 `app.py` 完全 Flask-free）。Agent 透過對話收集欄位、或輸出 inquiry form 讓使用者預填，呼叫 CLI 後轉交報告給使用者；所有計算與判斷邏輯保留在 skill-internal 的 `scripts/retrocast_cli.py` + `app.py` + `lib/`。

**v1.2 重點**：
- **Token 來源**：OpenClaw secrets `FINMIND_TOKEN`（首選，audit + runtime 一致），不再用 `./config/` 橋接檔
- **Inquery form**：新增 `form` subcommand 輸出含預設值的 JSON 範本，支援「對話式逐欄問」與「預填式直接 submit」兩種 workflow
- **Flask-free**：`app.py` 移除所有 Flask imports / routes / `SafeJSONProvider`；CLI 直接 `import _run_analyze`
- **Self-contained**：skill 不依賴 `/workspace/repos/RetroCast/` 或 `/mnt/d/stock/RetroCast/`，可作為獨立 GitHub repo issue

## 觸發條件

使用者意圖涉及下列任一項時觸發此 skill：

- 投資組合分析、回測、backtest、portfolio analysis
- 退休存活率、sequence risk、提款計畫
- 再平衡、rebalance、target weight
- 蒙地卡羅模擬、Monte Carlo、未來 N 年推估
- 個別台股歷史績效、CAGR / Sharpe / MDD / VaR
- 「跑 kadela 的分析」、「看一下我的 0050 退休規劃」這類對話
- 明確提及 RetroCast

## 核心約束（agent 行為契約）

**Agent 可以**：
- 詢問欄位、用預設值填補
- 把使用者口語值轉成 CLI JSON body（單位換算如 %→decimal 是必要的最小轉換）
- 用 `form` subcommand 產出 inquiry form 範本給使用者預填
- 呼叫 CLI 並讀取 stdout JSON
- Relay `summary` 區塊的數字給使用者、報告檔路徑
- 處理錯誤（轉述 `code` 與 `error`、引導使用者重試）

**Agent 嚴禁**：
- 自行換算 %↔decimal 以外的計算（如「總資產 = 市值加權」、「提款 30 年會破產」等推論）
- 解讀 CAGR / Sharpe / MDD / 存活率的好壞或推薦買賣
- 假造 CLI 沒回報的數字
- 改寫使用者沒提供的欄位
- 直接 import `lib/` 或 `app.py`——所有計算走 CLI
- **不要繞過 form 流程直接寫 JSON**：form 範本是給使用者看的契約，預填式才直接編輯；對話式必須走逐欄詢問

## 架構

```
User ──對話── Agent ──subprocess── scripts/retrocast_cli.py ──┐
                                            │                 │
                                            ▼                 │
                                  _run_analyze() (app.py)      │
                                            │                 │
                                            ▼                 ▼
                                    lib/ + data/price_cache   JSON envelope (stdout)
                                            │
                                            ▼
                                    HTML report (data/reports/)
```

**唯一入口**：`scripts/retrocast_cli.py`（位於 `~/.openclaw/agents/main/agent/workshop-skills/retrocast/scripts/`）

CLI 內建 venv auto-bootstrap（偵測到 pandas 不可用時自動用 `.venv/bin/python` re-exec），所以 agent 直接呼叫即可。**skill 完全 self-contained**——內含 `lib/`、`templates/`（report.html + rebalance_report.html）、`app.py`（Flask-free）、`app_config.py`、skill-internal `.venv/`、FinMind price cache 與 profile CSVs；**不需** `/workspace/repos/RetroCast/` 或 `/mnt/d/stock/RetroCast/`。

## Subcommand 速查

| 用途 | CLI 呼叫 |
|---|---|
| 列出 profile | `retrocast_cli.py profiles` |
| 預覽持倉 | `retrocast_cli.py preview <name>` |
| 上傳 CSV | `retrocast_cli.py upload-profile <csv-path>` |
| 健康檢查 | `retrocast_cli.py health [--v2]` |
| **輸出 inquiry form 範本** | `retrocast_cli.py form [--output PATH] [--profile NAME]` |
| 主分析 | `retrocast_cli.py analyze -i <body.json> [-o <result.json>]` |
| 產 HTML 報告 | `retrocast_cli.py export -r <result.json> -t forecast\|rebalance` |

完整欄位定義見 `references/input-schema.md`，對話流程範本見 `references/workflow.md`。

## 工作流（agent 標準流程）

### 路徑 A：對話式（推薦，較不容易打錯）

1. **列出 profiles** → `retrocast_cli.py profiles` → 給使用者看可用選項
2. **確認輸入** → 詢問必要欄位（profile、n 年、退休參數），其他用預設
3. **建構 body JSON** → 參考 `references/input-schema.md`，注意 %→decimal 換算
4. **跑 analyze** → `analyze -i body.json -o result.json` → 取 `summary` 區塊
5. **（選擇性）產報告** → `export -r result.json -t forecast` 或 `rebalance`
6. **回報使用者** → 用 `summary` 區塊數字 + 報告絕對路徑

### 路徑 B：預填式（使用者直接編輯 form.json）

1. **產 form 範本** → `retrocast_cli.py form > form.json`（或 `--output form.json`）
2. **使用者編輯** → 填寫 profile / n / 退休參數等欄位
3. **跑 analyze** → `analyze -i form.json -o result.json` → 取 `summary` 區塊
4. **（選擇性）產報告** → 同路徑 A 步驟 5
5. **回報使用者** → 同路徑 A 步驟 6

詳細對話範例見 `examples/kadela-forecast.md`，form JSON 範本見 `form` subcommand 輸出。

## 輸出契約

### analyze 回傳 JSON envelope

```json
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

### form 回傳（--output 時）

```json
{"ok": true, "form_path": "/path/to/form.json"}
```

form 純輸出（無 `--output`）直接印 JSON 到 stdout，可 pipe `> form.json`。

### export 回傳

```json
{
  "ok": true,
  "file": "kadela_portfolio_forecast_20260927_093217.html",
  "path": "/home/.openclaw/agents/main/agent/workshop-skills/retrocast/data/reports/kadela_portfolio_forecast_20260927_093217.html",
  "size": 158262,
  "type": "forecast"
}
```

### 錯誤 envelope（agent 應轉述 code 與 error）

```json
{
  "ok": false,
  "code": "PROFILE_NOT_FOUND",
  "error": "kadela.csv 不存在於 ~/.openclaw/agents/main/agent/workshop-skills/retrocast/user_profile"
}
```

常見 code：`TOKEN_MISSING` / `PROFILE_NOT_FOUND` / `INVALID_INPUT` / `TICKER_NOT_FOUND` / `FINMIND_ERROR` / `RENDER_FAILED`

## 不做的事

- 不發送任何對外訊息（email / 推文 / 券商下單）
- 不寫入投資建議或「建議買進 XXX」的對話內容
- 不 commit 任何變更（CSV、上傳檔）
- 不繞過 CLI 直接呼叫 `lib.finmind` / `app._run_analyze`（破壞 agent/script 邊界）

## 環境前置（v1.2）

- **Skill root**：`~/.openclaw/agents/main/agent/workshop-skills/retrocast/`（**唯一**依賴——所有檔案都在這裡）
- **不再需要** `/workspace/repos/RetroCast/` 或 `/mnt/d/stock/RetroCast/`。`/repos/RetroCast/` 保留作為 git 開發拷貝（HEAD `6bff1f9`），skill 與之脫鉤可獨立運作
- **FinMind token 三段式載入優先序**：
  1. **OpenClaw secrets `FINMIND_TOKEN`**（首選，audit + runtime 一致）
  2. `~/.config/retrocast/finmind-token`（legacy 本地橋接，chmod 600）
  3. env var `FINMIND_TOKEN`（CI / testing 友善）
- **Python 3.14.4 venv**：skill-internal `.venv/`；CLI 自動偵測並 re-exec
- **byte-level 驗證**：skill-internal CLI 產出與 `/repos/RetroCast/` 產出 0 byte diff（僅「生成時間」timestamp 不同）

## 首次安裝（agent 引導 SOP）

**情境 A — Token 還沒設定（v1.2 推薦路徑）**：

1. **Agent 提示使用者設定 FinMind token via OpenClaw secrets**：
   ```
   這個 skill 需要 FinMind API token。請用以下指令設定（token 不會留在 shell history）：
   $ openclaw secrets store set FINMIND_TOKEN --kind env
   # CLI 會要你貼 token（masked prompt）
   ```
2. **使用者執行指令並貼 token** → token 存進 OpenClaw secrets
3. **驗證 token 讀取**：
   ```bash
   .venv/bin/python scripts/retrocast_cli.py health
   # 預期: "finmind_token": true
   ```
4. **（可選）列出 profiles 確認 CSV 已就位**：
   ```bash
   .venv/bin/python scripts/retrocast_cli.py profiles
   ```

**情境 B — 已透過其他方式有 token（legacy 橋接）**：

- 把 token 寫到 `~/.config/retrocast/finmind-token`（純 token 字串，chmod 600）：
  ```bash
  umask 077
  echo "your_jwt_here" > ~/.config/retrocast/finmind-token
  chmod 600 ~/.config/retrocast/finmind-token
  ```
- 或 export `FINMIND_TOKEN` env var

**情境 C — 全新機器 / 重裝**：

```bash
# 1. 將整個 skill 目錄複製到目標位置（保留 .venv/ 或不保留均可）
# 2. 建立 venv + 裝套件
cd ~/.openclaw/agents/main/agent/workshop-skills/retrocast
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 3. 設定 FinMind token（見情境 A）
```

**首次跑 analyze 的快取**：skill-internal `data/price_cache/` 已封裝 29 個 FinMind JSON（11 MB），首次跑 analyze 不用重新抓台股歷史價。

## 作為獨立 GitHub repo issue

skill 已備好可直接 push：

```bash
cd ~/.openclaw/agents/main/agent/workshop-skills/retrocast
git init
git add .
git commit -m "feat: RetroCast v1.2 self-contained agent skill"
# 在 GitHub 建新 repo 後：
git remote add origin https://github.com/<owner>/RetroCast.git
git push -u origin main
```

需要的 GitHub 動作（使用者）：
1. 在 GitHub 建新 repo `RetroCast`（公開或私有都可）
2. 給本地端 push 權限（token / SSH key）
3. push 上去後即可 fork 與 PR

skill 內含完整檔案：`README.md` / `LICENSE` / `CHANGELOG.md` / `.gitignore` / `requirements.txt`，符合 GitHub repo 標準結構。
