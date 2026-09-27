"""
FinMind API Client
- 200ms thread-safe rate-limit
- 24h 價格快取（JSON in data/price_cache/<ticker>.json）
- 三段式 token 載入（v1.2）：
    1) OpenClaw secrets FINMIND_TOKEN  ← 首選（audit + runtime）
    2) ~/.config/retrocast/finmind-token  ← legacy 本地橋接
    3) env var FINMIND_TOKEN  ← CI / testing 友善
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests


# ───────── Constants ─────────
FINMIND_API_BASE = 'https://api.finmindtrade.com/api/v4/data'
RATE_LIMIT_MS = 200
# 30 天 TTL（v3.0.2 改）：配合 end_date 預設為「前一個月最後一天」的概念。
# 歷史回測只看月 K 層級的價格,一個月內資料不會變,24h TTL 太短會每天重抓。
# 跨月會自然 cache miss (因為 end_date 變了 → covers check 不過 → 補抓一個月)
PRICE_CACHE_TTL_SECONDS = 30 * 86400  # 30 days

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ───────── Errors ─────────
class FinMindError(RuntimeError):
    """FinMind API 錯誤（HTTP / 解析 / token 缺失）"""


# ───────── Token 載入 ─────────
_TOKEN_CACHE: str | None = None


def _parse_key_value_file(path: Path) -> dict:
    """
    解析 Key=Value 格式（~/.config/retrocast/finmind-token 也用此格式）：
      ACCOUNT = "x"
      FINMIND_TOKEN=eyJ...
    """
    out: dict = {}
    if not path.is_file():
        return out
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            v = v.strip()
            # 剝單/雙引號
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            out[k.strip()] = v
    except OSError:
        pass
    return out


def _load_finmind_token_from_openclaw() -> str:
    """OpenClaw secrets 讀 FINMIND_TOKEN。
    subprocess 失敗 → 回空字串（呼叫端繼續往下個 source）。"""
    try:
        result = subprocess.run(
            ['openclaw', 'secrets', 'store', 'get', 'FINMIND_TOKEN'],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ''


def load_finmind_token() -> str:
    """
    三段式讀取（v1.2）：
      1) OpenClaw secrets FINMIND_TOKEN  ← 首選（audit + runtime 一致）
      2) ~/.config/retrocast/finmind-token  ← legacy 本地橋接
      3) env var FINMIND_TOKEN  ← CI / testing 友善
    結果以 module-level _TOKEN_CACHE 快取,同一 process 重複 import 不再叫 subprocess。
    """
    global _TOKEN_CACHE
    if _TOKEN_CACHE is not None:
        return _TOKEN_CACHE

    # 1) OpenClaw secrets
    tok = _load_finmind_token_from_openclaw()
    if tok:
        _TOKEN_CACHE = tok
        return tok

    # 2) ~/.config/retrocast/finmind-token（legacy）
    legacy = Path.home() / '.config' / 'retrocast' / 'finmind-token'
    cfg = _parse_key_value_file(legacy)
    tok = cfg.get('FINMIND_TOKEN', '').strip()
    if tok:
        _TOKEN_CACHE = tok
        return tok

    # 3) env var
    tok = os.environ.get('FINMIND_TOKEN', '').strip()
    if tok:
        _TOKEN_CACHE = tok
        return tok

    return ''


def clear_token_cache() -> None:
    """測試用：清掉 module-level token cache,讓下次 load 重新讀 source。"""
    global _TOKEN_CACHE
    _TOKEN_CACHE = None


# ───────── Client ─────────
class FinMindClient:
    """Thread-safe FinMind API client（200ms rate-limit + 24h 價格快取）"""

    _lock = threading.Lock()
    _last_call_ms = 0

    def __init__(
        self,
        token: str | None = None,
        rate_limit_ms: int = RATE_LIMIT_MS,
        cache_dir: Path | None = None,
        cache_ttl_seconds: int = PRICE_CACHE_TTL_SECONDS,
    ):
        self.token = token or load_finmind_token()
        if not self.token:
            raise FinMindError(
                'FINMIND_TOKEN not found. Set it via OpenClaw secrets '
                '(openclaw secrets store set FINMIND_TOKEN), '
                'or place in ~/.config/retrocast/finmind-token, '
                'or set FINMIND_TOKEN env var.'
            )
        self.rate_limit_ms = rate_limit_ms
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'stock-portfolio-forecast/1.0 (Flask)'})
        self.cache_dir = cache_dir or (_PROJECT_ROOT / 'data' / 'price_cache')
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_seconds = cache_ttl_seconds
        # v3.0.4 fix: first_trading_day 專屬 cache,避免 9 檔就重抓 9 次
        self.first_trading_days_cache_file = self.cache_dir / 'first_trading_days.json'

    # ────────── 通用 query ──────────
    def query(self, dataset: str, params: dict | None = None) -> list[dict]:
        clean: dict[str, Any] = {'dataset': dataset, 'token': self.token}
        for k, v in (params or {}).items():
            if v is None:
                continue
            if isinstance(v, str) and v.lower() in ('undefined', 'null', ''):
                continue
            clean[k] = v
        resp = self._fetch(FINMIND_API_BASE, params=clean)
        data = resp.json()
        if not isinstance(data, dict):
            raise FinMindError(f'Invalid JSON from FinMind for {dataset}')
        if data.get('status', 0) != 200:
            raise FinMindError(f"FinMind error [{dataset}]: {data.get('msg', 'unknown')}")
        return data.get('data', [])

    # ────────── TaiwanStockPrice（with cache + ticker 自動試 variants） ──────────
    def get_stock_price(
        self,
        stock_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        use_cache: bool = True,
    ) -> list[dict]:
        """
        取得台股歷史股價（單一股）。
        start_date / end_date 格式 YYYY-MM-DD；不給 → 從 2000-01-01 抓到今天。
        24h 內同 ticker 會用本地 JSON cache（fetch 區間落在 cache 內時直接退回 cache）。

        自動試 variants：
          - 原文
          - 補 0 到 4 碼（純數字）
          - 補 0 到 6 碼（純數字 4 碼）
          - upper（字母型）
        哪個拿到資料就用哪個。
        """
        stock_id = stock_id.strip()
        if not stock_id:
            raise FinMindError('stock_id required')

        end_dt = datetime.strptime(end_date, '%Y-%m-%d') if end_date else datetime.now()
        start_dt = datetime.strptime(start_date, '%Y-%m-%d') if start_date else datetime(2000, 1, 1)
        if start_dt > end_dt:
            raise FinMindError('start_date > end_date')

        candidates = self._ticker_variants(stock_id)
        last_err: FinMindError | None = None
        for cand in candidates:
            try:
                rows = self._get_stock_price_single(cand, start_dt, end_dt, use_cache)
                if rows:
                    return self._slice_rows(rows, start_dt, end_dt)
            except FinMindError as e:
                last_err = e
                # 繼續試下一個 variant
                continue
        # 全部 variants 都拿不到 → 拋最後一個錯誤
        if last_err:
            raise last_err
        raise FinMindError(
            f'查無 {stock_id} 資料（試過 {candidates}）。'
            '若是新上 ETF 或已下市股票，請從名單移除。'
        )

    # ────────── TaiwanStockPriceAdj（還原除權息、Phase 3 新增） ──────────
    def get_stock_price_adj(
        self,
        stock_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        use_cache: bool = True,
    ) -> list[dict]:
        """取得「還原除權息」後的歷史股價。

        Phase 3 背景：原 `get_stock_price` 用 `TaiwanStockPrice` dataset，
        其 close 是「原始收盤價」，除息、除權當日會自然下跌，不含現金股利
        再投資效果，適合歷史回測計算「價格報酬」（CAGR 為價格收益率）。

        本方法改用 `TaiwanStockPriceAdj`，調整 close 反映配息、配股還原，
        CAGR 會包含「股利再投資」效果。

        重要：
          - 需 FinMind v4 token (TaiwanStockPriceAdj 為付費資料)
          - 寫入獨立 cache file `{stock_id}.adj.json`，不跟 raw close 混
          - 未預設啟用：須由上層明確呼叫才會使用
        """
        stock_id = stock_id.strip()
        if not stock_id:
            raise FinMindError('stock_id required')

        end_dt = datetime.strptime(end_date, '%Y-%m-%d') if end_date else datetime.now()
        start_dt = datetime.strptime(start_date, '%Y-%m-%d') if start_date else datetime(2000, 1, 1)
        if start_dt > end_dt:
            raise FinMindError('start_date > end_date')

        candidates = self._ticker_variants(stock_id)
        last_err: FinMindError | None = None
        for cand in candidates:
            try:
                rows = self._get_stock_price_single(
                    cand, start_dt, end_dt, use_cache,
                    dataset='TaiwanStockPriceAdj',
                )
                if rows:
                    return self._slice_rows(rows, start_dt, end_dt)
            except FinMindError as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        raise FinMindError(
            f'查無 {stock_id} 還原除權息資料（試過 {candidates}）。'
            'TaiwanStockPriceAdj 需要 FinMind v4 token 或資料來未涵蓋該 ticker。'
        )

    @staticmethod
    def _ticker_variants(stock_id: str) -> list[str]:
        """產生 ticker 候選清單（去掉重複，保留順序）"""
        out: list[str] = []
        seen: set[str] = set()
        for t in [stock_id, stock_id.upper(), stock_id.strip()]:
            if t and t not in seen:
                out.append(t)
                seen.add(t)
        # 純數字 ticker 加補 0 變體
        if stock_id.isdigit():
            for n in (4, 5, 6):
                z = stock_id.zfill(n)
                if z not in seen:
                    out.append(z)
                    seen.add(z)
        return out

    # ────────── TaiwanStockInfo：上市櫃總覽 + 預先驗證 ──────────
    STOCK_LIST_CACHE_FILE = None  # 設在 __init__（需要 path）

    def get_stock_list(self, use_cache: bool = True, ttl: int = 86400) -> list[dict]:
        """
        全上市櫃股票清單（24h cache）。
        每檔回傳 {stock_id, stock_name, industry_category, type, date}，
        date 是 FinMind 把該檔納入清單的日期。
        用來：
        1) 預先驗證 user 給的 ticker 是否存在（避免 713 這種「根本不存在」卻回傳假資料的陷阱）
        2) 自動 match 代號格式（50 → 0050、6208 → 006208）
        """
        cache_file = self.cache_dir / 'stock_list.json'
        if use_cache and cache_file.is_file():
            try:
                mtime = cache_file.stat().st_mtime
                if time.time() - mtime < ttl:
                    cached = json.loads(cache_file.read_text(encoding='utf-8'))
                    if isinstance(cached, list):
                        return cached
            except (json.JSONDecodeError, OSError):
                pass
        rows = self.query('TaiwanStockInfo')
        # 只留每檔最新一筆（避免同 stock_id 多筆）
        by_id: dict[str, dict] = {}
        for r in rows:
            sid = r.get('stock_id')
            if not sid:
                continue
            r_date = r.get('date', '')
            if sid not in by_id or by_id[sid].get('date', '') < r_date:
                by_id[sid] = r
        result = sorted(by_id.values(), key=lambda r: r['stock_id'])
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass
        return result

    def get_stock_info(self, stock_id: str) -> dict | None:
        """查單一檔基本資料（從本地清單快取查）。找不到 → None。"""
        stock_id = stock_id.strip()
        if not stock_id:
            return None
        # 先試直接 match
        for r in self.get_stock_list():
            if r.get('stock_id') == stock_id:
                return r
        # 試 variants
        for cand in self._ticker_variants(stock_id):
            for r in self.get_stock_list():
                if r.get('stock_id') == cand:
                    return r
        return None

    def match_ticker(self, user_input: str) -> dict | None:
        """
        用 TaiwanStockInfo 清單把使用者輸入的代號 match 到正確的 stock_id。
        回傳 {stock_id, stock_name, industry_category, type, source} 或 None（找不到）。
        source: 'exact' / 'padded_4' / 'padded_6' / 'upper' / 'name_partial'
        """
        ui = user_input.strip()
        if not ui:
            return None
        slist = self.get_stock_list()
        # 1) exact match
        for r in slist:
            if r['stock_id'] == ui:
                return {**r, 'source': 'exact'}
        # 2) variants
        for cand in self._ticker_variants(ui):
            for r in slist:
                if r['stock_id'] == cand:
                    return {**r, 'source': 'padded' if cand != ui else 'exact'}
        # 3) stock_name 內含（給中文名）
        for r in slist:
            if ui in r.get('stock_name', ''):
                return {**r, 'source': 'name_partial'}
        return None

    def match_tickers_batch(self, inputs: list[str]) -> dict[str, dict | None]:
        """
        批次把多個 user 輸入代號 match 到 FinMind 官方 stock_id。
        v3.0.3:用於 upload / analyze gate 一次檢查多個 ticker,避免每個都打一次 API。

        Args:
            inputs: list of user ticker strings(例如 ['50', '2002', '9999'])

        Returns:
            dict mapping 原始 input → FinMind 官方 ticker dict (含 stock_id / stock_name /
            industry_category / type / source),或 None(找不到)。
            - 重複 input 只 match 一次 (dedup)
            - 空 list 回空 dict
            - 空字串 / 純空白視為 None result
        """
        # dedup 但保留順序 (避免同 ticker 重複打 API)
        seen: set[str] = set()
        unique_inputs: list[str] = []
        for x in inputs:
            x_clean = (x or '').strip()
            if not x_clean:
                continue
            if x_clean not in seen:
                seen.add(x_clean)
                unique_inputs.append(x_clean)

        result: dict[str, dict | None] = {}
        for inp in unique_inputs:
            result[inp] = self.match_ticker(inp)
        return result

    def get_first_trading_day(self, stock_id: str) -> str | None:
        """
        查該 stock_id 最早一筆股價的日期（YYYY-MM-DD）。
        用來:
        1) 預先知道個股有資料的第一天（避免「股齡太短」進 N-Year 推估）
        2) 過濾掉 FinMind 對「不存在的 stock_id」回傳的 0 筆 / 假資料

        v3.0.4 fix: 加 first_trading_days.json cache。
        原本每次都 query FinMind 抓 1990~now 全部資料, 只為「第一筆日期」,
        一個 analyze endpoint 跑 9 檔 = 9 次 FinMind call, 同一天重跑也不會 hit。
        """
        stock_id = stock_id.strip()
        if not stock_id:
            return None

        # 1) Cache hit?
        entry = self._load_first_trading_day_entry(stock_id)
        if entry is not None:
            return entry

        # 2) Cache miss → query FinMind
        try:
            rows = self.query('TaiwanStockPrice', {
                'data_id': stock_id,
                'start_date': '1990-01-01',
                'end_date': datetime.now().strftime('%Y-%m-%d'),
            })
        except FinMindError:
            return None
        if not rows:
            return None
        # 過濾掉顯然是「預設填入」的垃圾資料（價 0 / 0.01）
        real = [r for r in rows if float(r.get('close', 0) or 0) > 0.5]
        if not real:
            return None
        date_str = real[0]['date']

        # 3) 寫 cache
        self._save_first_trading_day_entry(stock_id, date_str)
        return date_str

    def get_dividends(
        self,
        stock_id: str,
        start_date: str,
        end_date: str,
        use_cache: bool = True,
    ) -> list[dict]:
        """
        抓個股歷史股利紀錄（TaiwanStockDividend）。

        每筆回的關鍵欄位：
          - date:                 這筆股利的公告/異動日
          - CashEarningsDistribution + CashStatutorySurplus: 現金股利合計（元/股）
          - StockEarningsDistribution + StockStatutorySurplus: 股票股利合計（元/股，股票面額 10 元計算
                                                                 例如 0.5 表示每千股配 50 股）
          - CashExDividendTradingDate: 除息交易日 (close 那天會下跌反映 dividend 領走)
          - StockExDividendTradingDate: 除權交易日 (close 那天會下跌反映等比例稀釋)

        回傳結構化的 list：
        [
          {
            'date': '2025-07-21',                    # 除息除權基準日（取兩者中較早的交易日）
            'cash_div': 0.36,                        # 現金股利（元/股）
            'stock_div_ratio': 0.0,                  # 股票股利比例（= stock_div / 10）
            'cash_ex_date': '2025-07-21',            # 除息日（Y/N 股價下跌那天）
            'stock_ex_date': '',                     # 除權日（可能跟除息同日，也可能跟隔年配股）
          },
          ...
        ]

        cache：寫入 `data/price_cache/<stock_id>.dividend.json`，24h TTL。
        """
        stock_id = stock_id.strip()
        if not stock_id:
            return []
        cache_file = self.cache_dir / f'{stock_id}.dividend.json'

        if use_cache and cache_file.is_file():
            try:
                cache = json.loads(cache_file.read_text(encoding='utf-8'))
                ts = cache.get('fetched_at', 0)
                if time.time() - ts < self.cache_ttl_seconds:
                    # TTL 內才走 cache
                    rows = cache.get('rows', [])
                    return self._slice_div_rows(rows, start_date, end_date)
            except (json.JSONDecodeError, OSError):
                pass

        try:
            raw_rows = self.query('TaiwanStockDividend', {
                'data_id': stock_id,
                'start_date': start_date,
                'end_date': end_date,
            })
        except FinMindError:
            raw_rows = []

        # 統一結構（先用 raw_rows 走完清理、再寫 cache，以避免讀 cache 跟讀 API 走不同路）
        structured = self._structure_dividends(raw_rows)

        # 寫 cache（寫入已結構化的 rows）
        if use_cache:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps(
                        {'rows': structured, 'fetched_at': time.time()},
                        ensure_ascii=False,
                    ),
                    encoding='utf-8',
                )
            except OSError:
                pass
        return self._slice_div_rows(structured, start_date, end_date)

    @staticmethod
    def _structure_dividends(raw_rows: list[dict]) -> list[dict]:
        """把 FinMind TaiwanStockDividend 的 raw rows 轉成「以 base_date 為綁定鍵」結構。

        每個事件包含 cash_div、stock_div_ratio（股票股利除以 10，例 0.5元 = 0.05）。
        base_date 預設取 cash_ex 跟 stock_ex 較早的交易日。
        """
        out = []
        for r in raw_rows:
            cash = float(r.get('CashEarningsDistribution', 0) or 0) + float(r.get('CashStatutorySurplus', 0) or 0)
            stock_div = float(r.get('StockEarningsDistribution', 0) or 0) + float(r.get('StockStatutorySurplus', 0) or 0)
            cash_ex = str(r.get('CashExDividendTradingDate', '') or '').strip()
            stock_ex = str(r.get('StockExDividendTradingDate', '') or '').strip()
            ex_dates = [d for d in [cash_ex, stock_ex] if d]
            base_date = min(ex_dates) if ex_dates else str(r.get('date', '') or '').strip()
            if not base_date:
                continue
            out.append({
                'date': base_date,
                'cash_div': cash,
                'stock_div_ratio': stock_div / 10.0,
                'cash_ex_date': cash_ex,
                'stock_ex_date': stock_ex,
            })
        return out

    def get_splits(
        self,
        stock_id: str,
        start_date: str,
        end_date: str,
        use_cache: bool = True,
    ) -> list[dict]:
        """
        抓個股分割/反分割記錄（TaiwanStockSplitPrice）。

        type: '分割'（拆股，1 股變 N 股，after_price < before_price）
              '反分割'（合股，N 股變 1 股，after_price > before_price）

        結象表記息：split_ratio = before_price / after_price （例 4 分割 = 4.0）
        """
        stock_id = stock_id.strip()
        if not stock_id:
            return []
        cache_file = self.cache_dir / f'{stock_id}.split.json'
        if use_cache and cache_file.is_file():
            try:
                cache = json.loads(cache_file.read_text(encoding='utf-8'))
                ts = cache.get('fetched_at', 0)
                if time.time() - ts < self.cache_ttl_seconds:
                    rows = cache.get('rows', [])
                    return self._slice_div_rows(rows, start_date, end_date)
            except (json.JSONDecodeError, OSError):
                pass
        try:
            raw_rows = self.query('TaiwanStockSplitPrice', {
                'data_id': stock_id,
                'start_date': start_date,
                'end_date': end_date,
            })
        except FinMindError:
            raw_rows = []
        structured = []
        for r in raw_rows:
            try:
                before = float(r.get('before_price', 0) or 0)
                after = float(r.get('after_price', 0) or 0)
                ratio = before / after if after > 0 else 1.0
            except (TypeError, ValueError):
                ratio = 1.0
            structured.append({
                'date': str(r.get('date', '') or '').strip(),
                'type': str(r.get('type', '') or '').strip(),
                'before_price': before,
                'after_price': after,
                'split_ratio': ratio,
            })
        if use_cache:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps(
                        {'rows': structured, 'fetched_at': time.time()},
                        ensure_ascii=False,
                    ),
                    encoding='utf-8',
                )
            except OSError:
                pass
        return self._slice_div_rows(structured, start_date, end_date)

    @staticmethod
    def _slice_div_rows(rows: list[dict], start_date: str, end_date: str) -> list[dict]:
        """【股利專用】以 base_date 作為淺包依據"""
        out = []
        for r in rows:
            d = r.get('date', '')
            if not d:
                continue
            if start_date <= d <= end_date:
                out.append(r)
        return out

    # ────────── first_trading_days cache (v3.0.4) ──────────
    def _load_first_trading_day_entry(self, stock_id: str) -> str | None:
        """讀 cache, TTL 內 + 有 date 才回; 否 None"""
        try:
            cache = self._load_first_trading_days_cache()
        except OSError:
            return None
        entry = cache.get('days', {}).get(stock_id)
        if not entry:
            return None
        # entry 格式: {"date": "YYYY-MM-DD", "fetched_at": <epoch>}
        try:
            ts = float(entry.get('fetched_at', 0))
            if time.time() - ts > self.cache_ttl_seconds:
                return None
            return entry.get('date')
        except (ValueError, TypeError):
            return None

    def _save_first_trading_day_entry(self, stock_id: str, date_str: str) -> None:
        """寫進共用 cache 檔 (atomic-ish: read-modify-write)"""
        try:
            cache = self._load_first_trading_days_cache()
        except OSError:
            cache = {'days': {}}
        cache.setdefault('days', {})
        cache['days'][stock_id] = {
            'date': date_str,
            'fetched_at': time.time(),
        }
        try:
            self.first_trading_days_cache_file.write_text(
                json.dumps(cache, ensure_ascii=False),
                encoding='utf-8'
            )
        except OSError:
            pass  # cache 寫不進去不影響主流程

    def _load_first_trading_days_cache(self) -> dict:
        """讀整包 cache; 檔不存在 / 壞檔 / 空 dict 都回空"""
        if not self.first_trading_days_cache_file.is_file():
            return {'days': {}}
        try:
            data = json.loads(
                self.first_trading_days_cache_file.read_text(encoding='utf-8')
            )
        except (json.JSONDecodeError, OSError):
            return {'days': {}}
        if not isinstance(data, dict):
            return {'days': {}}
        data.setdefault('days', {})
        if not isinstance(data['days'], dict):
            data['days'] = {}
        return data

    # ────────── 批次抓多股 ──────────
    def get_many_prices(
        self,
        stock_ids: list[str],
        start_date: str | None = None,
        end_date: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, list[dict]]:
        """
        批次抓取多股歷史價格；用 200ms rate-limit 順序呼叫。
        回傳 {stock_id: [{date, open, max, min, close, ...}]}
        """
        out: dict[str, list[dict]] = {}
        for sid in stock_ids:
            try:
                out[sid] = self.get_stock_price(sid, start_date, end_date, use_cache=use_cache)
            except FinMindError as e:
                # 單股失敗不中斷整批；記在 _error 欄位
                out[sid] = []
                out[f'{sid}._error'] = str(e)  # type: ignore[assignment]
        return out

    # ────────── 內部：單一股票（不試 variants） ──────────
    def _get_stock_price_single(
        self,
        stock_id: str,
        start_dt: datetime,
        end_dt: datetime,
        use_cache: bool,
        dataset: str = 'TaiwanStockPrice',
    ) -> list[dict]:
        """單一 stock_id 抓價 + cache（不試 variants）

        Phase 3: 新增 dataset 參數,支援 TaiwanStockPriceAdj（還原除權息）。
        兩種 dataset 用獨立 cache file（TaiwanStockPrice → {id}.json；
        TaiwanStockPriceAdj → {id}.adj.json）,避免 raw 跟 adj 混在一起。

        v3.0.2 hotfix: 處理「cache.first > request.start」二種不同情境

        情境 A: cache.first=2003-06-30 (0050 上市日), user request start=2000-01-01
                → 之前 query 過 2000-2026,finmind 回的也只有 2003-2026
                → 不要再 fetch(浪費 API)。用 fetched_start_date 判斷。

        情境 B: cache.first=2021-01-01 (從未 fetch 過 2020), user request start=2020-01-01
                → 該 fetch 補 2020 資料 → merge 進 cache

        修法:
          - cache 新增 fetched_start_date 欄位(記錄「上次 query 的 start_date」)
          - _cache_covers 放寬語義: ttl OK + last >= end_dt + (fetched_start_date <= start_dt)
            (後者保護情境 A: 已試過不要再 fetch)
          - 寫 cache 時更新 fetched_start_date = min(old.fetched_start_date, new request start)
        """
        # Phase 3: 不同 dataset 用不同 cache file 名稱避免混
        cache_suffix = '.adj.json' if dataset == 'TaiwanStockPriceAdj' else '.json'
        cache_file = self.cache_dir / f'{stock_id}{cache_suffix}'

        # 1) cache 命中?
        if use_cache and cache_file.is_file():
            cache = self._load_cache(cache_file)
            if cache and self._cache_covers(cache, start_dt, end_dt):
                return self._slice_rows(cache['rows'], start_dt, end_dt)

        # 2) 抓 (用 user request 範圍, finmind 對上市前空段會自然回空 → merge 進 cache 不影響)
        rows = self.query(dataset, {
            'data_id': stock_id,
            'start_date': start_dt.strftime('%Y-%m-%d'),
            'end_date': end_dt.strftime('%Y-%m-%d'),
        })

        # 3) 寫 cache（合併:舊 cache + 新抓的 → 去重 + 同 date new 覆蓋 old）
        if use_cache:
            old_rows = []
            old_fetched_start: str | None = None
            if cache_file.is_file():
                old = self._load_cache(cache_file)
                if old:
                    old_rows = old.get('rows', [])
                    old_fetched_start = old.get('fetched_start_date')
            merged = self._merge_rows(old_rows, rows)
            # 追蹤「最遠已 fetch 過的 start_date」,給下次 _cache_covers 用
            new_fetched_start = start_dt.strftime('%Y-%m-%d')
            if old_fetched_start and old_fetched_start < new_fetched_start:
                new_fetched_start = old_fetched_start
            self._write_cache(cache_file, stock_id, merged, fetched_start_date=new_fetched_start)

        return rows

    # ────────── Cache 工具 ──────────
    @staticmethod
    def _merge_rows(old: list[dict], new: list[dict]) -> list[dict]:
        """去重 + 排序（按 date）

        合併策略(v3.0.2):
          - 兩個 list 都按 date 去重
          - 同 date 的 row 以 new 為主（剛抓的最準）
          - new 缺的 date 保留 old（old 可能有超出 request 範圍的歷史資料）
        """
        new_dates = {r['date'] for r in new if r.get('date')}
        by_date: dict[str, dict] = {}
        # 先放 old（new 缺的留著）
        for r in old:
            d = r.get('date')
            if not d or d in new_dates:
                continue  # new 會覆蓋,跳過避免 duplicate work
            by_date[d] = r
        # 再放 new（同 date 覆蓋）
        for r in new:
            d = r.get('date')
            if d:
                by_date[d] = r
        return sorted(by_date.values(), key=lambda x: x['date'])

    @staticmethod
    def _slice_rows(rows: list[dict], start_dt: datetime, end_dt: datetime) -> list[dict]:
        return [
            r for r in rows
            if start_dt <= datetime.strptime(r['date'], '%Y-%m-%d') <= end_dt
        ]

    @staticmethod
    def _cache_covers(cache: dict, start_dt: datetime, end_dt: datetime) -> bool:
        """Cache 是否涵蓋 [start_dt, end_dt] 範圍(v3.0.2 hotfix)

        語義(三條件同時成立才算 cache hit):
          1. TTL 未過期
          2. cache.last >= end_dt (資料夠新)
          3. fetched_start_date <= start_dt (已 fetch 過該段或更早,
             避免 cache.first=2003 但 user request=2000 時誤判「涵蓋 2000」)
        """
        try:
            ts = cache.get('fetched_at', 0)
            if time.time() - ts > PRICE_CACHE_TTL_SECONDS:
                return False
            rows = cache.get('rows', [])
            if not rows:
                return False
            last = datetime.strptime(rows[-1]['date'], '%Y-%m-%d')
            if last < end_dt:
                return False
            # 三條件:已 fetch 過 user.start 或更早
            # 若 cache 沒有 fetched_start_date(舊/v3.0.2 以前的 cache),
            # 保守假設「只 fetch 過 cache.first 以後」→ 以 cache.first 代替
            fetched_start = cache.get('fetched_start_date')
            if fetched_start is None:
                fetched_start = rows[0]['date']
            try:
                fsd = datetime.strptime(fetched_start, '%Y-%m-%d')
                if fsd > start_dt:
                    return False
            except (ValueError, TypeError):
                return False
            return True
        except (KeyError, ValueError, TypeError):
            return False

    @staticmethod
    def _load_cache(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _write_cache(
        path: Path,
        stock_id: str,
        rows: list[dict],
        fetched_start_date: str | None = None,
    ) -> None:
        payload = {
            'stock_id': stock_id,
            'fetched_at': time.time(),
            'fetched_at_iso': datetime.now().isoformat(timespec='seconds'),
            'fetched_start_date': fetched_start_date,
            'row_count': len(rows),
            'rows': rows,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    # ────────── HTTP ──────────
    def _fetch(self, url: str, params: dict) -> requests.Response:
        with self._lock:
            now_ms = int(time.monotonic() * 1000)
            gap = now_ms - self._last_call_ms
            if gap < self.rate_limit_ms:
                time.sleep((self.rate_limit_ms - gap) / 1000.0)
            self._last_call_ms = int(time.monotonic() * 1000)
        try:
            resp = self.session.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            raise FinMindError(f'Network error: {e}') from e
        if resp.status_code != 200:
            raise FinMindError(f'HTTP {resp.status_code}: {resp.text[:200]}')
        return resp
