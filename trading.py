import os
import random
import re
from datetime import datetime

from openpyxl import load_workbook


BASE_SYMBOL_OPTIONS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "NZDUSD",
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "NZDJPY",
    "EURGBP",
    "EURCHF",
    "EURAUD",
    "EURNZD",
    "EURCAD",
    "GBPCHF",
    "GBPAUD",
    "GBPCAD",
    "GBPNZD",
    "AUDCAD",
    "AUDCHF",
    "AUDNZD",
    "CADCHF",
    "NZDCHF",
    "NZDCAD",
    "XAUUSD",
    "XAGUSD",
    "US500",
    "NAS100",
    "US30",
    "GER40",
    "UK100",
    "FRA40",
    "EU50",
    "JP225",
    "HK50",
    "CHN50",
    "AUS200",
    "ESP35",
    "IT40",
]

SYMBOL_ALIASES = {
    "SPX500": "US500",
    "SP500": "US500",
    "US500CASH": "US500",
    "US500INDEX": "US500",
    "US100": "NAS100",
    "USTEC": "NAS100",
    "NAS100CASH": "NAS100",
    "NASDAQ100": "NAS100",
    "DJ30": "US30",
    "DJIA": "US30",
    "WS30": "US30",
    "US30CASH": "US30",
    "DE40": "GER40",
    "DAX40": "GER40",
    "GER30": "GER40",
    "DE30": "GER40",
    "DAX": "GER40",
    "FTSE100": "UK100",
    "UKX": "UK100",
    "U100": "UK100",
    "CAC40": "FRA40",
    "FR40": "FRA40",
    "STOXX50": "EU50",
    "EUSTX50": "EU50",
    "SX5E": "EU50",
    "N225": "JP225",
    "NI225": "JP225",
    "JP225CASH": "JP225",
    "HSI": "HK50",
    "HSI50": "HK50",
    "HK50CASH": "HK50",
    "CN50": "CHN50",
    "CHINA50": "CHN50",
    "CHINAA50": "CHN50",
    "A50": "CHN50",
    "AU200": "AUS200",
    "ASX200": "AUS200",
    "IBEX35": "ESP35",
    "ES35": "ESP35",
    "ITA40": "IT40",
    "EU": "EURUSD",
    "GU": "GBPUSD",
    "UJ": "USDJPY",
}

MT5_COLUMN_ALIASES = {
    "mt5_position": {"position", "position id", "position ticket", "ticket"},
    "symbol": {"symbol", "instrument"},
    "side": {"type", "deal", "side"},
    "lot_size": {"volume", "lots", "lot"},
    "entry_price": {"open price", "price", "entry price"},
    "exit_price": {"close price", "exit price", "price close"},
    "pnl": {"profit", "p/l", "pl", "net profit"},
    "opened_at": {"time", "date", "open time", "close time"},
}

MT5_SECTION_TITLES = {"positions", "orders", "deals", "results"}

INSTRUMENT_CONTRACT_SIZE = {
    "XAUUSD": 100.0,
    "XAGUSD": 5000.0,
    "US500": 1.0,
    "NAS100": 1.0,
    "US30": 1.0,
    "GER40": 1.0,
    "UK100": 1.0,
    "FRA40": 1.0,
    "EU50": 1.0,
    "JP225": 1.0,
    "HK50": 1.0,
    "CHN50": 1.0,
    "AUS200": 1.0,
    "ESP35": 1.0,
    "IT40": 1.0,
}


def normalize_symbol(symbol):
    return "".join(ch for ch in (symbol or "").upper() if ch.isalnum())


def canonicalize_symbol(symbol):
    normalized = normalize_symbol(symbol)
    if not normalized:
        return ""
    return SYMBOL_ALIASES.get(normalized, normalized)


def get_symbol_options(selected_symbol=None):
    options = list(BASE_SYMBOL_OPTIONS)
    selected = canonicalize_symbol(selected_symbol)
    if selected and selected not in options:
        options.insert(0, selected)
    return options


def normalize_header_name(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s_/\\-]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return " ".join(text.split())


def parse_side_value(value):
    text = str(value or "").strip().upper()
    if "BUY" in text:
        return "BUY"
    if "SELL" in text:
        return "SELL"
    return None


