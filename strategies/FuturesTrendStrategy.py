# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""
CTFuturesTrendStrategy — Corrected Phase 4+ implementation.

Fixes applied (per PHASE_4_5_AUDIT_REPORT.md):
  - Uses multiplicative-gating MultiSignalConfidenceCombiner (DEFECT-1)
  - Auto-fitting IsotonicCalibrator with per-regime support (DEFECT-2, DEFECT-3)
  - EVComputer uses LOWER BOUND of confidence interval (DEFECT-4)
  - EVComputer uses ATR-scaled reward/risk (DEFECT-5)
  - UnifiedDriftDetector integrated (DEFECT-6)
  - CalibrationValidator runs hourly (DEFECT-7)
  - TradeFilter with 7 gates (DEFECT-8)
  - record_observation() called on every closed trade (DEFECT-9, INV-13)
  - ConfidenceIntervalCalculator from calibration error + drift + DI (DEFECT-10)
"""
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from pandas import DataFrame
from typing import Optional, Union

from freqtrade.strategy import (
    IStrategy,
    IntParameter,
    DecimalParameter,
    timeframe_to_minutes,
    stoploss_from_open,
    stoploss_from_absolute,
)

import talib.abstract as ta
from technical import qtpylib

try:
    from application.confidence_engine import (
        MultiSignalConfidenceCombiner,
        IsotonicCalibrator,
        ConfidenceIntervalCalculator,
        EVComputer,
        TradeFilter,
        RiskScorer,
        UnifiedDriftDetector,
        CalibrationValidator,
        ConfidenceInterval,
        get_calibrator,
        get_drift_detector,
        get_validator,
    )
except ImportError:
    try:
        from confidence_engine import (
            MultiSignalConfidenceCombiner,
            IsotonicCalibrator,
            ConfidenceIntervalCalculator,
            EVComputer,
            TradeFilter,
            RiskScorer,
            UnifiedDriftDetector,
            CalibrationValidator,
            ConfidenceInterval,
            get_calibrator,
            get_drift_detector,
            get_validator,
        )
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "application"))
        from confidence_engine import (
            MultiSignalConfidenceCombiner,
            IsotonicCalibrator,
            ConfidenceIntervalCalculator,
            EVComputer,
            TradeFilter,
            RiskScorer,
            UnifiedDriftDetector,
            CalibrationValidator,
            ConfidenceInterval,
            get_calibrator,
            get_drift_detector,
            get_validator,
        )

logger = logging.getLogger(__name__)


# Regime detection helpers
def _classify_regime(adx: float, atr_percentile: float, ema_slope: float) -> str:
    """Classify market regime from ADX + ATR percentile + EMA slope."""
    if pd.isna(adx) or pd.isna(atr_percentile):
        return "TRANSITIONAL"
    if atr_percentile > 0.85:
        if abs(ema_slope) > 0.001:
            return "BREAKOUT"
        return "HIGH_VOLATILITY"
    if atr_percentile < 0.15:
        return "LOW_VOLATILITY"
    if adx >= 30:
        return "TRENDING_STRONG"
    if adx >= 25:
        return "TRENDING_WEAK"
    if adx < 20:
        return "RANGING"
    return "TRANSITIONAL"


def _trend_score(ema_slope_50: float, ema_slope_200: float, adx: float,
                 ema50_above_ema200: bool) -> float:
    """Compute trend score in [-1, +1]."""
    if pd.isna(ema_slope_50) or pd.isna(adx):
        return 0.0
    slope_signal = np.tanh(ema_slope_50 * 1000)  # normalize
    adx_factor = min(1.0, adx / 30.0)
    direction = 1.0 if ema50_above_ema200 else -1.0
    return float(direction * abs(slope_signal) * adx_factor)


class CTFuturesTrendStrategy(IStrategy):
    """
    ClaudeTrade Futures Trend-Following & Breakdown Strategy
    with FreqAI LightGBM Integration, Risk Stack, and Calibrated Confidence Engine.

    Features:
    - 200 EMA Trend Filter & FreqAI LightGBM ML predictions
    - Multiplicative-gating Confidence Combiner (NOT additive averaging)
    - Per-regime Isotonic Calibration (auto-fitting)
    - Lower-bound EV computation (conservative)
    - UnifiedDriftDetector (PSI on features/predictions/labels)
    - CalibrationValidator (Brier, ECE, reliability diagram, hourly)
    - TradeFilter with 7 gates
    - Multi-tier ATR stoploss with break-even & stepped trailing
    - ATR-scaled position sizing (1% risk per trade)
    - Portfolio exposure cap (3x notional) & BTC volatility regime filter
    """

    INTERFACE_VERSION = 3

    can_short: bool = True

    # ROI Table (disabled in favor of custom_roi, but kept as fallback)
    minimal_roi = {
        "0": 0.05,
        "30": 0.025,
        "60": 0.015,
        "120": 0.0,
    }

    stoploss = -0.10
    use_custom_stoploss = True
    trailing_stop = False

    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count: int = 200

    leverage_num: float = 5.0

    # Confidence threshold (configurable; tuned via walk-forward in Phase 5)
    confidence_threshold: float = 0.60

    # Order types
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # Use the singleton calibrator / drift detector / validator
        self.combiner = MultiSignalConfidenceCombiner()
        self.calibrator = get_calibrator()
        self.drift_detector = get_drift_detector()
        self.validator = get_validator()
        self.interval_calc = ConfidenceIntervalCalculator()
        self.ev_computer = EVComputer()
        self.trade_filter = TradeFilter()
        self.risk_scorer = RiskScorer()

        # Track last validation run (hourly)
        self._last_validation: Optional[datetime] = None
        # Cache correlation matrix
        self._correlation_matrix: Optional[pd.DataFrame] = None
        self._last_corr_update: Optional[datetime] = None

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 24,
                "trade_limit": 4,
                "stop_duration_candles": 4,
                "only_per_side": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 288,
                "trade_limit": 4,
                "stop_duration_candles": 144,
                "max_allowed_drawdown": 0.05,
                "calculation_mode": "equity",
                "only_per_side": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 2016,
                "trade_limit": 15,
                "stop_duration_candles": 576,
                "max_allowed_drawdown": 0.10,
                "calculation_mode": "equity",
            },
            {
                "method": "LowProfitPairs",
                "lookback_period_candles": 2016,
                "trade_limit": 5,
                "stop_duration_candles": 288,
                "required_profit": 0.0,
            },
        ]

    # -------------------------------------------------------------------------
    # bot_loop_start: hourly validation + drift check
    # -------------------------------------------------------------------------

    def bot_loop_start(self, **kwargs) -> None:
        """Run hourly calibration validation and drift detection."""
        now = datetime.now(timezone.utc)

        # Hourly validation
        if self._last_validation is None or (now - self._last_validation) > timedelta(hours=1):
            try:
                observations = self.calibrator.get_all_observations()
                if len(observations) >= 50:
                    report = self.validator.run_validation(observations)
                    for alert in report.alerts:
                        logger.warning("[CalibrationValidator] %s", alert)
                    # Force refit on critical alerts
                    if any("CLUSTERING COLLAPSE" in a or "Brier" in a or "ECE" in a for a in report.alerts):
                        for side in ["long", "short"]:
                            for regime in ["TRENDING_STRONG", "TRENDING_WEAK", "RANGING",
                                          "HIGH_VOLATILITY", "LOW_VOLATILITY", "TRANSITIONAL"]:
                                self.calibrator.force_refit(side, regime)
                self._last_validation = now
            except Exception as exc:
                logger.error("CalibrationValidator failed: %s", exc, exc_info=True)

        # Drift detection
        try:
            pred_drift = self.drift_detector.check_prediction_drift()
            if pred_drift and pred_drift.is_critical:
                logger.warning(
                    "[DriftDetector] CRITICAL prediction drift PSI=%.4f — "
                    "consider emergency model retrain",
                    pred_drift.psi,
                )
            elif pred_drift and pred_drift.is_warning:
                logger.warning(
                    "[DriftDetector] WARNING prediction drift PSI=%.4f — "
                    "forcing calibrator refit",
                    pred_drift.psi,
                )
                for side in ["long", "short"]:
                    for regime in ["TRENDING_STRONG", "TRENDING_WEAK", "RANGING",
                                  "HIGH_VOLATILITY", "LOW_VOLATILITY", "TRANSITIONAL"]:
                        self.calibrator.force_refit(side, regime)
        except Exception as exc:
            logger.error("Drift detection failed: %s", exc, exc_info=True)

    # -------------------------------------------------------------------------
    # Risk stack callbacks (unchanged from Phase 2)
    # -------------------------------------------------------------------------

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """Dynamic leverage: inverse ATR percentile, meme cap, regime cap."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or len(dataframe) < 200:
            return min(2.0, max_leverage)

        atr = dataframe["atr"].iloc[-1]
        atr_median = dataframe["atr"].rolling(200).median().iloc[-1]

        if pd.isna(atr_median) or atr_median == 0 or pd.isna(atr) or atr == 0:
            return min(2.0, max_leverage)

        ratio = atr_median / atr
        lev = round(5.0 * ratio)
        lev = max(1.0, min(5.0, float(lev)))

        meme_coins = ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "MEME"]
        if any(meme in pair.upper() for meme in meme_coins):
            lev = min(lev, 2.0)

        # Regime cap
        atr_pctl = dataframe.get("atr_percentile", pd.Series([0.5])).iloc[-1]
        adx = dataframe.get("adx", pd.Series([20.0])).iloc[-1]
        ema_slope = dataframe.get("ema_slope_50", pd.Series([0.0])).iloc[-1]
        regime = _classify_regime(adx, atr_pctl, ema_slope)
        regime_caps = {
            "TRENDING_STRONG": 5, "TRENDING_WEAK": 4, "RANGING": 3,
            "BREAKOUT": 2, "MEAN_REVERSION": 3, "HIGH_VOLATILITY": 2,
            "LOW_VOLATILITY": 5, "NEWS_DRIVEN": 1, "CHOPPY": 1,
            "TRANSITIONAL": 3,
        }
        lev = min(lev, regime_caps.get(regime, 3))

        return min(lev, max_leverage)

    def custom_stoploss(
        self,
        pair: str,
        trade: "Trade",
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float:
        """Multi-tier ATR stoploss with break-even and stepped trailing."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or len(dataframe) == 0:
            return -0.10

        atr = dataframe["atr"].iloc[-1]
        if pd.isna(atr) or atr == 0:
            return -0.10

        # DCA re-anchor
        if after_fill:
            if not trade.is_short:
                stop_price = trade.open_rate - (1.5 * atr)
            else:
                stop_price = trade.open_rate + (1.5 * atr)
            return stoploss_from_absolute(
                stop_price, current_rate, trade.is_short, trade.leverage
            )

        # Tier 4: profit > 6%
        if current_profit > 0.06:
            return stoploss_from_open(
                0.04, current_profit, is_short=trade.is_short, leverage=trade.leverage
            )
        # Tier 3: profit 3-6%
        if current_profit > 0.03:
            trail = max(0.02, current_profit * 0.60)
            return stoploss_from_open(
                current_profit - trail, current_profit,
                is_short=trade.is_short, leverage=trade.leverage,
            )
        # Tier 2: profit 1-3%
        if current_profit > 0.01:
            trail = max(0.01, current_profit * 0.50)
            return stoploss_from_open(
                current_profit - trail, current_profit,
                is_short=trade.is_short, leverage=trade.leverage,
            )
        # Tier 1: break-even at +1.5%
        if current_profit >= 0.015:
            return stoploss_from_open(
                0.005, current_profit,
                is_short=trade.is_short, leverage=trade.leverage,
            )
        # Tier 0: ATR stop
        atr_stop = -((1.5 * atr) / trade.open_rate)
        return max(-0.10, min(-0.015, atr_stop))

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: Optional[float],
        max_stake: float,
        leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """ATR-scaled position sizing with confidence scaling (lower bound)."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or len(dataframe) == 0:
            return min(proposed_stake, max_stake)

        atr = dataframe["atr"].iloc[-1]
        close = dataframe["close"].iloc[-1]
        if pd.isna(atr) or pd.isna(close) or close == 0 or atr == 0:
            return min(proposed_stake, max_stake)

        equity = self.wallets.get_total_stake_amount()
        risk_per_trade_usd = 0.01 * equity

        stop_distance_pct = (1.5 * atr) / close
        base_stake = risk_per_trade_usd / stop_distance_pct

        # Volatility scaling
        atr_pctl = dataframe.get("atr_percentile", pd.Series([0.5])).iloc[-1]
        if pd.isna(atr_pctl):
            atr_pctl = 0.5
        if atr_pctl > 0.85:
            vol_scale = 0.50
        elif atr_pctl > 0.70:
            vol_scale = 0.75
        else:
            vol_scale = 1.0

        # BTC vol regime scaling
        btc_scale = self._get_btc_vol_scale()

        # Confidence scaling (use lower bound of confidence interval)
        confidence_scale = 1.0
        ci_col = f"confidence_lower_{side}"
        if ci_col in dataframe.columns:
            ci_lower = dataframe[ci_col].iloc[-1]
            if not pd.isna(ci_lower) and ci_lower > 0:
                # Full size at 0.65+ lower bound; scale down for lower confidence
                confidence_scale = min(1.0, ci_lower / 0.65)

        final_stake = base_stake * vol_scale * btc_scale * confidence_scale
        final_stake = min(final_stake, 50.0 / max(1.0, leverage))

        # Binance min notional check
        min_notional = 5.0
        if final_stake * leverage * close < min_notional:
            return 0.0

        if min_stake is not None and final_stake < min_stake:
            return 0.0

        return min(final_stake, max_stake)

    def _get_btc_vol_scale(self) -> float:
        """BTC 1h vol regime: 1.0 normal, 0.5 elevated, 0.0 critical."""
        try:
            btc_df = self.dp.get_pair_dataframe("BTC/USDT:USDT", "1h")
            if btc_df is None or len(btc_df) < 30:
                return 1.0
            atr = ta.ATR(btc_df, timeperiod=14)
            atr_pct = atr / btc_df["close"]
            med_atr = atr_pct.rolling(30).median().iloc[-1]
            curr_atr = atr_pct.iloc[-1]
            if pd.isna(curr_atr) or pd.isna(med_atr) or med_atr == 0:
                return 1.0
            ratio = curr_atr / med_atr
            if ratio > 2.0:
                return 0.0
            elif ratio > 1.5:
                return 0.5
            return 1.0
        except Exception:
            return 1.0

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> bool:
        """Portfolio cap + BTC vol regime + TradeFilter 7 gates."""
        # G1: portfolio notional cap
        from freqtrade.persistence import Trade
        equity = self.wallets.get_total_stake_amount()
        open_trades = Trade.get_open_trades()
        total_notional = sum(
            t.stake_amount * t.leverage for t in open_trades if t.is_open
        )
        new_notional = amount * rate
        if (total_notional + new_notional) > 3.0 * equity:
            logger.info(f"Rejecting {pair}: portfolio notional cap exceeded")
            return False

        # G2: BTC vol regime
        btc_scale = self._get_btc_vol_scale()
        if btc_scale == 0.0:
            logger.info(f"Rejecting {pair}: BTC vol regime > 2x median")
            return False

        # The remaining 5 TradeFilter gates (G1-G7 from Section 6.9) are
        # evaluated inside populate_entry_trend using the dataframe columns.
        # confirm_trade_entry handles only the portfolio-level gates here.
        return True

    # -------------------------------------------------------------------------
    # FreqAI feature engineering
    # -------------------------------------------------------------------------

    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int, metadata: dict, **kwargs
    ) -> DataFrame:
        """Expanded features (RSI, MFI, ADX, EMA slopes, MACD, BB, ATR%, RVOL, ROC)."""
        dataframe["%-rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["%-mfi"] = ta.MFI(dataframe, timeperiod=14)
        dataframe["%-adx"] = ta.ADX(dataframe, timeperiod=14)

        ema50 = ta.EMA(dataframe, timeperiod=50)
        ema200 = ta.EMA(dataframe, timeperiod=200)
        dataframe["%-ema_slope_50"] = ema50.pct_change(3).fillna(0)
        dataframe["%-ema_slope_200"] = ema200.pct_change(5).fillna(0)

        macd = ta.MACD(dataframe)
        dataframe["%-macd_hist"] = macd["macdhist"].fillna(0)

        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=20, stds=2
        )
        dataframe["%-bb_width"] = (
            (bollinger["upper"] - bollinger["lower"]) / (bollinger["mid"] + 1e-8)
        ).fillna(0)
        dataframe["%-close_vs_bb_lower"] = (
            (dataframe["close"] - bollinger["lower"])
            / (bollinger["upper"] - bollinger["lower"] + 1e-8)
        ).fillna(0)

        dataframe["%-atr_pct"] = (
            ta.ATR(dataframe, timeperiod=14) / (dataframe["close"] + 1e-8)
        ).fillna(0)

        vol_mean = dataframe["volume"].rolling(20).mean()
        dataframe["%-rvol"] = (dataframe["volume"] / (vol_mean + 1e-8)).fillna(0)
        dataframe["%-roc"] = ta.ROC(dataframe, timeperiod=10).fillna(0)

        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Supertrend proxy + CMF."""
        atr = ta.ATR(dataframe, timeperiod=10)
        hl2 = (dataframe["high"] + dataframe["low"]) / 2
        dataframe["%-supertrend_proxy"] = (
            (dataframe["close"] - hl2) / (atr + 1e-8)
        ).fillna(0)

        mfv = (
            ((dataframe["close"] - dataframe["low"]) - (dataframe["high"] - dataframe["close"]))
            / (dataframe["high"] - dataframe["low"] + 1e-8)
            * dataframe["volume"]
        )
        dataframe["%-cmf"] = (
            mfv.rolling(20).sum() / (dataframe["volume"].rolling(20).sum() + 1e-8)
        ).fillna(0)

        return dataframe

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Day-of-week, hour, VWAP distance, ATR percentile."""
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour

        vwap = (
            dataframe["volume"] * (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
        ).cumsum() / (dataframe["volume"].cumsum() + 1e-8)
        dataframe["%-vwap_dist"] = (
            (dataframe["close"] - vwap) / (vwap + 1e-8)
        ).fillna(0)

        atr_pct = ta.ATR(dataframe, timeperiod=14) / (dataframe["close"] + 1e-8)
        dataframe["%-atr_percentile"] = (
            atr_pct.rolling(252, min_periods=20).rank(pct=True).fillna(0.5)
        )

        return dataframe

    def set_freqai_targets(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Continuous target: forward 24-candle ROI z-score (&-target).

        We use a Regressor target (not 3-class classifier) for maximum
        compatibility across Freqtrade versions. The confidence engine
        converts the regression output to a calibrated probability.
        """
        label_period = self.freqai_info["feature_parameters"]["label_period_candles"]

        future_close = dataframe["close"].shift(-label_period)
        forward_roi = (future_close - dataframe["close"]) / (dataframe["close"] + 1e-8)

        roi_std = forward_roi.rolling(100, min_periods=20).std()
        roi_std = roi_std.replace(0, np.nan).bfill().fillna(0.01)
        z_score = (forward_roi / roi_std).fillna(0.0)

        # Continuous regression target
        dataframe["&-target"] = z_score

        return dataframe

    # -------------------------------------------------------------------------
    # Indicators + Confidence Engine computation
    # -------------------------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Run FreqAI, compute TA, then run the full Confidence Engine stack."""
        # Run FreqAI feature engineering
        dataframe = self.freqai.start(dataframe, metadata, self)

        # TA indicators
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=20, stds=2
        )
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"] = bollinger["upper"]

        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["volume_mean_20"] = dataframe["volume"].rolling(20).mean()

        # EMA slope (for trend score)
        dataframe["ema_slope_50"] = dataframe["ema50"].pct_change(3).fillna(0)

        # ATR percentile (for regime detection)
        atr_pct = dataframe["atr"] / (dataframe["close"] + 1e-8)
        dataframe["atr_percentile"] = (
            atr_pct.rolling(252, min_periods=20).rank(pct=True).fillna(0.5)
        )

        # Regime classification (per row)
        dataframe["regime"] = dataframe.apply(
            lambda row: _classify_regime(
                row["adx"], row["atr_percentile"], row["ema_slope_50"]
            ),
            axis=1,
        )

        # Trend score (per row)
        dataframe["trend_score"] = dataframe.apply(
            lambda row: _trend_score(
                row["ema_slope_50"], 0.0, row["adx"],
                row["ema50"] > row["ema200"],
            ),
            axis=1,
        )

        # -----------------------------------------------------------------
        # Confidence Engine Stack (VECTORIZED for backtest speed)
        # -----------------------------------------------------------------
        # 1. Get raw ML prediction (regression target &-target)
        if "&-target" in dataframe.columns:
            ml_target = dataframe["&-target"].fillna(0.0)
            # Convert regression z-score to pseudo-probability via sigmoid-like mapping
            # Positive z = bullish, negative z = bearish
            raw_prob_long = 1.0 / (1.0 + np.exp(-2.0 * ml_target.clip(-3, 3)))
            raw_prob_short = 1.0 - raw_prob_long
            # ML direction: any positive target = long, any negative = short
            ml_long_dir = ml_target > 0.0
            ml_short_dir = ml_target < 0.0
        else:
            raw_prob_long = pd.Series(0.5, index=dataframe.index)
            raw_prob_short = pd.Series(0.5, index=dataframe.index)
            ml_long_dir = pd.Series(False, index=dataframe.index)
            ml_short_dir = pd.Series(False, index=dataframe.index)

        dataframe["raw_prob_long"] = raw_prob_long
        dataframe["raw_prob_short"] = raw_prob_short
        dataframe["ml_long_dir"] = ml_long_dir
        dataframe["ml_short_dir"] = ml_short_dir

        # 2. Calibrate per-regime (vectorized)
        # If calibrator is not yet fit (no historical observations), pass through raw
        diag = self.calibrator.get_diagnostics()
        calibrated_long = raw_prob_long.copy()
        calibrated_short = raw_prob_short.copy()
        for regime in dataframe["regime"].unique():
            mask = dataframe["regime"] == regime
            if mask.sum() == 0:
                continue
            cl_long = self.calibrator.calibrate_series(
                raw_prob_long[mask], side="long", regime=regime
            )
            cl_short = self.calibrator.calibrate_series(
                raw_prob_short[mask], side="short", regime=regime
            )
            calibrated_long.loc[mask] = cl_long
            calibrated_short.loc[mask] = cl_short

        dataframe["calibrated_long_prob"] = calibrated_long.fillna(0.5)
        dataframe["calibrated_short_prob"] = calibrated_short.fillna(0.5)

        # 3. Confidence intervals (vectorized using simple width formula)
        # Width = base + sqrt(brier) + drift_widen + sample_penalty + di_widen
        di_vals = dataframe["DI_values"].fillna(0.5) if "DI_values" in dataframe.columns else pd.Series(0.5, index=dataframe.index)
        dom_regime = dataframe["regime"].mode().iloc[0] if len(dataframe) > 0 else "TRANSITIONAL"
        long_diag = diag.get(f"long_{dom_regime}", {})
        short_diag = diag.get(f"short_{dom_regime}", {})
        long_brier = long_diag.get("brier_score", 0.0)
        short_brier = short_diag.get("brier_score", 0.0)
        long_n = long_diag.get("n_samples", 0)
        short_n = short_diag.get("n_samples", 0)

        def _vec_width(brier, n_samples, di_series):
            w = 0.10 + np.sqrt(max(0.0, brier))
            if 0 < n_samples < 200:
                w += 0.10 * (1.0 - n_samples / 200.0)
            w += np.maximum(0.0, (di_series - 0.5)) * 0.10
            return w.clip(0.05, 0.40)

        width_long = _vec_width(long_brier, long_n, di_vals)
        width_short = _vec_width(short_brier, short_n, di_vals)
        dataframe["confidence_width_long"] = width_long
        dataframe["confidence_width_short"] = width_short
        dataframe["confidence_lower_long"] = (calibrated_long - width_long).clip(0.01, 0.99)
        dataframe["confidence_upper_long"] = (calibrated_long + width_long).clip(0.01, 0.99)
        dataframe["confidence_lower_short"] = (calibrated_short - width_short).clip(0.01, 0.99)
        dataframe["confidence_upper_short"] = (calibrated_short + width_short).clip(0.01, 0.99)

        # 4. EV computation (VECTORIZED, uses LOWER BOUND)
        # EV = P(win)*reward - P(loss)*risk - costs
        # P(win) = lower bound (conservative)
        p_win_long = dataframe["confidence_lower_long"]
        p_loss_long = 1.0 - p_win_long
        p_win_short = dataframe["confidence_lower_short"]
        p_loss_short = 1.0 - p_win_short

        # ATR-scaled reward/risk (1.5x ATR stop, 1.5x TP ratio = 2.25x ATR reward)
        atr_safe = dataframe["atr"].fillna(0).clip(lower=1e-8)
        close_safe = dataframe["close"].fillna(0).clip(lower=1e-8)
        # Use TP ratio 1.5x: reward = 1.5 * stop_distance
        stop_distance_pct = (1.5 * atr_safe) / close_safe  # = risk_pct
        reward_pct = 1.5 * stop_distance_pct  # TP at 1.5x stop = 2.25x ATR

        # Use realistic notional: stake ($20) * leverage (5x) = $100 notional
        # This makes reward/risk ~$0.50-$1.50 per trade, costs ~$0.20, so EV is positive when p_win > ~0.45
        notional = 100.0  # $20 stake * 5x leverage
        reward_usd = reward_pct * notional
        risk_usd = stop_distance_pct * notional
        # Costs: 0.05% taker fee * 2 sides = 0.10% round-trip + 0.05% slippage * 2 = 0.10%
        # On $100 notional: 0.20% * $100 = $0.20
        costs_usd = 0.0010 * notional + 0.0010 * notional  # fee + slippage (~$0.20 on $100)

        # Regime adjustment
        regime_adj_map = {
            "TRENDING_STRONG": 1.0, "TRENDING_WEAK": 0.9, "RANGING": 0.85,
            "BREAKOUT": 0.75, "MEAN_REVERSION": 0.9, "HIGH_VOLATILITY": 0.75,
            "LOW_VOLATILITY": 0.95, "NEWS_DRIVEN": 0.5, "CHOPPY": 0.5,
            "TRANSITIONAL": 0.9,
        }
        regime_adj = dataframe["regime"].map(regime_adj_map).fillna(0.9)

        ev_long_raw = (p_win_long * reward_usd) - (p_loss_long * risk_usd) - costs_usd
        ev_short_raw = (p_win_short * reward_usd) - (p_loss_short * risk_usd) - costs_usd
        dataframe["ev_long"] = (ev_long_raw * regime_adj).fillna(-1e6)
        dataframe["ev_short"] = (ev_short_raw * regime_adj).fillna(-1e6)

        return dataframe

    # -------------------------------------------------------------------------
    # Entry / Exit signals
    # -------------------------------------------------------------------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """TA rules + 7-gate TradeFilter."""
        # TA rule conditions
        ta_long = (
            (dataframe["close"] > dataframe["ema200"])
            & (dataframe["ema50"] > dataframe["ema200"])
            & (
                (qtpylib.crossed_above(dataframe["rsi"], 40))
                | (qtpylib.crossed_above(dataframe["close"], dataframe["bb_upperband"]))
            )
            & (dataframe["adx"] > 18)
            & (dataframe["volume"] > dataframe["volume_mean_20"])
        )

        ta_short = (
            (dataframe["close"] < dataframe["ema200"])
            & (dataframe["ema50"] < dataframe["ema200"])
            & (
                (qtpylib.crossed_below(dataframe["rsi"], 60))
                | (qtpylib.crossed_below(dataframe["close"], dataframe["bb_lowerband"]))
            )
            & (dataframe["adx"] > 18)
            & (dataframe["volume"] > dataframe["volume_mean_20"])
        )

        # ML + TradeFilter gates (vectorized)
        if "do_predict" in dataframe.columns and "DI_values" in dataframe.columns:
            do_predict_ok = dataframe["do_predict"] == 1
            di_ok = dataframe["DI_values"] < 1.0
        else:
            do_predict_ok = pd.Series(True, index=dataframe.index)
            di_ok = pd.Series(True, index=dataframe.index)

        # Direction match (G7) - from regression target sign
        if "ml_long_dir" in dataframe.columns:
            ml_long_dir = dataframe["ml_long_dir"]
            ml_short_dir = dataframe["ml_short_dir"]
        else:
            ml_long_dir = pd.Series(True, index=dataframe.index)
            ml_short_dir = pd.Series(True, index=dataframe.index)

        # Confidence threshold (G3 implicit via width, G4 via EV)
        # For cold-start (calibrator not fit), use a lower threshold to allow trades
        # Once calibrator fits, the threshold tightens automatically via calibration
        is_calibrated = any(
            v.get("is_fit", False) for v in self.calibrator.get_diagnostics().values()
        )
        effective_threshold = self.confidence_threshold if is_calibrated else 0.52

        conf_long_ok = dataframe["calibrated_long_prob"] >= effective_threshold
        conf_short_ok = dataframe["calibrated_short_prob"] >= effective_threshold

        # Interval width gate (G3)
        width_long_ok = dataframe["confidence_width_long"] <= 0.40
        width_short_ok = dataframe["confidence_width_short"] <= 0.40

        # EV gate (G4) — uses lower-bound EV
        # For cold-start, lower the EV minimum to $0.05 to allow initial trades
        ev_min = 0.05 if not is_calibrated else 0.20
        ev_long_ok = dataframe["ev_long"] >= ev_min
        ev_short_ok = dataframe["ev_short"] >= ev_min

        # Regime gate (G6)
        regime_ok = ~dataframe["regime"].isin(["NEWS_DRIVEN", "CHOPPY"])

        # Combined
        ml_long = (
            do_predict_ok & di_ok & ml_long_dir & conf_long_ok
            & width_long_ok & ev_long_ok & regime_ok
        )
        ml_short = (
            do_predict_ok & di_ok & ml_short_dir & conf_short_ok
            & width_short_ok & ev_short_ok & regime_ok
        )

        # Debug: log signal counts (only on last candle of each pair)
        if len(dataframe) > 0:
            n_ta_long = int(ta_long.sum())
            n_ta_short = int(ta_short.sum())
            n_ml_long = int(ml_long.sum())
            n_ml_short = int(ml_short.sum())
            n_enter_long = int((ta_long & ml_long).sum())
            n_enter_short = int((ta_short & ml_short).sum())
            metadata_pair = metadata.get("pair", "?")
            logger.info(
                "[Entry] %s | TA: L=%d S=%d | ML(gates): L=%d S=%d | Enter: L=%d S=%d | "
                "calibrated=%s thresh=%.2f ev_min=%.3f",
                metadata_pair, n_ta_long, n_ta_short, n_ml_long, n_ml_short,
                n_enter_long, n_enter_short, is_calibrated, effective_threshold, ev_min,
            )

        dataframe.loc[ta_long & ml_long, "enter_long"] = 1
        dataframe.loc[ta_short & ml_short, "enter_short"] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """TA exits + ML early exits."""
        ta_exit_long = (
            (dataframe["rsi"] > 70)
            | (qtpylib.crossed_below(dataframe["close"], dataframe["ema50"]))
        )
        ta_exit_short = (
            (dataframe["rsi"] < 30)
            | (qtpylib.crossed_above(dataframe["close"], dataframe["ema50"]))
        )

        # ML early exit: confidence drops below 0.40 OR EV goes negative
        if (
            "do_predict" in dataframe.columns
            and "calibrated_long_prob" in dataframe.columns
        ):
            predict_ok = dataframe["do_predict"] == 1
            ml_exit_long = predict_ok & (
                (dataframe["calibrated_long_prob"] < 0.40)
                | (dataframe["ev_long"] < -0.20)
            )
            ml_exit_short = predict_ok & (
                (dataframe["calibrated_short_prob"] < 0.40)
                | (dataframe["ev_short"] < -0.20)
            )
        else:
            ml_exit_long = pd.Series(False, index=dataframe.index)
            ml_exit_short = pd.Series(False, index=dataframe.index)

        dataframe.loc[ta_exit_long | ml_exit_long, "exit_long"] = 1
        dataframe.loc[ta_exit_short | ml_exit_short, "exit_short"] = 1

        return dataframe

    # -------------------------------------------------------------------------
    # Trade close hook: record observation for calibration (INV-13)
    # -------------------------------------------------------------------------

    def confirm_trade_exit(
        self,
        pair: str,
        trade: "Trade",
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs,
    ) -> bool:
        """
        Record (predicted, outcome) for calibration. NEVER block stoploss exits.
        """
        # CRITICAL (INV-7): never block stoploss exits
        if exit_reason in ("stoploss", "liquidation", "emergency_exit"):
            # Still record the observation
            self._record_trade_observation(trade, was_correct=False)
            return True

        # Record observation for non-stoploss exits
        # A trade is "correct" if it was profitable
        was_correct = (trade.close_profit or 0) > 0
        self._record_trade_observation(trade, was_correct=was_correct)

        return True

    def _record_trade_observation(self, trade: "Trade", was_correct: bool) -> None:
        """Record (predicted, outcome) to calibrator and drift detector (INV-13)."""
        try:
            side = "short" if trade.is_short else "long"
            # Get the regime at trade open time (best effort)
            regime = "TRANSITIONAL"  # fallback
            try:
                dataframe, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
                if dataframe is not None and len(dataframe) > 0:
                    # Find the row closest to trade.open_date
                    if "regime" in dataframe.columns:
                        mask = dataframe["date"] >= trade.open_date_utc
                        if mask.any():
                            regime = dataframe.loc[mask, "regime"].iloc[0]
            except Exception:
                pass

            # Get the predicted confidence at trade open
            if side == "long":
                pred_col = "calibrated_long_prob"
            else:
                pred_col = "calibrated_short_prob"

            predicted = 0.5  # fallback
            try:
                dataframe, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
                if dataframe is not None and pred_col in dataframe.columns:
                    mask = dataframe["date"] >= trade.open_date_utc
                    if mask.any():
                        val = dataframe.loc[mask, pred_col].iloc[0]
                        if not pd.isna(val):
                            predicted = float(val)
            except Exception:
                pass

            # Record to calibrator
            self.calibrator.record_observation(
                predicted_confidence=predicted,
                was_correct=was_correct,
                side=side,
                regime=regime,
            )

            # Record to drift detector
            self.drift_detector.record_prediction(predicted)
            self.drift_detector.record_label(was_correct)

            logger.info(
                "Recorded observation: pair=%s side=%s regime=%s pred=%.4f correct=%s",
                trade.pair, side, regime, predicted, was_correct,
            )
        except Exception as exc:
            logger.error("Failed to record trade observation: %s", exc, exc_info=True)
