# CHoCH OANDA Bot — Canonical Frozen Strategy Specification

**Status:** Frozen implementation specification for the OANDA practice bot  
**Markets:** `EUR_USD`, `NAS100_USD` only  
**Purpose:** This document is the source of truth for porting the final research strategy into `bots/choch_live/`. It deliberately excludes superseded experiments.

---

## 1. Source hierarchy

This specification was reconstructed from the final research notebooks and the final decisions made after the timing-filter tests.

When implementing:

1. **Use the executable semantics in this document.**
2. Do not infer generic “CHoCH” behaviour from trading terminology.
3. Do not add filters or parameters that are not explicitly listed here.
4. If live broker mechanics differ from a historical bar simulator, preserve the signal semantics but use real OANDA transaction ordering for actual fills/exits.
5. The Friday timing filter at the end of this document is a **final operational overlay** selected after the core notebook runs. It supersedes the earlier notebook timing filter that tested Friday 08:00 alone.

---

# 2. Frozen Strategy 1

## 2.1 Universe and timeframe

- `EUR_USD`
- `NAS100_USD`
- Signal timeframe: **H1 / 60 minutes**
- Use **completed H1 candles only**
- Internal structure size: **5**
- ATR period: **14**
- ATR is a **simple rolling mean of True Range**, not Wilder/RMA/EMA.

```python
def add_atr(df, period=14):
    x = df.copy()
    prev = x["close"].shift(1)

    tr = pd.concat(
        [
            x["high"] - x["low"],
            (x["high"] - prev).abs(),
            (x["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)

    x["atr"] = tr.rolling(
        period,
        min_periods=period,
    ).mean()

    return x
```

Signal OHLC is midpoint OHLC where bid+ask quotes exist:

```python
mid = (bid + ask) / 2
```

Execution/fill/exit checks are bid/ask aware as described below.

---

## 2.2 Frozen constants

```python
TIMEFRAME_MIN = 60
STRUCTURE_SIZE = 5
ATR_PERIOD = 14

FROZEN_MIN_DISPLACEMENT_ATR = 2.0
FROZEN_MIN_BROKEN_STRUCTURE_ATR = 1.0
FROZEN_MIN_CLOSE_THROUGH_ATR = 0.25

RETRACE = 0.618
PENDING_EXPIRY_BARS = 20
MAX_ENTRY_DISTANCE_ATR = 2.0

FROZEN_TP_R = 1.5

CANCEL_ON_NEW_CHOCH = True
CANCEL_ON_OPPOSITE_BOS = True
INVALIDATE_ON_ORIGIN_BREAK = True

# Historical simulator only:
INTRABAR_PRIORITY = "stop_first"

# Historical research horizon:
MAX_HOLD_BARS = 120
```

There is **no runner**, **no breakeven rule**, **no trailing stop**, **no HTF filter**, **no volatility filter**, **no news filter**, and **no 13:00 filter** in the frozen implementation.

---

# 3. Exact causal internal-structure algorithm

This is the research implementation that produced the frozen S1 event stream.

The key causality rule is:

> A pivot at index `i - size` only becomes knowable at the close of current bar `i`.

