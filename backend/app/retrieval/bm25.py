from __future__ import annotations

import math


class BM25:
    """Pure-Python Okapi BM25 scorer over a fixed candidate corpus."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.documents: list[list[str]] = []
        self.doc_frequencies: dict[str, int] = {}
        self.doc_lengths: list[int] = []
        self.average_length = 0.0

    def fit(self, documents: list[list[str]]) -> None:
        self.documents = documents
        self.doc_frequencies = {}
        self.doc_lengths = [len(doc) for doc in documents]
        self.average_length = sum(self.doc_lengths) / max(1, len(documents))
        for doc in documents:
            for term in set(doc):
                self.doc_frequencies[term] = self.doc_frequencies.get(term, 0) + 1

    def idf(self, term: str) -> float:
        document_frequency = self.doc_frequencies.get(term, 0)
        if document_frequency == 0:
            return 0.0
        return math.log(1 + (len(self.documents) - document_frequency + 0.5) / (document_frequency + 0.5))

    def score(self, query_terms: list[str], document_index: int) -> float:
        if not self.documents:
            return 0.0
        document = self.documents[document_index]
        if not document:
            return 0.0
        term_frequencies: dict[str, int] = {}
        for term in document:
            term_frequencies[term] = term_frequencies.get(term, 0) + 1
        length = self.doc_lengths[document_index]
        denominator = self.k1 * (1 - self.b + self.b * length / self.average_length)
        total = 0.0
        for term in set(query_terms):
            frequency = term_frequencies.get(term, 0)
            if frequency == 0:
                continue
            total += self.idf(term) * (frequency * (self.k1 + 1)) / (frequency + denominator)
        return total