def parse_float_value(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return float(text)
    except ValueError:
        return None


def parse_datetime_value(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_mt5_position_value(value):
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return None

    text = str(value).strip()
    if not text:
        return None

    cleaned = text.replace(",", "")
    if re.fullmatch(r"\d+", cleaned):
        return cleaned
    if re.fullmatch(r"\d+\.0+", cleaned):
        return cleaned.split(".", 1)[0]
    return None


def build_import_signature(prefix="mt5"):
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    random_tail = os.urandom(4).hex()
    return f"{prefix}_{timestamp}_{random_tail}"


def parse_import_signature_datetime(signature):
    text_value = str(signature or "").strip()
    match = re.match(r"^[A-Za-z0-9]+_(\d{14})_[0-9a-fA-F]+$", text_value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def row_texts(row):
    return [str(cell).strip() for cell in row if cell is not None and str(cell).strip() != ""]


def find_section_row(rows, section_name):
    target = normalize_header_name(section_name)
    for idx, row in enumerate(rows):
        texts = row_texts(row)
        if len(texts) == 1 and normalize_header_name(texts[0]) == target:
            return idx
    return None


def is_section_title_row(row):
    texts = row_texts(row)
    if len(texts) != 1:
        return False
    return normalize_header_name(texts[0]) in MT5_SECTION_TITLES


def build_mt5_column_map(header_row):
    normalized = [normalize_header_name(cell) for cell in header_row]
    column_map = {}
    by_name = {}

    for idx, name in enumerate(normalized):
        if not name:
            continue
        by_name.setdefault(name, []).append(idx)
        for canonical, aliases in MT5_COLUMN_ALIASES.items():
            if canonical in column_map:
                continue
            if name in aliases:
                column_map[canonical] = idx
                break

    time_cols = by_name.get("time", [])
    price_cols = by_name.get("price", [])
    if "opened_at" not in column_map and time_cols:
        column_map["opened_at"] = time_cols[0]
    if "entry_price" not in column_map and price_cols:
        column_map["entry_price"] = price_cols[0]
    if "exit_price" not in column_map and len(price_cols) >= 2:
        column_map["exit_price"] = price_cols[1]

    return column_map


def parse_mt5_xlsx_stream(file_stream):
    workbook = load_workbook(file_stream, data_only=True, read_only=True)
    worksheet = workbook.active

    rows = list(worksheet.iter_rows(values_only=True))
    section_idx = find_section_row(rows, "positions")
    if section_idx is None:
        workbook.close()
        return [], 0, 0

    header_idx = None
    column_map = {}

    header_scan_end = min(section_idx + 10, len(rows))
    for idx in range(section_idx + 1, header_scan_end):
        detected = build_mt5_column_map(rows[idx])
        if (
            detected.get("symbol") is not None
            and detected.get("side") is not None
            and detected.get("lot_size") is not None
        ):
            header_idx = idx
            column_map = detected
            break

    if header_idx is None:
        workbook.close()
        return [], 0, 0

    parsed = []
    skipped = 0
    total = 0

    for row_idx in range(header_idx + 1, len(rows)):
        row = rows[row_idx]
        texts = row_texts(row)
        if not texts:
            continue
        if is_section_title_row(row):
            break

        total += 1

        def col(key):
            idx = column_map.get(key)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        symbol = canonicalize_symbol(col("symbol"))
        mt5_position_raw = col("mt5_position")
        mt5_position = parse_mt5_position_value(mt5_position_raw)
        side = parse_side_value(col("side"))
        lot_size = parse_float_value(col("lot_size"))
        entry_price = parse_float_value(col("entry_price"))
        exit_price = parse_float_value(col("exit_price"))
        pnl = parse_float_value(col("pnl"))
        opened_at = parse_datetime_value(col("opened_at"))

        if not symbol or not side or lot_size is None or entry_price is None:
            skipped += 1
            continue

        parsed.append(
            {
                "symbol": symbol,
                "mt5_position": mt5_position,
                "mt5_position_raw": mt5_position_raw,
                "side": side,
                "lot_size": lot_size,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl": pnl,
                "opened_at": opened_at,
            }
        )

    workbook.close()
    return parsed, total, skipped


def is_fx_pair(symbol):
    sym = normalize_symbol(symbol)
    return len(sym) == 6 and sym.isalpha()


def get_contract_size(symbol):
    sym = normalize_symbol(symbol)
    if sym in INSTRUMENT_CONTRACT_SIZE:
        return INSTRUMENT_CONTRACT_SIZE[sym]
    if is_fx_pair(sym):
        return 100000.0
    return 1.0


def quote_to_usd_rate(symbol, reference_price):
    sym = normalize_symbol(symbol)
    if sym.endswith("USD"):
        return 1.0
    if is_fx_pair(sym) and sym.startswith("USD") and reference_price and reference_price > 0:
        return 1.0 / reference_price
    return None


def get_pip_size(symbol):
    sym = normalize_symbol(symbol)
    if not is_fx_pair(sym):
        return None
    return 0.01 if sym.endswith("JPY") else 0.0001


def calc_pips_values(symbol, side, entry_price, exit_price):
    if entry_price is None or exit_price is None:
        return None

    pip_size = get_pip_size(symbol)
    if not pip_size:
        return None

    direction = 1.0 if (side or "").upper() == "BUY" else -1.0
    return direction * ((exit_price - entry_price) / pip_size)


def calc_pnl_values(symbol, side, entry_price, exit_price, lot_size):
    if entry_price is None or exit_price is None:
        return None
    if lot_size is None or lot_size <= 0:
        return None

    direction = 1.0 if (side or "").upper() == "BUY" else -1.0
    contract_size = get_contract_size(symbol)
    conversion_rate = quote_to_usd_rate(symbol, exit_price)
    if conversion_rate is None:
        return None

    return direction * (exit_price - entry_price) * lot_size * contract_size * conversion_rate


def derive_exit_price(symbol, side, entry_price, lot_size, pnl_value):
    if entry_price is None or pnl_value is None:
        return None
    if lot_size is None or lot_size <= 0:
        return None

    direction = 1.0 if (side or "").upper() == "BUY" else -1.0
    contract_size = get_contract_size(symbol)
    units = lot_size * contract_size
    if units <= 0:
        return None

    sym = normalize_symbol(symbol)
    if sym.endswith("USD"):
        return entry_price + (pnl_value / (direction * units))

    if is_fx_pair(sym) and sym.startswith("USD"):
        k = pnl_value / (direction * units)
        denom = 1.0 - k
        if abs(denom) < 1e-9:
            return None
        candidate = entry_price / denom
        return candidate if candidate > 0 else None

    return None


def calc_pnl(trade):
    return calc_pnl_values(
        symbol=trade.symbol,
        side=trade.side,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        lot_size=trade.lot_size,
    )


def resolve_pnl(trade):
    if trade.pnl is not None:
        return trade.pnl
    return calc_pnl(trade)


def resolve_pips(trade):
    return calc_pips_values(
        symbol=trade.symbol,
        side=trade.side,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
    )


def build_dashboard_insight(user_trades, closed_trades):
    total_trades = len(user_trades)
    total_closed = len(closed_trades)
    insights = []

    symbol_counts = {}
    for trade in user_trades:
        symbol = (trade.symbol or "").strip().upper()
        if not symbol:
            continue
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
    if symbol_counts:
        top_symbol, top_count = max(symbol_counts.items(), key=lambda item: (item[1], item[0]))
        top_share = (top_count / max(sum(symbol_counts.values()), 1)) * 100
        insights.append(
            {
                "title": "Most Traded Pair",
                "body": (
                    f"{top_symbol} is your most traded pair with {top_count} trades "
                    f"({top_share:.0f}% of your journal)."
                ),
            }
        )

    if total_closed >= 3:
        total_wins = sum(1 for _, pnl in closed_trades if pnl > 0)
        overall_win_rate = (total_wins / total_closed) * 100

        pair_stats = {}
        for trade, pnl in closed_trades:
            symbol = (trade.symbol or "").strip().upper()
            if not symbol:
                continue
            if symbol not in pair_stats:
                pair_stats[symbol] = {"count": 0, "wins": 0, "pnl_sum": 0.0}
            pair_stats[symbol]["count"] += 1
            pair_stats[symbol]["pnl_sum"] += float(pnl)
            if pnl > 0:
                pair_stats[symbol]["wins"] += 1

        eligible_pairs = [
            (symbol, stats) for symbol, stats in pair_stats.items() if stats["count"] >= 2
        ]
        if eligible_pairs:
            best_pair, best_stats = max(
                eligible_pairs,
                key=lambda item: (item[1]["wins"] / item[1]["count"], item[1]["count"]),
            )
            pair_win_rate = (best_stats["wins"] / best_stats["count"]) * 100
            pair_avg_pnl = best_stats["pnl_sum"] / best_stats["count"]
            if pair_win_rate > overall_win_rate:
                win_rate_delta = pair_win_rate - overall_win_rate
                insights.append(
                    {
                        "title": "Pair Edge",
                        "body": (
                            f"You perform {win_rate_delta:.1f}% better on {best_pair} by win rate "
                            f"({pair_win_rate:.1f}% vs {overall_win_rate:.1f}% overall)."
                        ),
                    }
                )
            insights.append(
                {
                    "title": "Pair Efficiency",
                    "body": (
                        f"Your average closed-trade result on {best_pair} is "
                        f"{pair_avg_pnl:+.2f} across {best_stats['count']} trades."
                    ),
                }
            )

    if total_closed >= 4:
        side_stats = {"BUY": {"count": 0, "pnl_sum": 0.0}, "SELL": {"count": 0, "pnl_sum": 0.0}}
        for trade, pnl in closed_trades:
            side = (trade.side or "").strip().upper()
            if side not in side_stats:
                continue
            side_stats[side]["count"] += 1
            side_stats[side]["pnl_sum"] += float(pnl)

        buy_count = side_stats["BUY"]["count"]
        sell_count = side_stats["SELL"]["count"]
        if buy_count >= 2 and sell_count >= 2:
            buy_avg = side_stats["BUY"]["pnl_sum"] / buy_count
            sell_avg = side_stats["SELL"]["pnl_sum"] / sell_count
            if abs(buy_avg - sell_avg) >= 0.01:
                better_side = "BUY" if buy_avg > sell_avg else "SELL"
                better_avg = buy_avg if buy_avg > sell_avg else sell_avg
                weaker_side = "SELL" if better_side == "BUY" else "BUY"
                weaker_avg = sell_avg if better_side == "BUY" else buy_avg
                insights.append(
                    {
                        "title": "Directional Bias",
                        "body": (
                            f"{better_side} setups outperform {weaker_side} "
                            f"({better_avg:+.2f} vs {weaker_avg:+.2f} average PnL per closed trade)."
                        ),
                    }
                )

    if total_closed >= 6:
        day_totals = {}
        for trade, pnl in closed_trades:
            if not trade.opened_at:
                continue
            day_key = trade.opened_at.strftime("%Y-%m-%d")
            if day_key not in day_totals:
                day_totals[day_key] = {"pnl_sum": 0.0, "count": 0}
            day_totals[day_key]["pnl_sum"] += float(pnl)
            day_totals[day_key]["count"] += 1

        if day_totals:
            trades_on_losing_days = sum(
                data["count"] for data in day_totals.values() if data["pnl_sum"] < 0
            )
            trades_on_non_losing_days = sum(
                data["count"] for data in day_totals.values() if data["pnl_sum"] >= 0
            )
            total_day_trades = trades_on_losing_days + trades_on_non_losing_days
            if total_day_trades > 0 and trades_on_losing_days > trades_on_non_losing_days:
                losing_day_ratio = (trades_on_losing_days / total_day_trades) * 100
                insights.append(
                    {
                        "title": "Risk Pattern",
                        "body": (
                            f"{losing_day_ratio:.0f}% of your trades happen on net-losing days. "
                            "Consider reducing frequency after early losses."
                        ),
                    }
                )

    if total_trades == 0:
        return {
            "title": "No Data Yet",
            "body": "Add your first trade to unlock personalized performance insights.",
        }
    if not insights:
        return {
            "title": "Developing Edge",
            "body": (
                f"You've logged {total_trades} trades so far. "
                "As more closed trades accumulate, your insights will become more specific."
            ),
        }
    return random.choice(insights)
