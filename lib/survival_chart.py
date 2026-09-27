"""Phase 6 (Item 10): F2 存活率曲線 vs 年齡 SVG

輸入: sequence_risk.success_rate_by_age = {age_str: rate}
       sequence_risk.config = {current_age, retirement_age, retirement_end_age}
輸出: SVG 字串

X 軸 = 年齡 (60, 65, 70, ...),Y 軸 = 存活率 0-100%
每個資料點標值,加上 50%/70%/90% 參考線
"""
from __future__ import annotations

from datetime import date
from typing import Mapping


def render_survival_curve_svg(
    success_rate_by_age: Mapping[str, float],
    config: Mapping,
    width: int = 720,
    height: int = 240,
) -> str:
    """產出「存活率 vs 年齡」SVG

    Args:
        success_rate_by_age: {age_str: rate 0-1}
        config: sr.config 含 current_age / retirement_age / retirement_end_age
    """
    if not success_rate_by_age:
        return ''

    # 排序並轉成 (age, rate) list
    points: list[tuple[int, float]] = []
    for k, v in success_rate_by_age.items():
        if v is None:
            continue
        try:
            age = int(k)
            rate = float(v)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= rate <= 1.0):
            continue
        points.append((age, rate))
    points.sort()

    if not points:
        return ''

    ages = [p[0] for p in points]
    rates = [p[1] for p in points]
    age_min, age_max = min(ages), max(ages)
    rate_min, rate_max = 0.0, 1.0

    # margin
    ml, mr, mt, mb = 60, 30, 30, 40
    plot_w = width - ml - mr
    plot_h = height - mt - mb

    def x_of(age: int) -> float:
        if age_max == age_min:
            return ml + plot_w / 2
        return ml + (age - age_min) / (age_max - age_min) * plot_w

    def y_of(rate: float) -> float:
        return mt + (1.0 - rate) / (rate_max - rate_min) * plot_h

    # 6 個 X 軸 tick
    if age_max - age_min >= 5:
        tick_ages = sorted(set([age_min, age_max] + [a for a in range(age_min, age_max + 1) if a % 5 == 0]))
    else:
        tick_ages = list(range(age_min, age_max + 1))

    # 5 個 Y 軸 tick (0, 25, 50, 75, 100%)
    tick_rates = [0.0, 0.25, 0.5, 0.75, 1.0]

    parts: list[str] = []
    parts.append(f'<svg viewBox="0 0 {width} {height}" '
                 f'xmlns="http://www.w3.org/2000/svg" '
                 f'style="font-family:-apple-system,sans-serif;font-size:11px;">')

    # 背景
    parts.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fafbfc"/>')

    # 網格 + Y 軸 tick
    for r in tick_rates:
        y = y_of(r)
        parts.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + plot_w}" y2="{y:.1f}" '
                     f'stroke="#e2e8f0" stroke-width="1"/>')
        parts.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" fill="#475569" '
                     f'text-anchor="end">{int(r*100)}%</text>')

    # 50% / 70% / 90% 參考線(粗體)
    for ref in (0.5, 0.7, 0.9):
        y = y_of(ref)
        parts.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + plot_w}" y2="{y:.1f}" '
                     f'stroke="#94a3b8" stroke-width="1" stroke-dasharray="4 4"/>')
        parts.append(f'<text x="{ml + plot_w - 4}" y="{y - 2:.1f}" fill="#94a3b8" '
                     f'text-anchor="end" font-size="10">ref {int(ref*100)}%</text>')

    # X 軸
    parts.append(f'<line x1="{ml}" y1="{mt + plot_h}" x2="{ml + plot_w}" y2="{mt + plot_h}" '
                 f'stroke="#64748b" stroke-width="1.5"/>')
    for age in tick_ages:
        x = x_of(age)
        parts.append(f'<line x1="{x:.1f}" y1="{mt + plot_h}" x2="{x:.1f}" y2="{mt + plot_h + 4}" '
                     f'stroke="#64748b" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{mt + plot_h + 16}" fill="#475569" '
                     f'text-anchor="middle">{age}</text>')

    # 折線 (顏色: 存活率 >= 0.7 綠, >= 0.5 橘, < 0.5 紅)
    if len(points) >= 2:
        pts_str = ' '.join(f'{x_of(a):.1f},{y_of(r):.1f}' for a, r in points)
        parts.append(f'<polyline points="{pts_str}" fill="none" stroke="#1e8e3e" '
                     f'stroke-width="2.5" stroke-linejoin="round"/>')

    # 資料點 + label
    for point_index, (age, rate) in enumerate(points):
        x = x_of(age)
        y = y_of(rate)
        color = '#1e8e3e' if rate >= 0.7 else ('#b45309' if rate >= 0.5 else '#b42318')
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" '
                     f'stroke="#fff" stroke-width="1.5"/>')
        # Keep all points in the curve, but label only milestones so a
        # 30–35 year horizon remains readable.
        if point_index == 0 or point_index == len(points) - 1 or age % 5 == 0:
            parts.append(f'<text x="{x:.1f}" y="{y - 8:.1f}" fill="#0f172a" '
                         f'text-anchor="middle" font-weight="600" font-size="10">'
                         f'{rate*100:.2f}%</text>')

    # X 軸標題
    parts.append(f'<text x="{ml + plot_w/2:.1f}" y="{height - 6}" fill="#475569" '
                 f'text-anchor="middle" font-size="11">年齡（歲）</text>')
    # Y 軸標題
    parts.append(f'<text x="14" y="{mt + plot_h/2:.1f}" fill="#475569" '
                 f'text-anchor="middle" font-size="11" '
                 f'transform="rotate(-90, 14, {mt + plot_h/2:.1f})">存活率（資產 &gt; 0 機率）</text>')

    parts.append('</svg>')
    return ''.join(parts)
