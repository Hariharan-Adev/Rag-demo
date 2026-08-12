"""Deterministic retrieval metrics for the local RAG evaluation suite."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Iterable, Sequence, TypeVar


Identifier = TypeVar("Identifier", bound=Hashable)
DEFAULT_K_VALUES = (1, 3, 5, 10, 15)


@dataclass(frozen=True)
class RetrievalMetricsAtK:
    """Core relevance metrics calculated at one retrieval cutoff."""

    k: int
    hit_rate: float
    recall: float
    precision: float


def _validate_k(k: int) -> None:
    """Reject invalid cutoffs so metric results cannot be silently misleading."""
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("Retrieval metric K must be a positive integer.")


def _relevant_hits_at_k(
    ranked_ids: Sequence[Identifier],
    relevant_ids: Iterable[Identifier],
    k: int,
) -> tuple[int, int]:
    """Return unique relevant hits and total unique relevant IDs at K."""
    _validate_k(k)
    relevant = set(relevant_ids)
    hits = len(set(ranked_ids[:k]) & relevant)
    return hits, len(relevant)


def hit_rate_at_k(
    ranked_ids: Sequence[Identifier],
    relevant_ids: Iterable[Identifier],
    k: int,
) -> float:
    """Return 1.0 when any relevant item appears in the first K results."""
    hits, _ = _relevant_hits_at_k(ranked_ids, relevant_ids, k)
    return 1.0 if hits else 0.0


def recall_at_k(
    ranked_ids: Sequence[Identifier],
    relevant_ids: Iterable[Identifier],
    k: int,
) -> float:
    """Return the fraction of all relevant items retrieved in the first K."""
    hits, relevant_count = _relevant_hits_at_k(ranked_ids, relevant_ids, k)
    return hits / relevant_count if relevant_count else 0.0


def precision_at_k(
    ranked_ids: Sequence[Identifier],
    relevant_ids: Iterable[Identifier],
    k: int,
) -> float:
    """Return unique relevant hits divided by K, even when fewer results exist."""
    hits, _ = _relevant_hits_at_k(ranked_ids, relevant_ids, k)
    return hits / k


def reciprocal_rank(
    ranked_ids: Sequence[Identifier],
    relevant_ids: Iterable[Identifier],
    k: int | None = None,
) -> float:
    """Return the reciprocal of the first relevant 1-based rank, or zero."""
    if k is not None:
        _validate_k(k)
    relevant = set(relevant_ids)
    limit = len(ranked_ids) if k is None else min(k, len(ranked_ids))
    for index, identifier in enumerate(ranked_ids[:limit], start=1):
        if identifier in relevant:
            return 1.0 / index
    return 0.0


def mean_reciprocal_rank(
    ranked_results: Sequence[Sequence[Identifier]],
    relevant_results: Sequence[Iterable[Identifier]],
    k: int | None = None,
) -> float:
    """Average reciprocal rank across queries, returning zero for no queries."""
    if len(ranked_results) != len(relevant_results):
        raise ValueError("Ranked and relevant query collections must have equal length.")
    if not ranked_results:
        return 0.0
    return sum(
        reciprocal_rank(ranked, relevant, k)
        for ranked, relevant in zip(ranked_results, relevant_results)
    ) / len(ranked_results)


def correct_source_selected(
    selected_source_id: Identifier | None,
    expected_source_ids: Iterable[Identifier],
) -> bool:
    """Report whether the selected source is one of the accepted sources."""
    return selected_source_id is not None and selected_source_id in set(expected_source_ids)


def expected_chunk_rank(
    ranked_chunk_ids: Sequence[Identifier],
    expected_chunk_ids: Iterable[Identifier],
    k: int | None = None,
) -> int | None:
    """Return the first expected chunk's 1-based rank, bounded by K if set."""
    if k is not None:
        _validate_k(k)
    expected = set(expected_chunk_ids)
    limit = len(ranked_chunk_ids) if k is None else min(k, len(ranked_chunk_ids))
    for index, chunk_id in enumerate(ranked_chunk_ids[:limit], start=1):
        if chunk_id in expected:
            return index
    return None


def calculate_metrics_at_k(
    ranked_ids: Sequence[Identifier],
    relevant_ids: Iterable[Identifier],
    k_values: Iterable[int] = DEFAULT_K_VALUES,
) -> dict[int, RetrievalMetricsAtK]:
    """Calculate core metrics for each unique configured K in input order."""
    relevant = tuple(relevant_ids)
    metrics: dict[int, RetrievalMetricsAtK] = {}
    for k in k_values:
        _validate_k(k)
        if k in metrics:
            continue
        metrics[k] = RetrievalMetricsAtK(
            k=k,
            hit_rate=hit_rate_at_k(ranked_ids, relevant, k),
            recall=recall_at_k(ranked_ids, relevant, k),
            precision=precision_at_k(ranked_ids, relevant, k),
        )
    return metrics
