"""Shared deduplication logic for Calibre preprocessing steps.

Used by step1 (前置预处理 dedup with title+bytes+initPath key).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Iterable

from knowledge_assets.preprocess.common import normalize_title


@dataclass(frozen=True)
class DedupRecord:
    """A record for dedup grouping.

    Attributes:
        book_id: Book ID from books table
        data_id: Data record ID from data table
        title: Book title from books.title
        bytes: File size (data.uncompressed_size)
        init_path: BookInitPath value (custom_column_1, aggregated if multiple)
        relative_path: Calibre library-relative path (books.path/data.name.format)
        sort_key: Optional sort key for keep selection (e.g., relative_path length)
    """
    book_id: int
    data_id: int
    title: str
    bytes: int
    init_path: str
    relative_path: str = ""
    sort_key: int = 0


@dataclass(frozen=True)
class DedupGroupResult:
    """Result of dedup grouping: one group with a keep decision."""
    group_key: str
    records: list[DedupRecord]
    keep_record: DedupRecord


def group_duplicate_records(
    records: list[DedupRecord],
    key_func: Callable[[DedupRecord], str],
) -> list[DedupGroupResult]:
    """Group records by dedup key and select which to keep.

    Args:
        records: List of records to deduplicate
        key_func: Function that computes dedup key from a record

    Returns:
        List of groups with >1 record, each with a keep decision
    """
    groups: dict[str, list[DedupRecord]] = {}
    for record in records:
        key = key_func(record)
        groups.setdefault(key, []).append(record)

    results: list[DedupGroupResult] = []
    for key, group in sorted(groups.items()):
        if len(group) > 1:
            keep = select_keep_record(group)
            results.append(DedupGroupResult(group_key=key, records=group, keep_record=keep))

    return results


def select_keep_record(records: list[DedupRecord]) -> DedupRecord:
    """Select which duplicate to keep.

    Priority:
    1. sort_key ascending (e.g., shorter relative_path)
    2. data_id ascending (stable tiebreaker)

    Args:
        records: List of duplicate records

    Returns:
        The record to keep
    """
    return min(records, key=lambda r: (r.sort_key, r.data_id))


def make_title_bytes_initpath_key(record: DedupRecord) -> str:
    """Dedup key: title + bytes + initPath (for step1)."""
    return f"{normalize_title(record.title)}|{record.bytes}|{record.init_path}"


def make_title_ext_bytes_key(record: DedupRecord, ext: str) -> str:
    """Dedup key: title + ext + bytes (legacy, kept for reference)."""
    return f"{normalize_title(record.title)}|{ext}|{record.bytes}"


@dataclass(frozen=True)
class FilteredRecord:
    """A record filtered out by junk/non-target checks."""
    record: DedupRecord
    reason: str
    detail: str


def filter_non_target_formats(
    records: list[DedupRecord],
    target_formats: frozenset[str],
    ext_by_data_id: dict[int, str],
) -> list[FilteredRecord]:
    """Filter out records whose format is not in target_formats."""
    return [
        FilteredRecord(record=r, reason="non_target_format", detail=ext_by_data_id.get(r.data_id, "").upper())
        for r in records
        if ext_by_data_id.get(r.data_id, "") not in target_formats
    ]


def filter_junk_titles(
    records: list[DedupRecord],
    junk_keywords: tuple[str, ...],
    exclude_data_ids: frozenset[int] | None = None,
) -> list[FilteredRecord]:
    """Filter out records whose title contains junk keywords."""
    excluded = exclude_data_ids or frozenset()
    results: list[FilteredRecord] = []
    for r in records:
        if r.data_id in excluded:
            continue
        for keyword in junk_keywords:
            if keyword in r.title:
                results.append(FilteredRecord(record=r, reason="title_junk_keyword", detail=keyword))
                break
    return results


@dataclass(frozen=True)
class AutoCandidate:
    """System-confirmed auto-delete candidate.

    Shared output format for step1 auto-delete CSVs.
    """
    batch: str
    book_id: int
    data_id: int
    title: str
    file_basename: str
    ext: str
    bytes: int | None
    relative_path: str
    reason_code: str
    reason_detail: str
    matched_value: str
    recommended_action: str
    custom_column_1_value: str = ""


_AUTO_CANDIDATE_FIELDS = [f.name for f in fields(AutoCandidate)]


def build_junk_title_candidates(
    filtered: list[FilteredRecord],
    *,
    batch: str = "",
    ext_by_data_id: dict[int, str] | None = None,
) -> list[AutoCandidate]:
    """Convert junk-title FilteredRecords to AutoCandidates."""
    exts = ext_by_data_id or {}
    return sorted(
        (
            AutoCandidate(
                batch=batch,
                book_id=f.record.book_id,
                data_id=f.record.data_id,
                title=f.record.title,
                file_basename="",
                ext=exts.get(f.record.data_id, ""),
                bytes=f.record.bytes,
                relative_path=f.record.relative_path,
                reason_code="title_junk_keyword",
                reason_detail="title 命中垃圾关键词",
                matched_value=f.detail,
                recommended_action="delete_file",
                custom_column_1_value=f.record.init_path,
            )
            for f in filtered
        ),
        key=lambda c: (c.book_id, c.data_id),
    )


def build_non_target_candidates(
    filtered: list[FilteredRecord],
    *,
    batch: str = "",
    ext_by_data_id: dict[int, str] | None = None,
) -> list[AutoCandidate]:
    """Convert non-target-format FilteredRecords to AutoCandidates."""
    exts = ext_by_data_id or {}
    return sorted(
        (
            AutoCandidate(
                batch=batch,
                book_id=f.record.book_id,
                data_id=f.record.data_id,
                title=f.record.title,
                file_basename="",
                ext=exts.get(f.record.data_id, ""),
                bytes=f.record.bytes,
                relative_path=f.record.relative_path,
                reason_code="non_target_format",
                reason_detail="非电子书格式",
                matched_value=f.detail,
                recommended_action="delete_file",
                custom_column_1_value=f.record.init_path,
            )
            for f in filtered
        ),
        key=lambda c: (c.book_id, c.data_id),
    )


def write_auto_candidates_csv(
    path: Path,
    candidates: Iterable[AutoCandidate],
) -> None:
    """Write AutoCandidates to CSV (utf-8-sig)."""
    rows = list(candidates)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=_AUTO_CANDIDATE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "batch": row.batch,
                    "book_id": row.book_id,
                    "data_id": row.data_id,
                    "title": row.title,
                    "file_basename": row.file_basename,
                    "ext": row.ext,
                    "bytes": row.bytes,
                    "relative_path": row.relative_path,
                    "reason_code": row.reason_code,
                    "reason_detail": row.reason_detail,
                    "matched_value": row.matched_value,
                    "recommended_action": row.recommended_action,
                    "custom_column_1_value": row.custom_column_1_value,
                }
            )
