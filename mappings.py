"""Persistent payee mappings and historical account suggestion analysis."""

import json
import os
import difflib
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config import MAPPINGS_FILE, TRANSACTION_HISTORY_FILE
from utils import atomic_write_json, tokenize


class PayeeAccountMapper:
    def __init__(self, mappings_file: str = MAPPINGS_FILE):
        self.mappings_file = mappings_file
        self.mappings = self._load()

    def _load(self) -> Dict:
        if not os.path.exists(self.mappings_file):
            return {}
        try:
            with open(self.mappings_file, "r") as handle:
                data = json.load(handle)
            print(f"Loaded {len(data)} payee mappings from {self.mappings_file}")
            return data
        except Exception as exc:
            print(f"Could not load mappings: {exc}")
            return {}

    def save(self) -> None:
        # Atomic write so a crash cannot corrupt the mapping store.
        atomic_write_json(self.mappings_file, self.mappings)

    def get_mapping(self, payee: str) -> Optional[Dict]:
        if payee in self.mappings:
            return self.mappings[payee]

        # Token-subset matching avoids false positives such as a mapping for
        # "amazon" wrongly applying to "amazonian restaurant".
        query_tokens = tokenize(payee)
        if not query_tokens:
            return None
        for known_payee, mapping in self.mappings.items():
            known_tokens = tokenize(known_payee)
            if not known_tokens:
                continue
            if known_tokens <= query_tokens or query_tokens <= known_tokens:
                return mapping
        return None

    def add_mapping(self, payee: str, account_fullname: str, account_guid: str) -> None:
        previous = self.mappings.get(payee, {})
        self.mappings[payee] = {
            "account_fullname": account_fullname,
            "account_guid": account_guid,
            "created": previous.get("created", datetime.now().isoformat()),
            "last_used": datetime.now().isoformat(),
            "use_count": previous.get("use_count", 0) + 1,
        }
        self.save()

    def update_last_used(self, payee: str) -> None:
        if payee in self.mappings:
            self.mappings[payee]["last_used"] = datetime.now().isoformat()
            self.mappings[payee]["use_count"] = self.mappings[payee].get("use_count", 0) + 1
            self.save()

    def remove_mapping(self, payee: str) -> bool:
        if payee not in self.mappings:
            return False
        del self.mappings[payee]
        self.save()
        return True

    def list_mappings(self) -> List[Tuple[str, Dict]]:
        return sorted(self.mappings.items(), key=lambda item: item[1].get("use_count", 0), reverse=True)


class TransactionHistoryAnalyzer:
    def __init__(self, book, analysis_file: str = TRANSACTION_HISTORY_FILE):
        self.book = book
        self.analysis_file = analysis_file
        self.payee_to_accounts = defaultdict(list)
        self.word_patterns = defaultdict(Counter)

    @staticmethod
    def extract_words(text: str) -> List[str]:
        # Delegates to the shared tokenizer so stop-word handling is identical
        # everywhere.
        return sorted(tokenize(text))

    @staticmethod
    def _sortable_datetime(value):
        """Normalize a date/datetime/None transaction field for sorting.

        GnuCash stores post_date as a plain ``date`` but enter_date as a
        ``datetime``; comparing the two directly raises TypeError in Python 3
        inside ``list.sort``, so convert everything to ``datetime`` first.
        """
        if value is None:
            return datetime.now()
        if isinstance(value, datetime):
            return value
        # Plain datetime.date (not a datetime) -> midnight of that day.
        try:
            return datetime.combine(value, datetime.min.time())
        except TypeError:
            return datetime.now()

    def analyze(self, max_transactions: int = 1000, write_cache: bool = True) -> None:
        print("Analyzing transaction history...")
        # Reset both accumulators so calling analyze() more than once can
        # never double-count the same book history.
        self.payee_to_accounts = defaultdict(list)
        self.word_patterns = defaultdict(Counter)
        transactions = list(self.book.transactions)
        transactions.sort(
            key=lambda txn: self._sortable_datetime(txn.post_date or txn.enter_date),
            reverse=True,
        )
        usage = defaultdict(Counter)

        for txn in transactions[:max_transactions]:
            payee = (txn.description or "").strip()
            if not payee:
                continue
            for split in txn.splits or []:
                if split.account:
                    usage[payee][split.account.fullname] += 1
                    for word in self.extract_words(payee):
                        self.word_patterns[word][split.account.fullname] += 1

        self.payee_to_accounts = defaultdict(list)
        for payee, accounts in usage.items():
            peak = max(accounts.values())
            for account, count in accounts.most_common(5):
                self.payee_to_accounts[payee].append(
                    {"account": account, "count": count, "confidence": min(count / peak, 1.0)}
                )

        cache = {
            "last_analyzed": datetime.now().isoformat(),
            "transactions_analyzed": min(len(transactions), max_transactions),
            "payee_mappings": self.payee_to_accounts,
            "word_patterns": {word: dict(counts.most_common(10)) for word, counts in self.word_patterns.items()},
        }
        # Respect dry-run: never write the cache file when write_cache is False.
        if write_cache:
            try:
                atomic_write_json(self.analysis_file, cache)
            except Exception as exc:
                print(f"Could not save history analysis: {exc}")
        else:
            print("Dry run: history analysis cache not written")
        print(
            f"Analyzed {min(len(transactions), max_transactions)} transactions, "
            f"found {len(self.payee_to_accounts)} unique payees"
        )

    def suggest_mapping(self, payee: str, description: str = "") -> List[Tuple[str, float, str]]:
        suggestions = []
        payee_lower = payee.lower().strip()
        description_lower = description.lower().strip()

        for item in self.payee_to_accounts.get(payee, []):
            suggestions.append((item["account"], item["confidence"], f"Exact match: '{payee}' used {item['count']} times"))

        for historic_payee, mappings in self.payee_to_accounts.items():
            similarity = difflib.SequenceMatcher(None, payee_lower, historic_payee.lower()).ratio()
            if similarity > 0.7:
                for item in mappings:
                    suggestions.append((item["account"], item["confidence"] * similarity, f"Fuzzy match: '{historic_payee}' ({similarity:.1%})"))

        scores = defaultdict(float)
        words = set(self.extract_words(payee + " " + description))
        for word in words:
            accounts = self.word_patterns.get(word, {})
            if not accounts:
                continue
            for account, count in accounts.items():
                scores[account] += count / len(accounts)
        for account, score in Counter(scores).most_common(5):
            if score > 0.15:
                suggestions.append((account, min(score, 0.9), "History keyword match"))

        if description:
            for historic_payee, mappings in self.payee_to_accounts.items():
                if description_lower in historic_payee.lower() or historic_payee.lower() in description_lower:
                    for item in mappings:
                        suggestions.append((item["account"], item["confidence"] * 0.9, "Description match"))

        best = {}
        for account, confidence, reason in suggestions:
            if account not in best or confidence > best[account][0]:
                best[account] = (confidence, reason)
        return [(account, confidence, reason) for account, (confidence, reason) in best.items()]
