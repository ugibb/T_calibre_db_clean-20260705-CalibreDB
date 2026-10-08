"""Step3 regression: scan an incremental Calibre library and emit preprocessing candidates."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Iterable

_SRC_DIR = Path(__file__).resolve().parents[2]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from knowledge_assets.preprocess.common import (
    align_batch_to_metadata_db,
    db_table_counts,
    default_batch,
    default_input_dir,
    default_log_dir,
    default_output_dir,
    load_json_config,
    matching_full_db,
    normalize_title,
    pipeline_config,
    read_csv_dicts,
    resolve_incremental_db,
    resolve_incremental_entity_dir,
    step_dir,
)
from knowledge_assets.preprocess import encoding
from knowledge_assets.utils.logger import get_logger
from knowledge_assets.utils.logger import setup_logger


_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "template"
_STEP1_RULES_PATH = _TEMPLATE_DIR / "step1_rules.json"


@dataclass(frozen=True)
class Step1Rules:
    target_formats: frozenset[str]
    junk_title_keywords: tuple[str, ...]
    unknown_basename_markers: tuple[str, ...]
    actions: dict[str, str]
    output_order: dict[str, int]
    book_files_query: str
    manual_review_outputs: tuple[tuple[str, str], ...]
    auto_delete_outputs: tuple[tuple[str, str], ...]
    manual_summary_outputs: tuple[tuple[str, str], ...]
    auto_summary_outputs: tuple[tuple[str, str, str], ...]


def _load_step1_rules(path: Path = _STEP1_RULES_PATH) -> Step1Rules:
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    return Step1Rules(
        target_formats=frozenset(item.lower() for item in raw["target_formats"]),
        junk_title_keywords=tuple(raw["junk_title_keywords"]),
        unknown_basename_markers=tuple(raw["unknown_basename_markers"]),
        actions=dict(raw["actions"]),
        output_order={key: int(value) for key, value in raw["output_order"].items()},
        book_files_query=raw["sql"]["book_files_query"],
        manual_review_outputs=tuple(
            (item["reason_code"], item["filename"])
            for item in raw["manual_review_outputs"]
        ),
        auto_delete_outputs=tuple(
            (item["reason_code"], item["filename"])
            for item in raw["auto_delete_outputs"]
        ),
        manual_summary_outputs=tuple(
            (item["reason_code"], item["label"])
            for item in raw["manual_summary_outputs"]
        ),
        auto_summary_outputs=tuple(
            (item["reason_code"], item["label"], item["summary_type"])
            for item in raw["auto_summary_outputs"]
        ),
    )


RULES = _load_step1_rules()

#: title 乱码的 reason_code。它在流水线中被三处引用，且承担「先于重复判定分流」的
#: 语义，故提为常量，避免各处字面量漂移。
MOJIBAKE_REASON_CODE = "title_mojibake"


@dataclass(frozen=True)
class BookFile:
    batch: str
    book_id: int
    data_id: int
    title: str
    file_basename: str
    ext: str
    bytes: int | None
    relative_path: str
    file_exists: bool
    actual_bytes: int | None
    custom_column_1_value: str = ""


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
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


@dataclass(frozen=True)
class ManualReview:
    review_id: str
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
    recommended_action: str = "keep"
    custom_column_1_value: str = ""
    step2_repair_title_repair_status: str = ""
    step2_repair_title_repair_reason: str = ""
    step2_repair_title_repair_note: str = ""
    step2_repair_title_candidate: str = ""
    step2_repair_basename_repair_status: str = ""
    step2_repair_basename_repair_reason: str = ""
    step2_repair_basename_repair_note: str = ""
    step2_repair_basename_candidate: str = ""
    #: title 乱码的「部分还原提示」：可还原部分已还原，缺口以 ◻ 标注，供人工判断
    partial_hint: str = ""
    human_decision: str = ""
    human_title: str = ""
    human_file_basename: str = ""
    human_note: str = ""


@dataclass(frozen=True)
class Step2RepairHandoffs:
    title_by_book: dict[int, dict[str, str]]
    basename_by_data: dict[int, dict[str, str]]
    has_title_report: bool
    has_basename_report: bool


@dataclass(frozen=True)
class MergePlanRow:
    merge_group: str
    path_prefix: str
    winner_book_id: int
    winner_path: str
    winner_format_count: int
    loser_book_id: int
    loser_path: str
    loser_formats: str
    moved_data_ids: str
    duplicate_data_ids: str
    merge_type: str = "path_prefix"


def run_step3_regression(batch: str, incremental_dir: Path, output_dir: Path) -> None:
    logger = get_logger("preprocess.step3")
    # include_step2_repair：若本批已跑过 Step2 anomaly 的编码修复，优先扫那份副本，乱码更少。
    # include_step2_apply=False：保持历史行为——Step3 regression 只认输入库，不认 Step4 apply 的产物。
    metadata_db = resolve_incremental_db(
        batch, incremental_dir, output_dir, include_step2_repair=True, include_step2_apply=False
    )
    batch, output_dir = align_batch_to_metadata_db(batch, metadata_db, output_dir)
    parent_output_dir = output_dir
    incremental_dir = resolve_incremental_entity_dir(incremental_dir, metadata_db)
    config = pipeline_config()
    step2_cfg = load_json_config("step2_config.json")
    library_root = Path(str(step2_cfg["bat"]["library_dir_default"]))
    repair_handoffs = _load_step2_repair_handoffs(step_dir(output_dir, "step2"), config)
    output_dir = step_dir(output_dir, "step3")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not metadata_db.exists():
        raise FileNotFoundError(f"metadata.db 不存在： {metadata_db}")

    business_log: list[str] = []

    def emit(message: str) -> None:
        business_log.append(message)
        logger.info(message)

    emit("========== 开始【Step3：回归验证【跨book_id合并 → 去重 → 垃圾标题 → 非目标格式 → 乱码 → 副本标题 → 未知标题】】 ==========")
    _remove_deprecated_outputs(output_dir, logger)
    emit(f"读取最新的增量 metadata.db： {_display_path(metadata_db)}")
    source_books, source_data = db_table_counts(metadata_db)
    emit(f"输入库统计： books {source_books} | data {source_data}")

    # ---- 阶段1：跨 book_id 同书合并（在回归扫描之前执行） ----
    merged_db_path = step_dir(parent_output_dir, "step3") / "metadata.merged.db"
    shutil.copy2(metadata_db, merged_db_path)
    emit(f"复制输入库用于合并： {_display_path(merged_db_path)}")

    merge_auto_path = output_dir / str(config["step3"]["merge_auto_filename"])
    merge_unknown_path = output_dir / str(config["step3"]["merge_unknown_filename"])
    merge_title_author_path = output_dir / str(config["step3"]["merge_title_author_filename"])
    auto_merge_plan, unknown_merge_plan = _scan_cross_bookid_merge(merged_db_path, emit)
    _write_csv(merge_auto_path, auto_merge_plan, MergePlanRow)
    _write_csv(merge_unknown_path, unknown_merge_plan, MergePlanRow)
    emit(f"输出跨book_id合并计划[auto]： {_display_path(merge_auto_path)}")
    emit(f"输出跨book_id合并计划[unknown]： {_display_path(merge_unknown_path)}")
    if auto_merge_plan:
        _apply_cross_bookid_merge(merged_db_path, auto_merge_plan, emit, library_root)

    # ---- 阶段1b：title+author 合并（捕获路径前缀匹配遗漏的重复） ----
    title_author_plan = _scan_title_author_merge(merged_db_path, emit)
    _write_csv(merge_title_author_path, title_author_plan, MergePlanRow)
    emit(f"输出跨book_id合并计划[title+author]： {_display_path(merge_title_author_path)}")
    if title_author_plan:
        _apply_cross_bookid_merge(merged_db_path, title_author_plan, emit, library_root)

    # ---- 阶段1c：生成合并删除 CSV（供 step4 生成物理文件删除指令） ----
    merge_deletion_csv = output_dir / "3-1-3：系统确认-合并删除data.csv"
    _write_merge_deletion_csv(metadata_db, merged_db_path, merge_deletion_csv, emit)

    merged_books, merged_data = db_table_counts(merged_db_path)
    emit(f"合并后数据库： books {merged_books} | data {merged_data}")
    emit("")

    # ---- 阶段2：回归扫描（基于合并后的数据库） ----
    emit("-----> 回归扫描（基于合并后数据库）")
    files = _load_book_files(batch, merged_db_path, incremental_dir)
    book_total = _count_books(merged_db_path)
    ext_counts = Counter(file.ext.lower() for file in files)

    emit(f"Calibre 扫描结果1：文件总数： {len(files)} | book总数： {book_total}")
    emit(f"Calibre 扫描结果2：文件类型统计： {_format_counter(ext_counts, lower=True)}")
    emit("")

    auto_candidates, manual_reviews = _build_ordered_outputs(files, repair_handoffs)

    manual_by_reason = _group_reviews_by_reason(manual_reviews)
    candidate_by_reason = _group_candidates_by_reason(auto_candidates)

    _emit_auto_summary(emit, candidate_by_reason)
    emit("")
    _emit_manual_summary(emit, manual_by_reason)
    emit("")

    snapshot_json = output_dir / str(config["step3"]["snapshot_filename"])
    scan_log = output_dir / str(config["step3"]["log_filename"])

    auto_csvs = _write_reason_csvs(output_dir, candidate_by_reason, RULES.auto_delete_outputs, Candidate)
    manual_csvs = _write_reason_csvs(output_dir, manual_by_reason, RULES.manual_review_outputs, ManualReview)
    _write_snapshot(snapshot_json, files, auto_candidates, manual_reviews, ext_counts)

    for reason_code, path in auto_csvs:
        emit(f"输出系统确认删除候选[{reason_code}]： {_display_path(path)}")
    for reason_code, path in manual_csvs:
        emit(f"输出待人工确认文件[{reason_code}]： {_display_path(path)}")

    emit("")
    _generate_and_apply_cleaning_sql(batch, incremental_dir, parent_output_dir, emit, metadata_db=merged_db_path)

    cleaned_db = step_dir(parent_output_dir, "step3") / str(config["step3"]["cleaned_db_filename"])
    if cleaned_db.exists():
        cleaned_books, cleaned_data = db_table_counts(cleaned_db)
        emit(f"输出库统计： books {cleaned_books} | data {cleaned_data}")
        emit(f"输入 → 输出： books {source_books} → {cleaned_books} | data {source_data} → {cleaned_data}")

    emit("========== 完成【Step3：回归验证】 ==========")

    # scan_log.write_text("\n".join(business_log) + "\n", encoding="utf-8")
    # logger.info("Step2 scan 业务日志已写入：%s", _display_path(scan_log))


def _display_path(path: Path) -> str:
    """Return a cwd-relative path for logs when possible."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _generate_and_apply_cleaning_sql(
    batch: str,
    incremental_dir: Path,
    output_dir: Path,
    emit: callable,
    metadata_db: Path | None = None,
) -> None:
    """Generate cleaning SQL from confirmed CSVs and apply to produce cleaned.db."""
    from knowledge_assets.preprocess.step4_sql import run_step4_sql
    from knowledge_assets.preprocess.step4_apply import run_step4_apply

    emit("-----> 生成自清洗 SQL")
    run_step4_sql(batch=batch, output_dir=output_dir, incremental_dir=incremental_dir, metadata_db=metadata_db)

    emit("-----> 执行自清洗 SQL 生成 cleaned.db")
    run_step4_apply(batch=batch, incremental_dir=incremental_dir, output_dir=output_dir, metadata_db=metadata_db)


