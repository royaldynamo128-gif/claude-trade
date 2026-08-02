"""
ClaudeTrade Confidence Engine — Corrected Implementation

Fixes the 10 critical defects identified in PHASE_4_5_AUDIT_REPORT.md:
  DEFECT-1:  Multiplicative gating instead of additive averaging (RC-1 fix)
  DEFECT-2:  Auto-fitting calibrator with observation recording (RC-2 fix)
  DEFECT-3:  Per-regime calibration (P7)
  DEFECT-4:  EV uses lower bound of confidence interval (P6, INV-9)
  DEFECT-5:  ATR-scaled reward/risk in EV
  DEFECT-6:  UnifiedDriftDetector module added (PSI on features/predictions/labels)
  DEFECT-7:  CalibrationValidator module added (Brier, ECE, reliability diagram)
  DEFECT-8:  TradeFilter module with 7 gates
  DEFECT-9:  record_observation() called on every closed trade (INV-13)
  DEFECT-10: Confidence interval from calibration error + drift + DI (not Wilson)

This module is the implementation of AI_IMPLEMENTATION_MASTER_PLAN.md Section 6.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration constants
# =============================================================================

DEFAULT_MIN_SAMPLES: int = 50          # need >=50 to fit calibrator
DEFAULT_MAX_SAMPLES: int = 1000        # rolling window
DEFAULT_REFIT_INTERVAL: int = 20       # refit every 20 new observations
DEFAULT_N_BINS: int = 10               # for ECE / reliability diagram

DEFAULT_BASE_WIDTH: float = 0.10       # base interval half-width
DEFAULT_MAX_WIDTH: float = 0.40        # cap (reject trades above this)
DEFAULT_MIN_WIDTH: float = 0.05        # floor
DEFAULT_DRIFT_WIDEN_WARNING: float = 0.15
DEFAULT_DRIFT_WIDEN_CRITICAL: float = 0.30

DEFAULT_PSI_WARN: float = 0.10
DEFAULT_PSI_CRITICAL: float = 0.25
DEFAULT_BASELINE_SIZE: int = 500
DEFAULT_RECENT_SIZE: int = 200

DEFAULT_FEE_RATE: float = 0.0010       # 0.10% round-trip Binance taker
DEFAULT_SLIPPAGE_RATE: float = 0.0010  # 0.10% estimated
DEFAULT_FUNDING_DRAG_PER_HOUR: float = 0.005  # 0.005% per hour
DEFAULT_TP_RATIO: float = 1.5          # TP at 1.5x stop distance
DEFAULT_MIN_EV_USD: float = 0.20       # minimum EV to accept trade


# =============================================================================
# Dataclasses
# =============================================================================


@dataclass
class ConfidenceInterval:
    """Confidence interval with point estimate and bounds."""
    point: float
    lower: float
    upper: float
    width: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "point": round(self.point, 4),
            "lower": round(self.lower, 4),
            "upper": round(self.upper, 4),
            "width": round(self.width, 4),
        }


@dataclass
class EVResult:
    """Expected value computation result."""
    ev_usd: float
    ev_per_dollar_risked: float
    p_win: float          # the lower bound used (NOT point estimate)
    p_loss: float
    reward_usd: float
    risk_usd: float
    costs_usd: float
    regime_adjustment: float
    meets_minimum: bool


@dataclass
class DriftResult:
    """Drift detection result."""
    dimension: str          # 'feature' | 'prediction' | 'label'
    feature_name: Optional[str]
    psi: float
    is_warning: bool
    is_critical: bool
    baseline_mean: float
    recent_mean: float


@dataclass
class CalibrationReport:
    """Calibration validation report."""
    timestamp: str
    n_observations: int
    brier_score: float
    ece: float
    log_loss: float
    auc_roc: float
    reliability_diagram: Dict[str, List[float]]
    confidence_stats: Dict[str, float]
    alerts: List[str] = field(default_factory=list)


@dataclass
class FilterResult:
    """TradeFilter result."""
    accepted: bool
    reason: str
    gate_failed: Optional[str] = None


# =============================================================================
# Module 1: IsotonicCalibrator (per-regime, auto-fitting)
# =============================================================================


class IsotonicCalibrator:
    """
    Isotonic regression calibrator with per-regime support and auto-refit.

    Fixes DEFECT-2 (auto-fit), DEFECT-3 (per-regime), DEFECT-9 (observation recording).

    Algorithm: Pool-Adjacent-Violators (PAV) via sklearn.isotonic.IsotonicRegression.
    Maintains a separate calibrator per (side, regime) tuple.
    """

    def __init__(
        self,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        max_samples: int = DEFAULT_MAX_SAMPLES,
        refit_interval: int = DEFAULT_REFIT_INTERVAL,
    ) -> None:
        self.min_samples = int(min_samples)
        self.max_samples = int(max_samples)
        self.refit_interval = int(refit_interval)

        self._lock = threading.RLock()
        # Per-regime calibrators: key = (side, regime)
        self._calibrators: Dict[Tuple[str, str], IsotonicRegression] = {}
        self._observations: Dict[Tuple[str, str], Deque[Tuple[float, bool]]] = {}
        self._obs_since_fit: Dict[Tuple[str, str], int] = {}
        self._is_fit: Dict[Tuple[str, str], bool] = {}
        self._brier: Dict[Tuple[str, str], float] = {}
        self._ece: Dict[Tuple[str, str], float] = {}

    def _ensure_regime(self, key: Tuple[str, str]) -> None:
        if key not in self._calibrators:
            self._calibrators[key] = IsotonicRegression(
                y_min=0.01, y_max=0.99, out_of_bounds="clip"
            )
            self._observations[key] = deque(maxlen=self.max_samples)
            self._obs_since_fit[key] = 0
            self._is_fit[key] = False
            self._brier[key] = 0.0
            self._ece[key] = 0.0

    def record_observation(
        self,
        predicted_confidence: float,
        was_correct: bool,
        side: str,
        regime: str,
    ) -> None:
        """
        Record a (prediction, outcome) pair and trigger refit if interval reached.

        Fixes DEFECT-9: this must be called on every closed trade (INV-13).
        """
        if pd.isna(predicted_confidence):
            logger.warning(
                "IsotonicCalibrator.record_observation: NaN predicted_confidence ignored"
            )
            return
        key = (side, regime)
        with self._lock:
            self._ensure_regime(key)
            self._observations[key].append((float(predicted_confidence), bool(was_correct)))
            self._obs_since_fit[key] += 1

            if (
                len(self._observations[key]) >= self.min_samples
                and self._obs_since_fit[key] >= self.refit_interval
            ):
                self._fit_regime(key)
                self._obs_since_fit[key] = 0

    def calibrate(
        self,
        predicted_confidence: float,
        side: str,
        regime: str,
    ) -> float:
        """
        Calibrate a single prediction. If calibrator not yet fit for this regime,
        returns input unchanged with a WARNING log (NOT a sigmoid stretch).

        Fixes DEFECT-2: no fake sigmoid fallback.
        """
        if pd.isna(predicted_confidence):
            logger.error(
                "IsotonicCalibrator.calibrate: NaN input, returning 0.50 neutral"
            )
            return 0.50

        key = (side, regime)
        with self._lock:
            self._ensure_regime(key)
            if not self._is_fit[key]:
                # Honest fallback: return input unchanged with warning.
                # Do NOT apply a sigmoid stretch that disguises the unfitted state.
                logger.debug(
                    "IsotonicCalibrator: not yet fit for %s (n=%d, need %d). "
                    "Returning raw probability.",
                    key, len(self._observations[key]), self.min_samples,
                )
                return float(predicted_confidence)
            return float(self._calibrators[key].predict([float(predicted_confidence)])[0])

    def calibrate_series(
        self,
        predicted: pd.Series,
        side: str,
        regime: str,
    ) -> pd.Series:
        """Vectorized calibration for a pandas Series (used in populate_indicators)."""
        if predicted is None or len(predicted) == 0:
            return predicted
        key = (side, regime)
        with self._lock:
            self._ensure_regime(key)
            if not self._is_fit[key]:
                logger.debug(
                    "IsotonicCalibrator: not yet fit for %s. Returning raw series.", key
                )
                return predicted
            return pd.Series(
                self._calibrators[key].predict(predicted.values),
                index=predicted.index,
            )

    def force_refit(self, side: str, regime: str) -> None:
        """Force immediate refit (called by drift detector on warning)."""
        key = (side, regime)
        with self._lock:
            self._ensure_regime(key)
            if len(self._observations[key]) >= self.min_samples:
                self._fit_regime(key)
                self._obs_since_fit[key] = 0
                logger.info(
                    "IsotonicCalibrator: force-refit %s (n=%d, Brier=%.4f, ECE=%.4f)",
                    key, len(self._observations[key]),
                    self._brier[key], self._ece[key],
                )

    def _fit_regime(self, key: Tuple[str, str]) -> None:
        """Fit isotonic regression on observations for this regime."""
        obs = list(self._observations[key])
        if len(obs) < self.min_samples:
            return
        predicted = np.array([p for p, _ in obs])
        actual = np.array([1.0 if a else 0.0 for _, a in obs])

        try:
            self._calibrators[key].fit(predicted, actual)
            self._is_fit[key] = True
            self._compute_diagnostics(key, predicted, actual)
            logger.info(
                "IsotonicCalibrator: fit %s on %d samples (Brier=%.4f, ECE=%.4f)",
                key, len(obs), self._brier[key], self._ece[key],
            )
        except Exception as exc:
            logger.error("IsotonicCalibrator fit failed for %s: %s", key, exc)
            self._is_fit[key] = False

    def _compute_diagnostics(
        self,
        key: Tuple[str, str],
        predicted: np.ndarray,
        actual: np.ndarray,
    ) -> None:
        """Compute Brier score and ECE for this regime."""
        n = len(predicted)
        if n == 0:
            return
        calibrated = self._calibrators[key].predict(predicted)
        # Brier score
        self._brier[key] = float(np.mean((calibrated - actual) ** 2))
        # ECE (10 bins)
        ece_sum = 0.0
        for i in range(DEFAULT_N_BINS):
            lo, hi = i / DEFAULT_N_BINS, (i + 1) / DEFAULT_N_BINS
            mask = (predicted >= lo) & (predicted < hi)
            bin_size = int(np.sum(mask))
            if bin_size == 0:
                continue
            avg_pred = float(np.mean(calibrated[mask]))
            avg_actual = float(np.mean(actual[mask]))
            ece_sum += (bin_size / n) * abs(avg_pred - avg_actual)
        self._ece[key] = float(ece_sum)

    def get_diagnostics(self) -> Dict[str, Any]:
        """Return diagnostics for all regimes."""
        with self._lock:
            result = {}
            for key in self._calibrators:
                result[f"{key[0]}_{key[1]}"] = {
                    "n_samples": len(self._observations[key]),
                    "is_fit": self._is_fit[key],
                    "brier_score": round(self._brier[key], 4),
                    "ece": round(self._ece[key], 4),
                }
            return result

    def get_all_observations(self) -> List[Tuple[str, str, float, bool]]:
        """Return all observations (for CalibrationValidator)."""
        with self._lock:
            obs = []
            for (side, regime), deque_data in self._observations.items():
                for pred, actual in deque_data:
                    obs.append((side, regime, pred, actual))
            return obs


# =============================================================================
# Module 2: ConfidenceIntervalCalculator
# =============================================================================


class ConfidenceIntervalCalculator:
    """
    Compute (lower, point, upper) bounds from calibration error + drift + DI.

    Fixes DEFECT-10: replaces the wrong Wilson-score implementation with
    the master plan's interval design (Section 6.5).
    """

    def __init__(
        self,
        base_width: float = DEFAULT_BASE_WIDTH,
        max_width: float = DEFAULT_MAX_WIDTH,
        min_width: float = DEFAULT_MIN_WIDTH,
        drift_widen_warning: float = DEFAULT_DRIFT_WIDEN_WARNING,
        drift_widen_critical: float = DEFAULT_DRIFT_WIDEN_CRITICAL,
    ) -> None:
        self.base_width = base_width
        self.max_width = max_width
        self.min_width = min_width
        self.drift_widen_warning = drift_widen_warning
        self.drift_widen_critical = drift_widen_critical

    def compute_interval(
        self,
        point_estimate: float,
        regime: str,
        di_value: float,
        drift_psi: float,
        calibrator_brier: float = 0.0,
        calibrator_n_samples: int = 0,
    ) -> ConfidenceInterval:
        """
        Compute confidence interval.

        Width sources (per master plan Section 6.5):
          1. Base calibration error (Brier score)
          2. Drift state (PSI)
          3. Sample size penalty (if n < 200)
          4. DI value (dissimilarity)
        """
        if pd.isna(point_estimate):
            logger.error("ConfidenceInterval: NaN point_estimate, returning neutral")
            return ConfidenceInterval(point=0.50, lower=0.40, upper=0.60, width=0.20)

        # Start with base width
        width = self.base_width

        # Add calibration error (Brier score is mean squared error; sqrt ~ std-dev)
        width += float(np.sqrt(max(0.0, calibrator_brier)))

        # Drift widening
        if drift_psi >= DEFAULT_PSI_CRITICAL:
            width += self.drift_widen_critical
        elif drift_psi >= DEFAULT_PSI_WARN:
            width += self.drift_widen_warning

        # Sample size penalty
        if 0 < calibrator_n_samples < 200:
            width += 0.10 * (1.0 - calibrator_n_samples / 200.0)

        # DI widening
        if di_value > 0.5:
            width += (di_value - 0.5) * 0.10

        # Clamp to [min, max]
        width = max(self.min_width, min(self.max_width, width))

        lower = max(0.01, point_estimate - width)
        upper = min(0.99, point_estimate + width)
        # Ensure lower <= point <= upper
        lower = min(lower, point_estimate)
        upper = max(upper, point_estimate)

        return ConfidenceInterval(
            point=float(point_estimate),
            lower=float(lower),
            upper=float(upper),
            width=float(width),
        )


# =============================================================================
# Module 3: MultiSignalConfidenceCombiner (MULTIPLICATIVE gating)
# =============================================================================


class MultiSignalConfidenceCombiner:
    """
    Combine calibrated ML probability with TA signals via MULTIPLICATIVE gating.

    Fixes DEFECT-1: replaces additive weighted averaging with multiplicative gating.

    Per master plan Section 6.6:
      composite = calibrated_ml_prob
                × trend_multiplier
                × regime_multiplier
                × volatility_multiplier
                × funding_multiplier (1.0 if not available)
                × oi_multiplier (1.0 if not available)
                × historical_win_rate_multiplier

    Each multiplier is in [0.5, 1.5]. Multiplicative means a single strong
    disagreement can veto a trade; additive averaging regresses to the mean.
    """

    # Regime multipliers (from master plan Section 5.16)
    REGIME_MULTIPLIERS: Dict[str, float] = {
        "TRENDING_STRONG": 1.00,
        "TRENDING_WEAK": 0.90,
        "RANGING": 0.85,
        "BREAKOUT": 0.75,
        "MEAN_REVERSION": 0.90,
        "HIGH_VOLATILITY": 0.75,
        "LOW_VOLATILITY": 0.95,
        "NEWS_DRIVEN": 0.50,
        "CHOPPY": 0.50,
        "TRANSITIONAL": 0.90,
    }

    def combine(
        self,
        calibrated_ml_prob: float,
        trend_score: float,
        regime: str,
        atr_percentile: float,
        ml_direction: str,
        rule_direction: str,
        funding_rate: Optional[float] = None,
        oi_roc: Optional[float] = None,
        historical_win_rate: Optional[float] = None,
    ) -> float:
        """
        Compute composite confidence via multiplicative gating.

        Parameters
        ----------
        calibrated_ml_prob : float
            The isotonic-calibrated ML probability (NOT raw).
        trend_score : float
            Trend score in [-1, +1] from trend detector.
        regime : str
            Regime label from regime detector.
        atr_percentile : float
            ATR percentile in [0, 1].
        ml_direction : str
            'long' or 'short' — the ML-predicted direction.
        rule_direction : str
            'long' or 'short' — the rule-based direction.
        funding_rate : float, optional
            Current funding rate (Phase 4+).
        oi_roc : float, optional
            Open Interest rate of change (Phase 4+).
        historical_win_rate : float, optional
            Historical win rate for this regime (from confidence_observations).
        """
        if pd.isna(calibrated_ml_prob):
            return 0.50

        # === Trend multiplier ===
        # trend_score in [-1, +1]; same sign as direction = agree
        direction_sign = 1.0 if ml_direction == "long" else -1.0
        trend_agreement = trend_score * direction_sign  # +ve = agree, -ve = disagree
        if trend_agreement > 0.5:
            trend_mult = 1.20
        elif trend_agreement > 0.0:
            trend_mult = 1.00
        elif trend_agreement > -0.5:
            trend_mult = 0.85
        else:
            trend_mult = 0.70

        # === Regime multiplier ===
        regime_mult = self.REGIME_MULTIPLIERS.get(regime, 0.90)

        # === Volatility multiplier ===
        if pd.isna(atr_percentile):
            vol_mult = 0.90
        elif atr_percentile < 0.15 or atr_percentile > 0.85:
            vol_mult = 0.75
        elif atr_percentile < 0.30 or atr_percentile > 0.70:
            vol_mult = 0.90
        else:
            vol_mult = 1.00

        # === Funding rate multiplier (Phase 4+) ===
        if funding_rate is None or pd.isna(funding_rate):
            funding_mult = 1.0
        else:
            # Extreme positive funding on a long entry = contrarian penalty
            abs_funding = abs(funding_rate)
            if abs_funding > 0.001:  # > 0.1% per 8h = extreme
                funding_mult = 0.80
            elif abs_funding > 0.0005:
                funding_mult = 0.90
            else:
                funding_mult = 1.00

        # === OI multiplier (Phase 4+) ===
        if oi_roc is None or pd.isna(oi_roc):
            oi_mult = 1.0
        else:
            # Rising OI + trending price = trend confirmed
            if oi_roc > 0.05:
                oi_mult = 1.10
            elif oi_roc < -0.05:
                oi_mult = 0.80  # deleveraging
            else:
                oi_mult = 1.00

        # === Historical win rate multiplier ===
        if historical_win_rate is None or pd.isna(historical_win_rate):
            hist_mult = 1.0
        elif historical_win_rate > 0.60:
            hist_mult = 1.20
        elif historical_win_rate > 0.45:
            hist_mult = 1.00
        elif historical_win_rate > 0.35:
            hist_mult = 0.85
        else:
            hist_mult = 0.70

        # === Multiplicative combination ===
        composite = (
            calibrated_ml_prob
            * trend_mult
            * regime_mult
            * vol_mult
            * funding_mult
            * oi_mult
            * hist_mult
        )

        # Clamp to [0, 0.95]
        return float(max(0.0, min(0.95, composite)))


# =============================================================================
# Module 4: UnifiedDriftDetector (PSI on features/predictions/labels)
# =============================================================================


class UnifiedDriftDetector:
    """
    Detect distribution drift via Population Stability Index (PSI).

    Fixes DEFECT-6: missing drift detector.

    Monitors three dimensions:
      1. Feature drift (per-feature PSI)
      2. Prediction drift (calibrated probability distribution)
      3. Label drift (realized win rate)
    """

    def __init__(
        self,
        baseline_size: int = DEFAULT_BASELINE_SIZE,
        recent_size: int = DEFAULT_RECENT_SIZE,
        psi_warn: float = DEFAULT_PSI_WARN,
        psi_critical: float = DEFAULT_PSI_CRITICAL,
    ) -> None:
        self.baseline_size = baseline_size
        self.recent_size = recent_size
        self.psi_warn = psi_warn
        self.psi_critical = psi_critical

        self._lock = threading.RLock()
        self._feature_baseline: Optional[pd.DataFrame] = None
        self._prediction_baseline: Optional[np.ndarray] = None
        self._label_baseline: Optional[np.ndarray] = None
        self._recent_predictions: Deque[float] = deque(maxlen=recent_size)
        self._recent_labels: Deque[bool] = deque(maxlen=recent_size)

    def update_feature_baseline(self, features: pd.DataFrame) -> None:
        """Set the feature baseline (training distribution)."""
        with self._lock:
            self._feature_baseline = features.tail(self.baseline_size).copy()

    def update_prediction_baseline(self, predictions: np.ndarray) -> None:
        """Set the prediction baseline (training-time predictions)."""
        with self._lock:
            self._prediction_baseline = np.array(predictions)[-self.baseline_size:]

    def update_label_baseline(self, outcomes: List[bool]) -> None:
        """Set the label baseline (training-time outcomes)."""
        with self._lock:
            self._label_baseline = np.array(
                [1.0 if o else 0.0 for o in outcomes]
            )[-self.baseline_size:]

    def record_prediction(self, prediction: float) -> None:
        with self._lock:
            self._recent_predictions.append(float(prediction))

    def record_label(self, outcome: bool) -> None:
        with self._lock:
            self._recent_labels.append(bool(outcome))

    def check_feature_drift(self, current_features: pd.DataFrame) -> List[DriftResult]:
        """Check PSI for each feature column."""
        results: List[DriftResult] = []
        if self._feature_baseline is None or current_features is None:
            return results
        recent = current_features.tail(self.recent_size)
        common_cols = [
            c for c in self._feature_baseline.columns
            if c in recent.columns and c != "date"
        ]
        for col in common_cols:
            try:
                psi = self._compute_psi(
                    self._feature_baseline[col].dropna().values,
                    recent[col].dropna().values,
                )
                if pd.isna(psi):
                    continue
                results.append(DriftResult(
                    dimension="feature",
                    feature_name=col,
                    psi=float(psi),
                    is_warning=psi >= self.psi_warn,
                    is_critical=psi >= self.psi_critical,
                    baseline_mean=float(self._feature_baseline[col].mean()),
                    recent_mean=float(recent[col].mean()),
                ))
            except Exception as exc:
                logger.debug("PSI computation failed for %s: %s", col, exc)
        return results

    def check_prediction_drift(self) -> Optional[DriftResult]:
        """Check PSI on recent predictions vs baseline."""
        with self._lock:
            if (
                self._prediction_baseline is None
                or len(self._recent_predictions) < self.recent_size // 2
            ):
                return None
            psi = self._compute_psi(
                self._prediction_baseline,
                np.array(list(self._recent_predictions)),
            )
            if pd.isna(psi):
                return None
            return DriftResult(
                dimension="prediction",
                feature_name=None,
                psi=float(psi),
                is_warning=psi >= self.psi_warn,
                is_critical=psi >= self.psi_critical,
                baseline_mean=float(np.mean(self._prediction_baseline)),
                recent_mean=float(np.mean(list(self._recent_predictions))),
            )

    def check_label_drift(self) -> Optional[DriftResult]:
        """Check PSI on recent labels vs baseline."""
        with self._lock:
            if (
                self._label_baseline is None
                or len(self._recent_labels) < self.recent_size // 2
            ):
                return None
            recent = np.array([1.0 if x else 0.0 for x in self._recent_labels])
            psi = self._compute_psi(self._label_baseline, recent)
            if pd.isna(psi):
                return None
            return DriftResult(
                dimension="label",
                feature_name=None,
                psi=float(psi),
                is_warning=psi >= self.psi_warn,
                is_critical=psi >= self.psi_critical,
                baseline_mean=float(np.mean(self._label_baseline)),
                recent_mean=float(np.mean(recent)),
            )

    @staticmethod
    def _compute_psi(baseline: np.ndarray, recent: np.ndarray) -> float:
        """
        Population Stability Index.

        PSI = Σ (actual_pct - expected_pct) × ln(actual_pct / expected_pct)

        Bins: 10 equal-frequency bins from baseline.
        """
        if len(baseline) < 10 or len(recent) < 10:
            return float("nan")
        # Use quantiles of baseline as bin edges
        quantiles = np.quantile(baseline, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
        bins = np.unique(np.concatenate([[-np.inf], quantiles, [np.inf]]))
        baseline_counts, _ = np.histogram(baseline, bins=bins)
        recent_counts, _ = np.histogram(recent, bins=bins)
        baseline_pct = baseline_counts / len(baseline)
        recent_pct = recent_counts / len(recent)
        # Avoid log(0)
        eps = 1e-6
        baseline_pct = np.clip(baseline_pct, eps, None)
        recent_pct = np.clip(recent_pct, eps, None)
        psi = np.sum((recent_pct - baseline_pct) * np.log(recent_pct / baseline_pct))
        return float(psi)

    def get_diagnostics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "baseline_size": self.baseline_size,
                "recent_size": self.recent_size,
                "has_feature_baseline": self._feature_baseline is not None,
                "has_prediction_baseline": self._prediction_baseline is not None,
                "has_label_baseline": self._label_baseline is not None,
                "n_recent_predictions": len(self._recent_predictions),
                "n_recent_labels": len(self._recent_labels),
            }


# =============================================================================
# Module 5: EVComputer (uses LOWER BOUND, ATR-scaled)
# =============================================================================


class EVComputer:
    """
    Compute Expected Value using the LOWER BOUND of the confidence interval
    and ATR-scaled reward/risk.

    Fixes DEFECT-4 (lower bound) and DEFECT-5 (ATR-scaled).

    EV = P(win) × reward_usd − P(loss) × risk_usd − costs_usd

    where:
      P(win)     = confidence_interval.lower  (NOT point estimate)
      P(loss)    = 1 − P(win)
      reward_usd = (atr × tp_ratio) × notional / close
      risk_usd   = (atr × stop_mult) × notional / close
      costs_usd  = fees + slippage + funding_drag
    """

    # Regime adjustment factors (from master plan Section 6.8)
    REGIME_ADJUSTMENTS: Dict[str, float] = {
        "TRENDING_STRONG": 1.00,
        "TRENDING_WEAK": 0.90,
        "RANGING": 0.85,
        "BREAKOUT": 0.75,
        "MEAN_REVERSION": 0.90,
        "HIGH_VOLATILITY": 0.75,
        "LOW_VOLATILITY": 0.95,
        "NEWS_DRIVEN": 0.50,
        "CHOPPY": 0.50,
        "TRANSITIONAL": 0.90,
    }

    def __init__(
        self,
        fee_rate: float = DEFAULT_FEE_RATE,
        slippage_rate: float = DEFAULT_SLIPPAGE_RATE,
        funding_drag_per_hour: float = DEFAULT_FUNDING_DRAG_PER_HOUR,
        tp_ratio: float = DEFAULT_TP_RATIO,
        stop_atr_mult: float = 1.5,
        min_ev_usd: float = DEFAULT_MIN_EV_USD,
    ) -> None:
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.funding_drag_per_hour = funding_drag_per_hour
        self.tp_ratio = tp_ratio
        self.stop_atr_mult = stop_atr_mult
        self.min_ev_usd = min_ev_usd

    def compute(
        self,
        confidence_interval: ConfidenceInterval,
        atr: float,
        close: float,
        notional: float,
        regime: str,
        leverage: float,
        expected_holding_hours: float = 0.5,
    ) -> EVResult:
        """
        Compute EV using lower bound of confidence interval.

        Parameters
        ----------
        confidence_interval : ConfidenceInterval
            The interval from ConfidenceIntervalCalculator.
        atr : float
            Current ATR value (price units).
        close : float
            Current close price.
        notional : float
            Position notional in USDT.
        regime : str
            Regime label.
        leverage : float
            Position leverage.
        expected_holding_hours : float
            Expected holding duration for funding drag estimation.
        """
        # Validate inputs
        if (
            pd.isna(atr) or atr <= 0
            or pd.isna(close) or close <= 0
            or pd.isna(notional) or notional <= 0
        ):
            return EVResult(
                ev_usd=float("-inf"),
                ev_per_dollar_risked=0.0,
                p_win=0.0,
                p_loss=1.0,
                reward_usd=0.0,
                risk_usd=0.0,
                costs_usd=0.0,
                regime_adjustment=1.0,
                meets_minimum=False,
            )

        # === USE LOWER BOUND (P6, INV-9) ===
        p_win = confidence_interval.lower
        p_loss = 1.0 - p_win

        # === ATR-scaled reward and risk (DEFECT-5 fix) ===
        reward_pct = (atr * self.tp_ratio) / close
        risk_pct = (atr * self.stop_atr_mult) / close

        reward_usd = reward_pct * notional
        risk_usd = risk_pct * notional

        # === Costs ===
        costs_usd = (
            self.fee_rate * notional * 2  # entry + exit
            + self.slippage_rate * notional * 2
            + self.funding_drag_per_hour * expected_holding_hours * notional / 100.0
        )

        # === Raw EV ===
        ev = (p_win * reward_usd) - (p_loss * risk_usd) - costs_usd

        # === Regime adjustment ===
        regime_adj = self.REGIME_ADJUSTMENTS.get(regime, 0.90)
        ev_adjusted = ev * regime_adj

        ev_per_dollar_risked = ev_adjusted / risk_usd if risk_usd > 0 else 0.0

        return EVResult(
            ev_usd=float(ev_adjusted),
            ev_per_dollar_risked=float(ev_per_dollar_risked),
            p_win=float(p_win),
            p_loss=float(p_loss),
            reward_usd=float(reward_usd),
            risk_usd=float(risk_usd),
            costs_usd=float(costs_usd),
            regime_adjustment=float(regime_adj),
            meets_minimum=bool(ev_adjusted >= self.min_ev_usd),
        )


# =============================================================================
# Module 6: TradeFilter (7 gates)
# =============================================================================


class TradeFilter:
    """
    Apply 7 confidence/risk gates. Any failure rejects the trade.

    Fixes DEFECT-8: replaces inline gating with a proper module.

    Gates (from master plan Section 6.9):
      G1: do_predict == 1
      G2: DI_values < 1.0
      G3: confidence_interval.width <= 0.40
      G4: ev_usd >= min_ev_usd
      G5: risk_score < 0.80
      G6: regime not in {NEWS_DRIVEN, CHOPPY}
      G7: ML direction matches rule direction
    """

    BLOCKED_REGIMES = {"NEWS_DRIVEN", "CHOPPY"}

    def __init__(
        self,
        max_interval_width: float = DEFAULT_MAX_WIDTH,
        min_ev_usd: float = DEFAULT_MIN_EV_USD,
        max_risk_score: float = 0.80,
        di_threshold: float = 1.0,
    ) -> None:
        self.max_interval_width = max_interval_width
        self.min_ev_usd = min_ev_usd
        self.max_risk_score = max_risk_score
        self.di_threshold = di_threshold

    def filter(
        self,
        do_predict: int,
        di_value: float,
        confidence_interval: ConfidenceInterval,
        ev_result: EVResult,
        risk_score: float,
        regime: str,
        ml_direction: str,
        rule_direction: str,
    ) -> FilterResult:
        """Run all 7 gates. Returns FilterResult with accepted/reason."""
        # G1: do_predict
        if do_predict != 1:
            return FilterResult(False, "G1: do_predict != 1 (out-of-distribution)", "G1")

        # G2: DI threshold
        if pd.isna(di_value) or di_value >= self.di_threshold:
            return FilterResult(
                False, f"G2: DI_values {di_value} >= {self.di_threshold}", "G2"
            )

        # G3: interval width
        if confidence_interval.width > self.max_interval_width:
            return FilterResult(
                False,
                f"G3: interval width {confidence_interval.width:.3f} > {self.max_interval_width}",
                "G3",
            )

        # G4: EV minimum
        if not ev_result.meets_minimum or ev_result.ev_usd < self.min_ev_usd:
            return FilterResult(
                False,
                f"G4: EV ${ev_result.ev_usd:.4f} < ${self.min_ev_usd}",
                "G4",
            )

        # G5: risk score
        if pd.isna(risk_score) or risk_score >= self.max_risk_score:
            return FilterResult(
                False,
                f"G5: risk_score {risk_score:.3f} >= {self.max_risk_score}",
                "G5",
            )

        # G6: regime block
        if regime in self.BLOCKED_REGIMES:
            return FilterResult(
                False, f"G6: regime {regime} blocks new entries", "G6"
            )

        # G7: direction match
        if ml_direction != rule_direction:
            return FilterResult(
                False,
                f"G7: ML direction {ml_direction} != rule direction {rule_direction}",
                "G7",
            )

        return FilterResult(True, "All gates passed")


# =============================================================================
# Module 7: CalibrationValidator (Brier, ECE, reliability diagram, etc.)
# =============================================================================


class CalibrationValidator:
    """
    Continuously validate calibration. Computes Brier, ECE, log-loss, AUC-ROC,
    reliability diagram, and confidence distribution stats.

    Fixes DEFECT-7: missing CalibrationValidator.

    Runs hourly (called from bot_loop_start).
    """

    def __init__(self, n_bins: int = DEFAULT_N_BINS) -> None:
        self.n_bins = n_bins
        self._latest_report: Optional[CalibrationReport] = None

    def run_validation(
        self,
        observations: List[Tuple[str, str, float, bool]],
    ) -> CalibrationReport:
        """
        Run full calibration validation on the observation list.

        Parameters
        ----------
        observations : list of (side, regime, predicted_prob, was_correct)
        """
        if len(observations) < self.n_bins:
            return CalibrationReport(
                timestamp=datetime.now(timezone.utc).isoformat(),
                n_observations=len(observations),
                brier_score=float("nan"),
                ece=float("nan"),
                log_loss=float("nan"),
                auc_roc=float("nan"),
                reliability_diagram={"predicted": [], "actual": []},
                confidence_stats={"mean": 0.5, "std": 0.0, "p10": 0.5, "p90": 0.5},
                alerts=["Insufficient observations for validation"],
            )

        predicted = np.array([o[2] for o in observations])
        actual = np.array([1.0 if o[3] else 0.0 for o in observations])

        # Brier score
        brier = float(np.mean((predicted - actual) ** 2))

        # ECE (n_bins bins)
        ece_sum = 0.0
        bin_predicted = []
        bin_actual = []
        for i in range(self.n_bins):
            lo, hi = i / self.n_bins, (i + 1) / self.n_bins
            mask = (predicted >= lo) & (predicted < hi)
            bin_size = int(np.sum(mask))
            if bin_size == 0:
                bin_predicted.append(0.0)
                bin_actual.append(0.0)
                continue
            avg_pred = float(np.mean(predicted[mask]))
            avg_actual = float(np.mean(actual[mask]))
            bin_predicted.append(avg_pred)
            bin_actual.append(avg_actual)
            ece_sum += (bin_size / len(predicted)) * abs(avg_pred - avg_actual)
        ece = float(ece_sum)

        # Log-loss
        eps = 1e-15
        pred_clipped = np.clip(predicted, eps, 1 - eps)
        log_loss = float(
            -np.mean(actual * np.log(pred_clipped) + (1 - actual) * np.log(1 - pred_clipped))
        )

        # AUC-ROC
        try:
            from sklearn.metrics import roc_auc_score
            if len(np.unique(actual)) > 1:
                auc = float(roc_auc_score(actual, predicted))
            else:
                auc = float("nan")
        except Exception:
            auc = float("nan")

        # Confidence distribution stats
        conf_stats = {
            "mean": float(np.mean(predicted)),
            "std": float(np.std(predicted)),
            "p10": float(np.percentile(predicted, 10)),
            "p90": float(np.percentile(predicted, 90)),
            "iqr": float(np.percentile(predicted, 75) - np.percentile(predicted, 25)),
        }

        # Alerts (from master plan Section 6.10)
        alerts: List[str] = []
        if not np.isnan(brier) and brier > 0.25:
            alerts.append(f"Brier score {brier:.4f} > 0.25 (force calibrator refit)")
        if not np.isnan(ece) and ece > 0.10:
            alerts.append(f"ECE {ece:.4f} > 0.10 (force calibrator refit)")
        if not np.isnan(log_loss) and log_loss > 0.69:
            alerts.append(f"Log-loss {log_loss:.4f} > 0.69 (model has no skill)")
        if not np.isnan(auc) and auc < 0.55:
            alerts.append(f"AUC-ROC {auc:.4f} < 0.55 (model has no skill)")
        if conf_stats["std"] < 0.05:
            alerts.append(
                f"Confidence std-dev {conf_stats['std']:.4f} < 0.05 (CLUSTERING COLLAPSE)"
            )
        if conf_stats["p90"] < 0.65:
            alerts.append(
                f"90th percentile confidence {conf_stats['p90']:.4f} < 0.65 "
                "(no high-confidence predictions)"
            )

        report = CalibrationReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            n_observations=len(observations),
            brier_score=brier,
            ece=ece,
            log_loss=log_loss,
            auc_roc=auc,
            reliability_diagram={"predicted": bin_predicted, "actual": bin_actual},
            confidence_stats=conf_stats,
            alerts=alerts,
        )
        self._latest_report = report
        return report

    def get_latest_report(self) -> Optional[CalibrationReport]:
        return self._latest_report


# =============================================================================
# Convenience: RiskScorer (lightweight, used by TradeFilter G5)
# =============================================================================


class RiskScorer:
    """
    Compute a risk_score in [0, 1] for a candidate trade.

    Used by TradeFilter gate G5. Combines:
      - Stoploss distance (ATR multiple)
      - Portfolio exposure after entry
      - Correlated exposure count
      - Sector exposure
      - Days since last stoploss
      - Current daily PnL
    """

    @staticmethod
    def compute(
        stop_distance_pct: float,
        portfolio_exposure_pct: float,
        correlated_count: int,
        sector_exposure_pct: float,
        days_since_stoploss: float,
        daily_pnl_pct: float,
    ) -> float:
        score = 0.0
        # Stoploss distance (closer to 5% = max risk)
        score += min(1.0, stop_distance_pct / 0.05) * 0.20
        # Portfolio exposure (closer to 3x = max risk)
        score += min(1.0, portfolio_exposure_pct / 3.0) * 0.25
        # Correlated positions (closer to 4 = max risk)
        score += min(1.0, correlated_count / 4.0) * 0.20
        # Sector exposure (closer to 40% = max risk)
        score += min(1.0, sector_exposure_pct / 0.40) * 0.15
        # Days since stoploss (recent stoploss = higher risk)
        if days_since_stoploss < 1.0:
            score += 0.10
        # Daily PnL (closer to -5% daily loss limit = higher risk)
        if daily_pnl_pct < 0:
            score += min(1.0, abs(daily_pnl_pct) / 0.05) * 0.10
        return float(max(0.0, min(1.0, score)))


# =============================================================================
# Process-wide singletons
# =============================================================================

_calibrator: Optional[IsotonicCalibrator] = None
_calibrator_lock = threading.Lock()
_drift_detector: Optional[UnifiedDriftDetector] = None
_drift_lock = threading.Lock()
_validator: Optional[CalibrationValidator] = None
_validator_lock = threading.Lock()


def get_calibrator() -> IsotonicCalibrator:
    global _calibrator
    if _calibrator is None:
        with _calibrator_lock:
            if _calibrator is None:
                _calibrator = IsotonicCalibrator()
    return _calibrator


def get_drift_detector() -> UnifiedDriftDetector:
    global _drift_detector
    if _drift_detector is None:
        with _drift_lock:
            if _drift_detector is None:
                _drift_detector = UnifiedDriftDetector()
    return _drift_detector


def get_validator() -> CalibrationValidator:
    global _validator
    if _validator is None:
        with _validator_lock:
            if _validator is None:
                _validator = CalibrationValidator()
    return _validator
