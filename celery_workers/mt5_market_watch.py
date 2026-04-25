"""Shared MT5 Market Watch symbol candidates for broker-time probing."""


def _unique_symbols(symbols):
    seen = set()
    unique = []
    for symbol in symbols:
        normalized = str(symbol or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return tuple(unique)


_CRYPTO_ROOTS = (
    "BTCUSD",
    "BTCUSDT",
    "XBTUSD",
    "ETHUSD",
    "ETHUSDT",
)

_BROKER_SUFFIXES = (
    "",
    ".m",
    ".r",
    ".raw",
    ".pro",
    ".ecn",
    ".c",
    ".i",
    ".a",
    ".b",
    ".x",
    "m",
    "r",
    "raw",
    "pro",
    "ecn",
    "micro",
    "mini",
)

MT5_MARKET_WATCH_CRYPTO_SEED_SYMBOLS = _unique_symbols(
    [f"{root}{suffix}" for root in _CRYPTO_ROOTS for suffix in _BROKER_SUFFIXES]
    + [
        "BTC/USD",
        "BTC/USD.m",
        "BTC/USD.r",
        "XBT/USD",
        "XBT/USD.m",
        "XBT/USD.r",
        "ETH/USD",
        "ETH/USD.m",
        "ETH/USD.r",
    ]
)

MT5_MARKET_WATCH_FALLBACK_SYMBOLS = (
    "XAUUSD",
    "XAUUSD.m",
    "XAUUSD.r",
    "GOLD",
    "GOLD.m",
    "EURUSD",
    "EURUSD.m",
    "EURUSD.r",
)
