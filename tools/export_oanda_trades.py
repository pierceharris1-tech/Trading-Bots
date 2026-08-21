#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

DEFAULT_SINCE_DAYS = 30
DEFAULT_OUTDIR = "exports"
LOOKBACK_TRANSACTIONS = 50000
HTTP_RETRIES = 3
HTTP_TIMEOUT_SECONDS = 20


def load_dotenv_simple(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def parse_iso_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    val = str(ts).strip()
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def first_present(*values: Any) -> Any:
    for v in values:
        if v is not None and str(v) != "":
            return v
    return None


def nested_get(obj: Any, *path: str) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def extract_tag_from_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates = [
        nested_get(payload, "clientExtensions", "tag"),
        nested_get(payload, "tradeClientExtensions", "tag"),
        nested_get(payload, "order", "clientExtensions", "tag"),
        nested_get(payload, "order", "tradeClientExtensions", "tag"),
        nested_get(payload, "trade", "clientExtensions", "tag"),
        nested_get(payload, "trade", "tradeClientExtensions", "tag"),
    ]
    tag = first_present(*candidates)
    return "" if tag is None else str(tag).strip()


def split_tag(tag: str) -> tuple[str, str]:
    if not tag:
        return "", ""
    parts = tag.split("|", 1)
    bot_id = parts[0].strip() if parts else ""
    timeframe = parts[1].strip() if len(parts) > 1 else ""
    return bot_id, timeframe


@dataclass
class ExportStats:
    accounts_processed: int = 0
    transactions_scanned: int = 0
    closed_trades_exported: int = 0
    missing_tag_rows: int = 0


class OandaClient:
    def __init__(self, api_key: str, env: str) -> None:
        self.env = env
        if env == "practice":
            self.base_url = "https://api-fxpractice.oanda.com"
        elif env == "live":
            self.base_url = "https://api-fxtrade.oanda.com"
        else:
            raise RuntimeError("OANDA_ENV must be 'practice' or 'live'")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
                if resp.status_code >= 500 and attempt < HTTP_RETRIES:
                    time.sleep(2 ** (attempt - 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException:
                if attempt >= HTTP_RETRIES:
                    raise
                time.sleep(2 ** (attempt - 1))
        raise RuntimeError(f"Failed to GET {path}")

    def get_last_transaction_id(self, account_id: str) -> int:
        payload = self.get(f"/v3/accounts/{account_id}/summary")
        raw_id = first_present(payload.get("lastTransactionID"), nested_get(payload, "account", "lastTransactionID"))
        if raw_id is None:
            raise RuntimeError(f"Missing lastTransactionID for account {account_id}")
        return int(str(raw_id))

    def get_transactions_idrange(self, account_id: str, from_id: int, to_id: int) -> list[dict[str, Any]]:
        payload = self.get(
            f"/v3/accounts/{account_id}/transactions/idrange",
            params={"from": str(from_id), "to": str(to_id)},
        )
        txns = payload.get("transactions") or []
        return [x for x in txns if isinstance(x, dict)]

    def get_order(self, account_id: str, order_id: str) -> dict[str, Any]:
        return self.get(f"/v3/accounts/{account_id}/orders/{order_id}")


def extract_order_id_candidates(txn: dict[str, Any]) -> list[str]:
    candidates = [
        first_present(txn.get("orderID"), txn.get("orderId"), nested_get(txn, "order", "id")),
        txn.get("id"),
    ]
    out: list[str] = []
    for c in candidates:
        if c is None:
            continue
        s = str(c).strip()
        if s and s not in out:
            out.append(s)
    return out


def extract_trade_id(txn: dict[str, Any], closed_item: dict[str, Any] | None = None) -> str:
    if isinstance(closed_item, dict):
        val = first_present(closed_item.get("tradeID"), closed_item.get("tradeId"), closed_item.get("id"))
        if val is not None:
            return str(val)
    val = first_present(txn.get("tradeID"), txn.get("tradeId"), nested_get(txn, "trade", "id"))
    return "" if val is None else str(val)


def extract_realized_pl(txn: dict[str, Any], closed_item: dict[str, Any] | None = None) -> float | None:
    if isinstance(closed_item, dict):
        val = to_float(first_present(closed_item.get("realizedPL"), closed_item.get("pl")))
        if val is not None:
            return val
    return to_float(first_present(txn.get("realizedPL"), txn.get("pl")))


def resolve_tag(
    client: OandaClient,
    account_id: str,
    txn: dict[str, Any],
    order_tag_cache: dict[tuple[str, str], str],
) -> str:
    tag = extract_tag_from_payload(txn)
    if tag:
        return tag

    for order_id in extract_order_id_candidates(txn):
        cache_key = (account_id, order_id)
        if cache_key in order_tag_cache:
            cached = order_tag_cache[cache_key]
            if cached:
                return cached
            continue
        try:
            order_payload = client.get_order(account_id, order_id)
            order_obj = order_payload.get("order") if isinstance(order_payload.get("order"), dict) else order_payload
            looked_up = extract_tag_from_payload(order_obj) or extract_tag_from_payload(order_payload)
        except Exception:
            looked_up = ""
        order_tag_cache[cache_key] = looked_up
        if looked_up:
            return looked_up
    return ""


def build_rows_for_transaction(
    export_time_utc: str,
    account_id: str,
    txn: dict[str, Any],
    tag: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    tx_type = str(txn.get("type", ""))
    tx_id = str(first_present(txn.get("id"), ""))
    instrument = str(first_present(txn.get("instrument"), nested_get(txn, "trade", "instrument"), ""))
    units_default = first_present(txn.get("units"), nested_get(txn, "trade", "units"), "")
    close_time = str(first_present(txn.get("time"), ""))
    open_time_default = str(
        first_present(
            txn.get("openTime"),
            nested_get(txn, "trade", "openTime"),
            "",
        )
    )
    order_id_default = str(first_present(txn.get("orderID"), txn.get("orderId"), nested_get(txn, "order", "id"), ""))

    trades_closed = txn.get("tradesClosed")
    if isinstance(trades_closed, list) and trades_closed:
        for item in trades_closed:
            if not isinstance(item, dict):
                continue
            realized_pl = extract_realized_pl(txn, item)
            if realized_pl is None:
                continue
            trade_id = extract_trade_id(txn, item)
            order_id = str(first_present(order_id_default, item.get("orderID"), item.get("orderId"), ""))
            units = first_present(item.get("units"), units_default, "")
            open_time = str(first_present(item.get("openTime"), open_time_default, ""))
            hold_minutes = ""
            open_dt = parse_iso_utc(open_time)
            close_dt = parse_iso_utc(close_time)
            if open_dt is not None and close_dt is not None:
                hold_minutes = round((close_dt - open_dt).total_seconds() / 60.0, 3)
            bot_id, timeframe = split_tag(tag)
            rows.append(
                {
                    "export_time_utc": export_time_utc,
                    "account_id": account_id,
                    "transaction_id": tx_id,
                    "trade_id": trade_id,
                    "order_id": order_id,
                    "instrument": instrument,
                    "units": units,
                    "open_time": open_time,
                    "close_time": close_time,
                    "hold_minutes": hold_minutes,
                    "realized_pl": realized_pl,
                    "bot_id": bot_id,
                    "timeframe": timeframe,
                    "tag": tag,
                }
            )
        return rows

    realized_pl = extract_realized_pl(txn, None)
    if tx_type == "TRADE_CLOSE" or tx_type == "ORDER_FILL" or realized_pl is not None:
        if realized_pl is None:
            return rows
        trade_id = extract_trade_id(txn, None)
        hold_minutes = ""
        open_dt = parse_iso_utc(open_time_default)
        close_dt = parse_iso_utc(close_time)
        if open_dt is not None and close_dt is not None:
            hold_minutes = round((close_dt - open_dt).total_seconds() / 60.0, 3)
        bot_id, timeframe = split_tag(tag)
        rows.append(
            {
                "export_time_utc": export_time_utc,
                "account_id": account_id,
                "transaction_id": tx_id,
                "trade_id": trade_id,
                "order_id": order_id_default,
                "instrument": instrument,
                "units": units_default,
                "open_time": open_time_default,
                "close_time": close_time,
                "hold_minutes": hold_minutes,
                "realized_pl": realized_pl,
                "bot_id": bot_id,
                "timeframe": timeframe,
                "tag": tag,
            }
        )
    return rows


def parse_accounts(raw_accounts: str | None, raw_account: str | None) -> list[str]:
    accounts: list[str] = []
    if raw_accounts:
        for part in raw_accounts.split(","):
            val = part.strip()
            if val:
                accounts.append(val)
    elif raw_account:
        val = raw_account.strip()
        if val:
            accounts.append(val)
    deduped: list[str] = []
    for a in accounts:
        if a not in deduped:
            deduped.append(a)
    return deduped


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv_simple(root / ".env")

    parser = argparse.ArgumentParser(description="Export OANDA closed trade performance rows to CSV.")
    parser.add_argument(
        "--since-days",
        type=int,
        default=int(os.getenv("OANDA_SINCE_DAYS", str(DEFAULT_SINCE_DAYS))),
        help=f"Only export closes since N days ago (default from OANDA_SINCE_DAYS or {DEFAULT_SINCE_DAYS}).",
    )
    parser.add_argument("--outdir", default=DEFAULT_OUTDIR, help="Output directory for CSV exports.")
    parser.add_argument("--env", choices=["practice", "live"], default=None, help="Override OANDA environment.")
    parser.add_argument("--accounts", default=None, help='Override accounts CSV, e.g. "id1,id2".')
    args = parser.parse_args()

    api_key = os.getenv("OANDA_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Missing OANDA_API_KEY")

    oanda_env = (args.env or os.getenv("OANDA_ENV", "practice")).strip().lower()
    accounts = parse_accounts(args.accounts or os.getenv("OANDA_ACCOUNT_IDS"), None)
    if not accounts:
        accounts = parse_accounts(None, os.getenv("OANDA_ACCOUNT_ID"))
    if not accounts:
        raise SystemExit("Missing OANDA_ACCOUNT_IDS or OANDA_ACCOUNT_ID")

    if args.since_days <= 0:
        raise SystemExit("--since-days must be > 0")

    now_utc = datetime.now(timezone.utc)
    export_time_utc = now_utc.isoformat()
    cutoff = now_utc - timedelta(days=args.since_days)

    client = OandaClient(api_key=api_key, env=oanda_env)
    order_tag_cache: dict[tuple[str, str], str] = {}
    rows: list[dict[str, Any]] = []
    stats = ExportStats()

    for account_id in accounts:
        last_tx_id = client.get_last_transaction_id(account_id)
        from_id = max(1, last_tx_id - LOOKBACK_TRANSACTIONS)
        txns = client.get_transactions_idrange(account_id, from_id=from_id, to_id=last_tx_id)
        stats.accounts_processed += 1
        stats.transactions_scanned += len(txns)
        for txn in txns:
            close_dt = parse_iso_utc(str(txn.get("time", "")))
            if close_dt is not None and close_dt < cutoff:
                continue
            tag = resolve_tag(client, account_id, txn, order_tag_cache)
            built = build_rows_for_transaction(export_time_utc, account_id, txn, tag)
            if built:
                rows.extend(built)

    for row in rows:
        if not row.get("tag"):
            stats.missing_tag_rows += 1
    stats.closed_trades_exported = len(rows)

    outdir = Path(args.outdir)
    if not outdir.is_absolute():
        outdir = root / outdir
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = now_utc.strftime("%Y-%m-%d_%H%M%S")
    outpath = outdir / f"oanda_trades_{stamp}.csv"

    fieldnames = [
        "export_time_utc",
        "account_id",
        "transaction_id",
        "trade_id",
        "order_id",
        "instrument",
        "units",
        "open_time",
        "close_time",
        "hold_minutes",
        "realized_pl",
        "bot_id",
        "timeframe",
        "tag",
    ]
    with outpath.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"saved: {outpath}")
    print(f"accounts_processed: {stats.accounts_processed}")
    print(f"transactions_scanned: {stats.transactions_scanned}")
    print(f"closed_trades_exported: {stats.closed_trades_exported}")
    print(f"rows_missing_tag: {stats.missing_tag_rows}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.HTTPError as exc:
        body = ""
        if exc.response is not None:
            try:
                body = exc.response.text
            except Exception:
                body = ""
        print(f"HTTP error: {exc} {body}", file=sys.stderr)
        raise
