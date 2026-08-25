"""Persistent payee mappings and historical account suggestion analysis."""

import json
import os
import re
import difflib
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config import MAPPINGS_FILE, TRANSACTION_HISTORY_FILE


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
        with open(self.mappings_file, "w") as handle:
            json.dump(self.mappings, handle, indent=2, sort_keys=True)

    def get_mapping(self, payee: str) -> Optional[Dict]:
        if payee in self.mappings:
            return self.mappings[payee]
        normalized = payee.lower().strip()
        for known_payee, mapping in self.mappings.items():
            known = known_payee.lower().strip()
            if len(known) >= 3 and len(normalized) >= 3 and (known in normalized or normalized in known):
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
        text = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
        stop_words = {"the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by"}
        return [word for word in text.split() if len(word) > 2 and word not in stop_words]

    def analyze(self, max_transactions: int = 1000) -> None:
        print("Analyzing transaction history...")
        transactions = list(self.book.transactions)
        transactions.sort(key=lambda txn: txn.post_date or txn.enter_date, reverse=True)
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
        try:
            with open(self.analysis_file, "w") as handle:
                json.dump(cache, handle, indent=2)
        except Exception as exc:
            print(f"Could not save history analysis: {exc}")
        print(f"Analyzed {min(len(transactions), max_transactions)} transactions, found {len(self.payee_to_accounts)} unique payees")

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
