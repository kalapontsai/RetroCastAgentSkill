"""Daily prices — 從 FinMind 取每檔日 K,pivot 成 (date × symbol) DataFrame

B1.5 fix（股寶 19:30 reject B1 根因）:
原 `_get_profile_daily_returns` 從 `_run_analyze` 取 NAV 是月 K（515 點 / 12.6y）,
被當日 K 餵 F1/F2/F3/F6 engine,複利次數被膨脹 12 倍。

本模組從 FinMind `TaiwanStockPrice` 拿每檔日 K,pivot 加權,給 F1/F6 用真的日報酬。

設計原則:
- 不動 lib/portfolio.py（B 法會動到 v1 共用邏輯,風險隔離失敗）
- 不在 engine 層除以 period 數（C 法是 hack,藏錯）
- 加權方式 = 起始市值權重(與 〇、組合起始市值同源：每支股票「自己最後一個有效日」raw close × 股數)
- **Cache 由 lib.finmind 管理** (v3.0.2): `data/price_cache/{ticker}.json` 單檔 per ticker,
  delta-merge + covers check。本層不重複 cache（舊 parquet 層已刪,為 dead code）。

邊界:
- 至少要有 60 個交易日資料
- 所有 symbol 必須在同段日期內有資料(inner join drop NaN)
- 任一 symbol 拉不到 → raise,但其他 symbol 已 cache 的不影響
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from lib.finmind import FinMindClient, FinMindError


# ───────── Custom Errors ─────────
class DailyPricesError(ValueError):
    pass


# ───────── Constants ─────────
MIN_TRADING_DAYS = 60  # 至少 ~3 個月歷史


# ───────── Config / Result ─────────
@dataclass
class DailyPricesConfig:
    start_date: str = '2014-01-01'
    end_date: str | None = None  # None = today
    use_cache: bool = True
    cache_dir: Path | None = None

    def to_dict(self) -> dict:
        return {
            'start_date': self.start_date,
            'end_date': self.end_date,
            'use_cache': self.use_cache,
            'cache_dir': str(self.cache_dir) if self.cache_dir else None,
        }


# ───────── Public API ─────────
def daily_prices_by_stock(
    client: FinMindClient,
    symbols: list[str],
    config: DailyPricesConfig,
) -> pd.DataFrame:
    """從 FinMind 取多檔日 K,pivot 成 DataFrame[date × symbol]

    Args:
        client: FinMindClient instance(支援真實或 mock)
        symbols: list of ticker(e.g. ['2330', '2317', '2882'])
        config: DailyPricesConfig

    Returns:
        pd.DataFrame with DatetimeIndex, columns = symbols(close price)
        inner-joined(任何一日有任一檔缺資料就 drop)

    Raises:
        DailyPricesError: 拉資料失敗 / 結果 < MIN_TRADING_DAYS
    """
    if not symbols:
        raise DailyPricesError('symbols 不可為空')
    if not config.start_date:
        raise DailyPricesError('start_date 必填')

    frames: dict[str, pd.Series] = {}
    failures: list[str] = []

    for symbol in symbols:
        try:
            rows = client.get_stock_price(
                symbol,
                config.start_date,
                config.end_date,
                use_cache=config.use_cache,
            )
        except FinMindError as e:
            failures.append(f'{symbol}: {e}')
            continue

        if not rows:
            failures.append(f'{symbol}: 沒有資料')
            continue

        df = pd.DataFrame(rows)
        if 'close' not in df.columns or 'date' not in df.columns:
            failures.append(f'{symbol}: 缺欄位 close/date')
            continue

        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').drop_duplicates('date', keep='last')
        s = pd.Series(df['close'].to_numpy(), index=df['date'], name=symbol)
        frames[symbol] = s

    if not frames:
        raise DailyPricesError(
            f'所有 symbol 都拉失敗:{"; ".join(failures)}'
        )

    # 對齊:inner join(只留全部 symbol 都有的日期)
    aligned = pd.concat(frames.values(), axis=1, join='inner')
    aligned.columns = list(frames.keys())

    if len(aligned) < MIN_TRADING_DAYS:
        raise DailyPricesError(
            f'對齊後交易日 < {MIN_TRADING_DAYS}(got {len(aligned)}),'
            f'請確認 symbols / 日期範圍'
        )

    # 警告有 symbol 沒拉到的(但不 raise,讓 caller 決定)
    if failures:
        import warnings
        warnings.warn(f'部分 symbol 拉取失敗:{failures}', stacklevel=2)

    return aligned


def portfolio_daily_returns(
    prices: pd.DataFrame,
    holdings_weights: dict[str, float],
) -> pd.Series:
    """給 (date × symbol) 的 close prices + 權重,算出 portfolio 加權日報酬 Series

    Args:
        prices: DataFrame[date × symbol],close price
        holdings_weights: {symbol: weight},加總應為 1.0
                       (用起始市值權重,與 〇、組合起始市值同源 — 主人 2026-08-31 18:45 更正)

    Returns:
        pd.Series(index=date, name='portfolio_return'),daily returns(已是 pct_change 結果)
        第一筆為 NaN(被 drop)
    """
    missing = set(holdings_weights.keys()) - set(prices.columns)
    if missing:
        raise DailyPricesError(
            f'holdings_weights 有 symbol 不在 prices: {sorted(missing)}'
        )

    # 每檔日報酬
    stock_returns = prices.pct_change()

    # 加權平均(Buy & Hold 假設:權重固定)
    weights = pd.Series(holdings_weights)
    # 對齊(只取交集 symbols)
    common_symbols = list(set(weights.index) & set(stock_returns.columns))
    if not common_symbols:
        raise DailyPricesError(
            f'holdings_weights 跟 prices 沒有共同 symbol: '
            f'holdings={list(weights.index)}, prices={list(stock_returns.columns)}'
        )
    w = weights.reindex(common_symbols).fillna(0)
    if w.sum() <= 0:
        raise DailyPricesError(f'加權總和應 > 0, got {w.sum()}')

    # 加權日報酬 = sum(w_i * r_i),axis=1 對每個 date 加權
    # min_count=1 保留全 NaN 列(不變成 0),讓 dropna() 能正常刪除首列
    portfolio_ret = (stock_returns[common_symbols] * w).sum(axis=1, min_count=1)

    return portfolio_ret.dropna()


# ───────── Convenience wrapper for Flask ─────────
def run_daily_prices(
    client: FinMindClient,
    symbols: list[str],
    body: dict,
    cache_dir: Path | None = None,
) -> pd.DataFrame:
    """Flask-friendly wrapper"""
    config = DailyPricesConfig(
        start_date=body.get('start_date', '2014-01-01'),
        end_date=body.get('end_date'),
        use_cache=bool(body.get('use_cache', True)),
        cache_dir=cache_dir,
    )
    return daily_prices_by_stock(client, symbols, config)