"""Conservative text normalization for retrieval inputs."""

from __future__ import annotations

import re
import unicodedata


_SPACE = re.compile(r"\s+")
_PERCENT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:per\s*cent|percent)(?!\w)", re.IGNORECASE)
_GROUPED_NUMBER = re.compile(r"(?<![\w,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w,])")
_PUNCTUATION = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
})


def normalize_retrieval_query(query: str) -> str:
    """Normalize presentation-only variation without rewriting identifiers or meaning."""
    value = unicodedata.normalize("NFKC", query).translate(_PUNCTUATION)
    value = _SPACE.sub(" ", value).strip()
    value = _PERCENT.sub(r"\1%", value)
    # Only remove separators from strict grouped numerals, never arbitrary commas.
    value = _GROUPED_NUMBER.sub(lambda match: match.group(0).replace(",", ""), value)
    return value
