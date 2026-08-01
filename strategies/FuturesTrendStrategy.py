# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
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
)

import talib.abstract as ta
from technical import qtpylib


class CTFuturesTrendStrategy(IStrategy):
    """
    ClaudeTrade Futures Trend-Following & Breakdown Strategy
    
    Features:
    - 200 EMA Trend Filter (Only Long above 200 EMA, Only Short below 200 EMA)
    - Momentum & Breakdown Entry Signals for both Long and Short
    - Dynamic 5x Leverage with Exchange Max Limit Check
    - Trailing Stop Loss for Profit Protection
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

    # Stoploss: 5% initial risk per trade
    stoploss = -0.05

    # Trailing stoploss settings
    trailing_stop = True
    trailing_stop_positive = 0.015
    trailing_stop_positive_offset = 0.025
    trailing_only_offset_is_reached = True

    # Optimal timeframe
    timeframe = "5m"

    # Process only new candles
    process_only_new_candles = True

    # Startup candle requirement for EMA 200 calculation
    startup_candle_count: int = 200

    # Configurable leverage setting
    leverage_num: float = 5.0

    # Order types configuration
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
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
        Calculates leverage for each trade, dynamically capped at exchange max leverage.
        """
        return min(self.leverage_num, max_leverage)

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
        # LONG Entry Rules:
        # 1. Price is ABOVE 200 EMA (Uptrend filter)
        # 2. 50 EMA is ABOVE 200 EMA (Strong bull trend)
        # 3. RSI crosses above 40 (bullish momentum) OR Close crosses above upper Bollinger Band (breakout)
        # 4. Volume is above 20-period average
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

        # SHORT Entry Rules:
        # 1. Price is BELOW 200 EMA (Downtrend filter)
        # 2. 50 EMA is BELOW 200 EMA (Strong bear trend)
        # 3. RSI crosses below 60 (bearish momentum) OR Close crosses below lower Bollinger Band (breakdown)
        # 4. Volume is above 20-period average
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
        # Exit Long: RSI > 70 or price crosses below 50 EMA
        dataframe.loc[
            (
                (dataframe["rsi"] > 70)
                | (qtpylib.crossed_below(dataframe["close"], dataframe["ema50"]))
            ),
            "exit_long",
        ] = 1

        # Exit Short: RSI < 30 or price crosses above 50 EMA
        dataframe.loc[
            (
                (dataframe["rsi"] < 30)
                | (qtpylib.crossed_above(dataframe["close"], dataframe["ema50"]))
            ),
            "exit_short",
        ] = 1

        return dataframe
