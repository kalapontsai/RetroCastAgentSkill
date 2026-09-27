# RetroCast

> **對話式驅動的台股投資組合歷史回測與退休存活率分析工具**
>
> Flask-free、self-contained、純 CLI + Jinja2 模板；所有計算透過 FinMind v4 API 取得台股歷史價格。

[![Python](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/) [![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## 為什麼 RetroCast

- **歷史回測**：從 FinMind TaiwanStockInfo 預驗證 ticker → 抓 TaiwanStockPrice + 股息 + 分割 → 建含息還原收盤價 → 三模式回測（common / dynamic / full）
- **未來 N 年推估**：基於歷史滾動 N 年收益分布分位數
- **退休存活率**：F2 Sequence Risk 模型，提款 + 年金 + 一次性支出 + 蒙地卡羅模擬 1000 次
- **風險指標**：VaR / CVaR（多 horizon × 多 confidence）、Sharpe（含台灣 10Y 公債預設 1.5%）
- **再平衡最佳化**：Evidence-aware（短歷史資產仍保留）+ 權重上下限可行性檢查 + Lambda/Gamma 敏感度

## 對話式介面（v1.2 設計）

不走 Flask 網頁 UI。Agent skill 透過 CLI 串接：

```
User ──對話── Agent ──subprocess── retrocast_cli.py ──→ HTML 報告
                              ├─ form      預填 inquiry form JSON
                              ├─ analyze   主分析 → result JSON
                              └─ export    result → HTML 報告
```

- **對話式填欄位**：agent 透過對話問每個欄位
- **預填式**：agent 給 JSON form,使用者編輯後直接 submit

## 安裝

```bash
git clone <this-repo>
cd RetroCast
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 第一次使用：設定 FinMind Token

Token 透過 **OpenClaw secrets** 管理（v1.2 設計）：

```bash
openclaw secrets store set FINMIND_TOKEN --kind env
# CLI 會要你貼 token（masked prompt，不會留在 shell history）
```

驗證 token 讀取：

```bash
.venv/bin/python scripts/retrocast_cli.py health
# 預期 finmind_token: true
```

若不想用 OpenClaw secrets，仍可走 legacy 來源：
- `~/.config/retrocast/finmind-token`（chmod 600,純 token 字串）
- `FINMIND_TOKEN` env var

## 快速開始

```bash
# 1. 健康檢查
.venv/bin/python scripts/retrocast_cli.py health --v2

# 2. 列出可用 profile
.venv/bin/python scripts/retrocast_cli.py profiles

# 3. 取得 inquiry form（兩種使用方式：對話式 or 預填式）
.venv/bin/python scripts/retrocast_cli.py form > form.json
# 編輯 form.json（填 profile / n / 退休參數）
.venv/bin/python scripts/retrocast_cli.py analyze -i form.json -o result.json

# 4. 產 HTML 報告
.venv/bin/python scripts/retrocast_cli.py export -r result.json -t forecast
.venv/bin/python scripts/retrocast_cli.py export -r result.json -t rebalance
```

產出位置：`data/reports/<profile>_portfolio_<type>_<timestamp>.html`

## CLI Subcommands

| Subcommand | 用途 |
|---|---|
| `profiles` | 列出 `user_profile/` 內所有 CSV |
| `preview <name>` | 預覽某 profile 的持倉 |
| `upload-profile <csv-path>` | 上傳 CSV 到 `user_profile/` |
| `health [--v2]` | 健康檢查（v2 加檢查 F1-F6 依賴） |
| `form [--output PATH]` | 輸出 inquiry form JSON 範本（含預設值） |
| `analyze -i <body.json> [-o <result.json>]` | 主分析 → JSON to stdout |
| `export -r <result.json> -t forecast\|rebalance` | 產 HTML 報告 |

完整 JSON body schema：`references/input-schema.md`

## 目錄結構

```
RetroCast/
├── README.md                 # 本檔
├── LICENSE                   # MIT
├── CHANGELOG.md              # 版本歷史
├── SKILL.md                  # OpenClaw agent skill 指令
├── .gitignore                # 排除 runtime artifacts
├── requirements.txt          # Python 套件清單
├── app.py                    # analyze_engine（Flask-free, v1.2 拆解後）
├── app_config.py             # 路徑常數
├── scripts/
│   └── retrocast_cli.py      # CLI 入口（6 subcommands）
├── lib/                      # 17 個 .py：核心商業邏輯
├── templates/                # Jinja2 HTML（report.html / rebalance_report.html）
├── references/
│   ├── input-schema.md       # 17 欄完整 schema
│   └── workflow.md           # 對話流程範本
├── examples/
│   └── kadela-forecast.md    # 完整執行紀錄 + 對附件差異分析
├── user_profile/             # CSV 持倉（sample_stock.csv 公開,其餘 gitignore）
├── data/
│   ├── price_cache/          # FinMind JSON 快取（首次執行後累積,gitignore）
│   └── reports/              # HTML 報告輸出（gitignore）
└── logs/                     # debug.log / app.log（gitignore）
```

## 設計哲學

- **計算與判斷分離**：所有計算（蒙地卡羅、回測、最佳化）在 `lib/`，CLI 與 agent 只做欄位收集與結果 relay
- **JSON in / JSON out**：body shape 與 Flask API 1:1，未來切換 HTTP 框架無痛
- **byte-level 等價**：CLI 產出與 Flask 報告**完全相同**（除生成時間戳）
- **不對外通訊**：不送 email、不推文、不下單——純分析工具

## License

MIT License — see [LICENSE](LICENSE)
