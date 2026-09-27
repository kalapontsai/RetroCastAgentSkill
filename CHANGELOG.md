# Changelog

All notable changes to RetroCast will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **`PRICE_CACHE_TTL_SECONDS`**（`lib/finmind.py`）:30 天 → **90 天**。
  配合 `end_date` 預設取「前一月最後一天」，跨季微漂移時仍能命中快取，月 K 級別的歷史價 / 還原除權息價 / 股利 / first_trading_day 共用此 TTL。
- **`get_stock_list()` 預設 `ttl`**：`86400` (24h) → **`7 * 86400` (7d)**。
  上市 / 下市事件頻率低於年頻率，7 天足以涵蓋日常 analyze use case，避免每天重抓一次 stock list。
  兩條 cache 同時延長後，預期 analyze 在週/月級週期內零 FinMind 重抓，僅在跨月 / 跨季 / 新增標的時自然補抓。

## [1.2.0] - 2026-09-27

### Changed (Breaking)
- **`app.py` 拆解**：移除所有 Flask 相依（Flask imports / `create_app()` / `@app.route` decorators / `SafeJSONProvider` / `jsonify`），改為純商業邏輯入口 `analyze_engine.py`。CLI 與未來其他 client 直接 `from app import _run_analyze`，沒有 HTTP 框架耦合
- **Token 來源優先序改為三段式**：
  1. OpenClaw secrets `FINMIND_TOKEN`（**首選**，audit + runtime 一致）
  2. `~/.config/retrocast/finmind-token`（legacy 本地橋接）
  3. env var `FINMIND_TOKEN`（CI / testing 友善）
- **`./config/` 目錄移除**：skill bundle 不再保留 `config/finmind-api-key`；token 統一走 OpenClaw secrets
- **移除 `scripts/run_dev.sh` wrapper**：不再需要（沒了 `./config/`，wrapper 沒事做）
- **移除 `templates/index.html`**：Flask UI form 不再保留；改為 `form` subcommand 輸出 inquiry form JSON

### Added
- **Inquery form subcommand**：`retrocast_cli.py form [--output PATH]` 輸出含預設值的 JSON 範本；對話式（agent 逐欄問）與預填式（使用者編輯 form.json）兩種 workflow 產出相同 JSON
- **OpenClaw secrets 引導**：`load_finmind_token()` 透過 subprocess 讀 `openclaw secrets store get FINMIND_TOKEN`；in-memory cache 避免重複呼叫
- **新增標準 repo 檔案**：`README.md` / `LICENSE` (MIT) / `.gitignore` / `CHANGELOG.md`，可作為獨立 GitHub repo issue

### Removed
- Flask app routes（`/api/profiles` / `/api/profile/<n>` / `/api/analyze` / `/api/export` / `/api/v2/*` 等 13 個 endpoints）
- `SafeJSONProvider` 與 Flask `app.json` 設定（CLI 自己用 `json.dumps` + `default=str` 處理）
- `run_dev.sh` token bridge wrapper
- `./config/` skill-internal 配置目錄
- `templates/index.html` Flask UI 入口

### Fixed
- CLI stdout 不再被 `portfolio_forecast` DEBUG log 污染（`app.py` 改用 WARNING level + file handler only）

## [1.1.0] - 2026-09-27

### Added
- **Self-contained skill bundle**：skill 內含 `lib/`、`templates/`、`app.py`、`app_config.py`、`.venv/`、`data/price_cache/`，不再依賴 `/workspace/repos/RetroCast/`
- **Token bridge wrapper** (`scripts/run_dev.sh`)：從 `~/.config/retrocast/finmind-token` 寫到 skill `config/finmind-api-key`（umask 077 → 0600）
- **Byte-level 驗證**：skill-internal CLI 產出與 `/repos/RetroCast/` 產出 0 byte diff

## [1.0.0] - 2026-09-27

### Added
- **Initial agent skill release**：CLI 入口 `scripts/retrocast_cli.py`（6 subcommands：profiles / preview / upload-profile / health / analyze / export）
- **OpenClaw skill files**：`SKILL.md` + `references/input-schema.md` + `references/workflow.md` + `examples/kadela-forecast.md`
- **Agent 行為契約**：嚴禁算術、解讀、投資建議；純欄位收集 + 結果 relay
- **JSON in / JSON out / HTML out**：body shape 與 Flask API 1:1
- **Venv auto-bootstrap**：CLI 偵測 pandas 不可用時自動用 `.venv/bin/python` re-exec

[Unreleased]: https://github.com/<owner>/RetroCast/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/<owner>/RetroCast/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/<owner>/RetroCast/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/<owner>/RetroCast/releases/tag/v1.0.0