def _load_book_files(batch: str, metadata_db: Path, incremental_dir: Path) -> list[BookFile]:
    with sqlite3.connect(metadata_db) as conn:
        conn.row_factory = sqlite3.Row
        query = _book_files_query_with_custom_column(conn)
        rows = conn.execute(query).fetchall()

    files: list[BookFile] = []
    for row in rows:
        ext = str(row["format"] or "").strip().lower()
        file_basename = str(row["file_basename"] or "").strip()
        relative_path = str(Path(row["book_path"]) / f"{file_basename}.{ext}")
        absolute_path = incremental_dir / relative_path
        file_exists = absolute_path.exists()
        actual_bytes = absolute_path.stat().st_size if file_exists else None
        files.append(
            BookFile(
                batch=batch,
                book_id=int(row["book_id"]),
                data_id=int(row["data_id"]),
                title=str(row["title"] or ""),
                file_basename=file_basename,
                ext=ext,
                bytes=int(row["bytes"]) if row["bytes"] is not None else None,
                relative_path=relative_path,
                file_exists=file_exists,
                actual_bytes=actual_bytes,
                custom_column_1_value=str(row["custom_column_1_value"] or "") if row["custom_column_1_value"] is not None else "",
            )
        )
    return files


def _load_step2_repair_handoffs(step2_dir: Path, config: dict[str, object]) -> Step2RepairHandoffs:
    repair_config = config.get("step2", {}).get("repair", {})
    if not isinstance(repair_config, dict):
        raise ValueError("pipeline_config.json 的 step2.repair 必须是对象")
    title_path = step2_dir / str(
        repair_config.get("title_copy_repair_filename", "step2_2_title_copy_repair.csv")
    )
    basename_path = step2_dir / str(
        repair_config.get("basename_repair_filename", "step2_3_basename_repair.csv")
    )
    return Step2RepairHandoffs(
        title_by_book=_index_step2_repair_handoff(read_csv_dicts(title_path), "book_id"),
        basename_by_data=_index_step2_repair_handoff(read_csv_dicts(basename_path), "data_id"),
        has_title_report=title_path.exists(),
        has_basename_report=basename_path.exists(),
    )


