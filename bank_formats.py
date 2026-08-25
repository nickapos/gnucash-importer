"""CSV detection and parsers for supported bank exports."""

import csv
from typing import Iterable, List, Tuple

from config import ALPHA_GR_HEADER_MARKER, BASE_CURRENCY, BOS_TYPE_DESCRIPTIONS


class BankFormatMapper:
    @staticmethod
    def map_revolut(row: dict) -> dict:
        raw_amount = row.get("Amount", row.get("amount", "0"))
        try:
            amount = float(str(raw_amount).replace("£", "").replace(",", "."))
        except ValueError:
            amount = 0.0
        state = row.get("State", "")
        memo_parts = [part for part in [row.get("Type", ""), state] if part]
        return {
            "date": row.get("Completed Date") or row.get("Started Date") or row.get("date") or row.get("transaction_date", ""),
            "payee": row.get("Description") or row.get("counterparty") or row.get("description") or row.get("merchant_name", ""),
            "amount": amount,
            "currency": (row.get("Currency") or row.get("currency", "")).upper() or BASE_CURRENCY,
            "memo": " / ".join(memo_parts) if memo_parts else row.get("notes", ""),
            "state": state,
            "is_pending": state.strip().upper() == "PENDING",
            "original_row": row,
        }

    @staticmethod
    def map_bof_scot(row: dict) -> dict:
        def clean_amount(raw) -> float:
            raw = (raw or "").strip()
            if not raw or raw.lower() == "blank":
                return 0.0
            try:
                return float(raw.replace("£", "").replace(",", ""))
            except ValueError:
                return 0.0

        type_code = (row.get("Type") or "").strip().upper()
        return {
            "date": row.get("Date") or row.get("Posting Date") or row.get("Value Date", ""),
            "payee": row.get("Description") or row.get("Payee", ""),
            "amount": clean_amount(row.get("Money In (£)") or row.get("Money In") or row.get("Credit"))
            - clean_amount(row.get("Money Out (£)") or row.get("Money Out") or row.get("Debit")),
            "currency": BASE_CURRENCY,
            "memo": BOS_TYPE_DESCRIPTIONS.get(type_code, type_code) or row.get("Reference") or row.get("Notes", ""),
            "is_pending": False,
            "original_row": row,
        }

    @staticmethod
    def map_alpha_gr(row: dict) -> dict:
        raw_amount = row.get("Ποσό") or "0"
        sign = (row.get("Πρόσημο ποσού") or "").strip().upper()
        try:
            amount = float(raw_amount.strip().replace(".", "").replace(",", "."))
        except ValueError:
            amount = 0.0
        if sign == "Χ":
            amount = -abs(amount)
        elif sign == "Π":
            amount = abs(amount)
        reference = row.get("Αρ. συναλλαγής") or ""
        return {
            "date": row.get("Ημ/νία") or "",
            "payee": row.get("Αιτιολογία") or "",
            "amount": amount,
            "currency": "EUR",
            "memo": f"Ref: {reference}" if reference else "",
            "is_pending": False,
            "original_row": row,
        }

    @staticmethod
    def map_generic(row: dict) -> dict:
        lower = {key.lower().strip(): value for key, value in row.items()}
        try:
            amount = float(str(lower.get("amount", "0")).replace("£", "").replace(",", "."))
        except (ValueError, AttributeError):
            amount = 0.0
        return {
            "date": lower.get("date", ""),
            "payee": lower.get("payee", ""),
            "amount": amount,
            "currency": lower.get("currency", "").upper() or BASE_CURRENCY,
            "memo": lower.get("memo", ""),
            "is_pending": False,
            "original_row": row,
        }

    @staticmethod
    def detect_format(csv_file: str) -> str:
        try:
            with open(csv_file, "r", encoding="utf-8-sig", newline="") as handle:
                for _ in range(10):
                    line = handle.readline()
                    if not line:
                        break
                    if line.strip().startswith(ALPHA_GR_HEADER_MARKER):
                        return "alpha_gr"
                handle.seek(0)
                header = next(csv.reader(handle))
            lower_header = [cell.strip().lower() for cell in header]
            original_header = [cell.strip() for cell in header]
            if any("counterparty" in cell for cell in lower_header) or sum(
                signal in lower_header for signal in ("started date", "completed date", "product", "state")
            ) >= 2:
                return "revolut"
            if any("posting date" == cell.lower() for cell in original_header) or any(
                "posting date" in cell for cell in lower_header
            ):
                return "bof_scot"
            if sum(any(signal in cell for signal in ("money in", "money out", "balance")) for cell in lower_header) >= 2 and any(
                "type" in cell for cell in lower_header
            ):
                return "bof_scot"
            if all(any(required in cell for cell in lower_header) for required in ("date", "payee", "amount")):
                return "generic"
        except Exception as exc:
            print(f"Warning: could not detect bank format: {exc}")
        return "generic"


def read_bank_csv(csv_file: str, include_pending: bool = False) -> Tuple[str, List[dict]]:
    mapper = BankFormatMapper()
    fmt = mapper.detect_format(csv_file)
    delimiter = ";" if fmt == "alpha_gr" else ","

    with open(csv_file, "r", encoding="utf-8-sig", newline="") as handle:
        lines = handle.readlines()

    start_index = 0
    if fmt == "alpha_gr":
        for index, line in enumerate(lines):
            if line.strip().startswith(ALPHA_GR_HEADER_MARKER):
                start_index = index
                break

    rows = []
    for row_number, row in enumerate(csv.DictReader(lines[start_index:], delimiter=delimiter), 1):
        if not any(str(value).strip() for value in row.values()):
            continue
        if fmt == "revolut":
            mapped = mapper.map_revolut(row)
        elif fmt == "bof_scot":
            mapped = mapper.map_bof_scot(row)
        elif fmt == "alpha_gr":
            mapped = mapper.map_alpha_gr(row)
        else:
            mapped = mapper.map_generic(row)
        if mapped["is_pending"] and not include_pending:
            continue
        mapped["row_number"] = row_number
        rows.append(mapped)

    return fmt, rows