```python
def detect_structure_events(df, size):
    x = add_atr(df, ATR_PERIOD)

    if len(x) <= size + 2:
        return pd.DataFrame()

    highs = x["high"].to_numpy(float)
    lows = x["low"].to_numpy(float)
    closes = x["close"].to_numpy(float)
    times = x.index
    atrs = x["atr"].to_numpy(float)

    h = x["high"]
    l = x["low"]

    new_leg_high = (
        h.shift(size) > h.rolling(size).max()
    ).fillna(False).to_numpy(bool)

    new_leg_low = (
        l.shift(size) < l.rolling(size).min()
    ).fillna(False).to_numpy(bool)

    BEARISH_LEG = 0
    BULLISH_LEG = 1
    BEARISH = -1
    BULLISH = 1

    leg = BEARISH_LEG
    trend = 0

    pivot_high = None
    pivot_low = None

    pivot_high_crossed = True
    pivot_low_crossed = True

    events = []

    for i in range(size, len(x)):
        previous_leg = leg

        if new_leg_high[i]:
            leg = BEARISH_LEG
        elif new_leg_low[i]:
            leg = BULLISH_LEG

        if leg != previous_leg:
            src = i - size

            if leg == BULLISH_LEG:
                pivot_low = {
                    "price": float(lows[src]),
                    "pivot_time": times[src],
                    "available_time": times[i],
                }
                pivot_low_crossed = False
            else:
                pivot_high = {
                    "price": float(highs[src]),
                    "pivot_time": times[src],
                    "available_time": times[i],
                }
                pivot_high_crossed = False

        previous_close = closes[i - 1]
        close = closes[i]

        # Bullish break
        if pivot_high is not None and not pivot_high_crossed:
            level = pivot_high["price"]

            if previous_close <= level and close > level:
                tag = "CHoCH" if trend == BEARISH else "BOS"
                trend = BULLISH
                pivot_high_crossed = True

                if pivot_low is not None and pivot_low["available_time"] <= times[i]:
                    origin = float(pivot_low["price"])
                    extreme = float(highs[i])
                    atr = float(atrs[i]) if not np.isnan(atrs[i]) else np.nan

                    events.append({
                        "event_time": times[i],
                        "bar_pos": i,
                        "type": tag,
                        "direction": "bullish",
                        "broken_level": level,
                        "origin_price": origin,
                        "event_extreme": extreme,
                        "atr": atr,
                        "break_strength_atr": (
                            (close - level) / atr
                            if atr and atr > 0
                            else np.nan
                        ),
                    })

        # Bearish break
        if pivot_low is not None and not pivot_low_crossed:
            level = pivot_low["price"]

            if previous_close >= level and close < level:
                tag = "CHoCH" if trend == BULLISH else "BOS"
                trend = BEARISH
                pivot_low_crossed = True

                if pivot_high is not None and pivot_high["available_time"] <= times[i]:
                    origin = float(pivot_high["price"])
                    extreme = float(lows[i])
                    atr = float(atrs[i]) if not np.isnan(atrs[i]) else np.nan

                    events.append({
                        "event_time": times[i],
                        "bar_pos": i,
                        "type": tag,
                        "direction": "bearish",
                        "broken_level": level,
                        "origin_price": origin,
                        "event_extreme": extreme,
                        "atr": atr,
                        "break_strength_atr": (
                            (level - close) / atr
                            if atr and atr > 0
                            else np.nan
                        ),
                    })

    ev = pd.DataFrame(events)

    if not ev.empty:
        ev["initial_displacement_atr"] = np.where(
            ev["direction"] == "bullish",
            (ev["event_extreme"] - ev["origin_price"]) / ev["atr"],
            (ev["origin_price"] - ev["event_extreme"]) / ev["atr"],
        )

        ev["broken_structure_size"] = (
            ev["broken_level"] - ev["origin_price"]
        ).abs()

        ev["broken_structure_atr"] = (
            ev["broken_structure_size"]
            / ev["atr"].replace(0, np.nan)
        )

    return ev
```

### CHoCH/BOS semantics

A break occurs on a **close crossing the pivot level**:

Bullish:
```python
previous_close <= pivot_high
and current_close > pivot_high
```

Bearish:
```python
previous_close >= pivot_low
and current_close < pivot_low
```

Tag:
```python
bullish break -> CHoCH only if previous trend == bearish, else BOS
bearish break -> CHoCH only if previous trend == bullish, else BOS
```

---

# 4. S1 event qualification

Only events with:

```python
event["type"] == "CHoCH"
event["broken_structure_atr"] >= 1.0
event["break_strength_atr"] >= 0.25
```

are eligible S1 events.

Definitions:

```python
broken_structure_atr =
    abs(broken_level - origin_price) / event_atr
```

Bullish close-through / break strength:
```python
(current_close - broken_level) / event_atr
```

Bearish:
```python
(broken_level - current_close) / event_atr
```

The **2 ATR displacement requirement is NOT another initial event filter**. It controls when the pending retracement setup becomes armed.

---

# 5. S1 dynamic retracement entry

### Retracement formula

Bullish:
```python
limit = extreme - 0.618 * (extreme - origin)
```

Bearish:
```python
limit = extreme + 0.618 * (origin - extreme)
```