def _index_step2_repair_handoff(rows: Iterable[dict[str, str]], key: str) -> dict[int, dict[str, str]]:
    indexed: dict[int, dict[str, str]] = {}
    for row in rows:
        try:
            indexed[int(row[key])] = row
        except (KeyError, TypeError, ValueError):
            continue
    return indexed


_BOOK_FILES_SELECT = (
    "SELECT books.id AS book_id, books.title AS title, books.path AS book_path, "
    "data.id AS data_id, data.format AS format, data.name AS file_basename, "
    "data.uncompressed_size AS bytes"
)


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _custom_column_join(conn: sqlite3.Connection) -> tuple[str, str]:
    """Return the (LEFT JOIN clause, value expression) pair for custom_column_1.

    Calibre 按自定义列类型生成两种表结构，需要分别适配：
    - 单值列：custom_column_1(id, book, value)，可直接按 book 关联；
    - normalized 多值列：custom_column_1(id, value, link) 配合关联表
      books_custom_column_1_link(book, value)，必须经关联表关联并按 book 聚合，
      否则同一条 data 记录会因多个值被放大成多行。
    """
    columns = _table_columns(conn, "custom_column_1")
    if not columns:
        return "", "NULL"
    if "book" in columns:
        return (
            "LEFT JOIN custom_column_1 ON custom_column_1.book = books.id",
            "custom_column_1.value",
        )
    if _table_exists(conn, "books_custom_column_1_link"):
        return (
            "LEFT JOIN ("
            "SELECT link.book AS book, GROUP_CONCAT(cc.value, '; ') AS value "
            "FROM books_custom_column_1_link AS link "
            "JOIN custom_column_1 AS cc ON cc.id = link.value "
            "GROUP BY link.book"
            ") AS cc_agg ON cc_agg.book = books.id",
            "cc_agg.value",
        )
    get_logger("preprocess.step3").warning(
        "custom_column_1 表结构无法识别（实际列： %s），本次扫描 custom_column_1_value 将全部为空",
        ", ".join(sorted(columns)),
    )
    return "", "NULL"


def _book_files_query_with_custom_column(conn: sqlite3.Connection) -> str:
    """Return the book_files_query with an optional LEFT JOIN to custom_column_1."""
    join_clause, value_expr = _custom_column_join(conn)
    query = RULES.book_files_query.replace("FROM books", f"FROM books {join_clause}")
    query = query.replace(
        _BOOK_FILES_SELECT,
        f"{_BOOK_FILES_SELECT}, {value_expr} AS custom_column_1_value",
    )
    if "custom_column_1_value" not in query:
        raise ValueError(
            "无法在扫描 SQL 中注入 custom_column_1_value："
            "template/step1_rules.json 的 book_files_query SELECT 段已变更，"
            f"需同步 {Path(__file__).name} 中的 _BOOK_FILES_SELECT 常量"
        )
    return query


