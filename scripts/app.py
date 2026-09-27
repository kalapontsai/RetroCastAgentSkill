"""
analyze_engine.py — Flask-free analysis engine
- 商業邏輯入口：CLI 與未來其他 HTTP 框架都可呼叫 _run_analyze()
- 已被 scripts/retrocast_cli.py 與其他 client 直接 import
- 不依賴 Flask（v1.2 拆解）
"""
from __future__ import annotations

import logging
import math
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

# 確保根目錄在 sys.path（讓 from lib.xxx 有效）
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Suppress pandas RuntimeWarnings (e.g., "invalid value encountered in subtract")
# from nanops.py when computing std/var on data with NaN/inf values
warnings.filterwarnings('ignore', category=RuntimeWarning, module='pandas')

# ───────── Logging setup (file handlers only, no stdout pollution) ─────────
# v1.2 拆解：原本用 logging.basicConfig(level=DEBUG) 會把 DEBUG 噴到 stdout，
# CLI 會被污染。改為 WARNING 級別，並只用 file handler 收 DEBUG。
# 想看 DEBUG log 看 logs/debug.log。
LOG_FORMAT = '[%(asctime)s] %(levelname)s %(name)s: %(message)s'

from app_config import (  # noqa: E402
    DATA_DIR, DEFAULT_N_YEARS, DEFAULT_PV, DEFAULT_START_DATE,
    LOGS_DIR, MAX_CONTENT_LENGTH, REPORTS_DIR, ROOT_DIR, STATIC_DIR,
    TEMPLATES_DIR, USER_PROFILE_DIR,
)
from lib.csv_loader import (  # noqa: E402
    CSVLintError, list_profile_csvs, load_portfolio_csv, normalize_profile_csv,
)
from lib.exporter import render_html_report, render_rebalance_report  # noqa: E402
from lib.finmind import FinMindClient, FinMindError, load_finmind_token  # noqa: E402
from lib.forecast import ForecastError, build_forecast  # noqa: E402
from lib.model_validator import (  # noqa: E402
    ModelValidationError, raise_if_critical, validate_all,
)
from lib.portfolio import (  # noqa: E402
    BacktestError, build_adjusted_close, build_benchmark, build_portfolio,
    compute_market_value, per_stock_history, per_stock_n_year_window,
    prices_to_pivot, recent_n_year_metrics,
)
from lib.risk_metrics import RiskMetricsError, run_risk_metrics  # noqa: E402
from lib.volatility_decay import VolatilityDecayError, run_volatility_decay  # noqa: E402
from lib.benchmarks import BenchmarkError, run_benchmark_compare  # noqa: E402
from lib.monte_carlo import MonteCarloConfig, MonteCarloError, simulate_monte_carlo  # noqa: E402
from lib.sequence_risk import (  # noqa: E402
    SequenceRiskConfig, SequenceRiskError, simulate_sequence_risk,
)
from lib.monthly_returns import (  # noqa: E402
    compute_monthly_returns_by_ticker, compute_monthly_returns_via_shares_tracking,
)
from lib.daily_prices import (  # noqa: E402
    DailyPricesConfig, DailyPricesError, daily_prices_by_stock, portfolio_daily_returns,
)
from lib.portfolio_optimization import build_optimization  # noqa: E402

# Logging config (放在 imports 之後以免 logger 還沒 setup 就有 import 警告)
logging.basicConfig(
    level=logging.WARNING,
    format=LOG_FORMAT,
    datefmt='%Y-%m-%d %H:%M:%S',
)
root_logger = logging.getLogger()

LOGS_DIR.mkdir(parents=True, exist_ok=True)

# debug.log: 全部 log (DEBUG+),append
file_handler = logging.FileHandler(
    LOGS_DIR / 'debug.log',
    mode='a',
    encoding='utf-8',
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt='%Y-%m-%d %H:%M:%S'))
root_logger.addHandler(file_handler)

# app.log: 只收 ERROR+,主人快查「今天炸了什麼」用的
error_handler = logging.FileHandler(
    LOGS_DIR / 'app.log',
    mode='a',
    encoding='utf-8',
)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt='%Y-%m-%d %H:%M:%S'))
root_logger.addHandler(error_handler)

logger = logging.getLogger('portfolio_forecast')


# ───────── Date helpers ─────────
def default_end_date(today: date | None = None) -> str:
    """回測 end_date 預設值：前一個月的最後一天 (YYYY-MM-DD)。

    設計意圖 (v3.0.2 fix):
      - 歷史回測不需要「當下」的價格,反正每月才更新一次資料
      - 同一個月內多次執行 → end_date 固定 → cache key 穩定 → 0 抓取
      - 跨月第一次執行 → end_date 推進一格 → cache miss → 補抓一個月 → merge

    Args:
        today: 注入用的「今天」(預設 = date.today()),測試用可傳任意 date 物件。

    Edge case (1 月跨年): 回 去年 12-31
    """
    if today is None:
        today = date.today()
    last_of_prev = date(today.year, today.month, 1) - timedelta(days=1)
    return last_of_prev.strftime('%Y-%m-%d')


