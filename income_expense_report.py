#!/usr/bin/env python3
"""
income_expense_report.py
=========================
Create monthly income, expense, and cash-flow reports from a SQLite-backed
GnuCash book.

Two reporting modes are available:

1. Native-currency mode (default)
   Prints a separate report for every account currency found.

2. Consolidated-currency mode (--report-currency)
   Converts every income/expense split to one selected reporting currency
   using the nearest historical exchange rate in GnuCash's Price Database.
   Rates are read only from the existing SQLite book; this script never
   downloads rates or changes the database.

Examples
--------

# Separate monthly reports for all currencies in 2026
python income_expense_report.py \
  --gnucash-file portfolio-sqlite.gnucash \
  --year 2026

# Consolidate all income/expense into GBP using GnuCash rates nearest to
# each transaction date. Also export conversion audit information.
python income_expense_report.py \
  --gnucash-file portfolio-sqlite.gnucash \
  --year 2026 \
  --report-currency GBP \
  --rate-tolerance-days 7 \
  --conversion-audit-csv conversion-audit-2026-gbp.csv \
  --detail \
  --plot cashflow-2026-gbp.png

For plots, install matplotlib:
  pip install matplotlib
"""

import argparse
import calendar
import csv
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Tuple


MONTHS = list(range(1, 13))
REPORT_ACCOUNT_TYPES = {"INCOME", "EXPENSE"}


def decimal_from_fraction(numerator, denominator) -> Decimal:
    if denominator in (None, 0):
        return Decimal("0")
    return Decimal(int(numerator)) / Decimal(int(denominator))


