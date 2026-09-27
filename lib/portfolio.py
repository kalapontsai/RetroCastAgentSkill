"""
Portfolio Backtest
- 3 種歷史模式：Common / Dynamic / Full
- 共用：Portfolio NAV + 指標（CAGR / MDD / Vol / Sharpe）
- 個股歷史長度摘要
"""
from __future__ import annotations

import warnings
import logging
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

logger = logging.getLogger('portfolio_forecast.portfolio')


Mode = Literal['common', 'dynamic', 'full']


@dataclass
class PortfolioResult:
    mode: str
    nav: pd.Series                # NAV index = Date
    daily_return: pd.Series       # daily portfolio return
    metrics: dict
    history_diag: dict            # 個股歷史長度
    pct_active: pd.Series | None = None  # 每日 active 股票數（dynamic 才有）

    def to_dict(self) -> dict:
        return {
            'mode': self.mode,
            'metrics': self.metrics,
            'history_diag': self.history_diag,
            'pct_active': self.pct_active.to_dict() if self.pct_active is not None else None,
        }


# ───────── Custom Errors ─────────
class BacktestError(ValueError):
    pass


# ───────── Entry point ─────────
def build_portfolio(
    prices: pd.DataFrame,
    mode: Mode,
    weights: dict[str, float] | None = None,
    shares: dict[str, int] | None = None,
) -> PortfolioResult:
    """
    prices: pivot table，index=Date, columns=Ticker, values=Close
            必須包含所有用戶持有的 ticker，缺值表示該日該股尚未上市或缺資料
    weights: {ticker: weight}，sum 不一定要 1，會自動 normalize
             None = 用 shares 算「起始市值權重」(與 〇、組合起始市值同源：每支股票「自己最後一個有效日」close × 股數)
             shares 也 None 時 fallback = 等權重（向後相容用 / 單元測試用）
    shares:  {ticker: 股數}；weights=None 時用來算起始市值權重

    注: 主人 2026-08-31 18:45 更正：weight = 最後一個有效日，與 組合起始市值同源。
    原本的 first_close × shares（buy & hold 語意）已被廢止。

    回傳 PortfolioResult：
      - nav: Portfolio NAV（起點 1.0）
      - daily_return: 每日組合 return
      - metrics: {start, end, years, total_return, cagr, mdd, volatility, sharpe}
      - history_diag: {stocks, min_years, median_years, max_years, per_stock: {ticker: years}}
    """
    if not isinstance(prices, pd.DataFrame):
        raise BacktestError('prices 必須是 DataFrame')
    if prices.empty:
        raise BacktestError('價格資料為空')
    if mode not in ('common', 'dynamic', 'full'):
        raise BacktestError(f'mode 必須是 common / dynamic / full，得到 {mode!r}')

    p = prices.sort_index().replace([np.inf, -np.inf], np.nan)
    tickers = list(p.columns)

    # 統一權重（normalize）
    # 優先序:自訂 weights > 起始市值權重(用 shares, 與 〇 同源) > 等權重 fallback
    if weights is not None:
        w = pd.Series(weights, dtype=float).reindex(tickers).fillna(0)
    elif shares is not None:
        # 預設 = 起始市值權重（與 〇、組合起始市值同源）
        # 每支股票「自己最後一個有效日」close × shares（NaN → 0：該股始終沒資料）
        last_close = pd.Series({
            t: (p[t].dropna().iloc[-1] if not p[t].dropna().empty else float('nan'))
            for t in tickers
        })
        mv = (
            last_close
            * pd.Series(shares, dtype=float).reindex(tickers).fillna(0)
        )
        w = mv.fillna(0)
    else:
        # 兩者都沒給 → 等權重（向後相容 / 單元測試用）
        w = pd.Series(1.0, index=tickers)
    if w.sum() <= 0:
        raise BacktestError('權重總和為 0，請檢查 weights / shares 設定')
    w = w / w.sum()

    if mode == 'common':
        pr, n_active = _mode_common(p, w)
    elif mode == 'dynamic':
        pr, n_active = _mode_dynamic(p, w)
    else:  # full
        pr, n_active = _mode_full(p, w)

    if pr.empty:
        raise BacktestError(f'模式 {mode} 沒有可計算的報酬資料')

    nav = (1 + pr.fillna(0)).cumprod()

    # 個股歷史長度診斷
    history_diag = _history_diag(prices)

    return PortfolioResult(
        mode=mode,
        nav=nav,
        daily_return=pr,
        metrics=_metrics(nav),
        history_diag=history_diag,
        pct_active=n_active,
    )