### Displacement

Bullish:
```python
(extreme - origin) / event_atr
```

Bearish:
```python
(origin - extreme) / event_atr
```

The ATR used here is the **ATR frozen on the CHoCH event bar**.

The pending order becomes armed when:

```python
displacement >= 2.0
```

It may be armed immediately from the CHoCH bar’s extreme, but the order is only live **after the CHoCH candle has closed**.

---

# 6. Dynamic impulse anchor

Initial:

```python
running_extreme = event_extreme
```

After each subsequent **completed H1 candle**:

Bullish:
```python
if bar.high > running_extreme:
    running_extreme = bar.high
```

Bearish:
```python
if bar.low < running_extreme:
    running_extreme = bar.low
```

If the setup is already armed and a new extreme is made:

```python
limit_price = retracement_price(
    origin,
    running_extreme,
    direction,
    0.618,
)
```

This update happens **after that candle closes**.

In live OANDA, the corresponding pending limit should therefore be replaced only after the completed H1 bar confirms the new extreme.

Once the entry fills:

```python
frozen_extreme = running_extreme
```

and the anchor is permanently frozen for that trade.

---

# 7. S1 executable fill semantics

For a bullish limit entry (buy), research tests the **ask low**:

```python
asklow <= limit_price
```

For a bearish limit entry (sell), research tests the **bid high**:

```python
bidhigh >= limit_price
```

Live OANDA should use an actual pending LIMIT order, so the broker provides the real fill.

---

# 8. S1 pending cancellation rules

## 8.1 New CHoCH supersession

For every event, research finds the first later event with:

```python
type == "CHoCH"
```

regardless of direction.

If a pending setup reaches that event time:

```python
cancel_new_choch
```

## 8.2 Opposite BOS

Research also finds the first later event with:

```python
type == "BOS"
and direction != current_event_direction
```

At that event time:

```python
cancel_opposite_bos
```

## 8.3 Origin break before fill

Bullish:
```python
bar.low <= origin
```

Bearish:
```python
bar.high >= origin
```

=> cancel.

Note: this historical invalidation check uses the bar OHLC/mid series rather than executable bid/ask side.

## 8.4 Expiry

`armed_pos` is the bar position at which displacement first becomes >= 2 ATR.

The historical condition is exactly:

```python
if pos - armed_pos > 20:
    cancel_expired
```

This means the implementation must preserve the notebook’s exact bar-count boundary rather than casually rewriting it as `>= 20`.

## 8.5 Maximum entry distance

After a completed H1 bar updates the running extreme/limit:

```python
distance_atr = abs(bar.close - limit_price) / event_atr
```

If:

```python
distance_atr > 2.0
```

cancel as too far.

---

# 9. S1 stop and final target

Structural stop:

```python
stop = event.origin_price
```

This is the causal pivot origin attached to the CHoCH event.

**Final S1 target is NOT the old structural extreme.**

The frozen portfolio replaced the structural target with:

```python
TP = 1.5R
```

where:

```python
risk = abs(entry - stop)
```

Bullish:
```python
tp = entry + 1.5 * risk
```

Bearish:
```python
tp = entry - 1.5 * risk
```

No runner.

Historical simulator max holding horizon was 120 H1 bars. Preserve this as the current frozen timeout unless the live implementation explicitly flags it for review rather than silently changing it.

---

# 10. S1 exit side

For an S1 **bullish/long** trade, executable exits use BID OHLC.

For an S1 **bearish/short** trade, executable exits use ASK OHLC.

Historical bar ambiguity:

```python
if stop_hit and target_hit:
    stop wins
```

This is a conservative backtest convention only.

In actual live OANDA trading:
- broker-side SL and TP should be attached;
- real transaction ordering determines which executes first;
- do not artificially impose “stop first” on real broker events.

---

# 11. Final S1 timing filter

This is the **final operational timing overlay** selected after the neighboring-hour robustness test.

Block **S1 entries** on Friday at:

```text
07:00 UTC
08:00 UTC
```

Allow 09:00 UTC onward.

The test showed:

```text
Friday 07 UTC:  7 trades, -2.0R, avg -0.2857R
Friday 08 UTC: 12 trades, -4.5R, avg -0.3750R
Friday 09 UTC: 13 trades, +2.0R, avg +0.1538R
```

