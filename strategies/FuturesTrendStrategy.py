# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone
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

logger = logging.getLogger(__name__)


class CTFuturesTrendStrategy(IStrategy):
    """
    ClaudeTrade Futures Trend-Following & Breakdown Strategy with Full Risk Stack
    
    Features:
    - 200 EMA Trend Filter (Long above 200 EMA, Short below 200 EMA)
    - Dynamic Inverse ATR Percentile & Regime Leverage (1x - 5x)
    - Multi-Tier ATR Stop Loss with Break-even & Stepped Trailing
    - ATR-Scaled Fixed Fractional Position Sizing (1% risk per trade)
    - Portfolio Exposure Cap (3x Notional) & BTC Volatility Regime Filter
    """

    INTERFACE_VERSION = 3

    # Futures trading enabled
    can_short: bool = True

    # ROI Table
    minimal_roi = {
        "0": 0.05,
        "30": 0.025,
        "60": 0.015,
        "120": 0.0,
    }

    # Hard Stoploss Floor
    stoploss = -0.10
    use_custom_stoploss = True

    # Trailing stoploss disabled here because custom_stoploss manages stepped trailing
    trailing_stop = False

    # Optimal timeframe
    timeframe = "5m"

    # Process only new candles
    process_only_new_candles = True

    # Startup candle requirement for EMA 200 calculation
    startup_candle_count: int = 200

    # Configurable leverage ceiling
    leverage_num: float = 5.0

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

    # Order types configuration
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
    }

    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

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
        """
        Dynamic leverage: scaled by inverse 200-candle ATR ratio, capped by meme tokens and exchange max.
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or len(dataframe) < 200:
            return min(2.0, max_leverage)

        atr = dataframe["atr"].iloc[-1]
        atr_median_30d = dataframe["atr"].rolling(200).median().iloc[-1]

        if pd.isna(atr_median_30d) or atr_median_30d == 0 or pd.isna(atr) or atr == 0:
            return min(2.0, max_leverage)

        ratio = atr_median_30d / atr
        lev = round(5.0 * ratio)
        lev = max(1.0, min(5.0, float(lev)))

        # Meme coin cap (2x)
        meme_coins = ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "MEME"]
        if any(meme in pair.upper() for meme in meme_coins):
            lev = min(lev, 2.0)

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
        """
        Multi-tier ATR-scaled stoploss with break-even and stepped trailing.
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or len(dataframe) == 0:
            return -0.10

        atr = dataframe["atr"].iloc[-1]
        if pd.isna(atr) or atr == 0:
            return -0.10

        # Tier 4: profit > 6% — lock 4% above entry
        if current_profit > 0.06:
            return stoploss_from_open(0.04, current_profit, is_short=trade.is_short, leverage=trade.leverage)

        # Tier 3: profit 3-6% — trail at 60% of profit (min 2%)
        if current_profit > 0.03:
            trail = max(0.02, current_profit * 0.60)
            return stoploss_from_open(current_profit - trail, current_profit, is_short=trade.is_short, leverage=trade.leverage)

        # Tier 2: profit 1-3% — trail at 50% of profit (min 1%)
        if current_profit > 0.01:
            trail = max(0.01, current_profit * 0.50)
            return stoploss_from_open(current_profit - trail, current_profit, is_short=trade.is_short, leverage=trade.leverage)

        # Tier 1: profit >= 1.5% — break-even (+0.5% to cover fees at 5x)
        if current_profit >= 0.015:
            return stoploss_from_open(0.005, current_profit, is_short=trade.is_short, leverage=trade.leverage)

        # Tier 0: profit < 1.5% — ATR stop (1.5 × ATR from open rate)
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
        """
        ATR-scaled position sizing within fixed max-risk-per-trade percentage (1% equity).
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or len(dataframe) == 0:
            return min(proposed_stake, max_stake)

        atr = dataframe["atr"].iloc[-1]
        close = dataframe["close"].iloc[-1]
        if pd.isna(atr) or pd.isna(close) or close == 0 or atr == 0:
            return min(proposed_stake, max_stake)

        # Risk per trade: 1% of total equity
        equity = self.wallets.get_total_stake_amount()
        risk_per_trade_usd = 0.01 * equity

        # Stop distance: 1.5 * ATR
        stop_distance_pct = (1.5 * atr) / close

        # Base stake = risk_usd / stop_distance_pct
        base_stake = risk_per_trade_usd / stop_distance_pct

        # Hard cap: $50 notional per trade
        final_stake = min(base_stake, 50.0 / max(1.0, leverage))

        # Binance min notional check (~$5 USDT)
        min_notional = 5.0
        if final_stake * leverage * close < min_notional:
            return 0.0

        if min_stake is not None and final_stake < min_stake:
            return 0.0

        return min(final_stake, max_stake)

    def _get_btc_vol_scale(self) -> float:
        """
        Checks BTC 1h volatility regime; returns 0.0 to block entries if BTC vol > 2x median.
        """
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
        """
        Portfolio-level entry gates: Notional cap (3x equity) and BTC Volatility regime block.
        """
        equity = self.wallets.get_total_stake_amount()
        open_trades = self.trades
        total_notional = sum(t.stake_amount * t.leverage for t in open_trades if t.is_open)
        new_notional = amount * rate

        if (total_notional + new_notional) > 3.0 * equity:
            logger.info(f"Rejecting entry for {pair}: portfolio notional cap exceeded.")
            return False

        btc_scale = self._get_btc_vol_scale()
        if btc_scale == 0.0:
            logger.info(f"Rejecting entry for {pair}: BTC vol regime > 2x median.")
            return False

        return True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Populate TA indicators for Trend-Following and Breakdown strategy.
        """
        # Trend Indicators (EMAs)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)

        # Momentum Indicators
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        # Bollinger Bands
        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"] = bollinger["upper"]

        # Volatility & Volume
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["volume_mean_20"] = dataframe["volume"].rolling(20).mean()

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Populates Long and Short entry signals based on 200 EMA trend filtering.
        """
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["ema200"])
                & (dataframe["ema50"] > dataframe["ema200"])
                & (
                    (qtpylib.crossed_above(dataframe["rsi"], 40))
                    | (qtpylib.crossed_above(dataframe["close"], dataframe["bb_upperband"]))
                )
                & (dataframe["adx"] > 18)
                & (dataframe["volume"] > dataframe["volume_mean_20"])
            ),
            "enter_long",
        ] = 1

        dataframe.loc[
            (
                (dataframe["close"] < dataframe["ema200"])
                & (dataframe["ema50"] < dataframe["ema200"])
                & (
                    (qtpylib.crossed_below(dataframe["rsi"], 60))
                    | (qtpylib.crossed_below(dataframe["close"], dataframe["bb_lowerband"]))
                )
                & (dataframe["adx"] > 18)
                & (dataframe["volume"] > dataframe["volume_mean_20"])
            ),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Populates exit signals for Long and Short positions.
        """
        dataframe.loc[
            (
                (dataframe["rsi"] > 70)
                | (qtpylib.crossed_below(dataframe["close"], dataframe["ema50"]))
            ),
            "exit_long",
        ] = 1

        dataframe.loc[
            (
                (dataframe["rsi"] < 30)
                | (qtpylib.crossed_above(dataframe["close"], dataframe["ema50"]))
            ),
            "exit_short",
        ] = 1

        return dataframe