# ───────── 3 種模式 ─────────
def _mode_common(p: pd.DataFrame, w: pd.Series) -> tuple[pd.Series, pd.Series]:
    """共同期間：所有股票都有資料才進組合"""
    starts = p.notna().idxmax()  # 第一個有效日期（每支股票）
    starts = starts.dropna()  # 過濾掉完全沒資料的欄位
    if starts.empty:
        raise BacktestError('所有股票都沒有歷史價格資料')
    common_start = starts.max()
    # 共同期間 = 每支股票「第一個有效日期」最晚的那一天起算；
    # 但只要該支股票在 common_start 之後有任何資料就保留（不要因為少數缺值日就整支砍掉）。
    # 修正前用 dropna(axis=1, how='any') 會誤殺，例如：
    #   0050 上市 2003-06-30，6208 上市 2002-12-23，common_start=2003-06-30；
    #   但 0050 在該日後仍有 5 個缺值日 → 兩支都被 how='any' 砍光 → 誤報「所有起點不重疊」。
    p = p.loc[common_start:].dropna(axis=1, how='all')
    if p.shape[1] == 0:
        diag = ', '.join(f'{c} 起始 {d.date()}' for c, d in starts.items())
        raise BacktestError(
            f'沒有共同歷史期間（共同起點 {common_start.date()} 之後沒有任何股票有資料）：{diag}'
        )
    w = w.reindex(p.columns).fillna(0)
    if w.sum() <= 0:
        raise BacktestError('共同期間內所有指定權重都為 0')
    w = w / w.sum()
    r = _safe_pct_change(p)
    pr = r.mul(w, axis=1).sum(axis=1, min_count=1)
    n_active = pd.Series(p.notna().sum(axis=1), index=p.index, dtype=float)
    return pr, n_active


def _mode_dynamic(p: pd.DataFrame, w: pd.Series) -> tuple[pd.Series, pd.Series]:
    """動態加入：每日只用當天有資料的股票，權重重新正規化

    加權重上限避免早期股票被放大（驗收標準 #8a）：
    - 原本: 1 個 stock active 時被正規化為 100% weight → early period MDD 被盢大
    - 修法: cap_per_stock = 1.5 / n_total（最多放大 1.5 倍）→ 早期股票不會獨大
      → 1 stock active 時上限 16.7%（vs 原本 100%）
      → 4+ stocks active 時 normal 運作

    跟 full 的差別保留：
    - dynamic: dropna + renormalize（cap 以避免極端）
    - full: fillna(0) + fixed weights（從第一天就以原重計入）
    """
    r = _safe_pct_change(p)
    vals: list[float] = []
    n_active_list: list[int] = []
    n_total = len(w)
    cap_per_stock = 1.5 / n_total if n_total > 0 else 1.0  # 最多放大 1.5 倍
    # n_active 以「當天有有效價格的股票數」算（更直觀，反映「組合含多少檔」）
    for date, row_p in p.iterrows():
        valid_prices = row_p.dropna()
        if valid_prices.empty:
            vals.append(0.0)
            n_active_list.append(0)
            continue
        # 對應的 return（已過濾 inf）
        ret_row = r.loc[date]
        valid_rets = ret_row.dropna()
        if valid_rets.empty:
            vals.append(0.0)
            n_active_list.append(int(len(valid_prices)))
            continue
        w_valid = w.reindex(valid_rets.index).fillna(0)
        s = w_valid.sum()
        if s <= 0:
            vals.append(0.0)
            n_active_list.append(int(len(valid_prices)))
            continue
        # 原本: w_valid = w_valid / s (完全正規化 → 1 個 stock 被放大為 100%)
        # 修法: 先正規化、再 cap 每檔上限
        w_normalized = w_valid / s
        w_capped = w_normalized.clip(upper=cap_per_stock)
        # 如果 cap 生效，sum 會 < 1（不是 100% allocation）→ 保留原意
        vals.append(float((valid_rets * w_capped).sum()))
        n_active_list.append(int(len(valid_prices)))
    pr = pd.Series(vals, index=p.index, name='portfolio_return')
    n_active = pd.Series(n_active_list, index=p.index, dtype=float)
    return pr, n_active


