"""Local-only embedding benchmark for compact candidate models."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import gc
import json
import os
from statistics import median
from time import perf_counter
from typing import Callable

from tests.rag_eval.metrics import expected_chunk_rank, mean_reciprocal_rank, recall_at_k


@dataclass(frozen=True)
class EmbeddingCandidate:
    """Small local model metadata and required retrieval input normalization."""

    name: str
    license: str
    query_prefix: str = ""
    passage_prefix: str = ""


CANDIDATES = (
    EmbeddingCandidate("sentence-transformers/all-MiniLM-L6-v2", "Apache-2.0"),
    EmbeddingCandidate("BAAI/bge-small-en-v1.5", "MIT"),
    EmbeddingCandidate("intfloat/e5-small-v2", "MIT", "query: ", "passage: "),
)

PROBES = (
    ("employee_name", "Maren Voss", "Employee record: Maren Voss supports reliability operations."),
    ("employee_id", "EMP-7429", "Employee ID EMP-7429 has approved field access."),
    ("code", "ZXQ-19", "Asset code ZXQ-19 is assigned to the calibration station."),
    ("filename", "nimbus-ledger-2026 file", "The Nimbus ledger records monthly operating balances."),
    ("exact_label", "NEEDS_CALIBRATION", "Status label NEEDS_CALIBRATION requires review before dispatch."),
    ("number", "847291", "Invoice number 847291 belongs to the service renewal."),
    ("column_heading", "Escalation Owner", "Escalation Owner\nAsha Patel\nEscalation Level\nCritical"),
    ("unusual_abbreviation", "RZT", "The telemetry abbreviation RZT means remote zero touch provisioning."),
)
DECOYS = {
    "employee_name": ("Juno Hale", "Ravi Shah", "Lena Ortiz", "Daria Cole", "Noah Reed", "Mika Stone", "Asha Jain", "Theo Park"),
    "employee_id": ("EMP-1101", "EMP-1102", "EMP-1103", "EMP-1104", "EMP-1105", "EMP-1106", "EMP-1107", "EMP-1108"),
    "code": ("ZXQ-11", "ZXQ-12", "ZXQ-13", "ZXQ-14", "ZXQ-15", "ZXQ-16", "ZXQ-17", "ZXQ-18"),
    "filename": tuple(str(year) for year in range(2017, 2025)),
    "exact_label": ("PENDING_REVIEW", "ON_HOLD", "READY_FOR_TEST", "NEEDS_APPROVAL", "AWAITING_PARTS", "OUT_OF_SCOPE", "PENDING_SIGNOFF", "REQUIRES_ESCALATION"),
    "number": ("847201", "847202", "847203", "847204", "847205", "847206", "847207", "847208"),
    "column_heading": ("Service Owner", "Incident Owner", "Review Owner", "Asset Owner", "Change Owner", "Risk Owner", "Policy Owner", "Project Owner"),
    "unusual_abbreviation": ("RZP", "RZQ", "RZR", "RZS", "RZU", "RZV", "RZW", "RZX"),
}


def _rss_bytes() -> int | None:
    """Return process working-set bytes on Windows without adding psutil."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_memory_info = psapi.GetProcessMemoryInfo
    get_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    get_memory_info.restype = wintypes.BOOL
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    if not get_memory_info(
        get_current_process(),
        ctypes.byref(counters),
        counters.cb,
    ):
        return None
    return int(counters.WorkingSetSize)


def _corpus() -> tuple[list[str], list[int]]:
    """Build a synthetic near-duplicate corpus with stable target positions."""
    passages: list[str] = []
    targets: list[int] = []
    for category, query, text in PROBES:
        marker = query if query in text else "Escalation Owner"
        if category == "filename":
            passages.extend("The Nimbus ledger records monthly operating balances." for _ in DECOYS[category])
        else:
            passages.extend(text.replace(marker, replacement) for replacement in DECOYS[category])
        targets.append(len(passages))
        passages.append(text)
    return passages, targets


def benchmark_candidate(candidate: EmbeddingCandidate, *, allow_download: bool) -> dict[str, object]:
    """Measure local retrieval accuracy, dimensions, latency, memory, and index cost."""
    from sentence_transformers import SentenceTransformer

    corpus, expected = _corpus()
    before_memory = _rss_bytes()
    started = perf_counter()
    model = SentenceTransformer(candidate.name, local_files_only=not allow_download)
    load_ms = (perf_counter() - started) * 1000
    after_load_memory = _rss_bytes()

    started = perf_counter()
    corpus_vectors = model.encode(
        [candidate.passage_prefix + passage for passage in corpus],
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    corpus_ms = (perf_counter() - started) * 1000
    query_latencies = []
    rankings: list[list[int]] = []
    for _, query, _ in PROBES:
        started = perf_counter()
        query_vector = model.encode(
            candidate.query_prefix + query,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        query_latencies.append((perf_counter() - started) * 1000)
        scores = corpus_vectors @ query_vector
        rankings.append([int(index) for index in scores.argsort()[::-1]])

    relevant = [{target} for target in expected]
    ranks = [expected_chunk_rank(ranking, target) for ranking, target in zip(rankings, relevant)]
    dimension = int(corpus_vectors.shape[1])
    result = {
        "model": candidate.name,
        "license": candidate.license,
        "embedding_dimension": dimension,
        "corpus_vectors": len(corpus),
        "recall_at_5": sum(recall_at_k(ranking, target, 5) for ranking, target in zip(rankings, relevant)) / len(rankings),
        "recall_at_10": sum(recall_at_k(ranking, target, 10) for ranking, target in zip(rankings, relevant)) / len(rankings),
        "mrr": mean_reciprocal_rank(rankings, relevant),
        "ranks": ranks,
        "model_load_ms": round(load_ms, 2),
        "median_query_embedding_ms": round(median(query_latencies), 2),
        "corpus_embedding_ms": round(corpus_ms, 2),
        "estimated_index_bytes": len(corpus) * dimension * 4,
        "estimated_reindex_ms_per_1000_chunks": round(corpus_ms * 1000 / len(corpus), 2),
        "rss_delta_bytes": (
            after_load_memory - before_memory
            if before_memory is not None and after_load_memory is not None
            else None
        ),
    }
    del model, corpus_vectors
    gc.collect()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark compact local embedding alternatives.")
    parser.add_argument("--allow-download", action="store_true", help="Permit downloading the two compact candidate models when absent from the local cache.")
    arguments = parser.parse_args()
    results = [asdict(candidate) | benchmark_candidate(candidate, allow_download=arguments.allow_download) for candidate in CANDIDATES]
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