# ───────── Errors ─────────
class _BadInput(ValueError):
    """可攜帶結構化 payload (code / failed / changes 等),
    讓 caller detail panel 能讀細節,不只看 error 字串。"""
    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


# ───────── Helpers ─────────
def _check_import(module_path: str) -> bool:
    """檢查模組是否可以 import(v2 health check 用)"""
    try:
        __import__(module_path)
        return True
    except Exception:  # noqa: BLE001
        return False


def _adj_close_daily_returns(
    ticker: str,
    client: FinMindClient,
    start_date: str,
    end_date: str,
) -> tuple[pd.Series | None, bool]:
    """v3.0.4 P0 fix: 含息還原後的 daily returns(graceful fallback)。

    邏輯:
      1. 抓 raw close + dividends + splits(走 cache)
      2. 走 build_adjusted_close → 含息 adj close
      3. pct_change → daily returns
      4. 若 div/split cache 都空 → fallback 到 raw close(等同 v3.0.3 行為)

    Returns:
        (returns_series, fallback_to_raw)
        - returns_series: pd.Series 含 date index, name=ticker
        - fallback_to_raw: True 表示該 ticker 走 graceful fallback(沒事件)
    """
    try:
        rows = client.get_stock_price(ticker, start_date, end_date, use_cache=True)
    except Exception:
        return None, False
    if not rows:
        return None, False

    try:
        divs = client.get_dividends(ticker, start_date, end_date)
    except Exception:
        divs = []
    try:
        splits = client.get_splits(ticker, start_date, end_date)
    except Exception:
        splits = []

    has_events = bool(divs) or bool(splits)

    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
    pivot = pd.DataFrame({ticker: df['close']})
    adj = build_adjusted_close(
        pivot,
        dividends_by_ticker={ticker: divs} if divs else {},
        splits_by_ticker={ticker: splits} if splits else {},
    )
    rets = adj[ticker].pct_change().dropna()
    # 過濾 inf (close=0 → pct_change(±∞))
    rets = rets.replace([float('inf'), float('-inf')], float('nan')).dropna()

    if len(rets) > 0:
        rets.name = ticker
        return rets, not has_events
    return None, not has_events


def _fetch_daily_portfolio_returns(
    profile: str,
    client: FinMindClient | None = None,
) -> tuple[pd.Series, dict]:
    """從 holdings CSV 取 daily portfolio return(v1.2 改用 Flask 原版, byte-level 等價)。"""
    profile_path = USER_PROFILE_DIR / f'{profile}.csv'
    if not profile_path.is_file():
        raise _BadInput(f'{profile}.csv 不存在')

    # v3.0.3 normalize gate（冪等）
    try:
        norm_result = normalize_profile_csv(profile_path)
    except CSVLintError as ce:
        raise _BadInput(f'CSV 格式錯誤:{ce}')
    if norm_result.failed:
        raise _BadInput(
            f'profile {profile!r} 的 CSV 有 {len(norm_result.failed)} 個代號無法辨識',
            code='TICKER_NOT_FOUND',
            details={'failed': norm_result.failed, 'profile': profile},
        )

    holdings = load_portfolio_csv(profile_path)
    if not holdings:
        raise _BadInput('名單為空')

    if client is None:
        client = FinMindClient()

    today = default_end_date()

    # A 法（用 lib.daily_prices 拿 date × symbol close）
    try:
        prices_config = DailyPricesConfig(
            start_date=DEFAULT_START_DATE,
            end_date=today,
            use_cache=True,
        )
        close_df = daily_prices_by_stock(
            client,
            [h.ticker for h in holdings],
            prices_config,
        )
    except DailyPricesError as e:
        raise _BadInput(f'daily prices 取得失敗:{e}') from e

    if len(close_df) < 30:
        raise _BadInput(
            f'對齊後歷史太短({len(close_df)} 天),至少需 30 個交易日'
        )

    # weights = 第一個交易日 close × shares
    first_close = close_df.iloc[0]
    shares_series = pd.Series({h.ticker: h.shares for h in holdings})
    market_values = first_close * shares_series
    total_mv = float(market_values.sum())
    if total_mv <= 0:
        raise _BadInput(f'初始市值 <= 0:{market_values.to_dict()}')
    weights = market_values / total_mv

    portfolio_returns = portfolio_daily_returns(close_df, weights.to_dict())
    portfolio_returns.name = 'portfolio'

    # 逐 ticker 算 daily returns（給 card ⑥ 月報表用）
    rows_by_ticker: dict[str, list] = {}
    divs_by_ticker: dict[str, list] = {}
    splits_by_ticker: dict[str, list] = {}
    for h in holdings:
        try:
            rows = client.get_stock_price(h.ticker, DEFAULT_START_DATE, today, use_cache=True)
        except Exception:
            continue
        if not rows:
            continue
        rows_by_ticker[h.ticker] = rows
        try:
            divs_by_ticker[h.ticker] = client.get_dividends(h.ticker, DEFAULT_START_DATE, today)
        except Exception:
            divs_by_ticker[h.ticker] = []
        try:
            splits_by_ticker[h.ticker] = client.get_splits(h.ticker, DEFAULT_START_DATE, today)
        except Exception:
            splits_by_ticker[h.ticker] = []

    if rows_by_ticker:
        prices_pivot = prices_to_pivot(rows_by_ticker, price_col='close')
        monthly_out = compute_monthly_returns_via_shares_tracking(
            prices_pivot,
            dividends_by_ticker=divs_by_ticker,
            splits_by_ticker=splits_by_ticker,
        )

    # daily_returns_by_ticker 給 dashboard 用
    daily_returns_by_ticker: dict[str, list] = {}
    for h in holdings:
        rets, _fallback = _adj_close_daily_returns(
            h.ticker, client, DEFAULT_START_DATE, today,
        )
        if rets is None or len(rets) == 0:
            continue
        daily_returns_by_ticker[h.ticker] = [
            {'date': d.strftime('%Y-%m-%d'), 'ret': float(r)}
            for d, r in rets.items()
        ]

    meta = {
        'profile': profile,
        'holdings': len(holdings),
        'tickers': [h.ticker for h in holdings],
        'weights': {t: round(float(w), 6) for t, w in weights.items()},
        'start': str(portfolio_returns.index[0].date()),
        'end': str(portfolio_returns.index[-1].date()),
        'days': len(portfolio_returns),
        'first_market_values': {
            t: round(float(v), 0) for t, v in market_values.items()
        },
        'daily_returns_by_ticker': daily_returns_by_ticker,
    }
    return portfolio_returns, meta


