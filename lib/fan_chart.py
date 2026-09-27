"""
lib/fan_chart.py
- 把 MonteCarloResult.percentile_bands (P5/P25/P50/P75/P95 per year)
  渲染成 SVG fan chart
- 用途:F1 區塊的視覺化補充,讓使用者一眼看出「未來資產可能分布」
"""
from __future__ import annotations

# ─────── 設計 ───────
# 圖大小固定 (因為嵌入 inline HTML);內容用 viewBox 縮放
WIDTH = 760
HEIGHT = 280
MARGIN_LEFT = 70
MARGIN_RIGHT = 30
MARGIN_TOP = 20
MARGIN_BOTTOM = 40

# 配色 (符合既有 CSS palette)
COLOR_P5_P95 = '#fbe5e5'    # 最外帶
COLOR_P25_P75 = '#f7c8c8'   # 中間帶
COLOR_P50 = '#b42318'       # 中位線
COLOR_AXIS = '#5a6a7e'
COLOR_GRID = '#e3e8ef'


def render_fan_chart_svg(percentile_bands: list[dict], initial_balance: float | None = None) -> str:
    """Fan chart SVG

    Args:
        percentile_bands: list of {'percentile': int, 'year': int, 'value': float}
                          必須包含 P5/P25/P50/P75/P95,每個 year 各一筆
        initial_balance:   顯示在 y=initial 的水平參考線 (optional)

    Returns:
        SVG string (含 viewBox)
    """
    if not percentile_bands:
        return '<svg width="100%" viewBox="0 0 760 280"><text x="380" y="140" text-anchor="middle" fill="#5a6a7e" font-size="14">（無 fan chart 資料）</text></svg>'

    # 1. 整理成 dict[percentile][year] = value
    by_p: dict[int, dict[int, float]] = {}
    for row in percentile_bands:
        v = row.get('value')
        if v is None:
            # 該 (percentile, year) 沒有可用樣本（例如 NAV 全部歸零無法推 MC）
            # → 跳過不要 raise，不要帶 None 進 float() 才崩
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v != v:  # NaN check
            continue
        p = int(row['percentile'])
        y = int(row['year'])
        by_p.setdefault(p, {})[y] = v

    years = sorted(set(y for d in by_p.values() for y in d.keys()))
    if not years:
        return '<svg width="100%" viewBox="0 0 760 280"><text x="380" y="140" text-anchor="middle" fill="#5a6a7e" font-size="14">（無 fan chart 資料）</text></svg>'

    # 2. y-scale (max = P95 終值 * 1.05, min = min(P5 min, initial_balance or 0) * 0.95)
    all_values = [v for d in by_p.values() for v in d.values() if v == v]   # filter NaN
    y_max_candidates = list(all_values)
    if initial_balance is not None:
        y_max_candidates.append(initial_balance)
    y_max = max(y_max_candidates) * 1.10
    y_min = min([v for v in all_values if v > 0] + ([initial_balance] if initial_balance and initial_balance > 0 else [])) * 0.85
    if y_min < 0:
        y_min = 0
    if y_max <= y_min:
        y_max = y_min + 1

    # 3. 座標換算 helper
    inner_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    inner_h = HEIGHT - MARGIN_TOP - MARGIN_BOTTOM

    def x_of(year: int) -> float:
        if len(years) <= 1:
            return MARGIN_LEFT + inner_w / 2
        return MARGIN_LEFT + (year - years[0]) / (years[-1] - years[0]) * inner_w

    def y_of(value: float) -> float:
        # log scale 比較好讀,小數值也看得到
        import math
        try:
            lv = math.log10(max(value, 1))
        except (ValueError, ZeroDivisionError):
            lv = 0
        try:
            ly_max = math.log10(max(y_max, 1))
            ly_min = math.log10(max(y_min, 1))
        except (ValueError, ZeroDivisionError):
            ly_max = ly_min = 0
        if ly_max == ly_min:
            return MARGIN_TOP + inner_h / 2
        return MARGIN_TOP + inner_h * (1 - (lv - ly_min) / (ly_max - ly_min))

    parts: list[str] = [
        f'<svg width="100%" viewBox="0 0 {WIDTH} {HEIGHT}" xmlns="http://www.w3.org/2000/svg" '
        f'preserveAspectRatio="xMidYMid meet" style="font-family: Arial, sans-serif; font-size: 11px;">',
    ]

    # 4. y-axis 網格 (4 條 log 標籤)
    import math
    try:
        ly_max = math.log10(max(y_max, 1))
        ly_min = math.log10(max(y_min, 1))
    except (ValueError, ZeroDivisionError):
        ly_max, ly_min = 1, 0
    n_grid = 4
    for i in range(n_grid + 1):
        lv = ly_min + (ly_max - ly_min) * i / n_grid
        v = 10 ** lv
        y = y_of(v)
        parts.append(f'<line x1="{MARGIN_LEFT}" y1="{y:.1f}" x2="{WIDTH - MARGIN_RIGHT}" y2="{y:.1f}" stroke="{COLOR_GRID}" stroke-dasharray="2 3"/>')
        label = f'{v:,.0f}'
        parts.append(f'<text x="{MARGIN_LEFT - 6}" y="{y + 3:.1f}" text-anchor="end" fill="{COLOR_AXIS}">{label}</text>')

    # 5. x-axis 標籤 (每 5 年一條)
    n_x = max(1, len(years) // 5)
    for i, year in enumerate(years):
        if i % n_x == 0 or i == len(years) - 1:
            x = x_of(year)
            parts.append(f'<text x="{x:.1f}" y="{HEIGHT - MARGIN_BOTTOM + 14}" text-anchor="middle" fill="{COLOR_AXIS}">第 {year} 年</text>')

    # 6. P5-P95 帶 (最外)
    if 5 in by_p and 95 in by_p:
        upper = ' '.join(f'{x_of(y):.1f},{y_of(by_p[95][y]):.1f}' for y in years if y in by_p[95])
        lower = ' '.join(f'{x_of(y):.1f},{y_of(by_p[5][y]):.1f}' for y in reversed(years) if y in by_p[5])
        parts.append(f'<polygon points="{upper} {lower}" fill="{COLOR_P5_P95}" stroke="none"/>')

    # 7. P25-P75 帶 (中間)
    if 25 in by_p and 75 in by_p:
        upper = ' '.join(f'{x_of(y):.1f},{y_of(by_p[75][y]):.1f}' for y in years if y in by_p[75])
        lower = ' '.join(f'{x_of(y):.1f},{y_of(by_p[25][y]):.1f}' for y in reversed(years) if y in by_p[25])
        parts.append(f'<polygon points="{upper} {lower}" fill="{COLOR_P25_P75}" stroke="none"/>')

    # 8. P50 中位線
    if 50 in by_p:
        path = ' '.join(f'{x_of(y):.1f},{y_of(by_p[50][y]):.1f}' for y in years if y in by_p[50])
        parts.append(f'<polyline points="{path}" fill="none" stroke="{COLOR_P50}" stroke-width="2"/>')

    # 9. 初值水平線 (若有)
    if initial_balance is not None and initial_balance > 0:
        y0 = y_of(initial_balance)
        parts.append(f'<line x1="{MARGIN_LEFT}" y1="{y0:.1f}" x2="{WIDTH - MARGIN_RIGHT}" y2="{y0:.1f}" stroke="#17365d" stroke-dasharray="4 4"/>')
        parts.append(f'<text x="{WIDTH - MARGIN_RIGHT - 4}" y="{y0 - 4:.1f}" text-anchor="end" fill="#17365d">初始 {initial_balance:,.0f}</text>')

    # 10. legend — 避免 fill 與 polygon 渲染混沖,用 stroke + 中性填色
    legend_y = MARGIN_TOP + 10
    legend_x = MARGIN_LEFT + 10
    # P5-P95 legend (透明背景 + 軸色邊框避免誤判為 polygon fill)
    parts.append(f'<rect x="{legend_x}" y="{legend_y - 8}" width="14" height="10" fill="white" stroke="{COLOR_P5_P95}" stroke-width="3"/>')
    parts.append(f'<text x="{legend_x + 18}" y="{legend_y + 1}" fill="{COLOR_AXIS}">P5-P95 (90%)</text>')
    legend_x += 100
    parts.append(f'<rect x="{legend_x}" y="{legend_y - 8}" width="14" height="10" fill="white" stroke="{COLOR_P25_P75}" stroke-width="3"/>')
    parts.append(f'<text x="{legend_x + 18}" y="{legend_y + 1}" fill="{COLOR_AXIS}">P25-P75 (50%)</text>')
    legend_x += 100
    parts.append(f'<line x1="{legend_x}" y1="{legend_y - 3}" x2="{legend_x + 14}" y2="{legend_y - 3}" stroke="{COLOR_P50}" stroke-width="2"/>')
    parts.append(f'<text x="{legend_x + 18}" y="{legend_y + 1}" fill="{COLOR_AXIS}">中位數 P50</text>')

    parts.append('</svg>')
    return '\n'.join(parts)
