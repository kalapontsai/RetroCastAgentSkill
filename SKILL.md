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
| 主分析 | `retrocast_cli.py analyze -i <file_path> [-o <result.json>]` **⚠️ -i 必為檔案路徑，絕不可接 inline JSON** |
| 產 HTML 報告 | `retrocast_cli.py export -r <result.json> -t forecast\|rebalance` |

完整欄位定義見 `references/input-schema.md`，對話流程範本見 `references/workflow.md`。

## 工作流（agent 標準流程）

### 路徑 A：對話式（推薦，較不容易打錯）

0. **持股來源驗證**（**僅在使用者提到「目前/現況 stock_hold」等 source-of-truth 觸發詞時執行**）→ 從 stock_hold `/holdings` 拉當下持股，跟候選 profile 的 `preview` 比對；若任一 ticker 股數不同、或 stock_hold 多了新 ticker，把 diff 列出來問使用者「重用舊 profile（資料較舊）／建新 profile（推薦 `<name>_YYYY-MM-DD` 保留歷史）？」——**絕不要在沒有讓使用者決策的情況下直接覆寫舊 profile**。
1. **列出 profiles** → `retrocast_cli.py profiles` → 給使用者看可用選項
2. **確認輸入** → 詢問必要欄位（profile、n 年、退休參數），其他用預設
3. **建構 body JSON** → 參考 `references/input-schema.md`，注意 %→decimal 換算
4. **跑 analyze** → `analyze -i body.json -o result.json` → 取 `summary` 區塊
5. **產報告** → `export -r result.json -t forecast`（預設）或 `rebalance`，**此步為 workflow 强制條件**
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

## 已知問題 / Caveats（v1.2 良性，不會讓 analyze 失敗）

**待修補（流程完整性）**：

- **[ISSUE: workflow 缺少 HTML 報告]** 標準工作流程第 5 步「（選擇性）產報告」應改為**强制步驟**——`export -r result.json -t forecast` 必須執行並回報報告路徑，否則使用者看不到可攜帶的 HTML 輸出。2026-09-27 Kala 執行 0050+0056 回測時遺漏此步，後補 `export` 才生成 `/home/bt994846/.openclaw/agents/main/agent/workshop-skills/retrocast/data/reports/kadela_portfolio_forecast_20260927_115152.html`。

agent 看到這幾個**不要當真 bug 追**：

- **[fixed in v1.3.4 commit]** 原 `_build_monthly_tickers` 在某些呼叫鏈會丟 `WARNING:portfolio_forecast:_build_monthly_tickers 失敗:compute_monthly_returns_by_ticker() takes 1 positional argument but 3 were given`，導致 forecast 報告 §② 歷史真實績效明細表呈現空白。改走 `compute_monthly_returns_via_shares_tracking` (3-arg + window) 並回傳 unwrapped `tickers` list 後已修。若在舊版本上看到這個 warning 跟空白表，就是這條 bug。
- **`current_assets_NT$` ≠ stock_hold `total_assets`**：retrocast 用 FinMind 當下收盤算市值；stock_hold `current_price` 來自使用者最後一次 `POST /prices` 上傳，沒上傳過時 `current_price = avg_cost` 導致 `unrealized_pl=0`，市值看起來錯。relay summary 時並列兩個數字並註明落差來源，避免使用者誤判。
- **`upload-profile` SameFileError**：若 CSV 已放在 `user_profile/<name>.csv`，又對該路徑跑 `upload-profile` 會丟 `shutil.SameFileError`（non-fatal 但 traceback 會污染輸出）。workaround：先寫到 `/tmp/<name>.csv` 再 upload，或直接 `preview <name>` 驗證（檔案已在 `user_profile/` 就是合法 profile）。

## 不做的事

- 不發送任何對外訊息（email / 推文 / 券商下單）
- 不寫入投資建議或「建議買進 XXX」的對話內容
- 不 commit 任何變更（CSV、上傳檔）
- 不繞過 CLI 直接呼叫 `lib.finmind` / `app._run_analyze`（破壞 agent/script 邊界）

