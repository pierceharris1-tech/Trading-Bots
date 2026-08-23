"""
Order Block detection — ported from qc_research/order_block_study.py
(Cell 2 + Cell 4), locked to the GA-optimised, OOS-validated params.

Live adaptation vs. the backtest:
  - Backtest entered on "mitigation touch" (price closing back into the
    zone) and measured forward return from there. Live trading instead
    places a resting LIMIT order at the OB body edge the moment BOS
    confirms, so Kraken fills it passively rather than the bot waiting
    for a touch and market-ordering in (misses less of the move).
  - "impulse_lookahead" / "bos_lookahead" are described as "wait windows"
    here, not "lookahead" -- nothing about live detection looks into the
    future; a bar is only used once it exists.
"""
import numpy as np
import pandas as pd

# ---- locked, GA-optimised + OOS-validated params ----
WINNER = {
    "swing_n": 3,
    "impulse_atr_mult": 1.25,
    "impulse_wait": 8,      # bars after the OB candle to find the impulse
    "bos_wait": 60,         # bars after the OB candle to find confirming BOS
    "mitigation_wait": 200, # bars after BOS an entry order stays live before cancel
    "mode": "body",         # kept for parity with the research; live entry uses a
                             # resting limit order at the OB edge regardless of mode
}


def bars_elapsed(from_time, to_time, candle_minutes):
    """Real elapsed candle-count between two timestamps -- safe to compare
    across cycles, unlike raw buffer positions. The live candle buffer
    (bot.fetch_candles's .tail(BUFFER_BARS)) is a fixed-size sliding
    window, so a given bar's *position* inside it shifts every cycle even
    though the bar's own timestamp never changes -- comparing positions
    captured in different cycles (as the original cancel_unfilled did)
    silently gives the wrong answer. Timestamps don't have that problem.
    Approximate when the resampled series has gaps (dropna in
    resample_ohlc), close enough for a 200-bar mitigation window check."""
    return (to_time - from_time).total_seconds() / (candle_minutes * 60)


def compute_atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def resample_ohlc(minute_df, freq="3min"):
    """Kraken's OHLC endpoint has no native 3-min interval -- pull 1-min
    candles and build 3M bars locally. Drops any period with no trades."""
    o = minute_df["open"].resample(freq).first()
    h = minute_df["high"].resample(freq).max()
    l = minute_df["low"].resample(freq).min()
    c = minute_df["close"].resample(freq).last()
    out = pd.DataFrame({"open": o, "high": h, "low": l, "close": c})
    return out.dropna(subset=["open", "high", "low", "close"])


def detect_swings(d, n=3):
    """Fractal swing high/low. A swing at bar i is only knowable n bars
    later once the confirming bars exist -- shifted forward by n so
    nothing downstream ever uses information from the future."""
    high, low = d["high"], d["low"]
    roll_max = high.rolling(2 * n + 1, center=True).max()
    roll_min = low.rolling(2 * n + 1, center=True).min()
    is_swing_high = (high == roll_max) & roll_max.notna()
    is_swing_low = (low == roll_min) & roll_min.notna()
    swing_high_confirmed = (is_swing_high.shift(n) == True)
    swing_low_confirmed = (is_swing_low.shift(n) == True)
    swing_high_level = high.shift(n).where(swing_high_confirmed)
    swing_low_level = low.shift(n).where(swing_low_confirmed)
    return swing_high_confirmed, swing_low_confirmed, swing_high_level, swing_low_level


def prepare_bars(df):
    """Attach atr + known swing levels to a raw OHLC frame. Call this once
    per cycle on the rolling candle buffer before find_order_blocks."""
    d = df.copy()
    d["atr"] = compute_atr(d)
    _, _, swh_lvl, swl_lvl = detect_swings(d, n=WINNER["swing_n"])
    d["known_swing_high"] = swh_lvl.ffill()
    d["known_swing_low"] = swl_lvl.ffill()
    return d


