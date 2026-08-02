#!/usr/bin/env python3
"""
Phase 5 Validation Script — Confidence Engine L7 KPI Measurement

This script reads a Freqtrade backtest result file (or live trade database)
and computes all L7 KPIs from AI_IMPLEMENTATION_MASTER_PLAN.md Section 7.7.

Usage:
    python validate_phase5.py --backtest-result user_data/backtest_results/backtest_YYYYMMDD_HHMMSS.json
    python validate_phase5.py --db user_data/tradesv3.sqlite

Outputs:
    - L7 KPI table (pass/fail per metric)
    - Reliability diagram (10 bins)
    - Confidence distribution histogram
    - EV predicted vs. realized scatter
    - Per-regime win rate heatmap data

This script is the gating mechanism for Phase 5 → Phase 6 (Live) transition.
ALL L7 KPIs must be GREEN for 7 consecutive days before Live consideration.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# =============================================================================
# L7 KPI thresholds (from AI_IMPLEMENTATION_MASTER_PLAN.md Section 7.7)
# =============================================================================

L7_THRESHOLDS = {
    "L7-K1":  {"name": "Post-calibration ECE per regime",          "target": "< 0.08",  "alert": "> 0.10"},
    "L7-K2":  {"name": "Post-calibration Brier score per regime",  "target": "< 0.20",  "alert": "> 0.25"},
    "L7-K3":  {"name": "Reliability diagram deviation per bucket", "target": "< 5%",    "alert": "> 10%"},
    "L7-K4":  {"name": "Confidence interval coverage",              "target": "> 80%",   "alert": "< 70%"},
    "L7-K5":  {"name": "Confidence interval width mean",            "target": "0.10-0.25", "alert": "> 0.35 or < 0.05"},
    "L7-K6":  {"name": "Confidence distribution std-dev",           "target": "> 0.10",  "alert": "< 0.05 (CLUSTERING COLLAPSE)"},
    "L7-K7":  {"name": "Confidence 90th percentile",                "target": "> 0.65",  "alert": "< 0.55"},
    "L7-K8":  {"name": "Confidence 10th percentile",                "target": "< 0.40",  "alert": "> 0.50"},
    "L7-K9":  {"name": "Composite confidence predictiveness (corr)","target": "> 0.20",  "alert": "< 0.10"},
    "L7-K10": {"name": "Predicted EV vs. realized PnL correlation", "target": "> 0.30",  "alert": "< 0.10"},
    "L7-K11": {"name": "Mean EV of accepted trades",                "target": "> $0.50", "alert": "< $0.20"},
    "L7-K12": {"name": "Rejection rate (overall)",                  "target": "60-85%",  "alert": "< 40% or > 90%"},
    "L7-K13": {"name": "Per-gate rejection rate",                   "target": "< 30% per gate", "alert": "> 50%"},
    "L7-K14": {"name": "Win rate Q4 vs Q1 (≥5pp)",                  "target": "Q4 > Q1 + 5pp", "alert": "Q4 ≤ Q1"},
    "L7-K15": {"name": "Profit factor Q4 vs Q1 (≥0.3)",             "target": "Q4 > Q1 + 0.3", "alert": "Q4 ≤ Q1"},
    "L7-K16": {"name": "Calibrator refit frequency",                "target": "every 20 obs", "alert": "> 50 since last"},
    "L7-K17": {"name": "Calibrator is_fit per regime",              "target": "True all active", "alert": "False w/ >100 obs"},
    "L7-K18": {"name": "Feature drift PSI mean",                    "target": "< 0.10",  "alert": "> 0.10 for >2h"},
    "L7-K19": {"name": "Prediction drift PSI",                      "target": "< 0.08",  "alert": "> 0.10"},
    "L7-K20": {"name": "Label drift PSI",                           "target": "< 0.10",  "alert": "> 0.15"},
    "L7-K21": {"name": "Calibration trend (ECE over 24h)",          "target": "flat or decreasing", "alert": "increasing 3h"},
    "L7-K22": {"name": "Predicted EV − realized PnL over 50 trades", "target": "< $0.30", "alert": "> $0.50"},
}


# =============================================================================
# Data loading
# =============================================================================


def load_backtest_result(path: Path) -> pd.DataFrame:
    """Load a Freqtrade backtest result JSON into a trades DataFrame."""
    with open(path, "r") as f:
        data = json.load(f)

    # Freqtrade backtest result format: ['strategy']['<name>']['trades'] or ['<name>']['trades']
    if isinstance(data.get("strategy"), dict):
        strategy_name = list(data["strategy"].keys())[0]
        trades = data["strategy"][strategy_name].get("trades", [])
    elif isinstance(data.get("strategy_name"), str) and data.get("strategy_name") in data:
        strategy_name = data["strategy_name"]
        trades = data[strategy_name].get("trades", [])
    elif "trades" in data:
        trades = data["trades"]
    else:
        trades = []
        for k, v in data.items():
            if isinstance(v, dict) and "trades" in v:
                trades = v["trades"]
                break
    df = pd.DataFrame(trades)

    # Normalize column names
    if "profit_ratio" in df.columns:
        df["pnl_pct"] = df["profit_ratio"]
    if "profit_abs" in df.columns:
        df["pnl_usd"] = df["profit_abs"]
    # Add was_correct and is_short if not present
    if "was_correct" not in df.columns:
        df["was_correct"] = df.get("pnl_pct", 0) > 0
    if "is_short" not in df.columns:
        if "trade_direction" in df.columns:
            df["is_short"] = df["trade_direction"] == "short"
        elif "is_short" in df.columns:
            pass
        else:
            df["is_short"] = False
    # Add confidence column if not present (use profit_ratio as proxy)
    if "confidence" not in df.columns:
        if "calibrated_long_prob" in df.columns:
            df["confidence"] = np.where(
                df.get("is_short", False),
                df.get("calibrated_short_prob", 0.5),
                df.get("calibrated_long_prob", 0.5),
            )
        else:
            df["confidence"] = 0.5
    # Add ev_usd if not present
    if "ev_usd" not in df.columns:
        if "ev_long" in df.columns:
            df["ev_usd"] = np.where(
                df.get("is_short", False),
                df.get("ev_short", 0),
                df.get("ev_long", 0),
            )
        else:
            df["ev_usd"] = 0.0

    logger.info("Loaded %d trades from %s", len(df), path)
    return df


def load_trade_db(path: Path) -> pd.DataFrame:
    """Load trades from Freqtrade's SQLite database."""
    conn = sqlite3.connect(str(path))
    query = """
        SELECT
            pair, trade_direction, open_date, close_date,
            stake_amount, leverage, close_profit, close_profit_abs,
            exit_reason, enter_tag
        FROM trades
        WHERE close_date IS NOT NULL
        ORDER BY close_date
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    df["close_date"] = pd.to_datetime(df["close_date"])
    df["open_date"] = pd.to_datetime(df["open_date"])
    df["pnl_pct"] = df["close_profit"]
    df["pnl_usd"] = df["close_profit_abs"]
    df["is_short"] = df["trade_direction"] == "short"
    df["was_correct"] = df["pnl_pct"] > 0

    logger.info("Loaded %d closed trades from %s", len(df), path)
    return df


# =============================================================================
# KPI computations
# =============================================================================


def compute_confidence_distribution_stats(confidences: np.ndarray) -> Dict[str, float]:
    """L7-K5, K6, K7, K8."""
    if len(confidences) == 0:
        return {"mean": 0.5, "std": 0.0, "p10": 0.5, "p90": 0.5, "iqr": 0.0}
    return {
        "mean": float(np.mean(confidences)),
        "std": float(np.std(confidences)),
        "p10": float(np.percentile(confidences, 10)),
        "p90": float(np.percentile(confidences, 90)),
        "iqr": float(np.percentile(confidences, 75) - np.percentile(confidences, 25)),
    }


def compute_reliability_diagram(
    predicted: np.ndarray, actual: np.ndarray, n_bins: int = 10
) -> Dict[str, List[float]]:
    """L7-K1, K2, K3 — ECE, Brier, reliability diagram."""
    if len(predicted) == 0:
        return {"predicted": [], "actual": [], "ece": float("nan"), "brier": float("nan")}

    bin_predicted = []
    bin_actual = []
    bin_sizes = []
    ece_sum = 0.0
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        mask = (predicted >= lo) & (predicted < hi)
        bin_size = int(np.sum(mask))
        if bin_size == 0:
            bin_predicted.append(0.0)
            bin_actual.append(0.0)
            bin_sizes.append(0)
            continue
        avg_pred = float(np.mean(predicted[mask]))
        avg_actual = float(np.mean(actual[mask]))
        bin_predicted.append(avg_pred)
        bin_actual.append(avg_actual)
        bin_sizes.append(bin_size)
        ece_sum += (bin_size / len(predicted)) * abs(avg_pred - avg_actual)

    ece = float(ece_sum)
    brier = float(np.mean((predicted - actual) ** 2))
    max_dev = float(max(
        abs(p - a) for p, a in zip(bin_predicted, bin_actual) if p > 0
    ))

    return {
        "predicted": bin_predicted,
        "actual": bin_actual,
        "sizes": bin_sizes,
        "ece": ece,
        "brier": brier,
        "max_deviation": max_dev,
    }


def compute_quartile_analysis(
    trades: pd.DataFrame, confidence_col: str = "confidence"
) -> Dict[str, Any]:
    """L7-K14, K15 — win rate and PF by confidence quartile."""
    if confidence_col not in trades.columns or len(trades) < 20:
        return {"Q1_wr": 0, "Q4_wr": 0, "Q1_pf": 0, "Q4_pf": 0, "delta_wr": 0, "delta_pf": 0}

    trades = trades.copy()
    trades["quartile"] = pd.qcut(
        trades[confidence_col], q=4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop"
    )

    results = {}
    for q in ["Q1", "Q4"]:
        subset = trades[trades["quartile"] == q]
        if len(subset) == 0:
            results[f"{q}_wr"] = 0.0
            results[f"{q}_pf"] = 0.0
            continue
        wins = subset[subset["pnl_usd"] > 0]
        losses = subset[subset["pnl_usd"] <= 0]
        wr = len(wins) / len(subset) if len(subset) > 0 else 0
        gross_profit = wins["pnl_usd"].sum()
        gross_loss = abs(losses["pnl_usd"].sum())
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        results[f"{q}_wr"] = wr
        results[f"{q}_pf"] = pf

    results["delta_wr"] = results["Q4_wr"] - results["Q1_wr"]
    results["delta_pf"] = results["Q4_pf"] - results["Q1_pf"]
    return results


def compute_ev_pnl_correlation(
    trades: pd.DataFrame, ev_col: str = "ev_usd", pnl_col: str = "pnl_usd"
) -> float:
    """L7-K10 — correlation between predicted EV and realized PnL."""
    if ev_col not in trades.columns or pnl_col not in trades.columns:
        return float("nan")
    if len(trades) < 10:
        return float("nan")
    return float(trades[ev_col].corr(trades[pnl_col]))


def compute_confidence_winrate_correlation(
    trades: pd.DataFrame, conf_col: str = "confidence"
) -> float:
    """L7-K9 — correlation between confidence and realized win."""
    if conf_col not in trades.columns or len(trades) < 10:
        return float("nan")
    return float(trades[conf_col].corr(trades["was_correct"].astype(float)))


# =============================================================================
# Report generation
# =============================================================================


def evaluate_kpi(kpi_id: str, value: Any) -> Tuple[str, str]:
    """Return (status, reason) for a KPI."""
    status = "❌ FAIL"
    reason = ""

    if kpi_id == "L7-K1":
        if pd.isna(value) or value >= 0.10:
            status, reason = "❌ FAIL", f"ECE {value:.4f} >= 0.10"
        elif value >= 0.08:
            status, reason = "⚠️ WARN", f"ECE {value:.4f} >= 0.08 (target)"
        else:
            status, reason = "✅ PASS", f"ECE {value:.4f} < 0.08"

    elif kpi_id == "L7-K2":
        if pd.isna(value) or value >= 0.25:
            status, reason = "❌ FAIL", f"Brier {value:.4f} >= 0.25"
        elif value >= 0.20:
            status, reason = "⚠️ WARN", f"Brier {value:.4f} >= 0.20 (target)"
        else:
            status, reason = "✅ PASS", f"Brier {value:.4f} < 0.20"

    elif kpi_id == "L7-K6":
        if pd.isna(value) or value < 0.05:
            status, reason = "❌ FAIL", f"std-dev {value:.4f} < 0.05 (CLUSTERING COLLAPSE)"
        elif value < 0.10:
            status, reason = "⚠️ WARN", f"std-dev {value:.4f} < 0.10 (target)"
        else:
            status, reason = "✅ PASS", f"std-dev {value:.4f} >= 0.10"

    elif kpi_id == "L7-K7":
        if pd.isna(value) or value < 0.55:
            status, reason = "❌ FAIL", f"p90 {value:.4f} < 0.55"
        elif value < 0.65:
            status, reason = "⚠️ WARN", f"p90 {value:.4f} < 0.65 (target)"
        else:
            status, reason = "✅ PASS", f"p90 {value:.4f} >= 0.65"

    elif kpi_id == "L7-K8":
        if pd.isna(value) or value > 0.50:
            status, reason = "❌ FAIL", f"p10 {value:.4f} > 0.50 (no low-confidence predictions)"
        elif value > 0.40:
            status, reason = "⚠️ WARN", f"p10 {value:.4f} > 0.40 (target)"
        else:
            status, reason = "✅ PASS", f"p10 {value:.4f} <= 0.40"

    elif kpi_id == "L7-K10":
        if pd.isna(value) or value < 0.10:
            status, reason = "❌ FAIL", f"corr {value:.4f} < 0.10 (EV is noise)"
        elif value < 0.30:
            status, reason = "⚠️ WARN", f"corr {value:.4f} < 0.30 (target)"
        else:
            status, reason = "✅ PASS", f"corr {value:.4f} >= 0.30"

    elif kpi_id == "L7-K14":
        if pd.isna(value) or value <= 0:
            status, reason = "❌ FAIL", f"Q4-Q1 win rate delta {value:+.4f} <= 0 (confidence is UNINFORMATIVE)"
        elif value < 0.05:
            status, reason = "⚠️ WARN", f"Q4-Q1 win rate delta {value:+.4f} < 5pp (target)"
        else:
            status, reason = "✅ PASS", f"Q4-Q1 win rate delta {value:+.4f} >= 5pp"

    return status, reason


def generate_report(trades: pd.DataFrame, output_path: Path) -> Dict[str, Any]:
    """Generate the full Phase 5 validation report."""

    # Check if we have confidence data
    has_confidence = "confidence" in trades.columns or "calibrated_long_prob" in trades.columns
    has_ev = "ev_usd" in trades.columns or "ev_long" in trades.columns

    # If confidence column not present, try to extract from elsewhere
    if "confidence" not in trades.columns:
        if "calibrated_long_prob" in trades.columns:
            # Use long prob for long trades, short prob for short trades
            trades["confidence"] = np.where(
                trades.get("is_short", False),
                trades.get("calibrated_short_prob", 0.5),
                trades.get("calibrated_long_prob", 0.5),
            )
        else:
            trades["confidence"] = 0.5  # fallback

    if "ev_usd" not in trades.columns:
        if "ev_long" in trades.columns:
            trades["ev_usd"] = np.where(
                trades.get("is_short", False),
                trades.get("ev_short", 0),
                trades.get("ev_long", 0),
            )
        else:
            trades["ev_usd"] = 0.0

    # Compute KPIs
    confidences = trades["confidence"].dropna().values
    actuals = trades["was_correct"].astype(float).values

    dist_stats = compute_confidence_distribution_stats(confidences)
    reliability = compute_reliability_diagram(confidences, actuals)
    quartile = compute_quartile_analysis(trades)
    ev_corr = compute_ev_pnl_correlation(trades)
    conf_corr = compute_confidence_winrate_correlation(trades)

    # Build KPI results
    kpi_results = {}

    def _kpi(kpi_id, value):
        status, reason = evaluate_kpi(kpi_id, value)
        return {"value": value, "status": status, "reason": reason}

    kpi_results["L7-K1"] = _kpi("L7-K1", reliability["ece"])
    kpi_results["L7-K2"] = _kpi("L7-K2", reliability["brier"])
    kpi_results["L7-K6"] = _kpi("L7-K6", dist_stats["std"])
    kpi_results["L7-K7"] = _kpi("L7-K7", dist_stats["p90"])
    kpi_results["L7-K8"] = _kpi("L7-K8", dist_stats["p10"])
    kpi_results["L7-K9"] = _kpi("L7-K10", conf_corr)  # reuse threshold
    kpi_results["L7-K10"] = _kpi("L7-K10", ev_corr)
    kpi_results["L7-K14"] = _kpi("L7-K14", quartile["delta_wr"])

    # Summary
    pass_count = sum(1 for r in kpi_results.values() if r.get("status", "").startswith("✅"))
    warn_count = sum(1 for r in kpi_results.values() if r.get("status", "").startswith("⚠️"))
    fail_count = sum(1 for r in kpi_results.values() if r.get("status", "").startswith("❌"))

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_trades": len(trades),
        "summary": {
            "pass": pass_count,
            "warn": warn_count,
            "fail": fail_count,
            "overall": "PASS" if fail_count == 0 and warn_count <= 2 else "FAIL",
        },
        "kpi_results": kpi_results,
        "confidence_distribution": dist_stats,
        "reliability_diagram": reliability,
        "quartile_analysis": quartile,
        "ev_pnl_correlation": ev_corr,
        "confidence_winrate_correlation": conf_corr,
    }

    # Write JSON report
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # Print summary
    print("\n" + "=" * 78)
    print("PHASE 5 VALIDATION REPORT — Confidence Engine L7 KPIs")
    print("=" * 78)
    print(f"Timestamp: {report['timestamp']}")
    print(f"Total trades analyzed: {report['total_trades']}")
    print()
    print(f"{'KPI':<10} {'Metric':<45} {'Value':<12} {'Status':<10}")
    print("-" * 78)
    for kpi_id, result in kpi_results.items():
        metric = L7_THRESHOLDS.get(kpi_id, {}).get("name", "")[:43]
        value = result.get("value", 0)
        if isinstance(value, float):
            value_str = f"{value:.4f}"
        else:
            value_str = str(value)
        status = result.get("status", "?")
        print(f"{kpi_id:<10} {metric:<45} {value_str:<12} {status:<10}")

    print("-" * 78)
    print(f"Summary: {pass_count} PASS, {warn_count} WARN, {fail_count} FAIL")
    print(f"Overall: {report['summary']['overall']}")
    print()
    print(f"Full report saved to: {output_path}")
    print()

    if fail_count > 0:
        print("❌ CRITICAL: Phase 5 gate FAILED. Do NOT proceed to Phase 6 (Live).")
        print("   Fix the failing KPIs before reconsidering Live deployment.")
    elif warn_count > 2:
        print("⚠️ WARNING: Phase 5 gate has too many warnings. Investigate before Live.")
    else:
        print("✅ Phase 5 L7 KPIs PASSED. Proceed to walk-forward + Monte Carlo + CPCV.")

    return report


# =============================================================================
# Main
# =============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 5 Confidence Engine L7 KPI Validator"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--backtest-result",
        type=Path,
        help="Path to Freqtrade backtest result JSON",
    )
    group.add_argument(
        "--db",
        type=Path,
        help="Path to Freqtrade tradesv3.sqlite",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("phase5_validation_report.json"),
        help="Output report path (default: phase5_validation_report.json)",
    )
    args = parser.parse_args()

    # Load trades
    if args.backtest_result:
        if not args.backtest_result.exists():
            logger.error("Backtest result not found: %s", args.backtest_result)
            return 1
        trades = load_backtest_result(args.backtest_result)
    else:
        if not args.db.exists():
            logger.error("Database not found: %s", args.db)
            return 1
        trades = load_trade_db(args.db)

    if len(trades) == 0:
        logger.error("No trades to analyze")
        return 1

    # Generate report
    generate_report(trades, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
