# Binance 4H Trend-Cross Trading Bot
# Baseline + ATR risk/exit configuration

SIGNAL_SYMBOL = "ETHUSDT"
LONG_SYMBOL = "ETHUSD_PERP"
SHORT_SYMBOL = "ETHUSDT"
INTERVAL = "4h"

# Indicator parameters
EMA_FAST = 50
EMA_SLOW = 100   # revised from 200 — column/var names ("ema100") were already
                  # named for this; the config value just hadn't matched until now.
ADX_LENGTH = 14
ADX_THRESHOLD = 20   # was 25 — ADX lags; 25 waited for the trend to be fully established

# Supertrend filter
SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 7.8
# Bollinger Bands (TEST 11: short-side re-entry filter)
BB_LENGTH = 20
BB_STD_MULTIPLIER = 2.0
USE_BB_SHORT_FILTER = False
CCI_LENGTH = 20
CCI_LONG_THRESHOLD = 100.0
CCI_SHORT_THRESHOLD = -50
RSI_LENGTH = 14
RSI_LONG_THRESHOLD = 52   # was 55 — catches momentum a bit earlier
RSI_LONG_MAX = 72.0       # kept as a hard, non-scored overbought cap (see strategy.py)
RSI_SHORT_THRESHOLD = 30

STOCH_RSI_RSI_LENGTH = 14
STOCH_RSI_STOCH_LENGTH = 14
STOCH_RSI_K_SMOOTH = 3
STOCH_RSI_D_SMOOTH = 3
STOCH_LONG_THRESHOLD = 20
STOCH_LONG_D_THRESHOLD = 30.0
STOCH_SHORT_THRESHOLD = 80

# Historical condition validity
CCI_VALID_BARS = 3    # was 1 — no longer forces CCI to spike on the exact same candle as the stoch cross
STOCH_VALID_BARS = 3

# Entry scoring — as of this test, long_signal/short_signal no longer require
# ALL 8 confluence conditions (see strategy.py long_conditions/short_conditions);
# this many of the 8 must be true. The RSI overbought cap remains a separate,
# always-hard safety veto and is not part of this count.
# Entry scoring — as of this test, long_signal/short_signal no longer require
# ALL 8 confluence conditions (see strategy.py long_conditions/short_conditions);
# this many of the 8 must be true. The RSI overbought cap remains a separate,
# always-hard safety veto and is not part of this count.
ENTRY_MIN_SCORE = 7          # LONG threshold
SHORT_ENTRY_MIN_SCORE = 8    # SHORT threshold — stricter: backtest showed SHORT
                               # net-losing (PF 0.90) at the same 7/8 bar LONG
                               # was thriving at (PF 1.38); ETH's long-run upward
                               # drift makes shorting it structurally harder.

# ATR risk / exit model
ATR_LENGTH = 14
ATR_SL_MULTIPLIER = 3.5
ATR_SHORT_SL_MULTIPLIER = 1.35       # TEST 1: Initial stop distance = ATR * 2.0
ATR_LONG_TP_MULTIPLIER = 4.0   # TEST 32: Long TP = ATR * 4.0
ATR_SHORT_TP_MULTIPLIER = 3.0  # TEST 32: Short TP = ATR * 3.0
ATR_TRAIL_ACTIVATION = 2.2    # TEST 1: Activate trailing after +2 ATR unrealized
ATR_TRAIL_MULTIPLIER = 1.3    # was 2.0 — at 2.0 the trail sat only 0.2 ATR above
                                # entry the instant it activated (2.2-2.0), so a
                                # small pullback right after activation locked in
                                # a near-breakeven exit instead of riding toward TP.
                                # 1.3 locks in ~0.9 ATR immediately and keeps
                                # trailing closer to the peak all the way up.

USE_ATR_SL = True
USE_ATR_TP = True
USE_ATR_TRAILING = True
USE_EMA100_EXIT = False

# Backtest execution / costs
INITIAL_CAPITAL = 10000.0

# Starting balance for the LIVE DASHBOARD's own paper-trading demo account
# (the main ETH/BTC bot equity curve shown on the site, separate from any
# real user's live-trading money). Deliberately its own constant, not
# INITIAL_CAPITAL above: INITIAL_CAPITAL anchors the published backtest
# reports (deck, /backtest page) to a $10,000 baseline, and must stay that
# way for those numbers to still make sense. Can still be overridden per
# deployment with the PAPER_INITIAL_CAPITAL env var.
PAPER_ACCOUNT_INITIAL_CAPITAL = 100000.0
POSITION_QTY_ETH = 1.0        # Fixed position size: exactly 1 ETH per trade
POSITION_SIZE_PCT = 1.0       # Kept for compatibility; NOT used for sizing in TEST 16
LEVERAGE = 1.0
FEE_RATE = 0.0004            # 0.04% per side
SLIPPAGE_RATE = 0.0002       # 0.02% assumed execution slippage

# Backtest data
DATA_START = "2020-01-01"
DATA_END = None              # e.g. "2026-09-01"

# Output
TRADES_CSV = "backtest_trades.csv"
EQUITY_CSV = "backtest_equity.csv"


# TEST 16: Fixed 1 ETH. LONG executes on ETHUSDT; SHORT executes on ETHUSDC.
# Signals/indicators are generated from ETHUSDT; execution/exits use the selected contract.
# RSI short threshold remains <30 from TEST 15.

# TEST 26 - MACD Long momentum filter
MACD_FAST_LENGTH = 12
MACD_SLOW_LENGTH = 26
MACD_SIGNAL_LENGTH = 9
USE_MACD_LONG_FILTER = True   # NOTE: no longer read as a hard gate — macd_bullish
                               # is now always one of the 8 scored LONG conditions.


# FINAL PAPER V1 — Daily volatility veto
VOLATILITY_ATR_LENGTH = 14
VOLATILITY_PERCENTILE_LENGTH = 365
VOLATILITY_LOW_PERCENTILE = 10.0
VOLATILITY_HIGH_PERCENTILE = 95.0   # was 90 — strong trend days are often high-volatility days; don't veto as many of them
USE_VOLATILE_FILTER = True

# Watchlist — symbols manually added from the scanner ("+ Ekle"). Same
# strategy/indicators as the main ETH engine, but sized in USD notional
# instead of a fixed coin quantity, since altcoin prices vary hugely.
WATCHLIST_POSITION_USD = 250.0
WATCHLIST_MAX_SYMBOLS = 10

# AI Trade Analyst — read-only, periodic LLM-written report on the bot's own
# closed-trade history. It NEVER opens/closes positions and NEVER changes any
# parameter above; it only writes a text summary to state + Telegram. Needs
# ANTHROPIC_API_KEY in the environment; auto-disables without it.
AI_ANALYST_ENABLED = True
AI_ANALYST_MODEL = "claude-sonnet-5"
AI_ANALYST_MIN_INTERVAL_HOURS = 168   # weekly
AI_ANALYST_MIN_NEW_TRADES = 3
AI_ANALYST_LOOKBACK_TRADES = 40