def _count_books(metadata_db: Path) -> int:
    with sqlite3.connect(metadata_db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM books").fetchone()[0])


def _count_physical_files(incremental_dir: Path) -> int:
    return sum(1 for path in incremental_dir.rglob("*") if path.is_file() and path.name != "metadata.db")


def _build_ordered_outputs(
    files: list[BookFile], repair_handoffs: Step2RepairHandoffs | None = None
) -> tuple[list[Candidate], list[ManualReview]]:
    auto_candidates: list[Candidate] = []
    manual_reviews: list[ManualReview] = []
    emitted_data_ids: set[int] = set()
    handoffs = repair_handoffs or Step2RepairHandoffs({}, {}, False, False)

    def collect_manual_reviews(reason_code: str) -> None:
        reviews = _build_manual_reviews(files, emitted_data_ids, reason_code, handoffs)
        manual_reviews.extend(reviews)
        emitted_data_ids.update(review.data_id for review in reviews)

    junk_title = _build_junk_title_candidates(files, emitted_data_ids)
    auto_candidates.extend(junk_title)
    emitted_data_ids.update(candidate.data_id for candidate in junk_title)

    # title 乱码意味着「元数据本身不可信」，必须先于重复判定分流：
    # 乱码记录的 title 恰好一致时会被 duplicate_title_ext_bytes 当作正常重复项
    # 直接判定去留，人工确认清单就此丢失这批记录。
    collect_manual_reviews(MOJIBAKE_REASON_CODE)

    duplicate_comparison = _build_duplicate_comparison_candidates(files, emitted_data_ids)
    auto_candidates.extend(duplicate_comparison)
    emitted_data_ids.update(candidate.data_id for candidate in duplicate_comparison)

    non_target_format = _build_non_target_format_candidates(files, emitted_data_ids)
    auto_candidates.extend(non_target_format)
    emitted_data_ids.update(candidate.data_id for candidate in non_target_format)

    for reason_code, _filename in RULES.manual_review_outputs:
        if reason_code == MOJIBAKE_REASON_CODE:
            continue
        collect_manual_reviews(reason_code)

    return (
        sorted(auto_candidates, key=lambda item: _output_sort_key(item.reason_code, item.book_id, item.data_id)),
        sorted(manual_reviews, key=lambda item: _output_sort_key(item.reason_code, item.book_id, item.data_id)),
    )


def _output_sort_key(reason_code: str, book_id: int, data_id: int) -> tuple[int, int, int]:
    return (RULES.output_order[reason_code], book_id, data_id)


def _build_non_target_format_candidates(files: Iterable[BookFile], emitted_data_ids: set[int]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for file in files:
        if file.data_id in emitted_data_ids:
            continue
        if file.ext in RULES.target_formats:
            continue
        candidates.append(
            _candidate(
                file,
                "non_target_format",
                "非电子书格式",
                file.ext.upper(),
                RULES.actions["delete"],
            )
        )
    return sorted(candidates, key=lambda item: (item.book_id, item.data_id))


def _build_junk_title_candidates(files: Iterable[BookFile], emitted_data_ids: set[int]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for file in files:
        if file.data_id in emitted_data_ids:
            continue
        for keyword in RULES.junk_title_keywords:
            if keyword in file.title:
                candidates.append(
                    _candidate(
                        file,
                        "title_junk_keyword",
                        "title 命中垃圾关键词",
                        keyword,
                        RULES.actions["delete"],
                    )
                )
                break
    return sorted(candidates, key=lambda item: (item.book_id, item.data_id))


def _build_duplicate_comparison_candidates(files: list[BookFile], emitted_data_ids: set[int]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for group_key, group in _duplicate_groups(files, emitted_data_ids):
        keep = sorted(group, key=_duplicate_keep_key)[0]
        custom_values = _custom_column_value_set(group)
        has_custom_value_conflict = len(custom_values) > 1
        reason_detail = "重复文件（title + ext + bytes）"
        if has_custom_value_conflict:
            reason_detail = f"{reason_detail}；custom_column_1_value 仅供参考： {';'.join(sorted(custom_values))}"
        for file in sorted(group, key=lambda item: (item.book_id, item.data_id)):
            action = RULES.actions["keep"] if file.data_id == keep.data_id else RULES.actions["delete"]
            candidates.append(
                _candidate(
                    file,
                    "duplicate_title_ext_bytes",
                    reason_detail,
                    group_key,
                    action,
                )
            )
    return sorted(candidates, key=lambda item: (item.matched_value, item.recommended_action != RULES.actions["keep"], item.book_id, item.data_id))


def _build_manual_reviews(
    files: Iterable[BookFile],
    emitted_data_ids: set[int],
    target_reason_code: str,
    repair_handoffs: Step2RepairHandoffs,
) -> list[ManualReview]:
    reviews: dict[str, ManualReview] = {}
    for file in files:
        if file.data_id in emitted_data_ids:
            continue
        for reason_code, reason_detail, matched_value in _manual_hits(file):
            if reason_code != target_reason_code:
                continue
            review_id = _stable_id(file.batch, file.book_id, file.data_id, reason_code, matched_value)
            title_handoff = _step2_repair_handoff_values(
                repair_handoffs.title_by_book.get(file.book_id),
                repair_handoffs.has_title_report,
                "title_candidate",
            )
            basename_handoff = _step2_repair_handoff_values(
                repair_handoffs.basename_by_data.get(file.data_id),
                repair_handoffs.has_basename_report,
                "file_basename_candidate",
            )
            reviews[review_id] = ManualReview(
                review_id=review_id,
                batch=file.batch,
                book_id=file.book_id,
                data_id=file.data_id,
                title=file.title,
                file_basename=file.file_basename,
                ext=file.ext,
                bytes=file.bytes,
                relative_path=file.relative_path,
                reason_code=reason_code,
                reason_detail=reason_detail,
                matched_value=matched_value,
                custom_column_1_value=file.custom_column_1_value,
                step2_repair_title_repair_status=title_handoff[0] if reason_code == "title_copy" else "",
                step2_repair_title_repair_reason=title_handoff[1] if reason_code == "title_copy" else "",
                step2_repair_title_repair_note=title_handoff[2] if reason_code == "title_copy" else "",
                step2_repair_title_candidate=title_handoff[3] if reason_code == "title_copy" else "",
                step2_repair_basename_repair_status=(
                    basename_handoff[0] if reason_code == "file_basename_unknown" else ""
                ),
                step2_repair_basename_repair_reason=(
                    basename_handoff[1] if reason_code == "file_basename_unknown" else ""
                ),
                step2_repair_basename_repair_note=(
                    basename_handoff[2] if reason_code == "file_basename_unknown" else ""
                ),
                step2_repair_basename_candidate=(
                    basename_handoff[3] if reason_code == "file_basename_unknown" else ""
                ),
                # 乱码若含 U+FFFD，字节已永久丢失、无法自动还原；此处给出「可还原
                # 部分」的提示（缺口以 ◻ 标注），降低人工核对成本。绝不据此改库。
                partial_hint=(
                    encoding.partial_hint(file.title)
                    if reason_code == MOJIBAKE_REASON_CODE
                    else ""
                ),
            )
    return sorted(reviews.values(), key=lambda item: (item.book_id, item.data_id, item.reason_code))


def _step2_repair_handoff_values(
    handoff: dict[str, str] | None, has_report: bool, candidate_field: str
) -> tuple[str, str, str, str]:
    if handoff is None:
        return ("not_reported" if has_report else "step2_repair_not_run", "", "", "")
    return (
        handoff.get("status", ""),
        handoff.get("reason_code", ""),
        handoff.get("reason_detail", ""),
        handoff.get(candidate_field, ""),
    )


def _manual_hits(file: BookFile) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    if _is_mojibake(file.title):
        hits.append((MOJIBAKE_REASON_CODE, "title 字段疑似乱码", file.title))
    if "副本" in file.title or "Fu Ben" in file.title:
        hits.append(("title_copy", "title 字段出现副本标记", file.title))
    for marker in RULES.unknown_basename_markers:
        if marker in file.file_basename:
            hits.append(("file_basename_unknown", "file_basename 未正确识别", marker))
            break
    return hits


def _is_mojibake(value: str) -> bool:
    """title 是否为编码错误产物。

    判定委托给 :mod:`knowledge_assets.preprocess.encoding` 的「编码探针」：
    只有能还原出合法中文的字符串才判为乱码，因此日文假名标题、含装饰符号的
    正常标题不会被误判——这是旧的「字符黑名单 + 异常占比」判据做不到的。
    """
    return encoding.is_mojibake(value)


def _candidate(
    file: BookFile,
    reason_code: str,
    reason_detail: str,
    matched_value: str,
    recommended_action: str,
) -> Candidate:
    candidate_id = _stable_id(file.batch, file.book_id, file.data_id, reason_code, matched_value)
    return Candidate(
        candidate_id=candidate_id,
        batch=file.batch,
        book_id=file.book_id,
        data_id=file.data_id,
        title=file.title,
        file_basename=file.file_basename,
        ext=file.ext,
        bytes=file.bytes,
        relative_path=file.relative_path,
        reason_code=reason_code,
        reason_detail=reason_detail,
        matched_value=matched_value,
        recommended_action=recommended_action,
        custom_column_1_value=file.custom_column_1_value,
    )


def _duplicate_groups(files: list[BookFile], emitted_data_ids: set[int]) -> list[tuple[str, list[BookFile]]]:
    groups: dict[str, list[BookFile]] = defaultdict(list)
    for file in files:
        if file.data_id in emitted_data_ids:
            continue
        size = file.bytes if file.bytes is not None else file.actual_bytes
        if size is None:
            continue
        groups[_duplicate_key(file)].append(file)

    return sorted(
        ((group_key, group) for group_key, group in groups.items() if len(group) > 1),
        key=lambda item: item[0],
    )


def _custom_column_value_set(files: Iterable[BookFile]) -> set[str]:
    return {str(file.custom_column_1_value or "").strip() for file in files}


def _duplicate_key(file: BookFile) -> str:
    size = file.bytes if file.bytes is not None else file.actual_bytes
    return f"{normalize_title(file.title)}|{file.ext.lower()}|{size}"


def _duplicate_keep_key(file: BookFile) -> tuple[int, int, int, int]:
    unknown_basename = any(marker in file.file_basename for marker in RULES.unknown_basename_markers)
    return (0 if file.file_exists else 1, 1 if unknown_basename else 0, len(file.relative_path), file.data_id)


def _group_reviews_by_reason(reviews: Iterable[ManualReview]) -> dict[str, list[ManualReview]]:
    grouped: dict[str, list[ManualReview]] = defaultdict(list)
    for review in reviews:
        grouped[review.reason_code].append(review)
    return grouped


def _group_candidates_by_reason(candidates: Iterable[Candidate]) -> dict[str, list[Candidate]]:
    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.reason_code].append(candidate)
    return grouped


def _emit_manual_summary(emit, grouped: dict[str, list[ManualReview]]) -> None:
    for reason_code, label in RULES.manual_summary_outputs:
        records = grouped.get(reason_code, [])
        groups = len({record.book_id for record in records})
        emit(f"{label}：共 {groups} 组 | 待确认文件 {len(records)} 个")


def _emit_auto_summary(emit, grouped: dict[str, list[Candidate]]) -> None:
    for reason_code, label, summary_type in RULES.auto_summary_outputs:
        records = grouped.get(reason_code, [])
        if summary_type == "duplicate_comparison":
            duplicate_groups = len({record.matched_value for record in records})
            keep_count = sum(1 for record in records if record.recommended_action == RULES.actions["keep"])
            delete_count = sum(1 for record in records if record.recommended_action == RULES.actions["delete"])
            emit(f"{label}：重复文件 {duplicate_groups} 组 | 对比文件 {len(records)} 个 | 保留 {keep_count} 个 | 待删除 {delete_count} 个")
        elif summary_type == "format_counts":
            ext_counts = Counter(record.ext.upper() for record in records)
            emit(f"{label}：总文件数： {len(records)} | {_format_counter(ext_counts, lower=False)}")
        else:
            raise ValueError(f"未知 Step2 scan 自动摘要类型： {summary_type}")


def _format_counter(counter: Counter[str], lower: bool) -> str:
    if not counter:
        return "无"
    items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    if lower:
        return ", ".join(f"{key.lower()}({count})" for key, count in items)
    return ", ".join(f"{key.upper()}({count})" for key, count in items)


def _write_reason_csvs(
    output_dir: Path,
    grouped_rows: dict[str, list[Candidate]] | dict[str, list[ManualReview]],
    specs: tuple[tuple[str, str], ...],
    row_type: type[Candidate] | type[ManualReview],
) -> list[tuple[str, Path]]:
    written: list[tuple[str, Path]] = []
    for reason_code, filename in specs:
        path = output_dir / filename
        _write_csv(path, grouped_rows.get(reason_code, []), row_type)
        written.append((reason_code, path))
    return written


def _write_csv(
    path: Path,
    rows: Iterable[Candidate | ManualReview],
    row_type: type[Candidate] | type[ManualReview],
) -> None:
    rows = list(rows)
    fieldnames = [field.name for field in fields(row_type)]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _write_snapshot(
    path: Path,
    files: list[BookFile],
    auto_candidates: list[Candidate],
    manual_reviews: list[ManualReview],
    ext_counts: Counter[str],
) -> None:
    payload = {
        "file_total": len(files),
        "book_file_missing_total": sum(1 for file in files if not file.file_exists),
        "format_counts": dict(sorted(ext_counts.items())),
        "auto_delete_candidates_total": len(auto_candidates),
        "manual_review_total": len(manual_reviews),
        "auto_delete_reason_counts": dict(Counter(item.reason_code for item in auto_candidates)),
        "manual_review_reason_counts": dict(Counter(item.reason_code for item in manual_reviews)),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _remove_deprecated_outputs(output_dir: Path, logger: logging.Logger | None = None) -> None:
    """Remove all CSV files in the output directory before regenerating."""
    removed: list[str] = []
    for path in sorted(output_dir.glob("*.csv")):
        path.unlink()
        removed.append(path.name)
    if removed:
        msg = f"清理所有旧 CSV 文件： %s 个 |  %s"
        (logger or logging.getLogger(__name__)).info(msg, len(removed), "、".join(removed))


def _stable_id(batch: str, book_id: int, data_id: int, reason_code: str, matched_value: str) -> str:
    raw = f"{batch}|{book_id}|{data_id}|{reason_code}|{matched_value}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _extract_path_prefix(path: str, book_id: int) -> str | None:
    suffix = f" ({book_id})"
    if path.endswith(suffix):
        return path[: -len(suffix)]
    return None


def _load_books_for_merge(
    metadata_db: Path,
) -> tuple[dict[int, dict], dict[int, list[dict]]]:
    with sqlite3.connect(metadata_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT b.id AS book_id, b.title, b.path, "
            "d.id AS data_id, d.format "
            "FROM books b "
            "LEFT JOIN data d ON d.book = b.id "
            "ORDER BY b.id, d.id"
        ).fetchall()

    books: dict[int, dict] = {}
    data_by_book: dict[int, list[dict]] = {}
    for row in rows:
        bid = int(row["book_id"])
        if bid not in books:
            books[bid] = {
                "book_id": bid,
                "title": str(row["title"] or ""),
                "path": str(row["path"] or ""),
            }
            data_by_book[bid] = []
        data_id = row["data_id"]
        if data_id is not None:
            data_by_book[bid].append(
                {"data_id": int(data_id), "format": str(row["format"] or "").upper()}
            )
    return books, data_by_book


def _scan_cross_bookid_merge(
    metadata_db: Path, emit: callable
) -> tuple[list[MergePlanRow], list[MergePlanRow]]:
    emit("-----> 扫描跨 book_id 同书合并")
    books, data_by_book = _load_books_for_merge(metadata_db)

    groups: dict[str, list[int]] = {}
    for bid, book in books.items():
        prefix = _extract_path_prefix(book["path"], bid)
        if prefix is None:
            continue
        groups.setdefault(prefix, []).append(bid)

    multi_groups = {k: v for k, v in groups.items() if len(v) > 1}
    auto_groups = {k: v for k, v in multi_groups.items() if not k.startswith("未知/")}
    unknown_groups = {k: v for k, v in multi_groups.items() if k.startswith("未知/")}

    auto_plan: list[MergePlanRow] = []
    group_num = 0
    for prefix in sorted(auto_groups):
        group_num += 1
        book_ids = auto_groups[prefix]
        winner_id = max(book_ids, key=lambda bid: (len(data_by_book[bid]), -bid))
        winner_formats = sorted({d["format"] for d in data_by_book[winner_id]})
        winner = books[winner_id]
        winner_fmt_set = set(winner_formats)

        for loser_id in sorted(bid for bid in book_ids if bid != winner_id):
            loser = books[loser_id]
            loser_data = data_by_book[loser_id]
            loser_fmts = sorted({d["format"] for d in loser_data})
            moved: list[int] = []
            dupes: list[int] = []
            for d in sorted(loser_data, key=lambda d: d["data_id"]):
                fmt = d["format"]
                if fmt and fmt not in winner_fmt_set:
                    moved.append(d["data_id"])
                    winner_fmt_set.add(fmt)
                else:
                    dupes.append(d["data_id"])
            auto_plan.append(
                MergePlanRow(
                    merge_group=f"G{group_num:04d}",
                    path_prefix=prefix,
                    winner_book_id=winner_id,
                    winner_path=winner["path"],
                    winner_format_count=len(winner_formats),
                    loser_book_id=loser_id,
                    loser_path=loser["path"],
                    loser_formats=",".join(loser_fmts),
                    moved_data_ids=",".join(str(x) for x in moved),
                    duplicate_data_ids=",".join(str(x) for x in dupes),
                )
            )

    unknown_plan: list[MergePlanRow] = []
    unknown_group_num = 0
    for prefix in sorted(unknown_groups):
        unknown_group_num += 1
        for bid in sorted(unknown_groups[prefix]):
            book = books[bid]
            fmts = sorted({d["format"] for d in data_by_book[bid]})
            unknown_plan.append(
                MergePlanRow(
                    merge_group=f"U{unknown_group_num:04d}",
                    path_prefix=prefix,
                    winner_book_id=0,
                    winner_path="",
                    winner_format_count=0,
                    loser_book_id=bid,
                    loser_path=book["path"],
                    loser_formats=",".join(fmts),
                    moved_data_ids="",
                    duplicate_data_ids="",
                )
            )

    auto_losers = len(auto_plan)
    auto_groups_count = len(auto_groups)
    unknown_books = len(unknown_plan)
    emit(
        f"跨book_id合并扫描[path_prefix]： auto {auto_groups_count} 组 / {auto_losers} loser | "
        f"未知/ {len(unknown_groups)} 组 / {unknown_books} book"
    )
    return auto_plan, unknown_plan


def _scan_title_author_merge(
    metadata_db: Path, emit: callable
) -> list[MergePlanRow]:
    """Scan for title+author duplicates that path_prefix matching missed.

    Some books share the same title and author but were imported at different
    times, resulting in different Calibre paths.  This second pass catches
    them by grouping on ``normalize_title(title) + author_sort``.
    """
    emit("-----> 扫描跨 book_id 同书合并[title+author]")
    books, data_by_book = _load_books_for_merge(metadata_db)

    with sqlite3.connect(metadata_db) as conn:
        conn.row_factory = sqlite3.Row
        author_rows = conn.execute(
            "SELECT b.id AS book_id, b.author_sort "
            "FROM books b"
        ).fetchall()
    author_map: dict[int, str] = {
        int(r["book_id"]): str(r["author_sort"] or "").strip().lower()
        for r in author_rows
    }

    groups: dict[tuple[str, str], list[int]] = {}
    for bid, book in books.items():
        title_key = normalize_title(book["title"])
        author_key = author_map.get(bid, "")
        if not title_key:
            continue
        groups.setdefault((title_key, author_key), []).append(bid)

    multi = {k: v for k, v in groups.items() if len(v) > 1}

    plan: list[MergePlanRow] = []
    group_num = 0
    for (title_key, author_key) in sorted(multi):
        group_num += 1
        book_ids = multi[(title_key, author_key)]
        winner_id = max(book_ids, key=lambda bid: (len(data_by_book[bid]), -bid))
        winner = books[winner_id]
        winner_formats = sorted({d["format"] for d in data_by_book[winner_id]})
        winner_fmt_set = set(winner_formats)

        for loser_id in sorted(bid for bid in book_ids if bid != winner_id):
            loser = books[loser_id]
            loser_data = data_by_book[loser_id]
            loser_fmts = sorted({d["format"] for d in loser_data})
            moved: list[int] = []
            dupes: list[int] = []
            for d in sorted(loser_data, key=lambda d: d["data_id"]):
                fmt = d["format"]
                if fmt and fmt not in winner_fmt_set:
                    moved.append(d["data_id"])
                    winner_fmt_set.add(fmt)
                else:
                    dupes.append(d["data_id"])
            plan.append(
                MergePlanRow(
                    merge_group=f"TA{group_num:04d}",
                    path_prefix=title_key,
                    winner_book_id=winner_id,
                    winner_path=winner["path"],
                    winner_format_count=len(winner_formats),
                    loser_book_id=loser_id,
                    loser_path=loser["path"],
                    loser_formats=",".join(loser_fmts),
                    moved_data_ids=",".join(str(x) for x in moved),
                    duplicate_data_ids=",".join(str(x) for x in dupes),
                    merge_type="title_author",
                )
            )

    total_groups = len(multi)
    total_losers = len(plan)
    total_dup_data = sum(
        len(p.duplicate_data_ids.split(",")) for p in plan if p.duplicate_data_ids
    )
    emit(
        f"跨book_id合并扫描[title+author]： {total_groups} 组 / "
        f"{total_losers} loser / {total_dup_data} 重复 data"
    )
    return plan


def _link_tables_with_book_column(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT m.name FROM sqlite_master m "
            "JOIN pragma_table_info(m.name) i "
            "WHERE m.type='table' AND i.name='book' "
            "GROUP BY m.name"
        ).fetchall()
    ]


def _apply_cross_bookid_merge(
    metadata_db: Path,
    merge_plan: list[MergePlanRow],
    emit: callable,
    library_root: Path | None = None,
) -> None:
    if not merge_plan:
        return
    emit("-----> 应用跨 book_id 合并到数据库")

    plans_by_winner: dict[int, list[MergePlanRow]] = {}
    for row in merge_plan:
        plans_by_winner.setdefault(row.winner_book_id, []).append(row)

    merge_groups = len(plans_by_winner)
    total_losers = len(merge_plan)
    total_moved = 0
    total_deleted = 0

    # ---- 收集物理文件复制操作（将 moved 文件从 loser 目录复制到 winner 目录） ----
    file_ops: list[tuple[Path, Path]] = []  # (src, dst)
    if library_root is not None:
        with sqlite3.connect(f"file:{metadata_db}?mode=ro", uri=True) as fconn:
            fconn.row_factory = sqlite3.Row
            for plan in merge_plan:
                moved_ids = [int(x) for x in plan.moved_data_ids.split(",") if x]
                if moved_ids:
                    ph = ",".join("?" * len(moved_ids))
                    winner_dir = library_root / fconn.execute(
                        "SELECT path FROM books WHERE id = ?", (plan.winner_book_id,)
                    ).fetchone()[0]
                    for r in fconn.execute(
                        f"SELECT d.name, d.format, b.path FROM data d "
                        f"JOIN books b ON b.id = d.book WHERE d.id IN ({ph})",
                        moved_ids,
                    ).fetchall():
                        fname = f"{r['name']}.{r['format']}"
                        src = library_root / r["path"] / fname
                        dst = winner_dir / fname
                        if src != dst:
                            file_ops.append((src, dst))

    with sqlite3.connect(metadata_db, isolation_level=None) as conn:
        link_tables = _link_tables_with_book_column(conn)
        conn.execute("BEGIN")
        for winner_id, plans in plans_by_winner.items():
            loser_ids = [p.loser_book_id for p in plans]
            for plan in plans:
                moved_ids = [
                    int(x) for x in plan.moved_data_ids.split(",") if x
                ]
                dupe_ids = [
                    int(x) for x in plan.duplicate_data_ids.split(",") if x
                ]
                for data_id in moved_ids:
                    conn.execute(
                        "UPDATE data SET book = ? WHERE id = ?",
                        (winner_id, data_id),
                    )
                if dupe_ids:
                    conn.executemany(
                        "DELETE FROM data WHERE id = ?",
                        [(d,) for d in dupe_ids],
                    )
                total_moved += len(moved_ids)
                total_deleted += len(dupe_ids)
            for table in link_tables:
                if table == "data":
                    continue
                conn.executemany(
                    f"DELETE FROM {table} WHERE book = ?",
                    [(lid,) for lid in loser_ids],
                )
            conn.executemany(
                "DELETE FROM books WHERE id = ?",
                [(lid,) for lid in loser_ids],
            )
        conn.execute("COMMIT")

    # ---- 物理文件操作：复制 moved 文件到 winner 目录 ----
    file_ops_ok = 0
    file_ops_skip = 0
    if library_root is not None:
        for src, dst in file_ops:
            try:
                if src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dst))
                    file_ops_ok += 1
                else:
                    file_ops_skip += 1
            except OSError:
                file_ops_skip += 1

    emit(
        f"合并完成： {merge_groups} 组 | {total_losers} loser | "
        f"移动 data {total_moved} | 删除 data {total_deleted}"
        + (f" | 文件操作 {file_ops_ok} 成功 / {file_ops_skip} 跳过" if library_root else "")
    )


def _write_merge_deletion_csv(
    input_db: Path,
    merged_db: Path,
    output_path: Path,
    emit: callable,
) -> None:
    """Write a Candidate-format CSV for ALL data records belonging to
    loser books.  Includes both deleted duplicates and data that was
    moved to the winner, so Step4/BAT quarantines every loser dir."""
    with sqlite3.connect(f"file:{input_db}?mode=ro", uri=True) as conn:
        input_data_ids = {
            int(r[0]) for r in conn.execute("SELECT id FROM data").fetchall()
        }
        input_book_ids = {
            int(r[0]) for r in conn.execute("SELECT id FROM books").fetchall()
        }

    with sqlite3.connect(f"file:{merged_db}?mode=ro", uri=True) as conn:
        merged_data_ids = {
            int(r[0]) for r in conn.execute("SELECT id FROM data").fetchall()
        }
        merged_book_ids = {
            int(r[0]) for r in conn.execute("SELECT id FROM books").fetchall()
        }

    deleted_data_ids = input_data_ids - merged_data_ids
    loser_book_ids = input_book_ids - merged_book_ids
    loser_data_ids: set[int] = set()
    if loser_book_ids:
        with sqlite3.connect(f"file:{input_db}?mode=ro", uri=True) as conn:
            ph = ",".join("?" * len(loser_book_ids))
            loser_data_ids = {
                int(r[0])
                for r in conn.execute(
                    f"SELECT id FROM data WHERE book IN ({ph})",
                    list(loser_book_ids),
                ).fetchall()
            }
    combined_ids = deleted_data_ids | loser_data_ids
    if not combined_ids:
        emit("合并删除数据：无（合并未删除或移动任何 data 记录）")
        return
    with sqlite3.connect(f"file:{input_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        ph = ",".join("?" * len(combined_ids))
        rows = conn.execute(
            f"SELECT d.id AS data_id, d.book AS book_id, d.format, "
            f"d.name AS file_basename, b.path AS book_path, b.title "
            f"FROM data d JOIN books b ON b.id = d.book "
            f"WHERE d.id IN ({ph})",
            list(combined_ids),
        ).fetchall()
    records: list[Candidate] = []
    for r in rows:
        data_id = int(r["data_id"])
        if data_id in deleted_data_ids:
            reason = "合并阶段删除的重复 data"
            action = "delete_file"
        else:
            reason = "合并阶段移至 winner 的 data（loser 目录需隔离）"
            action = "quarantine_file"
        ext = str(r["format"] or "").strip().lower()
        file_basename = str(r["file_basename"] or "").strip()
        book_path = str(r["book_path"] or "")
        relative_path = str(Path(book_path) / f"{file_basename}.{ext}")
        book_id = int(r["book_id"])
        cand = Candidate(
            candidate_id=_stable_id(
                "merge", book_id, data_id, "merge_deletion", str(data_id)
            ),
            batch="merge",
            book_id=book_id,
            data_id=data_id,
            title=str(r["title"] or ""),
            file_basename=file_basename,
            ext=ext,
            bytes=None,
            relative_path=relative_path,
            reason_code="merge_deletion",
            reason_detail=reason,
            matched_value=str(data_id),
            recommended_action=action,
        )
        records.append(cand)
    _write_csv(output_path, records, Candidate)
    emit(
        f"合并删除数据：{len(combined_ids)} 条 data "
        f"（删除 {len(deleted_data_ids)} + 移动 {len(loser_data_ids)}）"
        f" -> {_display_path(output_path)}"
    )


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Step2-2 scan：扫描增量预处理库【重复文件（title + ext + bytes）+「title」字段出现乱码 +「file_basename」未正确识别（「Wei Zhi」、「Unknown」、「Untitled」）】"
    )
    batch = default_batch()
    parser.add_argument("--batch", default=batch)
    parser.add_argument("--incremental-dir", type=Path, default=default_input_dir())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=default_log_dir())
    args = parser.parse_args()

    setup_logger(log_dir=str(args.log_dir))
    run_step3_regression(
        batch=args.batch,
        incremental_dir=args.incremental_dir,
        output_dir=args.output_dir or default_output_dir(args.batch),
    )


if __name__ == "__main__":
    _main()