def open_readonly_database(path: str) -> sqlite3.Connection:
    absolute_path = os.path.abspath(path)
    if not os.path.exists(absolute_path):
        raise FileNotFoundError(f"GnuCash file not found: {absolute_path}")
    conn = sqlite3.connect(f"file:{absolute_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_account_fullnames(conn: sqlite3.Connection) -> Dict[str, str]:
    rows = conn.execute("SELECT guid, name, parent_guid FROM accounts").fetchall()
    accounts = {
        row["guid"]: {"name": row["name"] or "", "parent_guid": row["parent_guid"]}
        for row in rows
    }
    cache: Dict[str, str] = {}

    def fullname(guid: str, trail=None) -> str:
        if guid in cache:
            return cache[guid]
        if trail is None:
            trail = set()
        if guid in trail:
            return accounts.get(guid, {}).get("name", guid)
        trail.add(guid)
        account = accounts.get(guid)
        if account is None:
            return ""
        parent_guid = account["parent_guid"]
        parent_name = fullname(parent_guid, trail) if parent_guid else ""
        result = f"{parent_name}:{account['name']}" if parent_name else account["name"]
        cache[guid] = result
        return result

    for guid in accounts:
        fullname(guid)
    return cache


def commodity_guid_map(conn: sqlite3.Connection) -> Dict[str, str]:
    """Return ISO currency mnemonic -> commodity GUID."""
    rows = conn.execute(
        "SELECT guid, mnemonic FROM commodities WHERE namespace = 'CURRENCY'"
    ).fetchall()
    return {str(row["mnemonic"]).upper(): row["guid"] for row in rows if row["mnemonic"]}


def fetch_report_splits(conn: sqlite3.Connection, year: int) -> Iterable[sqlite3.Row]:
    """Fetch income/expense splits with their own account commodity."""
    return conn.execute(
        """
        SELECT
            t.post_date AS post_date,
            t.description AS description,
            a.guid AS account_guid,
            a.account_type AS account_type,
            c.guid AS commodity_guid,
            c.mnemonic AS currency,
            s.quantity_num AS quantity_num,
            s.quantity_denom AS quantity_denom
        FROM splits AS s
        INNER JOIN transactions AS t ON t.guid = s.tx_guid
        INNER JOIN accounts AS a ON a.guid = s.account_guid
        LEFT JOIN commodities AS c ON c.guid = a.commodity_guid
        WHERE substr(t.post_date, 1, 4) = ?
          AND a.account_type IN ('INCOME', 'EXPENSE')
        ORDER BY t.post_date, a.account_type, a.name
        """,
        (str(year),),
    )


def transaction_date(value) -> date:
    text = str(value or "")
    # GnuCash stores date columns with a date prefix in its SQLite backend.
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


class PriceDatabaseRates:
    """Read-only historical FX converter using GnuCash's prices table.

    A direct row has commodity_guid=source currency and currency_guid=target
    reporting currency, representing target units per source unit.

    If no direct row exists, the reverse pair is searched and inverted.
    The closest rate within tolerance days is selected.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        target_currency: str,
        tolerance_days: int,
    ):
        self.conn = conn
        self.target_currency = target_currency.upper()
        self.tolerance_days = tolerance_days
        self.commodities = commodity_guid_map(conn)
        if self.target_currency not in self.commodities:
            raise ValueError(
                f"Target currency {self.target_currency!r} does not exist in the GnuCash Price Database"
            )
        self.target_guid = self.commodities[self.target_currency]
        self._cache: Dict[Tuple[str, str], Optional[dict]] = {}

    def _nearest_price(self, commodity_guid: str, currency_guid: str, on_date: date) -> Optional[dict]:
        key = (commodity_guid, currency_guid, on_date.isoformat())
        if key in self._cache:
            return self._cache[key]

        row = self.conn.execute(
            """
            SELECT
                date,
                value_num,
                value_denom,
                ABS(julianday(substr(date, 1, 10)) - julianday(?)) AS distance_days
            FROM prices
            WHERE commodity_guid = ?
              AND currency_guid = ?
            ORDER BY distance_days ASC, date DESC
            LIMIT 1
            """,
            (on_date.isoformat(), commodity_guid, currency_guid),
        ).fetchone()

        if row is None or row["distance_days"] is None or float(row["distance_days"]) > self.tolerance_days:
            self._cache[key] = None
            return None

        try:
            rate = decimal_from_fraction(row["value_num"], row["value_denom"])
        except (InvalidOperation, ValueError, TypeError):
            self._cache[key] = None
            return None

        if rate <= 0:
            self._cache[key] = None
            return None

        result = {
            "rate": rate,
            "rate_date": str(row["date"])[:10],
            "distance_days": float(row["distance_days"]),
        }
        self._cache[key] = result
        return result

    def convert(self, amount: Decimal, source_currency: str, on_date: date) -> dict:
        source_currency = (source_currency or "UNKNOWN").upper()
        if source_currency == self.target_currency:
            return {
                "status": "identity",
                "converted_amount": amount,
                "rate": Decimal("1"),
                "rate_date": on_date.isoformat(),
                "rate_source": "identity",
                "distance_days": Decimal("0"),
            }

        source_guid = self.commodities.get(source_currency)
        if not source_guid:
            return {
                "status": "missing",
                "reason": f"Source currency {source_currency} is absent from GnuCash commodities",
            }

        direct = self._nearest_price(source_guid, self.target_guid, on_date)
        if direct:
            return {
                "status": "converted",
                "converted_amount": amount * direct["rate"],
                "rate": direct["rate"],
                "rate_date": direct["rate_date"],
                "rate_source": "gnucash-db-direct",
                "distance_days": Decimal(str(direct["distance_days"])),
            }

        inverse = self._nearest_price(self.target_guid, source_guid, on_date)
        if inverse:
            rate = Decimal("1") / inverse["rate"]
            return {
                "status": "converted",
                "converted_amount": amount * rate,
                "rate": rate,
                "rate_date": inverse["rate_date"],
                "rate_source": "gnucash-db-inverted",
                "distance_days": Decimal(str(inverse["distance_days"])),
            }

        return {
            "status": "missing",
            "reason": (
                f"No {source_currency}/{self.target_currency} or "
                f"{self.target_currency}/{source_currency} Price Database rate within "
                f"{self.tolerance_days} day(s) of {on_date.isoformat()}"
            ),
        }


def make_report(
    conn: sqlite3.Connection,
    year: int,
    native_currency: Optional[str],
    report_currency: Optional[str],
    tolerance_days: int,
    missing_rate: str,
) -> Tuple[dict, dict, List[dict], List[dict]]:
    """Create native or converted report data.

    Returns:
      monthly totals per output currency,
      category totals per output currency,
      conversion audit records,
      missing-rate records.
    """
    fullnames = build_account_fullnames(conn)
    converter = (
        PriceDatabaseRates(conn, report_currency, tolerance_days)
        if report_currency
        else None
    )

    monthly = defaultdict(
        lambda: defaultdict(lambda: {"income": Decimal("0"), "expense": Decimal("0")})
    )
    categories = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))
    audit_rows: List[dict] = []
    missing_rows: List[dict] = []

    for row in fetch_report_splits(conn, year):
        try:
            post_date = transaction_date(row["post_date"])
        except ValueError:
            continue

        source_currency = (row["currency"] or "UNKNOWN").upper()
        if native_currency and source_currency != native_currency.upper():
            continue

        account_name = fullnames.get(row["account_guid"], row["account_guid"])
        quantity = decimal_from_fraction(row["quantity_num"], row["quantity_denom"])
        account_type = row["account_type"]

        # Convert income credits to positive, retain expense debit convention.
        native_amount = -quantity if account_type == "INCOME" else quantity
        output_currency = report_currency.upper() if report_currency else source_currency

        if converter:
            conversion = converter.convert(native_amount, source_currency, post_date)
            audit = {
                "post_date": post_date.isoformat(),
                "account": account_name,
                "account_type": account_type,
                "description": row["description"] or "",
                "source_amount": str(native_amount),
                "source_currency": source_currency,
                "target_currency": output_currency,
                "rate": str(conversion.get("rate", "")),
                "rate_date": conversion.get("rate_date", ""),
                "rate_source": conversion.get("rate_source", ""),
                "distance_days": str(conversion.get("distance_days", "")),
                "converted_amount": str(conversion.get("converted_amount", "")),
                "status": conversion["status"],
                "reason": conversion.get("reason", ""),
            }
            audit_rows.append(audit)

            if conversion["status"] == "missing":
                missing_rows.append(audit)
                if missing_rate == "fail":
                    raise RuntimeError(
                        f"Missing exchange rate: {audit['reason']} — account {account_name}"
                    )
                # "warn" and "skip" both exclude unconvertible records.
                continue
            amount = conversion["converted_amount"]
        else:
            amount = native_amount

        month = post_date.month
        if account_type == "INCOME":
            monthly[output_currency][month]["income"] += amount
        else:
            monthly[output_currency][month]["expense"] += amount
        categories[output_currency][account_name][month] += amount

    return monthly, categories, audit_rows, missing_rows


def money(value: Decimal, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def print_summary(currency: str, by_month: dict, year: int, converted: bool) -> None:
    print("\n" + "=" * 82)
    mode = "CONSOLIDATED" if converted else "NATIVE-CURRENCY"
    print(f"{mode} INCOME / EXPENSE / CASH-FLOW REPORT — {year} — {currency}")
    print("=" * 82)
    print(f"{'Month':<12}{'Income':>21}{'Expenses':>21}{'Net cash flow':>24}")
    print("-" * 82)

    total_income = Decimal("0")
    total_expense = Decimal("0")
    for month in MONTHS:
        values = by_month.get(month, {"income": Decimal("0"), "expense": Decimal("0")})
        income = values["income"]
        expense = values["expense"]
        net = income - expense
        total_income += income
        total_expense += expense
        print(
            f"{calendar.month_abbr[month]:<12}"
            f"{money(income, currency):>21}"
            f"{money(expense, currency):>21}"
            f"{money(net, currency):>24}"
        )

    print("-" * 82)
    print(
        f"{'YEAR TOTAL':<12}"
        f"{money(total_income, currency):>21}"
        f"{money(total_expense, currency):>21}"
        f"{money(total_income - total_expense, currency):>24}"
    )


def print_category_breakdown(currency: str, categories: dict, year: int) -> None:
    print("\n" + "=" * 82)
    print(f"ACCOUNT / CATEGORY BREAKDOWN — {year} — {currency}")
    print("=" * 82)
    print(f"{'Account':<52}{'Annual total':>30}")
    print("-" * 82)

    totals = [
        (account, sum(months.values(), Decimal("0")))
        for account, months in categories.items()
    ]
    for account, total in sorted(totals, key=lambda item: abs(item[1]), reverse=True):
        label = account if len(account) <= 51 else account[:48] + "..."
        print(f"{label:<52}{money(total, currency):>30}")


def write_summary_csv(path: str, all_monthly: dict, year: int) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["year", "currency", "month_number", "month", "income", "expenses", "net_cash_flow"])
        for currency, by_month in sorted(all_monthly.items()):
            total_income = Decimal("0")
            total_expense = Decimal("0")
            for month in MONTHS:
                values = by_month.get(month, {"income": Decimal("0"), "expense": Decimal("0")})
                income = values["income"]
                expense = values["expense"]
                total_income += income
                total_expense += expense
                writer.writerow(
                    [year, currency, month, calendar.month_name[month], str(income), str(expense), str(income - expense)]
                )
            writer.writerow(
                [year, currency, "TOTAL", "Year total", str(total_income), str(total_expense), str(total_income - total_expense)]
            )
    print(f"Wrote summary CSV: {path}")


def write_conversion_audit(path: str, rows: List[dict]) -> None:
    fields = [
        "post_date", "account", "account_type", "description",
        "source_amount", "source_currency", "target_currency",
        "rate", "rate_date", "rate_source", "distance_days",
        "converted_amount", "status", "reason",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote conversion audit CSV: {path}")


def make_plot(path: str, currency: str, by_month: dict, year: int, consolidated: bool) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plot generation requires matplotlib. Install it with: pip install matplotlib"
        ) from exc

    labels = [calendar.month_abbr[month] for month in MONTHS]
    income = [float(by_month.get(month, {}).get("income", Decimal("0"))) for month in MONTHS]
    expense = [float(by_month.get(month, {}).get("expense", Decimal("0"))) for month in MONTHS]
    net = [inc - exp for inc, exp in zip(income, expense)]
    positions = list(range(len(MONTHS)))
    width = 0.36

    figure, axes = plt.subplots(figsize=(13, 7))
    axes.bar([x - width / 2 for x in positions], income, width, label="Income", color="#2e8b57")
    axes.bar([x + width / 2 for x in positions], expense, width, label="Expenses", color="#c94c4c")
    axes.plot(positions, net, label="Net cash flow", color="#246eb9", marker="o", linewidth=2.2)
    axes.axhline(0, color="#333333", linewidth=0.8)
    axes.set_xticks(positions)
    axes.set_xticklabels(labels)
    axes.set_ylabel(currency)
    prefix = "Consolidated " if consolidated else ""
    axes.set_title(f"{prefix}Monthly Income, Expenses and Net Cash Flow — {year} — {currency}")
    axes.legend()
    axes.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)
    print(f"Wrote cash-flow plot: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create monthly income/expense/cash-flow reports from a SQLite GnuCash book."
    )
    parser.add_argument("--gnucash-file", required=True, help="Path to a SQLite-backed .gnucash file")
    parser.add_argument("--year", required=True, type=int, help="Calendar year to report, e.g. 2026")
    parser.add_argument(
        "--currency",
        default=None,
        help="Native report filter: e.g. GBP or EUR. Ignored when --report-currency is supplied.",
    )
    parser.add_argument(
        "--report-currency",
        default=None,
        help="Consolidate every income/expense split into this currency using GnuCash Price Database rates, e.g. GBP.",
    )
    parser.add_argument(
        "--rate-source",
        choices=["gnucash-db"],
        default="gnucash-db",
        help="Exchange-rate source for consolidated reports. Currently only GnuCash Price Database is supported.",
    )
    parser.add_argument(
        "--rate-tolerance-days",
        type=int,
        default=7,
        help="Maximum days between transaction and nearest GnuCash price date (default: 7).",
    )
    parser.add_argument(
        "--missing-rate",
        choices=["warn", "skip", "fail"],
        default="warn",
        help="Handling for missing FX rates: warn/skip excludes the row; fail stops the report (default: warn).",
    )
    parser.add_argument("--detail", action="store_true", help="Print annual totals by income/expense account")
    parser.add_argument("--csv-output", default=None, help="Optional path for monthly summary CSV output")
    parser.add_argument(
        "--conversion-audit-csv",
        default=None,
        help="Optional CSV recording source amount, FX rate, selected price date, conversion source, and missing-rate rows.",
    )
    parser.add_argument(
        "--plot",
        default=None,
        help="Optional PNG output path for a cash-flow chart. Requires a single report currency.",
    )
    args = parser.parse_args()

    if args.year < 1900 or args.year > 9999:
        parser.error("--year must be a four-digit calendar year")
    if args.rate_tolerance_days < 0:
        parser.error("--rate-tolerance-days must be zero or greater")
    if args.conversion_audit_csv and not args.report_currency:
        parser.error("--conversion-audit-csv requires --report-currency")

    conn = open_readonly_database(args.gnucash_file)
    try:
        monthly, categories, audit_rows, missing_rows = make_report(
            conn=conn,
            year=args.year,
            native_currency=None if args.report_currency else args.currency,
            report_currency=args.report_currency,
            tolerance_days=args.rate_tolerance_days,
            missing_rate=args.missing_rate,
        )
    finally:
        conn.close()

    if not monthly:
        target = args.report_currency or args.currency or "any currency"
        print(f"No reportable income or expense splits found for {args.year} in {target}.")
        return

    consolidated = bool(args.report_currency)
    for currency in sorted(monthly):
        print_summary(currency, monthly[currency], args.year, consolidated)
        if args.detail:
            print_category_breakdown(currency, categories[currency], args.year)

    if missing_rows:
        print("\n" + "!" * 82)
        print(f"WARNING: {len(missing_rows)} split(s) were excluded because no usable FX rate was found.")
        print("Use --conversion-audit-csv to inspect every missing or converted record.")
        print("Populate the GnuCash Price Database with suitable historical rates, then rerun.")
        print("!" * 82)

    if args.csv_output:
        write_summary_csv(args.csv_output, monthly, args.year)

    if args.conversion_audit_csv:
        write_conversion_audit(args.conversion_audit_csv, audit_rows)

    if args.plot:
        if args.report_currency:
            selected_currency = args.report_currency.upper()
        elif args.currency:
            selected_currency = args.currency.upper()
        elif len(monthly) == 1:
            selected_currency = next(iter(monthly))
        else:
            available = ", ".join(sorted(monthly))
            raise RuntimeError(
                f"Multiple currencies found ({available}). Use --currency or --report-currency for a single chart."
            )
        if selected_currency not in monthly:
            raise RuntimeError(f"No {selected_currency} data available for {args.year}; cannot create plot")
        make_plot(args.plot, selected_currency, monthly[selected_currency], args.year, consolidated)


if __name__ == "__main__":
    main()