def _get_profile_daily_returns(profile: str) -> tuple[pd.Series, dict]:
    client = FinMindClient()
    try:
        return _fetch_daily_portfolio_returns(profile, client=client)
    except FinMindError as e:
        raise _BadInput(f'FinMind 抓取失敗：{e}') from e


def _get_profile_nav(profile: str) -> tuple[pd.Series, dict]:
    client = FinMindClient()
    daily_returns, meta = _get_profile_daily_returns(profile)
    nav = (1 + daily_returns).cumprod()
    return nav, meta


def _parse_weights(raw, tickers: list[str]) -> dict[str, float] | None:
    """解析 weights 字串,例如 '2330:0.3,2317:0.7' → {'2330': 0.3, '2317': 0.7}"""
    if not raw:
        return None
    if not isinstance(raw, str):
        raise _BadInput('weights 必須是字串')
    out: dict[str, float] = {}
    for chunk in raw.split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ':' not in chunk:
            raise _BadInput(f'weights 格式錯誤 (應為 ticker:weight):{chunk!r}')
        tk, wt = chunk.split(':', 1)
        tk = tk.strip()
        try:
            wt_f = float(wt.strip())
        except ValueError:
            raise _BadInput(f'weights[{tk}] 不是數字:{wt!r}')
        if wt_f < 0 or wt_f > 1:
            raise _BadInput(f'weights[{tk}] 應在 [0,1]:{wt_f}')
        if tk not in tickers:
            raise _BadInput(f'weights 內的 ticker {tk} 不在 profile 內')
        out[tk] = wt_f
    s = sum(out.values())
    if s <= 0:
        return None
    return {k: v / s for k, v in out.items()}


