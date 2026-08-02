"""
ClaudeTrade Confidence Engine Calibration Stack
Solves prediction clustering (45-60% collapse) using Isotonic Calibration,
Multi-Signal Combination, Confidence Interval Estimation, and EV Calculation.
"""
import logging
import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional
from sklearn.isotonic import IsotonicRegression

logger = logging.getLogger(__name__)


class MultiSignalConfidenceCombiner:
    """
    Combines TA indicators (RSI, ADX, EMA slope, Volume RVOL) with FreqAI ML regression output
    into a unified raw composite signal score in [0.0, 1.0].
    """

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or {
            "ml_signal": 0.40,
            "trend_strength": 0.20,
            "rsi_momentum": 0.15,
            "volume_rvol": 0.15,
            "bb_position": 0.10,
        }

    def combine(self, df: pd.DataFrame, side: str = "long") -> pd.Series:
        """
        Calculates raw composite confidence score for each candle.
        """
        # ML signal score normalized to [0, 1]
        if "&-target" in df.columns:
            target_val = df["&-target"].fillna(0.0)
            if side == "long":
                ml_score = (np.clip(target_val, -2.0, 2.0) + 2.0) / 4.0
            else:
                ml_score = (2.0 - np.clip(target_val, -2.0, 2.0)) / 4.0
        else:
            ml_score = pd.Series(0.5, index=df.index)

        # Trend strength (ADX normalized to [0, 1])
        adx_series = df["adx"] if "adx" in df.columns else pd.Series(20.0, index=df.index)
        adx_score = np.clip(adx_series / 50.0, 0.0, 1.0)

        # RSI momentum score
        rsi_series = df["rsi"] if "rsi" in df.columns else pd.Series(50.0, index=df.index)
        if side == "long":
            rsi_score = np.clip((rsi_series - 30.0) / 40.0, 0.0, 1.0)
        else:
            rsi_score = np.clip((70.0 - rsi_series) / 40.0, 0.0, 1.0)

        # Volume RVOL score
        vol = df["volume"] if "volume" in df.columns else pd.Series(1.0, index=df.index)
        vol_mean = df["volume_mean_20"] if "volume_mean_20" in df.columns else pd.Series(1.0, index=df.index)
        rvol = vol / (vol_mean + 1e-8)
        rvol_score = np.clip(rvol / 3.0, 0.0, 1.0)

        # Bollinger position score
        bb_upper = df["bb_upperband"] if "bb_upperband" in df.columns else df["close"]
        bb_lower = df["bb_lowerband"] if "bb_lowerband" in df.columns else df["close"]
        bb_width = bb_upper - bb_lower
        if side == "long":
            bb_score = np.clip((df["close"] - bb_lower) / (bb_width + 1e-8), 0.0, 1.0)
        else:
            bb_score = np.clip((bb_upper - df["close"]) / (bb_width + 1e-8), 0.0, 1.0)

        composite = (
            self.weights["ml_signal"] * ml_score
            + self.weights["trend_strength"] * adx_score
            + self.weights["rsi_momentum"] * rsi_score
            + self.weights["volume_rvol"] * rvol_score
            + self.weights["bb_position"] * bb_score
        )

        return np.clip(composite, 0.0, 1.0)


class IsotonicCalibrator:
    """
    Calibrates raw multi-signal scores into true empirical probability estimates
    using Isotonic Regression, preventing probability compression in 45-60% range.
    """

    def __init__(self):
        self.calibrator = IsotonicRegression(y_min=0.01, y_max=0.99, out_of_bounds="clip")
        self.is_fitted = False

    def fit(self, raw_scores: np.ndarray, empirical_outcomes: np.ndarray):
        """
        Fits Isotonic Regression mapping raw_scores -> empirical binary outcomes (1 for win, 0 for loss).
        """
        if len(raw_scores) > 20:
            self.calibrator.fit(raw_scores, empirical_outcomes)
            self.is_fitted = True

    def calibrate(self, raw_scores: pd.Series) -> pd.Series:
        """
        Transforms raw score to calibrated probability score.
        If not fitted, uses sigmoid transformation to spread scores away from 0.5 center.
        """
        if self.is_fitted:
            calibrated = self.calibrator.predict(raw_scores.values)
            return pd.Series(calibrated, index=raw_scores.index)
        else:
            # Parametric sigmoid stretch around 0.5 to prevent clustering collapse
            k = 6.0  # Steepness parameter
            stretched = 1.0 / (1.0 + np.exp(-k * (raw_scores - 0.5)))
            return pd.Series(stretched, index=raw_scores.index)


class ConfidenceIntervalCalculator:
    """
    Computes 95% Wilson score confidence bounds around calibrated predictions.
    """

    @staticmethod
    def compute_bounds(p: pd.Series, n: int = 50, z: float = 1.96) -> Tuple[pd.Series, pd.Series]:
        """
        Wilson Score interval calculation for calibrated probabilities.
        """
        denominator = 1.0 + (z**2) / n
        center = (p + (z**2) / (2 * n)) / denominator
        spread = z * np.sqrt((p * (1.0 - p) + (z**2) / (4 * n)) / n) / denominator

        lower_bound = np.clip(center - spread, 0.0, 1.0)
        upper_bound = np.clip(center + spread, 0.0, 1.0)

        return lower_bound, upper_bound


class EVComputer:
    """
    Calculates Expected Value (EV) per trade based on calibrated win probability and risk/reward ratio.
    EV = p * Reward - (1 - p) * Risk
    """

    @staticmethod
    def calculate_ev(
        win_prob: pd.Series,
        reward_pct: float = 0.03,  # Target reward: 3%
        risk_pct: float = 0.015,  # ATR stop risk: 1.5%
    ) -> pd.Series:
        """
        EV = (Win_Prob * Reward_Pct) - ((1 - Win_Prob) * Risk_Pct)
        """
        ev = (win_prob * reward_pct) - ((1.0 - win_prob) * risk_pct)
        return ev
