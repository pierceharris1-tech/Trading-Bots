# Maps signal channel names → OANDA instrument codes
# and defines pip/point values for position sizing

INSTRUMENT_MAP = {
    # Forex
    "GOLD":    "XAU_USD",
    "XAUUSD":  "XAU_USD",
    "XAU":     "XAU_USD",
    "GBPJPY":  "GBP_JPY",
    "GBP/JPY": "GBP_JPY",
    "EURUSD":  "EUR_USD",
    "EUR/USD": "EUR_USD",
    "GBPUSD":  "GBP_USD",
    "GBP/USD": "GBP_USD",
    "USDJPY":  "USD_JPY",
    "USD/JPY": "USD_JPY",
    "AUDUSD":  "AUD_USD",
    "AUD/USD": "AUD_USD",
    "NZDUSD":  "NZD_USD",
    "NZD/USD": "NZD_USD",
    "USDCAD":  "USD_CAD",
    "USD/CAD": "USD_CAD",
    "EURGBP":  "EUR_GBP",
    "EUR/GBP": "EUR_GBP",
    # Indices
    "SPX500":  "SPX500_USD",
    "SP500":   "SPX500_USD",
    "US500":   "SPX500_USD",
    "S&P500":  "SPX500_USD",
    "SPX":     "SPX500_USD",
    "US30":    "US30_USD",
    "DOW":     "US30_USD",
    "USTEC":   "NAS100_USD",
    "NAS100":  "NAS100_USD",
    "NASDAQ":  "NAS100_USD",
    "GER30":   "DE30_EUR",
    "GER40":   "DE30_EUR",
    "DAX":     "DE30_EUR",
    "UK100":   "UK100_GBP",
    "FTSE":    "UK100_GBP",
}

# Minimum units per instrument on OANDA
MIN_UNITS = {
    "XAU_USD":    1,
    "GBP_JPY":    1,
    "EUR_USD":    1,
    "GBP_USD":    1,
    "USD_JPY":    1,
    "AUD_USD":    1,
    "NZD_USD":    1,
    "USD_CAD":    1,
    "EUR_GBP":    1,
    "SPX500_USD": 1,
    "US30_USD":   1,
    "NAS100_USD": 1,
    "DE30_EUR":   1,
    "UK100_GBP":  1,
}

def resolve_instrument(raw: str) -> str | None:
    """Convert signal instrument name to OANDA code. Returns None if unknown."""
    return INSTRUMENT_MAP.get(raw.upper().strip())