def _safe_pct_change(p: pd.DataFrame) -> pd.DataFrame:
    """pct_change + 過濾 inf（FinMind 偶爾回傳壞資料，造成單股 return=inf）"""
    r = p.pct_change(fill_method=None)
    return r.replace([np.inf, -np.inf], np.nan)


def _mode_full(p: pd.DataFrame, w: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Full Available History：每支股票從自己最早資料開始算（fixed weights, fillna(0)）

    跟 dynamic 的關鍵差別：
    - dynamic: 每日重新正規化權重（早期股票被放大）
    - full: 固定權重 + fillna(0)（早期股票不放大，避免 MDD 被讇大）

    驗收標準 #7 #8:
    - Full 跟 Dynamic 的 metrics 不會完全相同（因為 full 不重新正規化）
    - Full 的 MDD 應該比 Dynamic 小（因為沒有重複增強早期股票 contribution）
    """
    r = _safe_pct_change(p)
    # fillna(0) 而不是 dropna: 未上市的股票當天 return = 0（代表不在 portfolio）
    # 不重新正規化權重
    r_filled = r.fillna(0)
    pr = r_filled.mul(w, axis=1).sum(axis=1, min_count=1)
    n_active = pd.Series(p.notna().sum(axis=1), index=p.index, dtype=float)
    return pr, n_active


# ───────── 指標 ─────────
def _metrics(nav: pd.Series) -> dict:
    logger.debug(f'_metrics called: nav_len={len(nav)}, nav_empty={nav.empty}')
    if nav.empty or len(nav) < 2:
        logger.warning(f'_metrics: NAV too short (len={len(nav)})')
        raise BacktestError('NAV 資料點不足')
    
    # Check for NaN values in NAV
    if nav.isna().any():
        raise BacktestError('NAV 包含 NaN 值')
    
    r = nav.pct_change().dropna()
    logger.debug(f'_metrics: pct_change result len={len(r)}')
    
    # If pct_change results in empty series (e.g., nav has only 1 element),
    # variance is undefined - return NaN for volatility and sharpe
    if len(r) < 2:
        yrs = max((nav.index[-1] - nav.index[0]).days / 365.25, 1 / 365.25)
        peak = nav.cummax()
        dd = nav / peak - 1
        return {
            'start': str(nav.index[0].date()),
            'end': str(nav.index[-1].date()),
            'years': float(yrs),
            'total_return': float(nav.iloc[-1] / nav.iloc[0] - 1) if nav.iloc[0] else None,
            'cagr': float((nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1) if nav.iloc[0] > 0 else None,
            'mdd': float(dd.min()) if not dd.empty else None,
            'volatility': None,
            'sharpe': None,
        }
    
    yrs = max((nav.index[-1] - nav.index[0]).days / 365.25, 1 / 365.25)
    peak = nav.cummax()
    dd = nav / peak - 1
    
    if len(r) > 1:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            std_val = r.std(ddof=1)
        logger.debug(f'_metrics: std_val={std_val}, is_nan={pd.isna(std_val)}')
        vol = float(std_val * np.sqrt(252)) if not pd.isna(std_val) else None
        sharpe = float(r.mean() / std_val * np.sqrt(252)) if std_val > 0 and not pd.isna(std_val) else None
    else:
        logger.debug(f'_metrics: len(r) <= 1, returning None for vol/sharpe')
        vol = None
        sharpe = None
    
    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1) if nav.iloc[0] else None
    cagr = float((nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1) if nav.iloc[0] > 0 else None
    # Phase 6 (Item 11): MDD 詳細 — Peak Date / Trough Date / Recovery Date / 回撤天數
    mdd_detail = _compute_mdd_detail(nav, peak, dd)
    
    return {
        'start': str(nav.index[0].date()),
        'end': str(nav.index[-1].date()),
        'years': float(yrs),
        'total_return': total_return,
        'cagr': cagr,
        'mdd': float(dd.min()) if not dd.empty else None,
        'volatility': vol,
        'sharpe': sharpe,
        # Phase 6 (Item 11): MDD 詳細資訊
        **mdd_detail,
    }


def _compute_mdd_detail(nav: pd.Series, peak: pd.Series, dd: pd.Series) -> dict:
    """Phase 6 (Item 11): 算 MDD 的 Peak/Trough/Recovery Date + 回撤天數

    Args:
        nav: NAV series
        peak: nav.cummax() series
        dd: nav/peak - 1 series

    Returns:
        dict: {
          'mdd_peak_date': 'YYYY-MM-DD' | None,
          'mdd_trough_date': 'YYYY-MM-DD' | None,
          'mdd_recovery_date': 'YYYY-MM-DD' | None (未回復 = None),
          'mdd_drawdown_days': int | None (peak → trough 天數),
          'mdd_recovery_days': int | None (trough → recovery 天數,未恢復 = None),
        }
    """
    if dd.empty or dd.isna().all():
        return {
            'mdd_peak_date': None,
            'mdd_trough_date': None,
            'mdd_recovery_date': None,
            'mdd_drawdown_days': None,
            'mdd_recovery_days': None,
        }
    # Trough: dd 最負的那點(最深)
    trough_ts = dd.idxmin()
    # Peak: 在 trough 之前的最高峰
    pre_peak = peak.loc[:trough_ts]
    if pre_peak.empty:
        peak_ts = trough_ts
    else:
        peak_ts = pre_peak.idxmax()
    # Recovery: trough 之後第一個回到 peak 值的點
    peak_value = float(peak.loc[peak_ts])
    post_trough = nav.loc[trough_ts:]
    recovered_mask = post_trough >= peak_value
    if recovered_mask.any():
        # 取第一個 >= peak_value 的點(包含 trough 自己,代表當天就回)
        recovery_ts = post_trough[recovered_mask].index[0]
    else:
        recovery_ts = None
    # 天數
    drawdown_days = (trough_ts - peak_ts).days if peak_ts != trough_ts else 0
    recovery_days = ((recovery_ts - trough_ts).days
                     if recovery_ts is not None and recovery_ts != trough_ts
                     else None)
    return {
        'mdd_peak_date': str(peak_ts.date()) if hasattr(peak_ts, 'date') else str(peak_ts)[:10],
        'mdd_trough_date': str(trough_ts.date()) if hasattr(trough_ts, 'date') else str(trough_ts)[:10],
        'mdd_recovery_date': (str(recovery_ts.date())
                              if recovery_ts is not None and hasattr(recovery_ts, 'date')
                              else (str(recovery_ts)[:10] if recovery_ts is not None else None)),
        'mdd_drawdown_days': int(drawdown_days),
        'mdd_recovery_days': int(recovery_days) if recovery_days is not None else None,
    }


def recent_n_year_metrics(nav: pd.Series, n_years: int) -> dict | None:
    """取 nav 最末 n_years 年切片，重算 CAGR/MDD/vol/Sharpe。

    Phase 1.2 (audit P2): 區分「完整歷史 23.09 年」與「最近 N 年」(預設 10)。
    用途: 「最近 10 年真實績效」對使用者最有參考價值 — 完整歷史被早期 0050 剛上市、
    低波動低報酬的時段拉低 CAGR,容易誤導。

    Returns:
        None  if nav 太短或 n_years 期間不足(資料不夠 n_years 年)
        dict  keys: start, end, years, total_return, cagr, mdd, volatility, sharpe
    """
    if nav.empty or len(nav) < 2:
        return None
    if n_years <= 0:
        raise BacktestError(f'n_years 必須 > 0,got {n_years}')

    # 用實際日數 cutoff (避免 calendar 切邊界)
    end_date = nav.index[-1]
    cutoff = end_date - pd.Timedelta(days=int(n_years * 365.25))
    recent = nav.loc[nav.index >= cutoff]
    if len(recent) < 2:
        # 不到 2 點 → 無法算 CAGR/MDD
        return None
    # 重要: 把 recent 重正規化為 1.0 起點,避免被完整歷史的「前段水平」污染
    recent = recent / recent.iloc[0]
    result = _metrics(recent)
    # Phase 1.2 嚴格語意: 若實際 years < n_years (資料不夠) → 不回,避免誤導使用者
    # 「問最近 10 年但實際只算 1.92 年」會讓使用者誤以為這是 10 年的結果
    if result['years'] < n_years * 0.95:    # 容許 5% 偏差 (cutoff 取整造成)
        return None
    return result


# ───────── Benchmark 對照 ─────────
def build_benchmark(
    bench_prices: pd.DataFrame,
    ticker: str = 'BENCH',
) -> dict:
    """
    拿單一 ticker 的股價算 benchmark 指標（對照組）。
    同一個价格序列、同一套 metrics 公式。
    """
    if bench_prices.empty or ticker not in bench_prices.columns:
        return {'ticker': ticker, 'metrics': None}
    s = bench_prices[ticker].dropna()
    if len(s) < 2:
        return {'ticker': ticker, 'metrics': None}
    nav = s / s.iloc[0]
    metrics = _metrics(nav)
    return {
        'ticker': ticker,
        'stock_id': ticker,
        'metrics': metrics,
        'nav': [{'date': str(d.date()), 'nav': float(v)} for d, v in nav.items()],
    }


def _history_diag(prices: pd.DataFrame) -> dict:
    """計算每支股票的歷史長度（年）"""
    per_stock = {}
    levels = []
    for t in prices.columns:
        s = prices[t].dropna()
        if s.empty:
            per_stock[t] = 0.0
            continue
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        per_stock[t] = float(yrs)
        levels.append(yrs)
    if not levels:
        return {'stocks': 0, 'min_years': 0.0, 'median_years': 0.0, 'max_years': 0.0, 'per_stock': {}}
    return {
        'stocks': int(len(levels)),
        'min_years': float(min(levels)),
        'median_years': float(pd.Series(levels).median()),
        'max_years': float(max(levels)),
        'per_stock': {k: round(v, 2) for k, v in per_stock.items()},
    }


# ───────── 工具：把 FinMind rows 轉 pivot ─────────
def prices_to_pivot(rows_by_ticker: dict[str, list[dict]], price_col: str = 'close') -> pd.DataFrame:
    """
    把 FinMind 抓回來的 {ticker: [{date, open, max, min, close, ...}]}
    轉成 pd.DataFrame：index=Date, columns=Ticker, values=price_col
    """
    frames = []
    for ticker, rows in rows_by_ticker.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        if 'date' not in df.columns or price_col not in df.columns:
            continue
        df = df[['date', price_col]].copy()
        df['date'] = pd.to_datetime(df['date'])
        # FinMind 原始資料偶發 sentinel（close=0 代表減資 / 暫停交易；負數則是数据錯誤）
        # 這些不是真實市價，不該用來計算日報酬 → 在 pipeline 進口先過濾。
        df = df[df[price_col] > 0]
        df.columns = ['Date', ticker]
        frames.append(df.set_index('Date'))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1).sort_index()


def build_adjusted_close(
    raw_pivot: pd.DataFrame,
    dividends_by_ticker: dict[str, list[dict]] | None = None,
    splits_by_ticker: dict[str, list[dict]] | None = None,
) -> pd.DataFrame:
    """
    產生「還原除權息 + 分割/反分割」的 close DataFrame（**股數追蹤法 / Total Return**）。

    與「cum_addend 直接加到 raw 上」的舊算法不同，這裡採用**逐日模擬持有 1 股**：
    - 起始持有 1 股
    - 每個除息日：
        * 收到現金股利 cash_div 元 → 以當日 close 立刻再買（shares += cash_div / close）
        * 收到股票股利（比例 r）→ 股數變 1+r 倍（shares *= 1+r）
    - 每個 split 日：股數 × split_ratio
    - NAV(t) = shares(t) × close(t)

    這個算法 = 「還原除權息 Total Return」業界標準 (參照 Yahoo Finance / FinMind Adj Close 概念)，
    也是唯一能處理「除息後立刻再投入 + split 改變股數」正確交互作用的方法。

    驗證：0050 (2003-06-30 → 2026-07-31, 含 32 筆配息 + 1 次 4 分割) 應得
    ~+1900% Total Return (vs 主人 havocfuture.tw 對照 ~+2222%，差異來自 FinMind
    缺漏 2003-2004 年配息，與算法無關)。

    Args:
        raw_pivot: index=Date, columns=Ticker, values=raw close
        dividends_by_ticker: {ticker: [{date, cash_div, stock_div_ratio}, ...]}
        splits_by_ticker: {ticker: [{date, split_ratio, type}, ...]}
    """
    if raw_pivot.empty:
        return raw_pivot.copy()
    dividends_by_ticker = dividends_by_ticker or {}
    splits_by_ticker = splits_by_ticker or {}
    adj = raw_pivot.copy()
    for ticker in adj.columns:
        raw = raw_pivot[ticker].dropna()
        if raw.empty:
            continue
        # 收集該 ticker 在 raw index 範圍內的所有事件（用 base_date 對齊）
        index_dates_str = set(raw.index.strftime('%Y-%m-%d').tolist())
        events_by_date: dict[str, list[tuple[str, float]]] = {}
        for d in dividends_by_ticker.get(ticker) or []:
            base = d.get('date')
            if not base or base not in index_dates_str:
                continue
            cash = float(d.get('cash_div', 0) or 0)
            sr = float(d.get('stock_div_ratio', 0) or 0)
            if cash == 0 and sr == 0:
                continue
            events_by_date.setdefault(base, []).append(('div', cash, sr))
        for sp in splits_by_ticker.get(ticker) or []:
            base = sp.get('date')
            if not base or base not in index_dates_str:
                continue
            ratio = float(sp.get('split_ratio', 1.0) or 1.0)
            if ratio == 1.0:
                continue
            events_by_date.setdefault(base, []).append(('split', ratio))
        # 逐日模擬持有股數
        shares = 1.0
        nav_series: list[float] = []
        for t, p in raw.items():
            for ev in events_by_date.get(t.strftime('%Y-%m-%d'), []):
                if ev[0] == 'div':
                    cash, sr = ev[1], ev[2]
                    shares *= (1 + sr)
                    if p > 0 and cash > 0:
                        shares += cash / p
                elif ev[0] == 'split':
                    shares *= ev[1]
            nav_series.append(shares * float(p))
        adj[ticker] = pd.Series(nav_series, index=raw.index)
    return adj

# ───────── 工具：起始市值（最後收盤價 × 股數）─────────
def compute_market_value(
    prices: pd.DataFrame,
    shares: dict[str, int],
    as_of: str | None = None,
) -> dict:
    """
    計算組合當前市值。
    prices: pivot 表（index=Date, columns=Ticker, values=close）
    shares: {ticker: 股數}
    as_of: 'YYYY-MM-DD' 或 None（None = 最後一個共同交易日）
    回傳：
      {
        'as_of': str (date),
        'total': int (總市值),
        'per_stock': [{ticker, close, shares, value}],
        'missing': [ticker, ...]  # 該 ticker 在該日沒資料
      }
    """
    if prices.empty or not shares:
        return {'as_of': None, 'total': 0, 'per_stock': [], 'missing': list(shares.keys())}

    if as_of:
        # 找 <= as_of 的最後一個共同交易日
        target = pd.Timestamp(as_of)
        mask = prices.index <= target
        if not mask.any():
            target_date = prices.index[0]
        else:
            target_date = prices.index[mask][-1]
    else:
        target_date = prices.index[-1]

    per_stock = []
    missing = []
    total = 0
    for t, n in shares.items():
        if t not in prices.columns:
            missing.append(t)
            continue
        close = float(prices.loc[:target_date, t].dropna().iloc[-1]) if prices.loc[:target_date, t].notna().any() else 0
        if close <= 0:
            missing.append(t)
            continue
        value = int(round(close * n))
        total += value
        per_stock.append({
            'ticker': t,
            'close': round(close, 2),
            'shares': n,
            'value': value,
        })

    return {
        'as_of': str(target_date.date()),
        'total': int(total),
        'per_stock': per_stock,
        'missing': missing,
    }


# ───────── 工具：個股歷史長度診斷（加強版）─────────
def per_stock_history(
    prices: pd.DataFrame,
    shares: dict[str, int] | None = None,
) -> dict:
    """
    計算每支股票的歷史長度（年）+ 在 shares 中可買到的初始市值（用最早一天的價格）
    同時附上「價格報酬率」指標（total_return / CAGR / MDD / vol / Sharpe），
    把該股 close 序列冏 1 化（nav = price / first_close）後走同一套 _metrics()。
    （為了跟 common mode 的「組合報酬」不混，這裡用該股 **單獨** 走完整 _metrics，
     不是按組合方式加权）
    """
    per = {}
    for t in prices.columns:
        s = prices[t].dropna()
        # FinMind 原始 close 有時含 sentinel 値（0 / 負数）—代表減資 / 暫停交易。
        # 這些不是真實市價，不該用來計算 MDD / vol / Sharpe；避開它們。
        s = s[s > 0]
        if s.empty:
            per[t] = {
                'years': 0.0,
                'start': None,
                'end': None,
                'rows': 0,
                'first_close': None,
                'last_close': None,
                'total_return': None,
                'cagr': None,
                'mdd': None,
                'volatility': None,
                'sharpe': None,
            }
            continue
        per[t] = {
            'years': round((s.index[-1] - s.index[0]).days / 365.25, 2),
            'start': str(s.index[0].date()),
            'end': str(s.index[-1].date()),
            'rows': int(len(s)),
            'first_close': round(float(s.iloc[0]), 2),
            'last_close': round(float(s.iloc[-1]), 2),
        }
        # 計算「個股歷史真實績效」表需要的 5 個進階指標
        # nav 定義 = 1 起步、以 first_close 為基準的價格指數
        try:
            nav = s / float(s.iloc[0])
            m = _metrics(nav)
            per[t].update({
                'total_return': m.get('total_return'),
                'cagr': m.get('cagr'),
                'mdd': m.get('mdd'),
                'volatility': m.get('volatility'),
                'sharpe': m.get('sharpe'),
            })
        except BacktestError:
            # 個別股票歷史太短（< 2 筆）取不到完整指標 → 拉成 None，不影響其他股
            per[t].update({
                'total_return': None, 'cagr': None, 'mdd': None,
                'volatility': None, 'sharpe': None,
            })
    return per


def _window_shares_tracking_nav(
    raw_window: pd.Series,
    events_by_date: dict[str, list[tuple]],
) -> pd.Series:
    """v3.0.4 P0 fix: 抽出 per_stock_n_year_window 的核心 shares tracking。

    Input:
        raw_window: pd.Series index=Date, values=raw close
                    (通常會先取 [cutoff, end_date] 才進來)
        events_by_date: {date_str: [(kind, ...), ...]}
                        kind ∈ {'div': (cash, stock_ratio), 'split': (ratio,)}

    Output:
        nav: pd.Series index=Date, values=shares(t) × close(t)
             shares 從該窗口第一日 = 1.0 開始累積

    用法:
      - per_stock_n_year_window 拿來算 5y total return / CAGR / MDD
      - monthly_returns.compute_monthly_returns_via_shares_tracking 拿來算逐月報酬
        (兩者 nav series 同源 → 5y compound 跟 total return 必定 100% 對齊)
    """
    shares = 1.0
    nav_series: list[float] = []
    for d_idx, p in raw_window.items():
        for ev in events_by_date.get(d_idx.strftime('%Y-%m-%d'), []):
            if ev[0] == 'div':
                cash, sr = ev[1], ev[2]
                shares *= (1 + sr)
                if p > 0 and cash > 0:
                    shares += cash / p
            elif ev[0] == 'split':
                shares *= ev[1]
        nav_series.append(shares * float(p))
    return pd.Series(nav_series, index=raw_window.index)


def per_stock_n_year_window(
    raw_prices: pd.DataFrame,
    n_years: int,
    dividends_by_ticker: dict[str, list[dict]] | None = None,
    splits_by_ticker: dict[str, list[dict]] | None = None,
) -> dict:
    """
    對齊 N 年區間的個股 8 欄診斷表（per_stock 的 N-年限定版本）。

    關鍵差別：跟 per_stock_history「各股各自從上市第一天起算」不同，
    這裡是**時間對齊**：每支股票都以「最後N年」的窗口計算，
    避免「0050 報 23 年 vs 006208 報 14 年」那種起點不同造成的誤判。
    該股原本上市不足 N 年：years 會誠實反映實際觀察年數（但 total/CAGR/MDD/Sharpe 都計算）。

    計算語意（v2 fix 2026-08-31, 二寶）：
        在 window 起點「買進持有 N 年」的真實 Total Return。
        不用累積 adj (`build_adjusted_close` 結果) 當輸入 —— 那個 adj 從 2003 累積到今天，
        window 內的 first_close 已經含「window 之前」的配息再投入倍數，會 double-count。
        改為從 raw close 重新跑股數追蹤法（shares 從 1.0 起算），配息與 split 都在 window 內生效。

    Returns:
        {ticker: {
            'years':      實際窗口年數 (可能 < n_years)
            'start':      window 起始日期 (YYYY-MM-DD)
            'end':        window 結束日期 (YYYY-MM-DD)
            'first_close': window 起始日 raw close (float, round 2)
            'last_close':  window 結束日 raw close
            'total_return','cagr','mdd','volatility','sharpe': 該窗口內 5 個指標
            'n_years':    申請的 N（給前端說明）
            'short':      True if 實際 years < n_years × 0.95
        }, ...}
    """
    if raw_prices.empty or n_years <= 0:
        return {}
    dividends_by_ticker = dividends_by_ticker or {}
    splits_by_ticker = splits_by_ticker or {}
    per = {}
    # 統一 window 邊界：以「所有股票中最後一天」為 end，並取之前的 n_years
    end_date = raw_prices.apply(
        lambda s: s.dropna().index[-1] if not s.dropna().empty else None
    ).max()
    if end_date is None:
        return {}
    cutoff = end_date - pd.Timedelta(days=int(n_years * 365.25))
    for t in raw_prices.columns:
        s = raw_prices[t].dropna()
        # sentinel 過濾
        s = s[s > 0]
        if s.empty:
            per[t] = _empty_n_year_row(n_years)
            continue
        # 取 [cutoff, end_date] 範圍
        window = s.loc[(s.index >= cutoff) & (s.index <= end_date)]
        if len(window) < 2:
            per[t] = _empty_n_year_row(n_years)
            continue
        years_actual = (window.index[-1] - window.index[0]).days / 365.25
        short = years_actual < n_years * 0.95

        # 在 window 內跑股數追蹤法（v3 還原除權息）
        # v3.0.4 P0 fix: 抽出 _window_shares_tracking_nav helper,跟月報表共用
        window_index_str = set(window.index.strftime('%Y-%m-%d').tolist())
        events_by_date: dict[str, list[tuple]] = {}
        for d in dividends_by_ticker.get(t) or []:
            if d.get('date') and d['date'] in window_index_str:
                events_by_date.setdefault(d['date'], []).append(
                    ('div', float(d.get('cash_div', 0) or 0), float(d.get('stock_div_ratio', 0) or 0))
                )
        for sp in splits_by_ticker.get(t) or []:
            if sp.get('date') and sp['date'] in window_index_str:
                events_by_date.setdefault(sp['date'], []).append(
                    ('split', float(sp.get('split_ratio', 1.0) or 1.0))
                )
        nav = _window_shares_tracking_nav(window, events_by_date)

        try:
            m = _metrics(nav)
        except BacktestError:
            m = {'total_return': None, 'cagr': None, 'mdd': None, 'volatility': None, 'sharpe': None}
        per[t] = {
            'n_years': n_years,
            'years': round(years_actual, 2),
            'start': str(window.index[0].date()),
            'end': str(window.index[-1].date()),
            'rows': int(len(window)),
            'first_close': round(float(window.iloc[0]), 2),
            'last_close': round(float(window.iloc[-1]), 2),
            'total_return': m.get('total_return'),
            'cagr': m.get('cagr'),
            'mdd': m.get('mdd'),
            'volatility': m.get('volatility'),
            'sharpe': m.get('sharpe'),
            'short': short,
        }
    return per


def _empty_n_year_row(n_years: int) -> dict:
    return {
        'n_years': n_years,
        'years': 0.0,
        'start': None,
        'end': None,
        'rows': 0,
        'first_close': None,
        'last_close': None,
        'total_return': None,
        'cagr': None,
        'mdd': None,
        'volatility': None,
        'sharpe': None,
        'short': True,
    }