The chosen rule is therefore:

```python
block_s1_entry = (
    entry_time.weekday() == 4
    and entry_time.hour in {7, 8}
)
```

Important:
- this is an **ENTRY/fill-time filter**;
- the research timing analysis filtered on `S1 entry_time`, not CHoCH signal time;
- do not close an existing trade because 07:00/08:00 Friday arrives;
- do not add a whole-Friday ban;
- do not add the exploratory 13:00 or Monday-13:00 filters.

For a real resting OANDA limit order, the live implementation must prevent a pending S1 order from filling during blocked Friday 07/08 UTC. A safe implementation is to suspend/cancel the pending broker order for the blocked entry window while retaining/reconciling the strategy setup, then only restore it if the setup remains valid when entries are allowed again. Do not silently convert the filter to signal-time filtering.

---

# 12. Strategy 2 — frozen conditional reversal

S2 is **not an independent scanner**.

It exists only after an S1 trade reaches the frozen S1 target of **+1.5R**.

It uses the original S1 entry/risk/direction as its coordinate system.

## Frozen configuration

```python
S2_TF_MINUTES = 10
S2_DECISION_BAR = 10          # 100 minutes
S2_CLOSE_THRESHOLD_R = 0.5750
S2_AGAINST_RATIO = 0.3809
S2_MIN_CONSECUTIVE = 3
S2_REQUIRE_LOST_1P5 = True
S2_REQUIRE_NO_RECLAIM = True

S2_STOP_TYPE = "rolling_extreme"
S2_STOP_LOOKBACK = 2
S2_ATR_BUFFER = 0.0

S2_TARGET_R = 4.0
S2_MAX_HOLD_MINUTES = 90
```

`atr_stop_mult` is **inert** because the selected stop is `rolling_extreme`. Do not use it in S2 position geometry.

---

# 13. S2 trigger time and data window

Research identifies an S1 winner when S1’s 1.5R target is reached.

For live:
- use the **actual OANDA TP fill/close transaction timestamp** as `s1_tp_time`.

The S2 feature window is:

```python
df[df.index > tp_time]
```

That `>` is important.

The TP bar itself is excluded.

S2 uses only completed 10-minute candles after the TP event.

The decision occurs at:

```python
bar_num = 10
decision_minutes = 10 * 10 = 100
```

In historical research, `signal_time` is the timestamp of that tenth post-TP completed 10-minute bar.

If the bot is offline at the decision close and cannot enter at the frozen decision point, do not invent a retroactive historical fill. Record it as missed downtime unless the exact order semantics prove a legitimate order was already resting (the frozen S2 did not use a resting order).

---

# 14. Original S1 R coordinates

For original S1 direction = bullish:

```python
price_r = (price - original_entry) / original_risk
```

For original S1 direction = bearish:

```python
price_r = (original_entry - price) / original_risk
```

Post-TP observation uses the **S1 executable exit side**:

Original S1 bullish:
```python
bidopen, bidhigh, bidlow, bidclose
```

Original S1 bearish:
```python
askopen, askhigh, asklow, askclose
```

This is deliberate. Do not replace these observations with midpoint candles.

---

# 15. S2 feature definitions

For each completed post-TP M10 candle:

```python
open_r = price_to_original_r(open_price, original_entry, original_risk, original_direction)
close_r = price_to_original_r(close_price, original_entry, original_risk, original_direction)
```

An opposing/adverse candle is:

```python
closes_against = close_r < open_r
```

Maintain:

```python
against_count
against_run
max_against_run
```

where:

```python
against_ratio = against_count / bar_num
```

### lost_1p5

Initially False.

After each post-TP M10 close:

```python
if close_r < 1.5:
    lost_1p5 = True
```

### reclaimed_after_loss

```python
elif lost_1p5 and close_r >= 1.5:
    reclaimed_after_loss = True
```

This state remains True once triggered.

### Rolling adverse price

If original S1 was bullish, S2 will be short:

```python
adverse_price = executable_high
roll_adverse_2 = max(last_two_adverse_prices)
```

If original S1 was bearish, S2 will be long:

