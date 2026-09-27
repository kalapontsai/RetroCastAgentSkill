#!/usr/bin/env python3
"""
scripts/retrocast_cli.py — RetroCast agent skill CLI

JSON in, JSON out. All computation delegated to lib/ + app._run_analyze().
Reports written to data/reports/ matching Flask output filenames.

Design: this CLI replaces the Flask UI for agent-driven workflows.
The Flask app (app.py) remains for backward compatibility; both produce
identical reports given the same input.

Subcommands
-----------
profiles              列出 user_profile/ 內所有 CSV
preview <name>        預覽某 profile 的持倉
upload-profile <csv>  上傳 CSV 到 user_profile/
health [--v2]         健康檢查（v2 加檢查 F1-F6 依賴）
analyze -i <body.json> [-o <result.json>]    主分析 → JSON to stdout
export -r <result.json> -t forecast|rebalance [-p <profile>]   產 HTML 報告

JSON body schema matches Flask POST /api/analyze 1:1.
Form percent values (e.g. 3 for 3%) must be converted to decimal (0.03) before
passing to the CLI — see references/input-schema.md for the agent workflow.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# Repo paths — must come before app import so relative imports resolve
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Silence app.py's chatty debug logger — it writes DEBUG to stdout on import,
# polluting our JSON output. app.py keeps its own file handlers (debug.log,
# app.log) so logs are still preserved, just not on stdout/stderr.
logging.basicConfig(level=logging.WARNING, force=True)
logging.getLogger('portfolio_forecast').setLevel(logging.WARNING)

# Auto-bootstrap venv: if heavy deps (pandas) aren't importable but .venv exists
# alongside this script, re-exec the current invocation with the venv's Python.
# This lets `scripts/retrocast_cli.py ...` work without manual `source .venv/bin/activate`.
_VENV_PY = REPO_ROOT / '.venv' / 'bin' / 'python'
if _VENV_PY.exists():
    try:
        import pandas  # noqa: F401
    except ImportError:
        os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

# Import Flask app module — its module-level code only defines functions,
# `create_app()` is only called via `if __name__ == '__main__'` so importing
# has no side effects beyond registering the Flask app object.
from app import _run_analyze, _check_import  # noqa: E402
from app_config import USER_PROFILE_DIR, REPORTS_DIR  # noqa: E402
from lib.exporter import render_html_report, render_rebalance_report  # noqa: E402
from lib.finmind import load_finmind_token  # noqa: E402


# ───────── Output helpers ─────────
def _emit(obj: dict, exit_code: int = 0) -> None:
    """Emit JSON envelope to stdout, exit."""
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    sys.exit(exit_code)


def _emit_error(code: str, msg: str, exit_code: int = 1, **extra) -> None:
    """Emit structured error envelope."""
    obj = {"ok": False, "code": code, "error": msg}
    obj.update(extra)
    _emit(obj, exit_code)


# ───────── Subcommand: profiles ─────────
def cmd_profiles(args) -> None:
    if not USER_PROFILE_DIR.is_dir():
        return _emit_error("PROFILE_DIR_NOT_FOUND", f"{USER_PROFILE_DIR} not found")
    csvs = sorted(p.stem for p in USER_PROFILE_DIR.glob("*.csv"))
    _emit({"ok": True, "profiles": csvs, "count": len(csvs)})


# ───────── Subcommand: preview ─────────
def cmd_preview(args) -> None:
    name = args.name.strip()
    if "/" in name or "\\" in name or ".." in name:
        return _emit_error("INVALID_PROFILE", f"profile name 不合法: {name!r}")
    p = USER_PROFILE_DIR / f"{name}.csv"
    if not p.is_file():
        return _emit_error("PROFILE_NOT_FOUND", f"{name}.csv 不存在於 {USER_PROFILE_DIR}")
    rows = p.read_text(encoding="utf-8").splitlines()
    holdings: list[dict] = []
    for line in rows:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",", 1)
        if len(parts) >= 2:
            ticker = parts[0].strip()
            raw = parts[1].strip().strip('"').replace(",", "")
            entry: dict = {"ticker": ticker, "shares_raw": raw}
            try:
                entry["shares"] = int(raw)
            except ValueError:
                pass
            holdings.append(entry)
    _emit({"ok": True, "profile": name, "holdings": holdings, "raw_lines": rows})


# ───────── Subcommand: upload-profile ─────────
def cmd_upload_profile(args) -> None:
    src = Path(args.csv_path).resolve()
    if not src.is_file():
        return _emit_error("FILE_NOT_FOUND", f"{src} not found")
    if src.suffix.lower() != ".csv":
        return _emit_error("NOT_CSV", f"{src} 不是 .csv")
    USER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    dst_name = args.name or src.stem
    dst = USER_PROFILE_DIR / f"{dst_name}.csv"
    shutil.copy2(src, dst)
    _emit({"ok": True, "uploaded": dst.name, "path": str(dst)})


# ───────── Subcommand: health ─────────
def cmd_health(args) -> None:
    checks: dict = {
        "python_ok": True,
        "pandas_ok": True,
        "user_profile_dir": USER_PROFILE_DIR.is_dir(),
        "profile_csvs": (
            sorted(p.stem for p in USER_PROFILE_DIR.glob("*.csv"))
            if USER_PROFILE_DIR.is_dir() else []
        ),
        "finmind_token": bool(load_finmind_token()),
    }
    if args.v2:
        checks.update({
            "v1_healthy": True,
            "monte_carlo": _check_import("lib.monte_carlo"),
            "sequence_risk": _check_import("lib.sequence_risk"),
            "risk_metrics": _check_import("lib.risk_metrics"),
            "volatility_decay": _check_import("lib.volatility_decay"),
            "benchmarks": _check_import("lib.benchmarks"),
        })
    ok = all(v for k, v in checks.items() if isinstance(v, bool))
    _emit({"ok": ok, "checks": checks})


# ───────── Subcommand: analyze ─────────
def cmd_analyze(args) -> None:
    # Read body from --input file or stdin
    if args.input and args.input != "-":
        body_path = Path(args.input).resolve()
        if not body_path.is_file():
            return _emit_error("INPUT_NOT_FOUND", f"{body_path} not found")
        body = json.loads(body_path.read_text(encoding="utf-8"))
    else:
        # Read from stdin
        if sys.stdin.isatty():
            return _emit_error("MISSING_INPUT", "Provide --input <file> or pipe JSON via stdin")
        raw = sys.stdin.read()
        if not raw.strip():
            return _emit_error("MISSING_INPUT", "stdin 為空")
        body = json.loads(raw)

    if not isinstance(body, dict):
        return _emit_error("INVALID_INPUT", "body 必須是 JSON object")

    t0 = time.time()
    try:
        result = _run_analyze(body)
    except Exception as e:
        return _emit_error(type(e).__name__, str(e))
    duration = time.time() - t0

    # Optional: save full result to file
    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    summary = _build_summary(result)
    _emit({
        "ok": True,
        "duration_sec": round(duration, 2),
        "summary": summary,
        "result_path": str(Path(args.output).resolve()) if args.output else None,
    })


# ───────── Subcommand: export ─────────
def cmd_export(args) -> None:
    result_path = Path(args.result).resolve()
    if not result_path.is_file():
        return _emit_error("RESULT_NOT_FOUND", f"{result_path} not found")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _emit_error("INVALID_JSON", str(e))

    profile_name = args.profile or result.get("inputs", {}).get("profile", "profile")
    report_type = args.type
    if report_type not in ("forecast", "rebalance"):
        return _emit_error("INVALID_TYPE", "type 必須是 forecast 或 rebalance")

    try:
        if report_type == "rebalance":
            html = render_rebalance_report(result, profile_name=profile_name)
        else:
            html = render_html_report(result, profile_name=profile_name)
    except Exception as e:
        import traceback
        return _emit_error("RENDER_FAILED", f"{type(e).__name__}: {e}",
                           traceback=traceback.format_exc())

    # Filename matches Flask /api/export convention
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    profile_stem = Path(profile_name).stem if profile_name else ""
    prefix = f"{profile_stem}_" if profile_stem else ""
    fname = (
        f"{prefix}portfolio_rebalance_{ts}.html"
        if report_type == "rebalance"
        else f"{prefix}portfolio_forecast_{ts}.html"
    )
    out = REPORTS_DIR / fname
    out.write_text(html, encoding="utf-8")

    _emit({
        "ok": True,
        "file": fname,
        "path": str(out),
        "size": out.stat().st_size,
        "type": report_type,
    })


# ───────── Subcommand: form (inquiry form template) ─────────
_INQUIRY_FORM_TEMPLATE = {
    "_doc": "RetroCast inquiry form — 編輯後執行: retrocast_cli.py analyze -i form.json -o result.json",
    "_schema_ref": "references/input-schema.md（欄位說明、預設值、範圍、單位換算規則）",
    "_workflow": (
        "兩種使用方式：\n"
        "  A. 對話式：把這個 JSON 給 agent，agent 會逐欄詢問你（推薦，較不容易打錯）\n"
        "  B. 預填式：直接編輯此檔，存檔後跑 analyze\n"
        "兩種產出的 JSON 完全相同，可互通。"
    ),
    # ── Required ──
    "profile": "",
    # ── Backtest / forecast params ──
    "n": 10,
    "start_date": "2000-01-01",
    "end_date": None,  # 不填會自動用「上個月最後一個交易日」；寫死範例: "2026-08-31"
    "pv": None,
    "weights": None,
    "benchmark": None,
    # ── 交易成本（decimal，例 0.00142 = 0.142%）──
    "fee_buy": 0.00142,
    "fee_sell": 0.0,
    "tax_sell": 0.003,
    "slippage": 0.001,
    # ── v2 退休參數 ──
    "v2_current_age": 55,
    "v2_retirement_age": 65,
    "v2_retirement_end_age": 90,
    "v2_n_simulations": 1000,
    "v2_withdrawal_monthly": 0,
    "v2_withdrawal_inflation": 0.03,
    "v2_pension_monthly": 0.0,
    "v2_pension_inflation": 0.02,
    "v2_horizon_years": None,
    "v2_risk_free_rate": 0.015,
    # 一次性支出: [{"year_offset": 5, "amount": 1000000, "label": "房屋裝修"}]
    "v2_special_expenses": [],
}


def cmd_form(args) -> None:
    """輸出 inquiry form JSON 範本（含預設值）。

    兩種 workflow：
    A. 對話式：agent 透過 skill 詢問使用者各欄位,直接組 JSON 傳給 analyze
    B. 預填式：使用者編輯 form.json 後,執行 analyze -i form.json -o result.json
    """
    template = dict(_INQUIRY_FORM_TEMPLATE)
    if args.profile:
        template["profile"] = args.profile

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _emit({"ok": True, "form_path": str(out_path)})
    else:
        # Plain JSON to stdout (no envelope), so user can pipe: `form > form.json`
        sys.stdout.write(json.dumps(template, ensure_ascii=False, indent=2) + "\n")
        sys.exit(0)


# ───────── Summary builder (for agent to relay) ─────────
def _build_summary(result: dict) -> dict:
    """Build a summary block from analyze result for agent to relay to user.

    Agent should NOT interpret these — just relay the numbers verbatim.
    """
    summary: dict = {}

    inputs = result.get("inputs", {})
    if inputs:
        summary["profile"] = inputs.get("profile")
        summary["n_years"] = inputs.get("n")
        summary["end_date"] = inputs.get("end_date")
        pv_raw = inputs.get("pv_raw")
        if pv_raw is not None:
            summary["current_assets_NT$"] = int(pv_raw)
        mv = inputs.get("market_value", {})
        if isinstance(mv, dict):
            summary["market_value_total_NT$"] = mv.get("total")

    # Per-mode KPIs (common / dynamic / full) — keys live in `metrics`, not `kpis`
    for mode in ("common", "dynamic", "full"):
        mode_res = result.get(mode, {})
        if not mode_res:
            continue
        metrics = mode_res.get("metrics", {}) or {}
        summary[f"{mode}_cagr_pct"] = _pct(metrics.get("cagr"))
        summary[f"{mode}_volatility_pct"] = _pct(metrics.get("volatility"))
        summary[f"{mode}_mdd_pct"] = _pct(metrics.get("mdd"))
        summary[f"{mode}_sharpe"] = metrics.get("sharpe")

    # Forecast — percentiles dict (Bear/Conservative/Base/Optimistic/Bull)
    forecast = result.get("forecast", {})
    if forecast:
        pv = forecast.get("pv")
        if pv is not None:
            summary["forecast_pv_NT$"] = int(pv)
        percentiles = forecast.get("percentiles", {})
        if percentiles:
            for label in ("Bear", "Conservative", "Base", "Optimistic", "Bull"):
                v = percentiles.get(label)
                if v is not None:
                    summary[f"forecast_{label}_annualized_pct"] = _pct(v)
        summary["forecast_basis"] = forecast.get("basis")
        summary["forecast_sample_count"] = forecast.get("r_count")

    # v2 sections are top-level (not under _v2)
    mc = result.get("monte_carlo", {}) or {}
    if mc:
        mc_summary = mc.get("summary", {}) or {}
        for label in ("median_fv", "p50_fv", "median"):
            if label in mc_summary:
                summary["mc_median_fv_NT$"] = mc_summary[label]
                break
        for label in ("survival_to_horizon", "survival_rate"):
            if label in mc_summary:
                summary["mc_survival_to_horizon_pct"] = _pct(mc_summary[label])
                break

    seq = result.get("sequence_risk", {}) or {}
    if seq:
        for k_out, k_in in (
            ("survival_rate_pct", "survival_rate"),
            ("ruin_rate_pct", "ruin_rate"),
            ("earliest_ruin_age", "earliest_ruin_age"),
            ("median_terminal_balance_NT$", "median_final_balance"),
        ):
            v = seq.get(k_in)
            if v is not None:
                if k_out.endswith("_pct"):
                    summary[k_out] = _pct(v)
                else:
                    summary[k_out] = v

    rm = result.get("risk_metrics", {}) or {}
    if rm:
        var_cvar = rm.get("var_cvar", {}) or {}
        if var_cvar:
            summary["var_1d_95_pct"] = _pct(var_cvar.get("1d", {}).get("var_95"))
            summary["var_1y_95_pct"] = _pct(var_cvar.get("252d", {}).get("var_95"))
        sharpe = rm.get("sharpe", {})
        if isinstance(sharpe, dict):
            summary["sharpe_with_rf"] = sharpe.get("with_rf") or sharpe.get("sharpe")
        elif isinstance(sharpe, (int, float)):
            summary["sharpe_with_rf"] = sharpe

    opt = result.get("optimization", {}) or {}
    if opt:
        summary["optimization_status"] = opt.get("status")
        opt_summary = opt.get("current") or {}
        if opt_summary:
            summary["optimization_current_cagr_pct"] = _pct(opt_summary.get("cagr"))
        opt_new = opt.get("optimized") or {}
        if opt_new:
            summary["optimization_target_cagr_pct"] = _pct(opt_new.get("cagr"))

    return summary


def _pct(v, divisor: float = 100.0):
    """Convert decimal→percent if value is a number; pass-through None/NaN."""
    if v is None:
        return None
    try:
        import math
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return round(float(v) * divisor, 2)
    except (TypeError, ValueError):
        return None


# ───────── Argparse ─────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="retrocast",
        description="RetroCast agent skill CLI — JSON in, JSON out, HTML out",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="<subcommand>")

    sub.add_parser("profiles", help="列出 user_profile/ 內所有 CSV")

    p_prev = sub.add_parser("preview", help="預覽某 profile 的持倉")
    p_prev.add_argument("name", help="profile 名稱（不含 .csv）")

    p_up = sub.add_parser("upload-profile", help="上傳 CSV 到 user_profile/")
    p_up.add_argument("csv_path", help="來源 CSV 檔路徑")
    p_up.add_argument("--name", help="覆寫 profile 名稱（預設用檔名 stem）")

    p_health = sub.add_parser("health", help="健康檢查")
    p_health.add_argument("--v2", action="store_true", help="加檢查 v2 extensions (F1-F6)")

    p_an = sub.add_parser("analyze", help="主分析")
    p_an.add_argument("--input", "-i",
                      help="輸入 body JSON 檔路徑；'-' 或無值時讀 stdin")
    p_an.add_argument("--output", "-o",
                      help="輸出完整結果 JSON 檔路徑（給 export 用）")

    p_ex = sub.add_parser("export", help="產 HTML 報告")
    p_ex.add_argument("--result", "-r", required=True,
                      help="analyze 產出的 result JSON 檔路徑")
    p_ex.add_argument("--type", "-t", required=True,
                      choices=["forecast", "rebalance"],
                      help="報告類型")
    p_ex.add_argument("--profile", "-p",
                      help="profile 名稱（覆寫 result 內的 profile）")

    p_form = sub.add_parser("form", help="輸出 inquiry form JSON 範本（含預設值）")
    p_form.add_argument("--output", "-o", help="寫到檔案（預設 stdout，可直接 pipe）")
    p_form.add_argument("--profile", help="預填 profile 名稱")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    handlers = {
        "profiles": cmd_profiles,
        "preview": cmd_preview,
        "upload-profile": cmd_upload_profile,
        "health": cmd_health,
        "analyze": cmd_analyze,
        "export": cmd_export,
        "form": cmd_form,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
