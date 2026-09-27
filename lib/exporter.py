"""
Report Exporter
- HTML: Jinja2 模板（templates/report.html）
- 內含 SVG 折線圖（⑦ 歷史滾動 N 年收益分布）：純 Python 生成，
  不依賴 matplotlib / chart.js，self-contained。

不依賴 Flask，方便測試；吃 analyze 回傳的 result dict 直接輸出檔案
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

import pandas as pd

from .monthly_returns import compute_monthly_returns_by_ticker  # v3.0.3 N8 card ⑥
from .survival_chart import render_survival_curve_svg  # Phase 6 (Item 10) F2 存活率曲線
from .fan_chart import render_fan_chart_svg  # Phase 2E F1 fan chart

from jinja2 import Environment, FileSystemLoader, select_autoescape

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = _PROJECT_ROOT / 'templates'


# ───────── HTML ─────────
def _safe_pct(value, default='—'):
    """處理 None/NaN 值的安全格式化: 是 None 或非有限數 → 回傳預設值，否則乘 100"""
    if value is None:
        return default
    try:
        import math
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return default
        return float(value) * 100
    except (TypeError, ValueError):
        return default


def _safe_float(value, default='—'):
    """處理 None/NaN 值的安全格式化: 是 None 或非有限數 → 回傳預設值，否則轉 float"""
    if value is None:
        return default
    try:
        import math
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(['html', 'xml']),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters['safe_pct'] = _safe_pct
    env.filters['safe_float'] = _safe_float
    # 新增兩個「會自己處理格式」的 filter,給 template 直接拿字串 (避免
    # 拿到 None/NaN 後 '%.2f'|format 噴 TypeError: must be real number, not str)
    env.filters['fmt_pct'] = _fmt_pct
    env.filters['fmt_float'] = _fmt_float
    env.filters['fmt_money'] = _fmt_money
    # v3.1.2: 此次權重標註 filter（給 report.html 用）
    env.filters['weights_display'] = _weights_display
    return env


def _weights_display(analyze) -> str:
    """v3.1.2: 格式化「此次權重分配結果」。

    規則：
    - 權重由大到小排序
    - 去掉最小一筆（>= 2 才去）→ 避免 9 個 0.XXX 四捨五入累加成 1.0001
    - 格式：TICKER:0.XXX,TICKER:0.XXX,...

    回傳：例如「（市值加權總計為1） 2330:0.329, 2885:0.271, 2412:0.186」
    """
    ew = (analyze or {}).get('effective_weights') or {}
    src = (analyze or {}).get('weights_source') or 'unknown'
    src_map = {
        'user': '使用者自訂',
        'market_cap': '市值加權總計為1',
        'equal': '等權重（fallback）',
    }
    src_text = src_map.get(src, src)
    if not ew:
        return f'（{src_text}） —'
    sorted_w = sorted(ew.items(), key=lambda x: x[1], reverse=True)
    kept = sorted_w[:-1] if len(sorted_w) > 1 else sorted_w
    items = ', '.join(f'{t}:{w:.3f}' for t, w in kept)
    return f'（{src_text}） {items}' if items else f'（{src_text}） —'


def _fmt_pct(value, default='—', decimals=2):
    """值 × 100 後格式化為百分比字串。None / NaN / 非數值 → 回傳 default(預設 '—')。

    等同 ``safe_pct + format('%.{decimals}f')`` 兩步,但不會在 None 時炸
    ``TypeError: must be real number, not str``(2026-08-27 elhomeo_stock bug 根因)。

    用法::

        {{ bm.metrics.volatility | fmt_pct }}      → '18.00' 或 '—'
        {{ ratio | fmt_pct(decimals=1) }}           → '23.4' 或 '—'
    """
    if value is None:
        return default
    try:
        import math
        v = float(value) * 100
        if math.isnan(v) or math.isinf(v):
            return default
        return f'{v:.{int(decimals)}f}'
    except (TypeError, ValueError):
        return default


def _fmt_float(value, default='—', decimals=3):
    """float 格式化。None / NaN / 非數值 → 回傳 default(預設 '—')。

    用法::

        {{ m.sharpe | fmt_float }}       → '0.420' 或 '—'
        {{ x | fmt_float(decimals=4) }}   → '0.0012' 或 '—'
    """
    if value is None:
        return default
    try:
        import math
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return f'{v:.{int(decimals)}f}'
    except (TypeError, ValueError):
        return default


def _fmt_money(value, default='—', decimals=0):
    """NT$ 整數格式化（含千分逗點）。None / NaN / 非數值 → 回傳 default。

    用法::

        {{ mcs.median_final | fmt_money }}      → '4,201,302' 或 '—'
        {{ v | fmt_money(decimals=1) }}          → '1,234,567.8' 或 '—'
    """
    if value is None:
        return default
    try:
        import math
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return f'{v:,.{int(decimals)}f}'
    except (TypeError, ValueError):
        return default


def render_html_report(analyze: dict, profile_name: str = '') -> str:
    """
    將 analyze 結果（{common, dynamic, full, forecast, ...}）轉成 HTML 字串。
    模板：templates/report.html
    """
    env = _env()
    tpl = env.get_template('report.html')
    # Phase 2E: 預渲染 F1 fan chart SVG (避免在 template 內呼顓 Python)
    mc = analyze.get('monte_carlo') or {}
    pct_bands = mc.get('percentile_bands') or []
    initial_for_chart = (
        (mc.get('config') or {}).get('initial_balance')
        if isinstance(mc.get('config'), dict)
        else None
    )
    fan_chart_svg = render_fan_chart_svg(pct_bands, initial_balance=initial_for_chart)
    # Phase 6 (Item 10): F2 存活率曲線 SVG
    sr = analyze.get('sequence_risk') or {}
    survival_curve_svg = render_survival_curve_svg(
        sr.get('success_rate_by_age') or {},
        sr.get('config') or {},
    )
    ruin_age_chart_svg = _render_ruin_age_chart_svg(sr)
    monthly_stats = _get_monthly_tickers(analyze)
    return tpl.render(
        analyze=analyze,
        profile_name=profile_name,
        generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        rolling_chart_svg=_render_rolling_chart_svg(analyze.get('forecast') or {}),
        fan_chart_svg=fan_chart_svg,        # Phase 2E
        survival_curve_svg=survival_curve_svg,  # Phase 6 (Item 10)
        ruin_age_chart_svg=ruin_age_chart_svg,
        monthly_stats=monthly_stats,
        monthly_chart_svg=_render_monthly_chart_svg(monthly_stats),
    )


def render_rebalance_report(analyze: dict, profile_name: str = '') -> str:
    """Render a standalone rebalance report from an existing analyze result.

    The result is intentionally passed through unchanged: the rebalance report
    is a second presentation of the already calculated source dataset, never a
    second data fetch or an estimate from the forecast HTML.
    """
    env = _env()
    tpl = env.get_template('rebalance_report.html')
    return tpl.render(
        analyze=analyze,
        opt=analyze.get('optimization') or {},
        profile_name=profile_name,
        generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    )


def _get_monthly_tickers(analyze: dict) -> dict:
    """v3.0.4 P0 fix: 月報表走 fresh-start-per-month shares tracking。

    優先讀 analyze.monthly_tickers(_run_analyze 已預算好),
    缺資料時 fallback 到 daily_returns_by_ticker + compute_monthly_returns_by_ticker
    (舊路徑,僅供舊 analyze 結構或單元測試使用)

    Returns:
        dict: {
          'tickers': list[ticker_dict],
          'n': int,
          'actual_start': str 'YYYY-MM-DD',
          'actual_end': str 'YYYY-MM-DD',
          'actual_years': float,
        }
    """
    n_years = int((analyze.get('forecast') or {}).get('n') or 0)

    # 新路徑(主人 2026-09-03 P0 fix)
    pre = analyze.get('monthly_tickers')
    if pre is not None:
        # 順手算 actual_start/end/years 給 template
        actual_dates: list = []
        for tk in pre:
            for y, m in (tk.get('data') or {}).items():
                if y == 'year_avg':
                    continue
                for k, v in m.items():
                    if k == 'year_avg':
                        continue
                    # data 裡只有月報酬,沒有日期字串,從 first_year/last_year 推
        first_year = min((tk['first_year'] for tk in pre), default=None)
        last_year = max((tk['last_year'] for tk in pre), default=None)
        if first_year is not None:
            actual_start = f'{first_year}-01-01'
            actual_end = f'{last_year}-12-31' if last_year else '—'
            actual_years = float(max(last_year - first_year + 1, 1)) if last_year else 0.0
        else:
            actual_start = actual_end = '—'
            actual_years = 0.0
        return {
            'tickers': pre,
            'n': n_years,
            'actual_start': actual_start,
            'actual_end': actual_end,
            'actual_years': actual_years,
        }

    # 舊路徑 fallback
    daily_rets_raw = (analyze.get('meta') or {}).get('daily_returns_by_ticker') or {}
    if not daily_rets_raw:
        return {'tickers': [], 'n': n_years,
                'actual_start': '—', 'actual_end': '—', 'actual_years': 0.0}

    # Phase 6 (Item 1): 決定 N 年 cutoff（以「最晚資料日」往回推 N 年）
    cut_off = None
    all_dates = []
    for rows in daily_rets_raw.values():
        for r in rows:
            d = r.get('date')
            if d:
                all_dates.append(d)
    if all_dates:
        max_date = pd.Timestamp(max(all_dates))
        if n_years > 0:
            cut_off = max_date - pd.DateOffset(years=n_years)

    daily_rets: dict[str, pd.Series] = {}
    for ticker, rows in daily_rets_raw.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'])
        if cut_off is not None:
            df = df[df['date'] >= cut_off]
        if df.empty:
            continue
        s = pd.Series(df['ret'].values, index=df['date'].values, name=ticker).sort_index()
        daily_rets[ticker] = s

    actual_dates = []
    for s in daily_rets.values():
        actual_dates.extend(s.index.tolist())
    if actual_dates:
        actual_start_ts = min(actual_dates)
        actual_end_ts = max(actual_dates)
        actual_start = actual_start_ts.strftime('%Y-%m-%d')
        actual_end = actual_end_ts.strftime('%Y-%m-%d')
        actual_years = round((actual_end_ts - actual_start_ts).days / 365.25, 2)
    else:
        actual_start = actual_end = '—'
        actual_years = 0.0

    tickers = (compute_monthly_returns_by_ticker(daily_rets).get('tickers', [])
               if daily_rets else [])
    return {
        'tickers': tickers,
        'n': n_years,
        'actual_start': actual_start,
        'actual_end': actual_end,
        'actual_years': actual_years,
    }



def save_html_report(analyze: dict, out_path: Path, profile_name: str = '') -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html_report(analyze, profile_name), encoding='utf-8')
    return out_path


# ───────── ⑦ 滾動 N 年收益分布圖（純 SVG）─────────
def _render_rolling_chart_svg(forecast: dict, width: int = 720, height: int = 280) -> str:
    """
    把 forecast.rolling（list of {start, end, years, cagr}）畫成 SVG 折線圖，
    再疊上 5 條水平分位線（Bear / Conservative / Base / Optimistic / Bull）。

    - 純 Python，無第三方依賴（matplotlib / chart.js 都不必裝）
    - self-contained: 所有樣式 inline
    - 顏色/虛線 與 web UI Chart.js 配色對齊（GitHub dark 配色系）
    """
    rolling = forecast.get('rolling') or []
    scenarios = forecast.get('scenarios') or [
        {'label': label, 'cagr': value}
        for label, value in (forecast.get('percentiles') or {}).items()
    ]
    if not rolling or not scenarios:
        return ''

    # 取 X/Y 範圍 (跳過 None 值以避免 TypeError)
    cagrs_pct = [r['cagr'] * 100 for r in rolling if r.get('cagr') is not None]
    pct_band = [s['cagr'] * 100 for s in scenarios if s.get('cagr') is not None]
    if not cagrs_pct or not pct_band:
        return ''
    y_min = min(cagrs_pct + pct_band)
    y_max = max(cagrs_pct + pct_band)
    span = max(y_max - y_min, 0.5)  # 避免全相等時除以 0
    y_min -= span * 0.08
    y_max += span * 0.08

    margin_l, margin_r, margin_t, margin_b = 56, 24, 16, 36
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    def x_of(i: int) -> float:
        if len(rolling) <= 1:
            return margin_l + plot_w / 2
        return margin_l + (i / (len(rolling) - 1)) * plot_w

    def y_of(v: float) -> float:
        return margin_t + (1 - (v - y_min) / (y_max - y_min)) * plot_h

    parts: list[str] = []

    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="100%" height="auto" '
        f'style="font-family:Arial,Microsoft JhengHei,sans-serif;font-size:11px">'
    )

    # 座標軸背景
    parts.append(
        f'<rect x="{margin_l}" y="{margin_t}" width="{plot_w}" height="{plot_h}" '
        f'fill="#f8fafc" stroke="#cbd5e1" stroke-width="1"/>'
    )
    # Y 格線 + 標籤
    n_ticks = 5
    for k in range(n_ticks + 1):
        yv = y_min + (y_max - y_min) * k / n_ticks
        y = y_of(yv)
        parts.append(
            f'<line x1="{margin_l}" y1="{y:.2f}" x2="{margin_l + plot_w}" y2="{y:.2f}" '
            f'stroke="#e2e8f0" stroke-width="0.5"/>'
        )
        parts.append(
            f'<text x="{margin_l - 6:.2f}" y="{y + 4:.2f}" text-anchor="end" '
            f'fill="#475569">{yv:+.2f}%</text>'
        )

    # X 軸標籤（最多 6 個）。使用等距索引但以 round 取樣，確保最後
    # 一個 tick 必定對齊最後一筆有效 rolling sample，而不是落在倒數
    # 第二筆（舊版 floor step 會顯示 2026-07-28 而資料最後是 07-31）。
    n_x_ticks = min(6, len(rolling))
    if n_x_ticks > 0:
        for k in range(n_x_ticks):
            idx = round(k * (len(rolling) - 1) / (n_x_ticks - 1)) if n_x_ticks > 1 else 0
            x = x_of(idx)
            label = str(rolling[idx]["end"])
            # Dates are easier to scan when the year is not repeated on every
            # tick, while the endpoints retain the full ISO date for auditability.
            if 0 < k < n_x_ticks - 1 and len(label) >= 7:
                label = label[:7]
            parts.append(
                f'<text x="{x:.2f}" y="{margin_t + plot_h + 16:.2f}" text-anchor="middle" '
                f'fill="#475569">{html.escape(label)}</text>'
            )

    # 5 條水平分位線（虛線）
    pct_colors = {
        'Bear':         '#f85149',  # P10 紅
        'Conservative': '#d29922',  # P25 橘
        'Base':         '#58a6ff',  # P50 藍
        'Optimistic':   '#3fb950',  # P75 �
        'Bull':         '#8957e5',  # P90 紫
    }
    for s in scenarios:
        label = s.get('label') or s.get('scenario', '—')
        # 跳過 cagr 為 None 的 scenario
        if s.get('cagr') is None:
            continue
        yv = s['cagr'] * 100
        y = y_of(yv)
        color = pct_colors.get(label, '#888')
        parts.append(
            f'<line x1="{margin_l}" y1="{y:.2f}" x2="{margin_l + plot_w}" y2="{y:.2f}" '
            f'stroke="{color}" stroke-width="1.2" stroke-dasharray="5,4" opacity="0.85"/>'
        )
        # 行尾 label: 情境名 + 分位 + CAGR
        q = int((s.get('quantile') or s.get('percentile') or 0) * 100)
        tag = f'{label} P{q} {yv:+.2f}%'
        parts.append(
            f'<rect x="{margin_l + plot_w - 118:.2f}" y="{y - 9:.2f}" width="116" height="14" '
            f'fill="white" opacity="0.9" rx="2"/>'
        )
        parts.append(
            f'<text x="{margin_l + plot_w - 4:.2f}" y="{y + 3:.2f}" text-anchor="end" '
            f'fill="{color}" font-weight="bold">{html.escape(tag)}</text>'
        )

    # 主折線（滾動 CAGR）
    pts: list[str] = []
    for i, r in enumerate(rolling):
        # 跳過 cagr 為 None 的點
        if r.get('cagr') is None:
            continue
        pts.append(f'{x_of(i):.2f},{y_of(r["cagr"] * 100):.2f}')
    if pts:
        parts.append(
            f'<polyline fill="none" stroke="#58a6ff" stroke-width="1.6" '
            f'points="{" ".join(pts)}"/>'
        )

    # 右上小字（N / 樣本數）
    n = forecast.get('n', '—')
    rc = len(rolling)
    parts.append(
        f'<text x="{margin_l + plot_w:.2f}" y="{margin_t + 10:.2f}" text-anchor="end" '
        f'fill="#64748b">N = {n} 年　樣本數 = {rc}</text>'
    )

    # Y 軸標題（旋轉）
    parts.append(
        f'<text x="{margin_l - 44}" y="{margin_t + plot_h / 2:.2f}" '
        f'transform="rotate(-90 {margin_l - 44},{margin_t + plot_h / 2:.2f})" '
        f'text-anchor="middle" fill="#64748b">年化報酬率（CAGR）</text>'
    )
    parts.append('</svg>')
    return ''.join(parts)


def _render_ruin_age_chart_svg(sequence_risk: dict, width: int = 720, height: int = 260) -> str:
    """Render a vertical histogram of bankruptcy ages from simulated paths."""
    ages = [int(age) for age in (sequence_risk.get('ruin_age_distribution') or [])]
    if not ages:
        return ''
    counts = {}
    for age in ages:
        counts[age] = counts.get(age, 0) + 1
    items = sorted(counts.items())
    ml, mr, mt, mb = 56, 24, 24, 42
    pw, ph = width - ml - mr, height - mt - mb
    max_count = max(counts.values())
    gap = 5
    bar_w = max(8, (pw - gap * (len(items) - 1)) / len(items))
    def x(i): return ml + i * (bar_w + gap)
    def y(v): return mt + ph - (v / max_count) * ph
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" height="auto" style="font-family:Arial,Microsoft JhengHei,sans-serif;font-size:11px">', f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="#f8fafc" stroke="#cbd5e1"/>']
    for tick in range(0, max_count + 1, max(1, (max_count + 4) // 5)):
        parts += [f'<line x1="{ml}" y1="{y(tick):.1f}" x2="{ml+pw}" y2="{y(tick):.1f}" stroke="#e2e8f0"/>', f'<text x="{ml-6}" y="{y(tick)+4:.1f}" text-anchor="end" fill="#475569">{tick}</text>']
    for i, (age, count) in enumerate(items):
        bx, by = x(i), y(count)
        parts += [f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{mt+ph-by:.1f}" fill="#b42318" opacity="0.82" rx="2"/>', f'<text x="{bx+bar_w/2:.1f}" y="{by-5:.1f}" text-anchor="middle" fill="#7f1d1d" font-weight="bold">{count}</text>', f'<text x="{bx+bar_w/2:.1f}" y="{height-22}" text-anchor="middle" fill="#475569">{age}</text>']
    parts.append(f'<text x="{ml+pw/2:.1f}" y="{height-6}" text-anchor="middle" fill="#475569">破產年齡（歲）</text>')
    parts.append(f'<text x="14" y="{mt+ph/2:.1f}" text-anchor="middle" fill="#64748b" transform="rotate(-90,14,{mt+ph/2:.1f})">破產路徑數</text></svg>')
    return ''.join(parts)


def _render_monthly_chart_svg(monthly_stats: dict, width: int = 720, height: int = 280) -> str:
    """Render cumulative return curves from the exact N-year monthly table data."""
    tickers = monthly_stats.get('tickers') or []
    n_years = int(monthly_stats.get('n') or 0)
    required_months = n_years * 12
    series = []
    for ticker in tickers:
        values = []
        for year in sorted(ticker.get('data', {})):
            for month in range(1, 13):
                value = ticker['data'][year].get(str(month))
                if value is not None:
                    values.append((f'{year}-{month:02d}', float(value)))
        complete_n_years = True
        if required_months > 0:
            if len(values) < required_months:
                complete_n_years = False
            else:
                first_year, first_month = map(int, values[0][0].split('-'))
                last_year, last_month = map(int, values[-1][0].split('-'))
                span_months = (last_year - first_year) * 12 + last_month - first_month + 1
                month_numbers = [(int(label[:4]) * 12 + int(label[5:7])) for label, _ in values]
                complete_n_years = (
                    span_months >= required_months
                    and len(month_numbers) == span_months
                    and len(set(month_numbers)) == span_months
                )
        if values and complete_n_years:
            level = 1.0
            points = []
            for label, value in values:
                level *= 1.0 + value
                points.append((label, level - 1.0))
            series.append((ticker['ticker'], points))
    if not series:
        return ''
    # Keep the detailed table complete, but limit the chart to the five
    # strongest end-of-period cumulative returns among complete N-year series.
    series.sort(key=lambda item: item[1][-1][1], reverse=True)
    series = series[:5]
    all_values = [v for _, points in series for _, v in points]
    lo, hi = min(0.0, min(all_values)), max(0.0, max(all_values))
    pad = max((hi - lo) * 0.08, 0.02)
    lo, hi = lo - pad, hi + pad
    # Reserve two legend rows above the plot; otherwise tickers 4–5 are
    # rendered outside the SVG viewBox and appear to be missing.
    ml, mr, mt, mb = 58, 24, 42, 42
    pw, ph = width - ml - mr, height - mt - mb
    max_len = max(len(points) for _, points in series)
    def x(i): return ml + (i / max(max_len - 1, 1)) * pw
    def y(v): return mt + (1 - (v - lo) / (hi - lo)) * ph
    colors = ['#2563eb', '#16a34a', '#d97706', '#9333ea', '#dc2626', '#0891b2']
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" height="auto" style="font-family:Arial,Microsoft JhengHei,sans-serif;font-size:11px">', f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="#f8fafc" stroke="#cbd5e1"/>']
    for i in range(5):
        value = lo + (hi - lo) * i / 4
        parts += [f'<line x1="{ml}" y1="{y(value):.1f}" x2="{ml+pw}" y2="{y(value):.1f}" stroke="#e2e8f0"/>', f'<text x="{ml-6}" y="{y(value)+4:.1f}" text-anchor="end" fill="#475569">{value*100:+.0f}%</text>']
    for k, (ticker, points) in enumerate(series):
        parts.append(f'<polyline fill="none" stroke="{colors[k % len(colors)]}" stroke-width="2" points="{" ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(points))}"/>')
        parts.append(f'<text x="{ml + 8 + (k % 3) * 105}" y="{mt - 7 - (k // 3) * 13}" fill="{colors[k % len(colors)]}" font-weight="bold">{html.escape(ticker)}</text>')
    labels = series[0][1]
    for i in [0, round((len(labels)-1)/2), len(labels)-1]:
        parts.append(f'<text x="{x(i):.1f}" y="{height-20}" text-anchor="middle" fill="#475569">{html.escape(labels[i][0])}</text>')
    parts.append(f'<text x="{ml+pw/2:.1f}" y="{height-5}" text-anchor="middle" fill="#475569">月份（N={monthly_stats.get("n", "—")} 年）</text></svg>')
    return ''.join(parts)
    parts.append(
        f'<text x="{margin_l + plot_w / 2:.2f}" y="{height - 6:.2f}" '
        f'text-anchor="middle" fill="#64748b">有效 rolling 視窗結束日（N={html.escape(str(n))} 年）</text>'
    )

    parts.append('</svg>')
    return ''.join(parts)