```python
adverse_price = executable_low
roll_adverse_2 = min(last_two_adverse_prices)
```

---

# 16. S2 qualification at bar 10

At exactly the 10th completed M10 candle after S1 TP, take S2 only if **all** are true:

```python
close_r <= 0.5750
against_ratio >= 0.3809
max_against_run >= 3
lost_1p5 is True
reclaimed_after_loss is False
```

This is an exact one-time decision at 100 minutes.

Do not keep waiting for the conditions to become true later.

---

# 17. S2 direction

This was resolved explicitly in the final simulator:

> **Strategy 2 is opposite Strategy 1.**

If original S1 direction is bullish:
```text
S2 = SHORT
```

If original S1 direction is bearish:
```text
S2 = LONG
```

Do not infer direction from any earlier continuation/re-entry experiment. This is the exact frozen S2 simulator direction.

---

# 18. S2 entry

Entry occurs at the **decision candle executable close** used in feature construction:

```python
entry = signal_close
```

Because the original path is observed on S1’s executable exit side:

- after bullish S1, S2 short entry research price is the decision candle **bid close**;
- after bearish S1, S2 long entry research price is the decision candle **ask close**.

This happens to coincide with the correct executable entry side for the reversal trade:
- short sells at bid;
- long buys at ask.

Live implementation should submit the order immediately after the completed 10-minute decision candle and record actual OANDA fill price; shadow-mode comparison should retain the research decision-close reference separately.

---

# 19. S2 stop

Frozen stop:

```python
stop_type = "rolling_extreme"
stop_lookback = 2
atr_buffer = 0
```

If original S1 bullish => S2 short:

```python
stop = max(executable highs of the last 2 observed M10 bars)
```

If original S1 bearish => S2 long:

```python
stop = min(executable lows of the last 2 observed M10 bars)
```

No ATR buffer.

Risk:

Short:
```python
risk = stop - entry
```

Long:
```python
risk = entry - stop
```

If risk <= 0, reject/no trade.

---

# 20. S2 target

S2 target = **4R based on S2’s own entry/stop risk**.

S2 short:
```python
target = entry - 4.0 * risk
```

S2 long:
```python
target = entry + 4.0 * risk
```

---

# 21. S2 exit execution

After the decision candle, the historical simulator begins evaluation at:

```python
pos + 1
```

So the decision candle itself is **not** used to stop/target the newly entered S2.

For S2 short (original S1 bullish), exits use ASK:
```python
askhigh for stop
asklow for target
```

For S2 long (original S1 bearish), exits use BID:
```python
bidlow for stop
bidhigh for target
```

Historical same-bar ambiguity is stop-first.

Actual OANDA live execution uses broker transaction ordering.

---

# 22. S2 timeout

Frozen:

```python
max_hold_minutes = 90
```

At 10-minute bars:

```python
max_hold_bars = ceil(90 / 10) = 9
```

Historical simulator evaluates the next 9 completed M10 bars after the decision candle.

If neither SL nor TP is hit, exit at the executable close of the ninth future M10 candle.

For live OANDA:
- broker-side SL/TP should remain active;
- at the 90-minute timeout, close the remaining S2 trade at market;
- persist/reconcile the timeout write idempotently.

---

# 23. Risk and account limits for the current practice build

These are **live execution settings**, not part of historical signal discovery:

```yaml
risk_pct: 0.02
max_account_risk_open: 0.04
```

Meaning:
- target approximately 2% account NAV/equity loss if the candidate trade reaches its stop;
- aggregate open CHoCH stop-risk may not exceed 4%.

Risk sizing must use actual price distance to stop and correct GBP account-currency conversion.

OANDA-reported instrument metadata is authoritative for:
- `marginRate`
- minimum trade size
- unit precision
- price precision

Current practice-account observations:

```text
EUR_USD:
  type: CURRENCY
  marginRate: 0.03333333333333
  effective leverage: ~30:1

NAS100_USD:
  type: CFD
  marginRate: 0.05
  effective leverage: ~20:1
```

Margin/leverage is **not** the risk definition. It is only an order feasibility constraint after risk sizing.

---

# 24. Research sanity references

These are diagnostic references, not values to force.

## S1 — EURUSD + NAS100, fixed 1.5R