def find_order_blocks(d, impulse_wait=None, impulse_atr_mult=None, bos_wait=None):
    """Same algorithm as the research notebook's find_order_blocks (Cell 4),
    param names only renamed lookahead->wait. Returns bull_df, bear_df of
    every OB whose impulse + BOS have already confirmed within the buffer."""
    impulse_wait = impulse_wait or WINNER["impulse_wait"]
    impulse_atr_mult = impulse_atr_mult or WINNER["impulse_atr_mult"]
    bos_wait = bos_wait or WINNER["bos_wait"]

    close = d["close"].values
    open_ = d["open"].values
    high = d["high"].values
    low = d["low"].values
    atr = d["atr"].values
    swh = d["known_swing_high"].values
    swl = d["known_swing_low"].values
    n = len(d)

    bull_obs, bear_obs = [], []
    last_i = n - bos_wait - 2
    for i in range(1, max(last_i, 1)):
        end = min(i + impulse_wait, n - 1)
        if end <= i:
            continue

        if close[i] < open_[i]:  # bullish OB candidate: red candle
            impulse_high = high[i + 1:end + 1].max()
            impulse_ok = (not np.isnan(atr[i]) and atr[i] > 0 and
                          (impulse_high - low[i]) >= impulse_atr_mult * atr[i])
            if impulse_ok:
                fvg_ok = any(low[k] > high[k - 2] for k in range(i + 2, end + 1))
                if fvg_ok:
                    bos_idx = None
                    for j in range(i + 1, min(i + 1 + bos_wait, n)):
                        level = swh[j]
                        if not np.isnan(level) and close[j] > level and close[j - 1] <= level:
                            bos_idx = j
                            break
                    if bos_idx is not None:
                        bull_obs.append({"ob_idx": i, "ob_time": d.index[i],
                                          "ob_high": high[i], "ob_low": low[i],
                                          "bos_idx": bos_idx, "bos_time": d.index[bos_idx]})

        if close[i] > open_[i]:  # bearish OB candidate: green candle
            impulse_low = low[i + 1:end + 1].min()
            impulse_ok = (not np.isnan(atr[i]) and atr[i] > 0 and
                          (high[i] - impulse_low) >= impulse_atr_mult * atr[i])
            if impulse_ok:
                fvg_ok = any(high[k] < low[k - 2] for k in range(i + 2, end + 1))
                if fvg_ok:
                    bos_idx = None
                    for j in range(i + 1, min(i + 1 + bos_wait, n)):
                        level = swl[j]
                        if not np.isnan(level) and close[j] < level and close[j - 1] >= level:
                            bos_idx = j
                            break
                    if bos_idx is not None:
                        bear_obs.append({"ob_idx": i, "ob_time": d.index[i],
                                          "ob_high": high[i], "ob_low": low[i],
                                          "bos_idx": bos_idx, "bos_time": d.index[bos_idx]})

    return pd.DataFrame(bull_obs), pd.DataFrame(bear_obs)


def get_new_signals(df, seen_ob_times, mitigation_wait=None):
    """
    df: prepared rolling candle buffer (output of prepare_bars).
    seen_ob_times: set of ob_time values the bot has already acted on
                   (persisted in state.py) -- prevents re-placing an
                   order for an OB detected on a previous cycle.
    mitigation_wait: an OB whose BOS confirmed more than this many bars
                   ago is stale -- price has had its whole entry window
                   to return to the zone and didn't, so it's no longer
                   tradeable. Filtered out here rather than left for the
                   caller, so "new signal" always means "still live",
                   never "old setup the bot just hasn't seen yet" (the
                   gap this closes: on a cold start with no seen_ob_times,
                   EVERY historical OB in the buffer would otherwise look
                   new and get an entry attempt).

    Returns a list of dicts, one per new tradeable signal:
      {direction, ob_time, ob_high, ob_low, entry_price, bos_time}
    entry_price is the OB body edge -- top for bull, bottom for bear --
    i.e. where the resting limit order should be placed.
    """
    mitigation_wait = mitigation_wait or WINNER["mitigation_wait"]
    bull_df, bear_df = find_order_blocks(df)
    current_bar_idx = len(df) - 1
    signals = []
    for direction, obs_df, entry_col in (("bull", bull_df, "ob_high"), ("bear", bear_df, "ob_low")):
        for _, row in obs_df.iterrows():
            if row["ob_time"] in seen_ob_times:
                continue
            if current_bar_idx - int(row["bos_idx"]) > mitigation_wait:
                continue  # stale -- BOS confirmed too long ago, entry window elapsed
            signals.append({
                "direction": direction, "ob_time": row["ob_time"], "bos_time": row["bos_time"],
                "ob_high": row["ob_high"], "ob_low": row["ob_low"],
                "entry_price": row[entry_col],
            })
    return signals