def _run_analyze(body: dict) -> dict:
    """主分析流程（Flask-free，CLI 與未來其他 client 直接呼叫）：
    1) 讀名單 → FinMind TaiwanStockInfo 預先驗證 stock_id 存在 → 過濾假代號
    2) 抓 first_trading_day + 標記歷史太短的股票
    3) 抓 FinMind TaiwanStockPrice（只抓驗證過的）
    4) 三模式回測
    5) 計算起始市值（最後收盤價 × 股數）
    6) N-Year 預估
    7) 組裝回傳（含 bias 警告）

    Raises:
        _BadInput: 任何輸入欄位不合法（CLI 包成 INVALID_INPUT envelope）
        CSVLintError, BacktestError, ForecastError, FinMindError: 細部錯誤
    """
    # 1) 解析輸入
    profile = (body.get('profile') or '').strip()
    if not profile:
        raise _BadInput('profile 必填（從 /api/profiles 選一個）')
    if '/' in profile or '\\' in profile or '..' in profile:
        raise _BadInput('profile 名稱不合法')
    profile_path = USER_PROFILE_DIR / f'{profile}.csv'
    if not profile_path.is_file():
        raise _BadInput(f'{profile}.csv 不存在')

    # v3.0.3 normalize gate
    try:
        norm_result = normalize_profile_csv(profile_path)
    except CSVLintError as ce:
        raise _BadInput(f'CSV 格式錯誤:{ce}')
    if norm_result.failed:
        raise _BadInput(
            f'profile {profile!r} 的 CSV 有 {len(norm_result.failed)} 個代號無法辨識',
            code='TICKER_NOT_FOUND',
            details={'failed': norm_result.failed, 'profile': profile},
        )

    holdings = load_portfolio_csv(profile_path)
    user_tickers = [h.ticker for h in holdings]
    shares_map = {h.ticker: h.shares for h in holdings}

    n = int(body.get('n', DEFAULT_N_YEARS))
    if n < 1 or n > 50:
        raise _BadInput('n 必須在 1~50 之間')
    user_pv = body.get('pv')  # None = 自動用實際市值
    start_date = (body.get('start_date') or DEFAULT_START_DATE).strip()
    end_date = (body.get('end_date') or default_end_date()).strip()
    weights = _parse_weights(body.get('weights'), user_tickers)

    # 1.5) 交易成本（選填，預設 0 = 不計）
    fee_buy = float(body.get('fee_buy', 0) or 0)
    fee_sell = float(body.get('fee_sell', 0) or 0)
    tax_sell = float(body.get('tax_sell', 0) or 0)
    slippage = float(body.get('slippage', 0) or 0)
    if any(x < 0 or x > 0.1 for x in (fee_buy, fee_sell, tax_sell, slippage)):
        raise _BadInput('fee/tax/slippage 應在 0~0.1（10%）之間')

    # 1.6) Benchmark（選填）
    benchmark_id = (body.get('benchmark') or '').strip() or None

    # 2) 預先驗證
    client = FinMindClient()
    try:
        stock_list = client.get_stock_list()
    except FinMindError as e:
        raise _BadInput(f'FinMind TaiwanStockInfo 抓取失敗：{e}') from e

    matched: dict[str, dict] = {}
    invalid_tickers: list[dict] = []
    for ut in user_tickers:
        m = client.match_ticker(ut)
        if m is None:
            invalid_tickers.append({
                'user_input': ut,
                'reason': f'在 TaiwanStockInfo 清單中查無此代號（可能是 typo 或已下市）',
            })
            continue
        sid = m['stock_id']
        if sid in matched:
            matched[sid]['matched_from'].append(ut)
            continue
        matched[sid] = {
            'stock_id': sid,
            'stock_name': m.get('stock_name', ''),
            'industry_category': m.get('industry_category', ''),
            'type': m.get('type', ''),
            'source': m.get('source', ''),
            'matched_from': [ut],
        }

    if not matched:
        raise _BadInput(
            '名單中所有 ticker 都不在 FinMind TaiwanStockInfo 清單內。'
            '請檢查代號是否正確（例如 50 → 0050、6208 → 006208）。'
        )

    # 3) 抓 first_trading_day + 標記歷史太短
    valid_stock_ids = list(matched.keys())
    first_trading_days: dict[str, str | None] = {}
    short_history: list[str] = []
    today_ts = pd.Timestamp(end_date)
    n_years_ago = today_ts - pd.DateOffset(years=n)

    for sid in valid_stock_ids:
        try:
            ftd = client.get_first_trading_day(sid)
        except FinMindError:
            ftd = None
        first_trading_days[sid] = ftd
        if ftd is None:
            invalid_tickers.append({
                'user_input': matched[sid]['matched_from'][0],
                'stock_id': sid,
                'reason': f'{sid}（{matched[sid].get("stock_name", "")}）查無任何歷史股價資料',
            })
            del matched[sid]
        else:
            ftd_ts = pd.Timestamp(ftd)
            if ftd_ts > n_years_ago:
                short_history.append(sid)

    if not matched:
        raise _BadInput('過濾掉無歷史資料的 ticker 後，沒有任何可用股票。請檢查名單。')

    # 4) 抓歷史價格（只抓驗證過 + 有 first_trading_day 的）
    final_stock_ids = list(matched.keys())
    rows_by_ticker: dict[str, list[dict]] = {}
    fetch_errors: dict[str, str] = {}
    for sid in final_stock_ids:
        try:
            ftd = first_trading_days.get(sid, start_date)
            actual_start = max(ftd, start_date) if ftd else start_date
            rows_by_ticker[sid] = client.get_stock_price(sid, actual_start, end_date)
        except FinMindError as e:
            fetch_errors[sid] = str(e)
    for sid in list(rows_by_ticker.keys()):
        if not rows_by_ticker[sid]:
            del rows_by_ticker[sid]

    if not rows_by_ticker:
        raise FinMindError(f'驗證後的股票都抓不到歷史價格：{fetch_errors}')

    # 5) 轉 pivot (raw close) → 扣除 sentinel
    prices = prices_to_pivot(rows_by_ticker, price_col='close')
    if prices.empty:
        raise BacktestError('抓回的價格資料為空')

    # 5.5) 抓股息 → 產生「還原除權息後股價」（含息再投入）
    dividends_by_ticker: dict[str, list[dict]] = {}
    splits_by_ticker: dict[str, list[dict]] = {}
    div_fetch_errors: dict[str, str] = {}
    for sid in final_stock_ids:
        if sid not in rows_by_ticker:
            continue
        try:
            raw_rows = rows_by_ticker[sid]
            if not raw_rows:
                continue
            actual_start = min(r['date'] for r in raw_rows)
            divs = client.get_dividends(sid, actual_start, end_date)
            splits = client.get_splits(sid, actual_start, end_date)
            dividends_by_ticker[sid] = divs
            splits_by_ticker[sid] = splits
        except Exception as e:
            div_fetch_errors[sid] = str(e)
            dividends_by_ticker[sid] = []
            splits_by_ticker[sid] = []

    prices_adj = build_adjusted_close(prices, dividends_by_ticker, splits_by_ticker)

    # 6) 起始市值
    combined_shares: dict[str, int] = {}
    for sid, info in matched.items():
        for ut in info['matched_from']:
            combined_shares[sid] = combined_shares.get(sid, 0) + shares_map[ut]

    mv = compute_market_value(prices, combined_shares)
    if user_pv is None:
        raw_pv = mv['total']
        pv_source = 'market_value'
    else:
        raw_pv = float(user_pv)
        pv_source = 'user_input'
        if raw_pv < 0:
            raise _BadInput('pv 不可為負')

    initial_cost_rate = fee_buy + slippage
    if initial_cost_rate > 0 and pv_source == 'market_value':
        forecast_pv = raw_pv / (1 + initial_cost_rate)
        cost_text = f'（預估終值已扣買入手續費 {fee_buy*100:.3f}% + 滑價 {slippage*100:.3f}%）'
    else:
        forecast_pv = raw_pv
        cost_text = ''
    pv = raw_pv
    forecast_pv_value = forecast_pv
    pv_raw = raw_pv
    pv_cost_text = cost_text

    # 7) 三模式
    mv_total = float(mv.get('total', 0))
    mv_weights: dict[str, float] | None = None
    if mv_total > 0 and mv.get('per_stock'):
        mv_weights = {
            item['ticker']: float(item['value'] / mv_total)
            for item in mv['per_stock']
        }
    effective_weights = weights if weights else mv_weights
    if weights:
        weights_source = 'user'
    elif mv_weights is not None:
        weights_source = 'market_cap'
    elif effective_weights is None:
        weights_source = 'equal'
    else:
        weights_source = 'unknown'
    common_res = build_portfolio(prices_adj, mode='common', weights=effective_weights)
    dynamic_res = build_portfolio(prices_adj, mode='dynamic', weights=effective_weights)
    full_res = build_portfolio(prices_adj, mode='full', weights=effective_weights)

    # 7.5) Benchmark
    benchmark = None
    if benchmark_id:
        try:
            bench_rows = client.get_stock_price(benchmark_id, start_date, end_date)
            bench_prices = prices_to_pivot({benchmark_id: bench_rows}, price_col='close')
            if not bench_prices.empty:
                b_start = min(r['date'] for r in bench_rows)
                bench_div = client.get_dividends(benchmark_id, b_start, end_date)
                bench_split = client.get_splits(benchmark_id, b_start, end_date)
                bench_prices = build_adjusted_close(
                    bench_prices,
                    {benchmark_id: bench_div},
                    {benchmark_id: bench_split},
                )
                bench_prices = bench_prices.loc[:dynamic_res.nav.index[-1]] if not dynamic_res.nav.empty else bench_prices
                benchmark = build_benchmark(bench_prices, ticker=benchmark_id)
        except FinMindError as e:
            benchmark = {'ticker': benchmark_id, 'error': str(e)}

    # 8) N-Year 預估
    forecast_basis = 'common'
    forecast = None
    for basis, res in (('common', common_res), ('dynamic', dynamic_res), ('full', full_res)):
        try:
            forecast = build_forecast(res.nav, n=n, pv=forecast_pv_value)
            forecast_basis = basis
            break
        except ForecastError:
            continue
    if forecast is None:
        raise ForecastError(
            f'三個模式的歷史長度都無法建立 N={n} 年 rolling outcome。'
            f'請縮短 N 年數，或加入上市更久的股票。'
        )
    forecast['basis'] = forecast_basis

    # 9) 個股歷史長度
    psh = per_stock_history(prices_adj)

    # 10) 組裝回傳
    retirement_config = {
        'initial_balance': float(pv_raw) if pv_raw else 7_236_096,
        'horizon_years': int(body.get('v2_horizon_years') or (int(body.get('v2_retirement_end_age', 85)) - int(body.get('v2_current_age', 55)))),
        'n_simulations': int(body.get('v2_n_simulations', 1000)),
        'retirement_age': int(body.get('v2_retirement_age', 60)),
        'current_age': int(body.get('v2_current_age', body.get('current_age', 55))),
        'retirement_end_age': int(body.get('v2_retirement_end_age', 85)),
        'withdrawal_monthly': float(body.get('v2_withdrawal_monthly', 30_000)),
        'withdrawal_inflation': float(body.get('v2_withdrawal_inflation', 0.03)),
        'pension_monthly': float(body.get('v2_pension_monthly', 0.0)),
        'pension_inflation': float(body.get('v2_pension_inflation', 0.02)),
        'pension_start_age': int(body.get('v2_retirement_age', 60)),
        'special_expenses': body.get('v2_special_expenses') or [],
        'seed': 42,
    }
    optimization = build_optimization(
        prices_adj,
        current_weights=effective_weights or {},
        current_values={x['ticker']: x['value'] for x in mv.get('per_stock', [])},
        current_prices={x['ticker']: x['close'] for x in mv.get('per_stock', [])},
        shares=combined_shares,
        n_years=n,
        fees={'commission_buy': fee_buy, 'commission_sell': fee_sell,
              'slippage': slippage, 'tax_sell': tax_sell},
        risk_free_rate=float(body.get('v2_risk_free_rate', body.get('risk_free_rate', 0.015))),
        retirement_config=retirement_config,
    )
    if psh:
        all_starts = [info['start'] for info in psh.values() if info.get('start')]
        all_ends = [info['end'] for info in psh.values() if info.get('end')]
        all_rows = [info['rows'] for info in psh.values() if info.get('rows')]
        all_first_close = [info['first_close'] for info in psh.values() if info.get('first_close') is not None]
        all_last_close = [info['last_close'] for info in psh.values() if info.get('last_close') is not None]
        overview = {
            'start': min(all_starts) if all_starts else None,
            'end': max(all_ends) if all_ends else None,
            'rows': sum(all_rows) if all_rows else 0,
            'first_close': round(sum(all_first_close) / len(all_first_close), 2) if all_first_close else None,
            'last_close': round(sum(all_last_close) / len(all_last_close), 2) if all_last_close else None,
            'stocks': len(matched),
            'min_years': min((v['years'] for v in psh.values()), default=0),
            'median_years': sorted([v['years'] for v in psh.values()])[len(psh) // 2] if psh else 0,
            'max_years': max((v['years'] for v in psh.values()), default=0),
        }
    else:
        overview = {
            'start': None, 'end': None, 'rows': 0,
            'first_close': None, 'last_close': None,
            'stocks': 0, 'min_years': 0, 'median_years': 0, 'max_years': 0,
        }

    # Phase 6: v2 extensions
    _v2 = _compute_v2_extensions(body, client, holdings, pv_raw, n)
    result = {
        'inputs': {
            'profile': profile,
            'user_tickers': user_tickers,
            'tickers': final_stock_ids,
            'shares': shares_map,
            'combined_shares': combined_shares,
            'n': n,
            'pv': pv,
            'pv_raw': pv_raw,
            'pv_source': pv_source,
            'pv_cost_text': pv_cost_text,
            'fees': {
                'fee_buy': fee_buy,
                'fee_sell': fee_sell,
                'tax_sell': tax_sell,
                'slippage': slippage,
            },
            'start_date': start_date,
            'end_date': end_date,
            'weights': weights,
            'invalid_tickers': invalid_tickers,
            'first_trading_days': first_trading_days,
            'short_history': short_history,
            'fetch_errors': fetch_errors,
            'ticker_match': {sid: {
                'stock_id': sid,
                'stock_name': info['stock_name'],
                'industry': info['industry_category'],
                'type': info['type'],
                'source': info['source'],
                'matched_from': info['matched_from'],
            } for sid, info in matched.items()},
        },
        'effective_weights': effective_weights,
        'weights_source': weights_source,
        'market_value': mv,
        'benchmark': benchmark,
        'common': _serialize_result(common_res, recent_n_years=n),
        'dynamic': _serialize_result(dynamic_res, recent_n_years=n),
        'full': _serialize_result(full_res, recent_n_years=n),
        'forecast': forecast,
        'history': {
            'overview': overview,
            'per_stock': psh,
            'per_stock_n_year': per_stock_n_year_window(prices, n, dividends_by_ticker, splits_by_ticker),
        },
        'optimization': optimization,
        'nav_series': {
            'common': _serialize_result(common_res)['nav'],
            'dynamic': _serialize_result(dynamic_res)['nav'],
            'full': _serialize_result(full_res)['nav'],
        },
        'monte_carlo': _v2.get('monte_carlo'),
        'sequence_risk': _v2.get('sequence_risk'),
        'risk_metrics': _v2.get('risk_metrics'),
        'retirement_inputs': _v2.get('retirement_inputs'),
        'monthly_tickers': _build_monthly_tickers(holdings, client, start_date, end_date, n, dividends_by_ticker, splits_by_ticker),
        '_build_analyze_meta': _build_analyze_meta(client, final_stock_ids, start_date, end_date),
        'validation': _validate_wrapper(validation_pipeline(
            profile=profile, body=body, result=None  # 略，CLI 與 web 不依賴 strict validation
        )),
        'meta': {
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'end_date': end_date,
            'start_date': start_date,
            'profile': profile,
            'n_years': n,
        },
    }

    # Final validation pass (after result assembly)
    try:
        validation_report = validate_all(result)
        # v1.2: 轉成 dict 讓 Jinja2 訪問方便（dataclass 在 sandboxed env 可能 attribute access 受限）
        result['validation'] = validation_report.to_dict() if hasattr(validation_report, 'to_dict') else validation_report
    except ModelValidationError as e:
        logger.warning(f'_run_analyze validation 失敗:{e}')

    return result


def _validate_wrapper(_unused):
    """預留 hook：CLI 與 web 各自決定要不要做 strict validation。
    v1.2 拆解後,web 不存在,留 API 給 CLI/health 檢查用。"""
    return {'skipped': True, 'reason': 'analyze_engine 不做 strict validation,僅 log warning'}


def _build_monthly_tickers(holdings, client, start_date, end_date, n_years, dividends_by_ticker, splits_by_ticker):
    """每月逐年報酬表（給 card ⑥ 用）

    v3.0.4 P0 fix: 走 compute_monthly_returns_via_shares_tracking（fresh-start-per-month），
    與「一.6 per_stock_n_year_window」同源算法，確保「N 年月報酬連乘」 == 「N 年 total return」。
    修正先前呼叫 1-arg 版 compute_monthly_returns_by_ticker 但傳 3 個 args 導致 silent 失敗的 bug。
    """
    try:
        rows_by_ticker: dict[str, list[dict]] = {}
        for h in holdings:
            sid = h.ticker
            try:
                rows = client.get_stock_price(sid, start_date, end_date)
            except FinMindError:
                rows = []
            if rows:
                rows_by_ticker[sid] = rows
        if not rows_by_ticker:
            logger.warning('_build_monthly_tickers: 全部 holdings 都抓不到 price rows')
            return {}

        # 整理成 raw_pivot: DataFrame index=Date, columns=Ticker, values=raw close
        per_ticker_df = []
        for ticker, rows in rows_by_ticker.items():
            df = pd.DataFrame(rows)
            if df.empty or 'date' not in df.columns or 'close' not in df.columns:
                continue
            df['date'] = pd.to_datetime(df['date'])
            per_ticker_df.append(
                df.set_index('date').sort_index()[['close']].rename(columns={'close': ticker})
            )
        if not per_ticker_df:
            logger.warning('_build_monthly_tickers: 整理後 pivot 為空（price row 缺少 date/close 欄）')
            return {}
        raw_pivot = pd.concat(per_ticker_df, axis=1).sort_index()

        # N 年窗口：與 exporter fallback 對齊，以 max_date 往回推 N 年
        window_end_ts = raw_pivot.index.max()
        window_end = window_end_ts.strftime('%Y-%m-%d')
        window_start = None
        if n_years and n_years > 0:
            window_start = (window_end_ts - pd.DateOffset(years=n_years)).strftime('%Y-%m-%d')

        result = compute_monthly_returns_via_shares_tracking(
            raw_pivot,
            dividends_by_ticker or {},
            splits_by_ticker or {},
            window_start=window_start,
            window_end=window_end,
        )
        # 回傳 unwrapped tickers list（lib.exporter._get_monthly_tickers 預期 list[dict]，
        # 而不是 wrapper {'tickers': [...]}；wrapper 形式會讓 exporter 把 dict 的 keys 當
        # tickers 迭代、tk.get('data') 直接炸）
        return result.get('tickers', [])
    except Exception as e:
        logger.warning(f'_build_monthly_tickers 失敗:{e}')
        return {}


def _build_analyze_meta(client, tickers, start_date, end_date):
    return {
        'tickers': tickers,
        'start_date': start_date,
        'end_date': end_date,
        'client_class': type(client).__name__,
    }


def validation_pipeline(profile: str, body: dict, result):
    """CLI 與 web 共用的 validation pipeline。
    預設 no-op（CLI 不依賴 strict gate）。
    """
    return {}


def _compute_v2_extensions(
    body: dict,
    client: FinMindClient,
    holdings,
    pv_raw: float,
    n_years: int = 5,
) -> dict:
    """B4 整合：拿 FinMind daily 股價 + weights → 跑 F1 Monte Carlo + F2 Sequence Risk + F3/F6 risk metrics"""
    if body.get('enable_v2') is False:
        return {'monte_carlo': None, 'sequence_risk': None, 'risk_metrics': None}

    n_sims = int(body.get('v2_n_simulations', 1000))
    initial = float(pv_raw) if pv_raw else 7_236_096
    current_age = int(body.get('v2_current_age', body.get('current_age', 55)))
    retirement_age = int(body.get('v2_retirement_age', body.get('retirement_age', 60)))
    retirement_end_age = int(body.get('v2_retirement_end_age', 85))
    horizon = int(body.get('v2_horizon_years') or (retirement_end_age - current_age))
    forecast_horizon = int(body.get('v2_forecast_horizon', body.get('n', n_years)))

    out: dict = {'monte_carlo': None, 'sequence_risk': None, 'risk_metrics': None}

    try:
        daily_returns, _meta = _fetch_daily_portfolio_returns(
            body.get('profile', 'kadela_stock'),
            client=client,
        )
    except _BadInput as e:
        logger.warning(
            f'_compute_v2_extensions skip:{e}'
        )
        skip_meta = {'skip_reason': f'daily_returns 取不到:{e}'}
        if e.code:
            skip_meta['code'] = e.code
            skip_meta['failed'] = e.details.get('failed', [])
        out['_meta'] = skip_meta
        return out

    # F1 Monte Carlo
    try:
        mc_cfg = MonteCarloConfig(
            initial_balance=initial,
            horizon_years=n_years,
            n_simulations=n_sims,
            annual_withdrawal=0.0,
            seed=42,
        )
        mc_result = simulate_monte_carlo(daily_returns, mc_cfg)
        out['monte_carlo'] = mc_result.to_dict()
    except (MonteCarloError, ValueError, ArithmeticError) as e:
        logger.warning(f'_compute_v2_extensions F1 失敗:{e}')

    # F2 Sequence Risk
    try:
        sr_cfg = SequenceRiskConfig(
            initial_balance=initial,
            horizon_years=horizon,
            n_simulations=n_sims,
            retirement_age=retirement_age,
            current_age=current_age,
            retirement_end_age=retirement_end_age,
            withdrawal_monthly=float(body.get('v2_withdrawal_monthly', 30_000)),
            withdrawal_inflation=float(body.get('v2_withdrawal_inflation', 0.03)),
            pension_monthly=float(body.get('v2_pension_monthly', 0.0)),
            pension_inflation=float(body.get('v2_pension_inflation', 0.02)),
            pension_start_age=retirement_age,
            special_expenses=body.get('v2_special_expenses') or [],
            seed=42,
        )
        sr_result = simulate_sequence_risk(daily_returns, sr_cfg)
        out['sequence_risk'] = sr_result.to_dict()
    except (SequenceRiskError, ValueError, ArithmeticError) as e:
        logger.warning(f'_compute_v2_extensions F2 失敗:{e}')

    # F3 + F6 risk metrics
    try:
        out['risk_metrics'] = run_risk_metrics(
            daily_returns,
            {
                'confidence_levels': [0.95, 0.99],
                'horizon_days': [1, 21, 252],
                'risk_free_rate': float(body.get('v2_risk_free_rate', 0.015)),
                'risk_free_source': body.get('v2_risk_free_source', 'tw_10y_bond'),
            },
        )
    except (RiskMetricsError, ValueError) as e:
        logger.warning(f'_compute_v2_extensions F3+F6 失敗:{e}')

    out['retirement_inputs'] = {
        'current_age': current_age,
        'retirement_age': retirement_age,
        'retirement_end_age': retirement_end_age,
        'forecast_horizon': forecast_horizon,
        'retirement_horizon': retirement_end_age - current_age,
        'forecast_end_age': current_age + forecast_horizon,
    }

    return out


def _serialize_result(r, recent_n_years: int | None = None) -> dict:
    """把 PortfolioResult 轉 JSON-safe dict（v1.2 用 Flask 原版,簡單結構）。

    Args:
        r: PortfolioResult
        recent_n_years: 若指定, 計算「最近 N 年真實績效」(rebase 到 1.0 起點)
                       None = 不算 (向後相容)
    """
    return {
        'mode': r.mode,
        'metrics': r.metrics,
        'recent_metrics': (
            recent_n_year_metrics(r.nav, recent_n_years)
            if recent_n_years is not None else None
        ),
        'nav': _downsample_nav(r.nav),
        'pct_active': (
            [{'date': str(d.date()), 'n': int(v)} for d, v in r.pct_active.items()]
            if r.pct_active is not None else None
        ),
    }


def _downsample_nav(nav, max_points: int = 500) -> list[dict]:
    """NAV 太長時下採樣到 ~500 點（避免 JSON 太大）

    v1.2 改: 直接吃 Series,回傳 list of dicts (與 Flask 一致)。
    """
    if nav is None or (hasattr(nav, 'empty') and nav.empty):
        return []
    if len(nav) <= max_points:
        return [{'date': str(d.date()), 'nav': float(v)} for d, v in nav.items()]
    step = max(1, len(nav) // max_points)
    sampled = nav.iloc[::step]
    if sampled.index[-1] != nav.index[-1]:
        # 確保最後一點是真正的 end-of-data
        sampled = pd_concat_safe(sampled, nav.iloc[[-1]])
    return [{'date': str(d.date()), 'nav': float(v)} for d, v in sampled.items()]


def pd_concat_safe(*series) -> 'pd.Series':
    """小工具：concat 多個 Series 並去重（by index）"""
    import pandas as _pd
    s = _pd.concat(list(series))
    s = s[~s.index.duplicated(keep='last')].sort_index()
    return s


# ───────── CLI helper: clean NaN/inf → JSON-safe ─────────
def _scrub_nan(obj):
    """遞歸把 float NaN / ±inf 換成 None;list / dict 走訪子節點。
    Flask SafeJSONProvider 原本做的事,Flask 拆解後 CLI 自己處理。"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _scrub_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        cleaned = [_scrub_nan(v) for v in obj]
        return type(obj)(cleaned) if isinstance(obj, tuple) else cleaned
    return obj


def clean_result_for_json(result: dict) -> dict:
    """CLI 用：把 _run_analyze 回傳的 dict 清成 JSON-safe（NaN→None）。
    等同 Flask SafeJSONProvider 的 dumps() 行為。"""
    return _scrub_nan(result)