2022–2025:
```text
342 trades
avg ≈ +0.22807R
total ≈ +78R
max DD ≈ -8R
4/4 years positive
```

2026:
```text
29 trades
avg ≈ +0.5517R
total ≈ +16R
```

Combined 2022–2026:
```text
371 trades
+94R
```

## Frozen S2

```text
2022: n=6,  avg≈-0.6075R, total≈-3.6449R
2023: n=13, avg≈+0.7503R, total≈+9.7545R
2024: n=7,  avg≈+1.35R,   total≈+9.45R
2025: n=7,  avg≈+2.0707R, total≈+14.4949R
2026: n=3,  avg≈+0.0471R, total≈+0.1414R
```

Combined:
```text
36 trades
≈ +30.1959R
```

Combined pre-Friday-07/08 overlay:
```text
S1 + S2 = 407 trades
≈ +124.1959R
```

## Friday neighboring-hour diagnostic

```text
Fri 07: n=7,  total=-2.0R
Fri 08: n=12, total=-4.5R
Fri 09: n=13, total=+2.0R
```

Selected operational filter:
```text
NO S1 ENTRY Friday 07:00–08:59 UTC
```

---

# 25. Explicitly superseded / rejected ideas

Do **not** implement these:

- structural S1 take-profit as the final target
- S1 2R or 3R final target
- S1 runner
- HTF alignment as a live filter
- immediate post-TP S1 reversal variants from V10
- 1H same-direction re-entry
- 15m CHoCH re-entry
- fixed 60-minute lower-timeframe triggers
- first-candle rejection strategy
- generic volatility filter
- 13:00 UTC filter
- Monday 13:00 filter
- whole-Friday ban
- Friday 09:00 ban
- GBPUSD
- USDJPY
- XAUUSD
- SPX500
- optimisation/GA logic in production
- `atr_stop_mult` for the frozen rolling-extreme S2 stop
- retroactive S2 entries after downtime

---

# 26. Required state linkage

S2 must be linked permanently to the S1 winner that armed it.

Persist at minimum:

```text
S1:
  setup ID
  deterministic client order key
  instrument
  direction
  CHoCH event time
  origin
  event ATR
  running/frozen extreme
  entry
  stop
  risk
  1.5R TP
  actual OANDA order/trade IDs
  actual TP transaction time

S2:
  parent S1 ID
  parent OANDA trade ID
  original S1 direction
  original S1 entry
  original S1 risk
  s1_tp_time
  post-TP M10 observation state
  decision time
  decision metrics
  S2 direction
  entry
  rolling-2 stop
  S2 risk
  4R TP
  timeout
```

---

# 27. Restart semantics

On startup, reconcile OANDA before strategy decisions.

If a known S1 TP occurred while offline:
1. recover the actual OANDA TP transaction timestamp;
2. rebuild completed M10 bars strictly **after** TP;
3. reconstruct the S2 feature state causally.

If the 100-minute S2 decision point was missed while the bot was offline:
- do not retroactively enter at a historical price;
- record `S2_MISSED_DOWNTIME`.

Pending S1 state must reconcile against actual OANDA pending orders using deterministic client identifiers before any replacement/cancellation/placement decision.

---

# 28. Dry-run acceptance requirement

Before enabling OANDA practice writes:

- run S1/S2 in shadow mode;
- log every setup, update, cancellation, hypothetical fill, S1 TP/SL, S2 arm, S2 decision and S2 hypothetical exit;
- compare live decisions with a deterministic reference harness built from the functions/semantics in this document;
- do not enable trading merely because the service runs without exceptions.

Current deployed safety state must remain:

```text
OANDA_ENV=practice
trading_enabled=false
dry_run=true
CHOCH_TRADING_ENABLED=0
```

until shadow-mode validation is accepted.

---

# 29. Implementation instruction for Codex

Treat this file as the frozen strategy source of truth.

Do not “improve” it.

Do not reinterpret CHoCH.

Do not port the evolutionary search.

Do not add filters.

Port the causal event/state semantics exactly, then adapt only the broker execution layer for real OANDA REST transactions, order replacement, dependent SL/TP, account-currency sizing, idempotency and restart reconciliation.
