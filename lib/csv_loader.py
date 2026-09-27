"""
Portfolio CSV Loader
讀取 (Ticker, Shares) 格式的用戶名單 CSV

支援：
- 欄位名：Ticker / 代號 / 股票代號 / Code / Symbol / Stock
- 股數欄位：Shares / 股數 / 張數 / Quantity / Qty
- 股數字串帶千分位逗號（"21,315" → 21315）
- 無 header（僅兩欄）
- 副檔名 .csv
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Holding:
    ticker: str
    shares: int


class CSVLintError(ValueError):
    """CSV 格式錯誤（給前端明確訊息）"""


# 常見欄位別名（lowercase 比對）
TICKER_KEYS = {'ticker', 'symbol', 'code', 'stock', 'stock_id', '代號', '股票代號', '股票代碼', '標的'}
SHARES_KEYS = {'shares', 'qty', 'quantity', 'units', '股數', '張數', '持股', '單位', '庫存'}


def _norm_key(k: str) -> str:
    return re.sub(r'\s+', '', k.strip().lower())


def _to_int_shares(raw: str) -> int:
    """支援 "21,315" / "21315" / 21315.0 / 2.5（張 → 股 若合理）"""
    s = raw.strip().replace(',', '').replace(' ', '')
    if not s:
        raise CSVLintError('股數為空')
    try:
        v = float(s)
    except ValueError:
        raise CSVLintError(f'股數不可解析: {raw!r}')
    # 整數（含小數 .0）或 5+ 位數當作「股」
    if v.is_integer():
        return int(v)
    # 帶小數且 < 100 → 視為「張」，轉股（1 張 = 1000 股）
    if v < 100:
        return int(round(v * 1000))
    raise CSVLintError(f'股數看起來不對: {raw!r}')


def load_portfolio_csv(file: IO[str] | str | Path) -> list[Holding]:
    """
    解析用戶名單 CSV。回傳 list[Holding]。
    - str  → 視為 CSV 內容
    - Path → 讀成檔案
    - IO   → 視為 file-like
    Raises:
        CSVLintError: 格式錯誤
    """
    if isinstance(file, Path):
        text = file.read_text(encoding='utf-8-sig')
    elif hasattr(file, 'read'):
        text = file.read()
    else:
        text = str(file)

    if not text.strip():
        raise CSVLintError('CSV 是空的')

    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r and any(c.strip() for c in r)]
    if not rows:
        raise CSVLintError('CSV 是空的')

    first = rows[0]
    # 判斷是否有 header：第二行起是純數字 / 沒對應 ticker 別名 → 視為無 header
    normed = [_norm_key(c) for c in first]
    has_header = any(k in TICKER_KEYS for k in normed) or any(k in SHARES_KEYS for k in normed)

    if has_header:
        tk_idx, sh_idx = None, None
        for i, k in enumerate(normed):
            if k in TICKER_KEYS and tk_idx is None:
                tk_idx = i
            if k in SHARES_KEYS and sh_idx is None:
                sh_idx = i
        if tk_idx is None:
            raise CSVLintError(f'找不到 Ticker 欄位（支援：{", ".join(sorted(TICKER_KEYS))}）')
        if sh_idx is None:
            raise CSVLintError(f'找不到 Shares 欄位（支援：{", ".join(sorted(SHARES_KEYS))}）')
        data_rows = rows[1:]
    else:
        # 沒有 header：兩欄 = (Ticker, Shares)
        if len(first) < 2:
            raise CSVLintError('無 header CSV 必須至少兩欄 (Ticker, Shares)')
        tk_idx, sh_idx = 0, 1
        data_rows = rows

    holdings: list[Holding] = []
    seen: set[str] = set()
    for line_no, row in enumerate(data_rows, start=2 if has_header else 1):
        ticker_raw = row[tk_idx].strip().strip('"').strip() if len(row) > tk_idx else ''
        shares_raw = row[sh_idx].strip() if len(row) > sh_idx else ''
        # 只跳過完全空白行（不計入錯誤）
        if not ticker_raw and not shares_raw:
            continue
        # 有 ticker 但缺 shares 欄 → 視為漏了逗號
        if ticker_raw and not shares_raw:
            raise CSVLintError(
                f'第 {line_no} 行：只有一欄 {ticker_raw!r}，少了一個逗號？'
                f'正確格式：「ticker,shares」例如 2881,745'
            )
        # ticker 太長 / 含不合法字元 → 視為漏了逗號
        if ticker_raw and (len(ticker_raw) > 6 or not re.fullmatch(r'[0-9A-Za-z]+', ticker_raw)):
            raise CSVLintError(
                f'第 {line_no} 行：ticker {ticker_raw!r} 不合法 '
                f'（台股代號 ≤6 碼、英數字組成），是否漏了逗號？'
            )
        try:
            shares = _to_int_shares(shares_raw)
        except CSVLintError as e:
            raise CSVLintError(f'第 {line_no} 行：{e}') from None
        if shares <= 0:
            continue
        ticker = ticker_raw
        if ticker in seen:
            # 去重：同 ticker 累加
            for i, h in enumerate(holdings):
                if h.ticker == ticker:
                    holdings[i] = Holding(ticker, h.shares + shares)
                    break
        else:
            seen.add(ticker)
            holdings.append(Holding(ticker, shares))

    if not holdings:
        raise CSVLintError('CSV 沒有有效資料')
    return holdings


def list_profile_csvs(profile_dir: Path) -> list[str]:
    """列出 user_profile/*.csv 檔名（不含副檔名）"""
    if not profile_dir.is_dir():
        return []
    return sorted(p.stem for p in profile_dir.glob('*.csv') if p.is_file())


# ───────── v3.0.3: Ticker 標準化（normalize_profile_csv） ─────────
@dataclass(frozen=True)
class NormalizeResult:
    """normalize_profile_csv 回傳值
    - applied: True 表示有改寫並寫入檔案
    - changes: list of {line, from, to, name}，每個有改的 ticker
    - failed:  list of {line, ticker, reason}，找不到的 ticker(任一失敗 → 整批 abort)
    """
    applied: bool
    changes: list[dict]
    failed: list[dict]

    def to_dict(self) -> dict:
        return {
            'applied': self.applied,
            'changes': self.changes,
            'failed': self.failed,
        }


def _parse_csv_preserving_lines(text: str) -> tuple[list[str], list[dict], bool]:
    """讀 CSV 保留 raw lines,同時 parse 出每個 row 的 line_no / ticker_raw / shares_raw

    Args:
        text: 完整 CSV 文字內容

    Returns:
        (raw_lines, parsed_rows, has_header)
        - raw_lines: list of lines (含換行字元)
        - parsed_rows: list of {'line_no', 'tk_idx', 'ticker_raw', 'shares_raw', 'is_header'}
        - has_header: 是否有 header

    Raises:
        CSVLintError: CSV 格式錯誤(透傳自既有 parser 邏輯)
    """
    # 偵測行尾
    if '\r\n' in text:
        newline = '\r\n'
        lines = text.split('\r\n')
    else:
        newline = '\n'
        lines = text.split('\n')
    # 注意:split 會在最後多一個空字串(若檔案以換行結尾)
    # 為簡化,保留但標記

    try:
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader if r and any(c.strip() for c in r)]
    except csv.Error as e:
        # v3.0.4 fix: 某些編輯器(Mac 舊式 / Windows 上某些工具) 寫出
        # `\\r\\r` 或純 `\\r` 之類怪行尾 → csv.reader 在 unquoted field 看到
        # newline character 會 raise。若是這種 case, 把所有 CR-style 行尾
        # 統一成 `\\n` 重試一次。
        if 'new-line character' in str(e):
            logger.warning('CSV 偵測到異常行尾, 已 normalize 為 \\n')
            sanitized = re.sub(r'\r\n|\r\r|\r', '\n', text)
            lines = sanitized.split('\n')
            newline = '\n'
            reader = csv.reader(io.StringIO(sanitized))
            rows = [r for r in reader if r and any(c.strip() for c in r)]
        else:
            raise
    if not rows:
        raise CSVLintError('CSV 是空的')

    first = rows[0]
    normed = [_norm_key(c) for c in first]
    has_header = any(k in TICKER_KEYS for k in normed) or any(k in SHARES_KEYS for k in normed)

    if has_header:
        tk_idx, sh_idx = None, None
        for i, k in enumerate(normed):
            if k in TICKER_KEYS and tk_idx is None:
                tk_idx = i
            if k in SHARES_KEYS and sh_idx is None:
                sh_idx = i
        if tk_idx is None:
            raise CSVLintError(f'找不到 Ticker 欄位（支援：{", ".join(sorted(TICKER_KEYS))}）')
        if sh_idx is None:
            raise CSVLintError(f'找不到 Shares 欄位（支援：{", ".join(sorted(SHARES_KEYS))}）')
        data_rows = rows[1:]
        line_offset = 2  # header 是 line 1, data 從 line 2 開始
    else:
        if len(first) < 2:
            raise CSVLintError('無 header CSV 必須至少兩欄 (Ticker, Shares)')
        tk_idx, sh_idx = 0, 1
        data_rows = rows
        line_offset = 1  # data 從 line 1 開始

    parsed_rows = []
    for i, row in enumerate(data_rows):
        line_no = line_offset + i
        ticker_raw = row[tk_idx].strip().strip('"').strip() if len(row) > tk_idx else ''
        shares_raw = row[sh_idx].strip() if len(row) > sh_idx else ''
        parsed_rows.append({
            'line_no': line_no,
            'tk_idx': tk_idx,
            'ticker_raw': ticker_raw,
            'shares_raw': shares_raw,
            'is_header': False,
        })
    if has_header:
        # header 也加進去(讓替換邏輯可以跳過)
        parsed_rows.insert(0, {
            'line_no': 1,
            'tk_idx': tk_idx,
            'ticker_raw': first[tk_idx] if tk_idx < len(first) else '',
            'shares_raw': first[sh_idx] if sh_idx < len(first) else '',
            'is_header': True,
        })
    return lines, parsed_rows, has_header


def _replace_ticker_in_line(line: str, tk_idx: int, old_ticker: str, new_ticker: str) -> str:
    """Replace ticker at column tk_idx in a CSV line, preserving formatting.

    用 csv reader/writer 確保 quoting 正確。如果原本 ticker 是裸值(如 50),
    新 ticker 也是裸值(如 0050),輸出就是裸值。如果原本有 quotes,csv.writer
    會自動處理(只在需要時加 quotes)。
    """
    # 去掉行尾(讀進來時可能帶 \n 或 \r\n)
    stripped = line.rstrip('\r\n')

    reader = csv.reader(io.StringIO(stripped))
    try:
        fields = next(reader)
    except StopIteration:
        return line
    if len(fields) <= tk_idx:
        return line
    if fields[tk_idx] != old_ticker:
        # 內容不一致 → 不動(安全檢查)
        return line
    fields[tk_idx] = new_ticker

    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(fields)
    # csv.writer 預設會加 \r\n,我們不要這個(eol 由 caller 控管)
    return out.getvalue().rstrip('\r\n')


def normalize_profile_csv(
    path: Path,
    *,
    client=None,
) -> NormalizeResult:
    """v3.0.3:讀 CSV → 對 FinMind match → 若需改寫則 atomic 寫回。

    Args:
        path: user_profile/<name>.csv 路徑
        client: FinMindClient 實例(None 會自動 new 一個,測試可注入 mock)

    Returns:
        NormalizeResult(applied, changes, failed)

    語意:
    - 任一 ticker 對不上 → applied=False,failed 列出來,**檔案不動**
    - 全部對得上但已 canonical → applied=False,changes=[],**檔案不動**(冪等)
    - 有 ticker 從 X 變 Y → applied=True,changes 列出每個,**檔案 atomic 寫回**
    """
    # 1) 讀檔 + parse
    if not path.is_file():
        raise CSVLintError(f'檔案不存在: {path}')

    raw_bytes = path.read_bytes()
    text = raw_bytes.decode('utf-8-sig')  # 去掉 BOM

    lines, parsed_rows, _ = _parse_csv_preserving_lines(text)
    if not parsed_rows:
        raise CSVLintError('CSV 沒有有效資料')

    # 2) 取 unique tickers (跳過 header / 空 ticker)
    data_rows = [r for r in parsed_rows if not r['is_header'] and r['ticker_raw']]
    unique_tickers = []
    seen = set()
    for r in data_rows:
        t = r['ticker_raw']
        if t and t not in seen:
            seen.add(t)
            unique_tickers.append(t)

    # 3) match all tickers at once
    if client is None:
        from lib.finmind import FinMindClient
        client = FinMindClient()
    matched = client.match_tickers_batch(unique_tickers)

    # 4) 比對每個 row
    changes: list[dict] = []
    failed: list[dict] = []
    line_replacements: dict[int, str] = {}  # line_no → new line text

    for r in data_rows:
        original = r['ticker_raw']
        m = matched.get(original)
        if m is None:
            failed.append({
                'line': r['line_no'],
                'ticker': original,
                'reason': 'FinMind 查無此代號',
            })
            continue
        canonical = m['stock_id']
        if canonical != original:
            # 這行需要改
            if r['line_no'] < 1 or r['line_no'] > len(lines):
                continue
            old_line = lines[r['line_no'] - 1]
            new_line = _replace_ticker_in_line(old_line, r['tk_idx'], original, canonical)
            line_replacements[r['line_no']] = new_line
            changes.append({
                'line': r['line_no'],
                'from': original,
                'to': canonical,
                'name': m.get('stock_name', ''),
            })

    # 5) 失敗 → 不寫
    if failed:
        return NormalizeResult(applied=False, changes=changes, failed=failed)

    # 6) 沒改動 → 冪等
    if not line_replacements:
        return NormalizeResult(applied=False, changes=[], failed=[])

    # 7) Atomic 寫回
    # 注意:lines 是 text.split(eol) 的結果,每個元素都不含 eol。
    #    重建時需要按原檔案的 eol 重新連接,並保留是否結尾有 eol。
    ends_with_newline = text.endswith('\n')
    if '\r\n' in text:
        eol = '\r\n'
    else:
        eol = '\n'

    new_lines = list(lines)
    for line_no, new_line in line_replacements.items():
        new_lines[line_no - 1] = new_line

    new_text = eol.join(new_lines)
    if ends_with_newline and new_text and not new_text.endswith('\n'):
        new_text += eol
    new_bytes = new_text.encode('utf-8-sig')

    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_bytes(new_bytes)
    tmp.replace(path)

    return NormalizeResult(applied=True, changes=changes, failed=[])