## 環境前置（v1.2）

- **Skill root**：`~/.openclaw/agents/main/agent/workshop-skills/retrocast/`（**唯一**依賴——所有檔案都在這裡）
- **不再需要** `/workspace/repos/RetroCast/`（dev copy 已於 2026-09-27 trash 移除）或 `/mnt/d/stock/RetroCast/`（frozen，保留為 Apache fallback）
- **FinMind token 三段式載入優先序**：
  1. **OpenClaw secrets `FINMIND_TOKEN`**（首選，audit + runtime 一致）
  2. `~/.config/retrocast/finmind-token`（legacy 本地橋接，chmod 600）
  3. env var `FINMIND_TOKEN`（CI / testing 友善）
- **Python 3.14.4 venv**：skill-internal `.venv/`；CLI 自動偵測並 re-exec
- **(歷史)** v1.1 時期曾以 byte-level diff 驗證 skill CLI 產出與 Flask 主線報告 0 byte 差異（僅「生成時間」timestamp 不同）；2026-09-27 起 dev copy 已移除，skill 獨立演進

## 首次安裝（agent 引導 SOP，v1.3.3）

**關鍵**：OpenClaw secrets `store get` 在 env-kind 模式下只回 redacted preview（`eyJ0eX…RlnQ`,11 chars + Unicode 省略號），**不是完整 JWT**。所以 skill runtime 實際依賴 `~/.config/retrocast/finmind-token`（本地檔寫入完整 token）。**首次安裝時這個檔必須建立**。

**Step 1 — Agent 引導使用者跑 `setup_token.py`**（一步到位）：

```bash
.venv/bin/python scripts/setup_token.py
```

script 內部流程：
1. 試從 `openclaw secrets store get FINMIND_TOKEN` 讀完整 token（長度 ≥ 50 且不含 `…` 才視為完整）
   - 讀到 → 直接寫入 `~/.config/retrocast/finmind-token` (chmod 600, umask 077)
   - 沒讀到（preview-only 或沒設定）→ 提示使用者手動貼 token（用 `getpass` 隱藏輸入，不進 shell history）
2. 寫到 `~/.config/retrocast/finmind-token`（含 `FINMIND_TOKEN=***` 格式，`chmod 600`，目錄 `~/.config/retrocast/` 不存在會自動建立）
3. 跑 `retrocast_cli.py health` 驗證 `finmind_token: true`

**Step 2 — 若使用者已透過其他方式有 token（legacy / 不想用 OpenClaw secrets）**：

直接寫到本地檔：
```bash
mkdir -p ~/.config/retrocast
umask 077
echo "your_jwt_here" > ~/.config/retrocast/finmind-token
chmod 600 ~/.config/retrocast/finmind-token
```

或 export `FINMIND_TOKEN` env var（CI / testing 友善，但**只用於測試**，正式使用仍建議本地檔）。

**Step 3 — 全新機器 / 重裝**：

```bash
# 1. clone skill (從 kalapontsai/RetroCast branch agent-skill)
git clone -b agent-skill https://github.com/kalapontsai/RetroCast.git ~/RetroCast-skill
cd ~/RetroCast-skill

# 2. 建立 venv + 裝套件
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. 跑 setup_token (這一步不可省略!)
.venv/bin/python scripts/setup_token.py

# 4. 驗證
.venv/bin/python scripts/retrocast_cli.py health
```

**為什麼必須跑 setup_token**：skill runtime 對 token 來源的優先序是
1. `~/.config/retrocast/finmind-token`（首選，本地檔含完整 JWT）
2. OpenClaw secrets `FINMIND_TOKEN`（audit/rotation source，但 env-kind 只能 preview）
3. env var `FINMIND_TOKEN`（CI / testing 友善）

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
