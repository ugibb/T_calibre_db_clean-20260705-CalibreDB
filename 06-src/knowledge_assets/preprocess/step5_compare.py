"""Step5: sync the cleaned incremental Calibre library to a full library."""

from __future__ import annotations

import argparse
import filecmp
import json
import re
import shutil
import sqlite3
import sys
import uuid
from collections import Counter
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[2]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from knowledge_assets.preprocess.common import (
    default_batch,
    default_full_db_dir,
    default_input_dir,
    default_log_dir,
    default_output_dir,
    display_path,
    latest_incremental_db,
    load_json_config,
    pipeline_config,
    read_csv_dicts,
    resolve_incremental_entity_dir,
    step_dir,
    write_dict_csv,
)
from knowledge_assets.utils.logger import get_logger, setup_logger

_SQL_PATH = _SRC_DIR / "template" / "step4_sql.json"
with _SQL_PATH.open(encoding="utf-8") as _handle:
    SQL = json.load(_handle)


REPORT_FIELDS = [
    "book_id",
    "target_book_id",
    "data_id",
    "target_data_id",
    "title",
    "ext",
    "bytes",
    "relative_path",
    "book_status",
    "sync_status",
    "verification_status",
    "metadata_status",
    "metadata_details",
    "error_message",
]

SCAN_FIELDS = [
    "sync_id",
    "batch",
    "reason_code",
    "compare_side",
    "source_book_id",
    "source_data_id",
    "target_book_id",
    "target_data_id",
    "book_id",
    "data_id",
    "title",
    "author",
    "book_path",
    "file_basename",
    "ext",
    "bytes",
    "relative_path",
    "authors",
    "tags",
    "publisher",
    "languages",
    "rating",
    "series",
    "identifiers",
    "comments_len",
    "metadata",
    "books_record",
    "data_record",
    "recommended_action",
    "human_decision",
    "human_note",
]

METADATA_COMPARE_FIELDS = [
    "batch",
    "metadata_scope",
    "metadata_key",
    "type",
    "pair_key",
    "compare_status",
    "compare_side",
    "record_id",
    "field_value",
    "record_json",
    "recommended_action",
    "detail",
]

MERGE_REPORT_FIELDS = [
    "sync_id",
    "source_book_id",
    "source_data_id",
    "target_book_id",
    "target_data_id",
    "recommended_action",
    "execute_status",
    "verification_status",
    "metadata_status",
    "metadata_details",
    "error_message",
]

MANUAL_CHECK_FIELDS = [
    "source_file",
    "sync_id",
    "source_book_id",
    "source_data_id",
    "recommended_action",
    "human_decision",
    "check_status",
    "message",
]

STEP5_CONFIG = load_json_config("step4_config.json")
METADATA_CONFLICT_POLICIES = tuple(STEP5_CONFIG["metadata_conflict"]["valid"])
DEPRECATED_SCAN_OUTPUT_FILENAMES = (
    "3-2-2：待人工确认-元数据冲突.csv",
    "5-3-9：元数据比对-metadata_compare.csv",
)
STEP5_NOOP_ACTIONS = {"keep_target", "report_only"}
STEP5_EXECUTABLE_ACTIONS = set(STEP5_CONFIG["actions"]["valid"]) - STEP5_NOOP_ACTIONS


def run_step5(
    batch: str,
    incremental_dir: Path,
    full_library_dir: Path,
    output_dir: Path,
    force: bool = False,
    metadata_conflict: str | None = None,
) -> None:
    """Backward-compatible Step5 entrypoint.

    The Step5 flow is scan -> human confirmation -> plan -> apply, so the
    legacy one-shot command only performs scan to avoid bypassing confirmation.
    """
    run_step5_compare(
        batch=batch,
        incremental_dir=incremental_dir,
        full_library_dir=full_library_dir,
        output_dir=output_dir,
        force=force,
        metadata_conflict=metadata_conflict,
    )


def run_step5_compare(
    batch: str,
    incremental_dir: Path,
    full_library_dir: Path,
    output_dir: Path,
    force: bool = False,
    metadata_conflict: str | None = None,
) -> None:
    metadata_conflict = metadata_conflict or str(STEP5_CONFIG["metadata_conflict"]["default"])
    if metadata_conflict not in METADATA_CONFLICT_POLICIES:
        raise ValueError(f"Unsupported metadata conflict policy: {metadata_conflict}")

    logger = get_logger("preprocess.step5")
    context = _step5_context(batch, incremental_dir, full_library_dir, output_dir)
    step5_dir = context["step5_dir"]
    inc_db = context["inc_db"]
    full_db = context["full_db"]
    full_library_dir = context["full_library_dir"]
    log_path = step5_dir / str(STEP5_CONFIG["outputs"]["log_filename"])
    business_log: list[str] = []

    def emit(message: str) -> None:
        business_log.append(message)
        logger.info(message)

    emit("========== 开始【Step5 compare：比对清洗好的增量预处理库与全量预处理库】 ==========")
    emit(f"读取清洗完成的增量数据库： {display_path(inc_db)}")
    emit(f"本次目标全量预处理库： {display_path(full_db)}")

    blockers = _preflight_blockers(context["output_dir"], inc_db)
    if blockers and not force:
        for blocker in blockers[:20]:
            emit(f"阻塞： {blocker}")
        emit("未执行 Step5 compare scan；如需强制继续请传入 --force")
        log_path.write_text("\n".join(business_log) + "\n", encoding="utf-8")
        return

    full_db_exists = full_db.exists() and _is_usable_full_db(full_db)
    source_full_db = _latest_full_db(full_library_dir, exclude=full_db)
    comparison_full_db = full_db if full_db_exists else source_full_db
    if comparison_full_db is None:
        _remove_generated_step5_scan_outputs(step5_dir)
        emit("未找到可用于比对的历史全量预处理库；Step5 compare 不创建全量库，待 Step5 apply 执行时初始化")
        emit("========== 完成【Step5 compare：比对清洗好的增量预处理库与全量预处理库】 ==========")
        log_path.write_text("\n".join(business_log) + "\n", encoding="utf-8")
        return
    if comparison_full_db != full_db:
        emit(f"本批次目标全量库尚不存在；Step5 compare 仅读取最新历史全量库用于比对： {display_path(comparison_full_db)}")

    if _same_database_file(inc_db, comparison_full_db):
        _remove_generated_step5_scan_outputs(step5_dir)
        emit("清洗增量库与用于比对的全量库内容一致，无需生成 Step5 compare 比对 CSV")
        emit("========== 完成【Step5 compare：比对清洗好的增量预处理库与全量预处理库】 ==========")
        log_path.write_text("\n".join(business_log) + "\n", encoding="utf-8")
        return

    buckets = _empty_scan_buckets()
    buckets = _scan_against_full_db(batch, inc_db, comparison_full_db, full_library_dir, metadata_conflict)

    paths = _scan_output_paths(step5_dir)
    _remove_deprecated_scan_outputs(step5_dir)
    for key, path in paths.items():
        rows = _preserve_human_fields(path, _expand_comparison_rows(buckets[key]))
        write_dict_csv(path, rows, SCAN_FIELDS)
        emit(f" {path.name} | {_step5_scan_output_summary(buckets[key])}")
    for path, rows in _write_metadata_compare_outputs(step5_dir, buckets, batch, inc_db, full_db):
        summary = _step5_scan_output_summary(rows, source_side_only=True, compare_only=True)
        emit(f" {path.name} | {summary}")
    emit(f"Step5 compare scan 统计： {_bucket_counts(buckets)}，待执行SQL {_pending_sql_count_for_buckets(buckets)}条")
    emit("========== 完成【Step5 compare：比对清洗好的增量预处理库与全量预处理库】 ==========")
    log_path.write_text("\n".join(business_log) + "\n", encoding="utf-8")


def run_step5_plan(batch: str, output_dir: Path) -> None:
    logger = get_logger("preprocess.step5")
    output_dir = _resolve_step5_output_dir(batch, output_dir)
    step5_dir = step_dir(output_dir, "step5")
    step5_dir.mkdir(parents=True, exist_ok=True)
    _ensure_scan_outputs_exist(step5_dir)

    business_log: list[str] = []
    def emit(message: str) -> None:
        business_log.append(message)
        logger.info(message)

    emit("========== 开始【Step5 plan：根据确认 CSV 生成全量同步 SQL】 ==========")
    rows = _load_step5_confirmation_rows(step5_dir)
    sql_path = step5_dir / str(STEP5_CONFIG["outputs"]["merge_sql_filename"])
    pending = _pending_sql_count(rows)
    emit(f"读取 Step5 compare 确认数据： {len(rows)} 条计划，待执行SQL {pending}条")
    lines = [
        "-- Auto-generated by Step5 plan. Review before running step5 apply.",
        f"-- batch: {batch}",
        "-- No intermediate plan table is created.",
        "-- Step5 apply apply writes directly to the existing full-library tables.",
        "BEGIN TRANSACTION;",
    ]
    for row in rows:
        action = _resolved_step5_action(row)
        lines.extend(_step5_merge_sql_notes(row, action))
    lines.append("COMMIT;")
    sql_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    emit(f"输出 Step5 plan 同步 SQL： {display_path(sql_path)} | {len(rows)} 条计划，待执行SQL {pending}条")
    emit("========== 完成【Step5 plan：根据确认 CSV 生成全量同步 SQL】 ==========")


def run_step5_validate_manual(batch: str, output_dir: Path) -> None:
    logger = get_logger("preprocess.step5")
    output_dir = _resolve_step5_output_dir(batch, output_dir)
    step5_dir = step_dir(output_dir, "step5")
    step5_dir.mkdir(parents=True, exist_ok=True)
    _ensure_scan_outputs_exist(step5_dir)
    report_path = step5_dir / str(STEP5_CONFIG["outputs"]["manual_check_report_filename"])
    manual_files = {
        str(value)
        for key, value in STEP5_CONFIG["scan_outputs"].items()
        if key.startswith("manual_")
    }
    valid_actions = set(STEP5_CONFIG["actions"]["valid"])
    reports: list[dict[str, str]] = []
    for path in _scan_output_paths(step5_dir).values():
        if path.name not in manual_files:
            continue
        for row in read_csv_dicts(path):
            if row.get("compare_side", "incremental") != "incremental":
                continue
            decision = (row.get("human_decision") or "").strip()
            if not decision:
                status = "manual_required"
                message = "human_decision 为空"
            elif decision not in valid_actions:
                status = "invalid_action"
                message = f"human_decision 不在允许动作内： {decision}"
            else:
                status = "ready"
                message = ""
            reports.append(
                {
                    "source_file": path.name,
                    "sync_id": row.get("sync_id", ""),
                    "source_book_id": row.get("source_book_id", ""),
                    "source_data_id": row.get("source_data_id", ""),
                    "recommended_action": row.get("recommended_action", ""),
                    "human_decision": decision,
                    "check_status": status,
                    "message": message,
                }
            )
    write_dict_csv(report_path, reports, MANUAL_CHECK_FIELDS)
    logger.info(
        "输出 Step5 人工确认检查报告：%s | %s",
        display_path(report_path),
        _manual_check_counts(reports),
    )


def run_step5_apply(
    batch: str,
    incremental_dir: Path,
    full_library_dir: Path,
    output_dir: Path,
    force: bool = False,
    metadata_conflict: str | None = None,
) -> None:
    metadata_conflict = metadata_conflict or str(STEP5_CONFIG["metadata_conflict"]["default"])
    if metadata_conflict not in METADATA_CONFLICT_POLICIES:
        raise ValueError(f"Unsupported metadata conflict policy: {metadata_conflict}")

    logger = get_logger("preprocess.step5")
    context = _step5_context(batch, incremental_dir, full_library_dir, output_dir)
    step5_dir = context["step5_dir"]
    step5_dir = context["step5_dir"]
    inc_db = context["inc_db"]
    full_db = context["full_db"]
    incremental_entity_dir = context["incremental_entity_dir"]
    full_library_dir = context["full_library_dir"]
    sql_path = step5_dir / str(STEP5_CONFIG["outputs"]["merge_sql_filename"])
    report_path = step5_dir / str(STEP5_CONFIG["outputs"]["merge_report_filename"])
    log_path = step5_dir / str(STEP5_CONFIG["outputs"]["log_filename"])
    business_log: list[str] = []

    def emit(message: str) -> None:
        business_log.append(message)
        logger.info(message)

    emit("========== 开始【Step5 apply：执行同步 SQL 并更新全量预处理库】 ==========")
    emit(f"读取 Step5 plan 同步 SQL： {display_path(sql_path)}")
    if not full_db.exists() or not _is_usable_full_db(full_db):
        source_full_db = _latest_full_db(full_library_dir, exclude=full_db)
        if source_full_db is not None:
            shutil.copy2(source_full_db, full_db)
            emit(f"已将最新全量预处理库复制为本批次目标库： {display_path(source_full_db)} -> {display_path(full_db)}")
        else:
            reports = _initialize_full_library(inc_db, incremental_entity_dir, full_db, full_library_dir)
            merge_reports = [
                _merge_report("", row["book_id"], row["data_id"], row["target_book_id"], row["target_data_id"], "init_full_library", row["sync_status"], row["verification_status"], row["metadata_status"], row["metadata_details"], row["error_message"])
                for row in reports
            ]
            write_dict_csv(report_path, merge_reports, MERGE_REPORT_FIELDS)
            emit(f"不存在历史全量库，已初始化全量库： {display_path(full_db)}")
            emit(f"输出 Step5 apply 同步报告： {display_path(report_path)} | {len(merge_reports)} 条")
            log_path.write_text("\n".join(business_log) + "\n", encoding="utf-8")
            return
    if not sql_path.exists():
        raise FileNotFoundError(f"Step5 plan 同步 SQL 不存在： {sql_path}")

    _execute_merge_plan_sql(full_db, sql_path)
    rows = _load_step5_confirmation_rows(step5_dir)
    reports = _apply_confirmation_rows(
        inc_db,
        incremental_entity_dir,
        full_db,
        full_library_dir,
        rows,
        metadata_conflict,
        force,
    )
    write_dict_csv(report_path, reports, MERGE_REPORT_FIELDS)
    emit(f"Step5 apply apply 统计： {_merge_status_counts(reports)}")
    emit(f"输出 Step5 apply 同步报告： {display_path(report_path)} | {_merge_report_summary(reports)}")
    emit("========== 完成【Step5 apply：执行同步 SQL 并更新全量预处理库】 ==========")
    log_path.write_text("\n".join(business_log) + "\n", encoding="utf-8")


def _step5_context(batch: str, incremental_dir: Path, full_library_dir: Path, output_dir: Path) -> dict[str, object]:
    incremental_dir = incremental_dir.resolve()
    full_library_dir = full_library_dir.resolve()
    output_dir = _resolve_step5_output_dir(batch, output_dir)
    step5_dir = step_dir(output_dir, "step5")
    step5_dir.mkdir(parents=True, exist_ok=True)
    full_library_dir.mkdir(parents=True, exist_ok=True)
    inc_db = _resolve_step5_incremental_db(batch, incremental_dir, output_dir)
    return {
        "batch": batch,
        "output_dir": output_dir,
        "step5_dir": step5_dir,
        "inc_db": inc_db,
        "incremental_entity_dir": resolve_incremental_entity_dir(incremental_dir, inc_db),
        "full_library_dir": full_library_dir,
        "full_db": _target_full_db(full_library_dir, inc_db, batch),
    }


def _resolve_step5_output_dir(batch: str, output_dir: Path) -> Path:
    configured_default_batch = default_batch()
    configured_default_output = default_output_dir(configured_default_batch)
    if batch != configured_default_batch and output_dir == configured_default_output:
        return default_output_dir(batch)
    return output_dir


def _empty_scan_buckets() -> dict[str, list[dict[str, str]]]:
    base = {key: [] for key in STEP5_CONFIG["scan_outputs"]}
    # manual_metadata_conflict is not a standalone scan output (replaced by 5-3-*
    # compare CSVs), but the bucket is still used as a data source for
    # _write_metadata_compare_outputs so entries appear in the compare CSVs.
    base.setdefault("manual_metadata_conflict", [])
    return base


def _scan_output_paths(step5_dir: Path) -> dict[str, Path]:
    return {
        key: step5_dir / str(filename)
        for key, filename in STEP5_CONFIG["scan_outputs"].items()
    }


def _metadata_compare_output_paths(step5_dir: Path) -> dict[str, Path]:
    filenames = {
        "authors_compare": "5-3-1：元数据比对-authors_compare.csv",
        "tags_compare": "5-3-2：元数据比对-tags_compare.csv",
        "publisher_compare": "5-3-3：元数据比对-publisher_compare.csv",
        "languages_compare": "5-3-4：元数据比对-languages_compare.csv",
        "rating_compare": "5-3-5：元数据比对-rating_compare.csv",
        "series_compare": "5-3-6：元数据比对-series_compare.csv",
        "comments_compare": "5-3-7：元数据比对-comments_compare.csv",
        "identifiers_compare": "5-3-8：元数据比对-identifiers_compare.csv",
    }
    return {key: step5_dir / filename for key, filename in filenames.items()}


def _remove_deprecated_scan_outputs(step5_dir: Path) -> None:
    for filename in DEPRECATED_SCAN_OUTPUT_FILENAMES:
        path = step5_dir / filename
        if path.exists():
            path.unlink()


def _remove_generated_step5_scan_outputs(step5_dir: Path) -> None:
    for path in list(_scan_output_paths(step5_dir).values()) + list(_metadata_compare_output_paths(step5_dir).values()):
        if path.exists():
            path.unlink()
    _remove_deprecated_scan_outputs(step5_dir)


DICT_COMPARE_SPECS: list[tuple[str, str, str, str, str]] = [
    ("authors_compare", "authors", "name", "books_authors_link", "author"),
    ("tags_compare", "tags", "name", "books_tags_link", "tag"),
    ("publisher_compare", "publishers", "name", "books_publishers_link", "publisher"),
    ("languages_compare", "languages", "lang_code", "books_languages_link", "lang_code"),
    ("rating_compare", "ratings", "rating", "books_ratings_link", "rating"),
    ("series_compare", "series", "name", "books_series_link", "series"),
]


def _write_metadata_compare_outputs(
    step5_dir: Path,
    buckets: dict[str, list[dict[str, str]]],
    batch: str,
    inc_db: Path,
    full_db: Path,
) -> list[tuple[Path, list[dict[str, str]]]]:
    """Write 5-3-* metadata compare CSVs.

    Dictionary tables (5-3-1 ~ 5-3-6) are diffed by directly comparing
    source and target table rows by business key.
    Book-level metadata (5-3-7 ~ 5-3-8) is diffed by mapped book pairs,
    with unmapped source/target rows reported as source_only/target_only.
    """
    rows_by_scope: dict[str, list[dict[str, str]]] = {key: [] for key in _metadata_compare_output_paths(step5_dir)}

    # 1) Dictionary-level: directly compare dictionary tables (5-3-1 ~ 5-3-6)
    _fill_dictionary_compare_rows(rows_by_scope, batch, inc_db, full_db)

    # 2) Book-level: full compare by mapped book pairs (5-3-7 ~ 5-3-8)
    _fill_book_metadata_compare_rows(rows_by_scope, batch, buckets, inc_db, full_db)

    results: list[tuple[Path, list[dict[str, str]]]] = []
    for key, path in _metadata_compare_output_paths(step5_dir).items():
        rows = rows_by_scope[key]
        write_dict_csv(path, rows, METADATA_COMPARE_FIELDS)
        results.append((path, rows))
    return results


def _fill_dictionary_compare_rows(
    rows_by_scope: dict[str, list[dict[str, str]]],
    batch: str,
    inc_db: Path,
    full_db: Path,
) -> None:
    """Compare dictionary tables directly and include both source and target rows."""
    with sqlite3.connect(str(inc_db)) as inc, sqlite3.connect(str(full_db)) as full:
        inc.row_factory = sqlite3.Row
        full.row_factory = sqlite3.Row
        for output_key, dict_table, key_column, link_table, link_column in DICT_COMPARE_SPECS:
            rows_by_scope[output_key].extend(_compare_dict_table(batch, inc, full, dict_table, key_column))


def _compare_dict_table(
    batch: str,
    inc: sqlite3.Connection,
    full: sqlite3.Connection,
    dict_table: str,
    key_column: str,
) -> list[dict[str, str]]:
    source_rows = _dictionary_rows_by_key(inc, dict_table, key_column)
    target_rows = _dictionary_rows_by_key(full, dict_table, key_column)
    rows: list[dict[str, str]] = []
    for key in sorted(set(source_rows) | set(target_rows)):
        source_row = source_rows.get(key)
        target_row = target_rows.get(key)
        source_value = _row_text(source_row, key_column)
        target_value = _row_text(target_row, key_column)
        source_record = _json_row(source_row)
        target_record = _json_row(target_row)
        if source_row is None:
            status = "target_only"
            action = "keep_target"
        elif target_row is None:
            status = "source_only"
            action = "insert"
        elif _dictionary_compare_record(source_row, target_row) == "same":
            status = "same"
            action = _same_dictionary_action(source_row, target_row)
        else:
            status = "conflict"
            action = "merge_metadata"
        pair_key = source_value or target_value
        if source_row is not None:
            rows.append(
                _build_metadata_compare_row(
                    batch=batch,
                    metadata_scope=dict_table,
                    metadata_key=pair_key,
                    pair_key=pair_key,
                    compare_status=status,
                    compare_side="incremental",
                    record_id=_row_text(source_row, "id"),
                    field_value=source_value,
                    record_json=source_record,
                    recommended_action=action,
                    detail=f"{dict_table}:{status}",
                )
            )
        if target_row is not None:
            rows.append(
                _build_metadata_compare_row(
                    batch=batch,
                    metadata_scope=dict_table,
                    metadata_key=pair_key,
                    pair_key=pair_key,
                    compare_status=status,
                    compare_side="full",
                    record_id=_row_text(target_row, "id"),
                    field_value=target_value,
                    record_json=target_record,
                    recommended_action=action,
                    detail=f"{dict_table}:{status}",
                )
            )
    return rows


def _dictionary_rows_by_key(conn: sqlite3.Connection, table: str, key_column: str) -> dict[str, sqlite3.Row]:
    rows: dict[str, sqlite3.Row] = {}
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY {key_column}, id").fetchall():
        key = str(row[key_column] or "").strip().lower()
        if key and key not in rows:
            rows[key] = row
    return rows


def _dictionary_compare_record(source_row: sqlite3.Row, target_row: sqlite3.Row) -> str:
    source = {key: source_row[key] for key in source_row.keys() if key != "id"}
    target = {key: target_row[key] for key in target_row.keys() if key != "id"}
    return "same" if source == target else "conflict"


def _same_dictionary_action(source_row: sqlite3.Row, target_row: sqlite3.Row) -> str:
    return _same_metadata_record_action(source_row, target_row)


def _same_metadata_record_action(source_row: sqlite3.Row, target_row: sqlite3.Row) -> str:
    if _row_text(source_row, "id") == _row_text(target_row, "id"):
        return "keep_target"
    return "relink_metadata"


def _row_text(row: sqlite3.Row | None, column: str) -> str:
    if row is None:
        return ""
    return str(row[column] if row[column] is not None else "")


def _json_row(row: sqlite3.Row | None) -> str:
    if row is None:
        return ""
    return json.dumps({key: row[key] for key in row.keys()}, ensure_ascii=False, sort_keys=True)


def _fill_book_metadata_compare_rows(
    rows_by_scope: dict[str, list[dict[str, str]]],
    batch: str,
    buckets: dict[str, list[dict[str, str]]],
    inc_db: Path,
    full_db: Path,
) -> None:
    """Fill book-level metadata compare rows by fully comparing source/target tables."""
    book_map = _book_compare_mapping(buckets)
    with sqlite3.connect(str(inc_db)) as inc, sqlite3.connect(str(full_db)) as full:
        inc.row_factory = sqlite3.Row
        full.row_factory = sqlite3.Row
        rows_by_scope["comments_compare"].extend(_compare_comments_table(batch, inc, full, book_map))
        rows_by_scope["identifiers_compare"].extend(_compare_identifiers_table(batch, inc, full, book_map))


def _book_compare_mapping(buckets: dict[str, list[dict[str, str]]]) -> dict[str, dict[str, str]]:
    source_to_target: dict[str, str] = {}
    target_to_source: dict[str, str] = {}
    sync_by_source: dict[str, str] = {}
    sync_by_target: dict[str, str] = {}
    for rows in buckets.values():
        for row in rows:
            source_book_id = row.get("source_book_id", "")
            target_book_id = row.get("target_book_id", "")
            sync_id = row.get("sync_id", "")
            if source_book_id and source_book_id not in source_to_target:
                source_to_target[source_book_id] = target_book_id
                sync_by_source[source_book_id] = sync_id
            if target_book_id and target_book_id not in target_to_source:
                target_to_source[target_book_id] = source_book_id
                sync_by_target[target_book_id] = sync_id
    return {
        "source_to_target": source_to_target,
        "target_to_source": target_to_source,
        "sync_by_source": sync_by_source,
        "sync_by_target": sync_by_target,
    }


def _compare_comments_table(
    batch: str,
    inc: sqlite3.Connection,
    full: sqlite3.Connection,
    book_map: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    source_rows = _rows_by_book(inc, "comments")
    target_rows = _rows_by_book(full, "comments")
    return _compare_book_child_rows(
        batch,
        "comments",
        "text",
        source_rows,
        target_rows,
        book_map,
        lambda row: f"len={len(str(row['text'] or '').strip())}",
    )


def _compare_identifiers_table(
    batch: str,
    inc: sqlite3.Connection,
    full: sqlite3.Connection,
    book_map: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    source_rows = _identifier_rows_by_book_type(inc)
    target_rows = _identifier_rows_by_book_type(full)
    return _compare_book_child_rows(
        batch,
        "identifiers",
        "type",
        source_rows,
        target_rows,
        book_map,
        lambda row: f"{row['type']}={row['val']}",
    )


def _compare_book_child_rows(
    batch: str,
    metadata_scope: str,
    key_column: str,
    source_rows: dict[str, sqlite3.Row],
    target_rows: dict[str, sqlite3.Row],
    book_map: dict[str, dict[str, str]],
    field_value: Callable[[sqlite3.Row], str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    source_to_target = book_map["source_to_target"]
    target_to_source = book_map["target_to_source"]
    handled_targets: set[str] = set()
    for source_key in sorted(source_rows, key=_book_child_sort_key):
        source_row = source_rows[source_key]
        source_book_id = _row_text(source_row, "book")
        source_child_key = "" if metadata_scope == "comments" else _row_text(source_row, key_column)
        target_book_id = source_to_target.get(source_book_id, "")
        target_key = _mapped_child_key(target_book_id, source_child_key)
        target_row = target_rows.get(target_key) if target_book_id else None
        if target_key:
            handled_targets.add(target_key)
        if target_row is None:
            status = "source_only"
            action = "insert"
        elif _child_rows_same(source_row, target_row):
            status = "same"
            action = "keep_target"
        else:
            status = "conflict"
            action = "merge_metadata"
        rows.append(
            _book_child_compare_row(
                batch,
                book_map,
                metadata_scope,
                source_child_key,
                "incremental",
                source_book_id,
                target_book_id,
                status,
                action,
                source_row,
                field_value(source_row),
            )
        )
        if target_row is not None:
            rows.append(
                _book_child_compare_row(
                    batch,
                    book_map,
                    metadata_scope,
                    source_child_key,
                    "full",
                    source_book_id,
                    target_book_id,
                    status,
                    action,
                    target_row,
                    field_value(target_row),
                )
            )
    for target_key in sorted(set(target_rows) - handled_targets, key=_book_child_sort_key):
        target_row = target_rows[target_key]
        target_book_id = _row_text(target_row, "book")
        source_book_id = target_to_source.get(target_book_id, "")
        target_child_key = "" if metadata_scope == "comments" else _row_text(target_row, key_column)
        rows.append(
            _book_child_compare_row(
                batch,
                book_map,
                metadata_scope,
                target_child_key,
                "full",
                source_book_id,
                target_book_id,
                "target_only",
                "keep_target",
                target_row,
                field_value(target_row),
            )
        )
    return rows


def _rows_by_book(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {
        _mapped_child_key(row["book"], ""): row
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY book").fetchall()
    }


def _identifier_rows_by_book_type(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        _mapped_child_key(row["book"], row["type"]): row
        for row in conn.execute("SELECT * FROM identifiers ORDER BY book, type").fetchall()
    }


def _mapped_child_key(book_id: object, child_key: object) -> str:
    return f"{book_id}:{str(child_key or '').strip().lower()}"


def _book_child_sort_key(value: str) -> tuple[int, str, str]:
    book_id, _, child_key = value.partition(":")
    return (*_numeric_text_key(book_id), child_key)


def _numeric_text_key(value: object) -> tuple[int, str]:
    text = str(value)
    return (int(text), "") if text.isdigit() else (10**12, text)


def _child_rows_same(source_row: sqlite3.Row, target_row: sqlite3.Row) -> bool:
    source = {key: source_row[key] for key in source_row.keys() if key not in {"id", "book"}}
    target = {key: target_row[key] for key in target_row.keys() if key not in {"id", "book"}}
    return source == target


def _book_child_compare_row(
    batch: str,
    book_map: dict[str, dict[str, str]],
    metadata_scope: str,
    metadata_key: str,
    compare_side: str,
    source_book_id: str,
    target_book_id: str,
    compare_status: str,
    recommended_action: str,
    row: sqlite3.Row,
    field_value: str,
) -> dict[str, str]:
    side_book_id = source_book_id if compare_side == "incremental" else target_book_id
    return _build_metadata_compare_row(
        sync_id=_metadata_sync_id(book_map, source_book_id, target_book_id),
        batch=batch,
        metadata_scope=metadata_scope,
        metadata_key=metadata_key,
        type=metadata_key if metadata_scope == "identifiers" else "",
        pair_key=f"{source_book_id or '-'}:{target_book_id or '-'}:{metadata_scope}:{metadata_key}",
        compare_status=compare_status,
        compare_side=compare_side,
        record_id=_row_text(row, "id"),
        field_value=field_value,
        record_json=_json_row(row),
        recommended_action=recommended_action,
        detail=f"{metadata_scope}:book={side_book_id}:{compare_status}",
    )


def _metadata_sync_id(book_map: dict[str, dict[str, str]], source_book_id: str, target_book_id: str) -> str:
    if source_book_id:
        return book_map["sync_by_source"].get(source_book_id, "")
    if target_book_id:
        return book_map["sync_by_target"].get(target_book_id, "")
    return ""


def _build_metadata_compare_row(
    *,
    sync_id: str = "",
    batch: str = "",
    metadata_scope: str = "",
    metadata_key: str = "",
    type: str = "",
    pair_key: str = "",
    compare_status: str = "",
    compare_side: str = "",
    record_id: str = "",
    field_value: str = "",
    record_json: str = "",
    recommended_action: str = "",
    detail: str = "",
) -> dict[str, str]:
    """Build one internal metadata-compare row (pre-expansion)."""
    return {
        "sync_id": sync_id,
        "batch": batch,
        "metadata_scope": metadata_scope,
        "metadata_key": metadata_key,
        "type": type,
        "pair_key": pair_key,
        "compare_status": compare_status,
        "compare_side": compare_side,
        "record_id": record_id,
        "field_value": field_value,
        "record_json": record_json,
        "recommended_action": recommended_action,
        "detail": detail,
    }


def _ensure_scan_outputs_exist(step5_dir: Path) -> None:
    scan_paths = list(_scan_output_paths(step5_dir).values())
    if any(path.exists() for path in scan_paths):
        return
    expected = ", ".join(path.name for path in scan_paths)
    raise FileNotFoundError(
        f"Step5 scan scan CSV 不存在： {display_path(step5_dir)}。"
        f"请先执行 step5 scan，或确认 --batch/--output-dir 指向正确批次。预期文件： {expected}"
    )


def _preserve_human_fields(path: Path, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not path.exists():
        return rows
    old_rows = {
        (row.get("sync_id", ""), row.get("compare_side", "incremental") or "incremental"): row
        for row in read_csv_dicts(path)
    }
    preserved_fields = ("human_decision", "human_note")
    for row in rows:
        old_row = old_rows.get((row.get("sync_id", ""), row.get("compare_side", "incremental") or "incremental"))
        if old_row is None:
            continue
        for field in preserved_fields:
            if old_row.get(field):
                row[field] = old_row[field]
    return rows


def _expand_comparison_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    expanded: list[dict[str, str]] = []
    for row in rows:
        expanded.append(_comparison_side_row(row, "incremental"))
        if row.get("target_book_id") or row.get("target_data_id"):
            expanded.append(_comparison_side_row(row, "full"))
    return expanded


def _comparison_side_row(row: dict[str, str], side: str) -> dict[str, str]:
    prefix = "source" if side == "incremental" else "target"
    return {
        "sync_id": row.get("sync_id", ""),
        "batch": row.get("batch", ""),
        "reason_code": row.get("reason_code", ""),
        "compare_side": side,
        "source_book_id": row.get("source_book_id", ""),
        "source_data_id": row.get("source_data_id", ""),
        "target_book_id": row.get("target_book_id", ""),
        "target_data_id": row.get("target_data_id", ""),
        "book_id": row.get(f"{prefix}_book_id", row.get("source_book_id" if side == "incremental" else "target_book_id", "")),
        "data_id": row.get(f"{prefix}_data_id", row.get("source_data_id" if side == "incremental" else "target_data_id", "")),
        "title": row.get(f"{prefix}_title", ""),
        "author": row.get(f"{prefix}_author", ""),
        "book_path": row.get(f"{prefix}_book_path", ""),
        "file_basename": row.get(f"{prefix}_file_basename", ""),
        "ext": row.get(f"{prefix}_ext", ""),
        "bytes": row.get(f"{prefix}_bytes", ""),
        "relative_path": row.get(f"{prefix}_relative_path", ""),
        "authors": row.get(f"{prefix}_authors", ""),
        "tags": row.get(f"{prefix}_tags", ""),
        "publisher": row.get(f"{prefix}_publisher", ""),
        "languages": row.get(f"{prefix}_languages", ""),
        "rating": row.get(f"{prefix}_rating", ""),
        "series": row.get(f"{prefix}_series", ""),
        "identifiers": row.get(f"{prefix}_identifiers", ""),
        "comments_len": row.get(f"{prefix}_comments_len", ""),
        "metadata": row.get(f"{prefix}_metadata", ""),
        "books_record": row.get(f"{prefix}_books_record", ""),
        "data_record": row.get(f"{prefix}_data_record", ""),
        "recommended_action": row.get("recommended_action", ""),
        "human_decision": row.get("human_decision", "") if side == "incremental" else "",
        "human_note": row.get("human_note", "") if side == "incremental" else "",
    }


def _scan_against_full_db(
    batch: str,
    inc_db: Path,
    full_db: Path,
    full_library_dir: Path,
    metadata_conflict: str,
) -> dict[str, list[dict[str, str]]]:
    buckets = _empty_scan_buckets()
    with sqlite3.connect(inc_db) as inc, sqlite3.connect(full_db) as full:
        inc.row_factory = sqlite3.Row
        full.row_factory = sqlite3.Row
        full_file_map = _full_file_row_map(full)
        full_book_map = _full_book_key_map(full)
        full_title_map = _full_title_map(full)
        for source_row in inc.execute(SQL["book_files_query"]).fetchall():
            source_author = _primary_author(inc, source_row["book_id"])
            source_relative = _relative_from_row(source_row)
            file_match = full_file_map.get(_sync_key(source_row["title"], source_row["format"], source_row["uncompressed_size"]))
            target_path = full_library_dir / source_relative

            target_row: sqlite3.Row | None = None
            if file_match is not None:
                target_row = file_match
                reason_code, action = _duplicate_scan_decision(
                    inc,
                    full,
                    int(source_row["book_id"]),
                    int(target_row["book_id"]),
                )
                buckets["system_duplicate"].append(_scan_row(batch, source_row, target_row, reason_code, action, inc, full))
            elif target_path.exists() and target_path.stat().st_size != int(source_row["uncompressed_size"] or -1):
                target_row = _target_by_relative(full, source_relative)
                buckets["system_duplicate"].append(_scan_row(batch, source_row, target_row, "target_path_exists_keep_target", "keep_target", inc, full))
            else:
                book_key = _book_key(source_row["title"], source_author)
                target_row = full_book_map.get(book_key)
                if target_row is not None:
                    buckets["manual_suspected_duplicate"].append(_scan_row(batch, source_row, target_row, "suspected_duplicate_book", "report_only", inc, full))
                else:
                    same_title_rows = full_title_map.get(_normalize(source_row["title"]), [])
                    if same_title_rows:
                        target_row = same_title_rows[0]
                        buckets["manual_suspected_duplicate"].append(_scan_row(batch, source_row, target_row, "same_title_different_file", "report_only", inc, full))
                    else:
                        buckets["system_insert"].append(_scan_row(batch, source_row, None, "new_book_or_file", "insert_book", inc, full))
    return buckets


def _scan_row(
    batch: str,
    source_row: sqlite3.Row,
    target_row: sqlite3.Row | None,
    reason_code: str,
    action: str,
    inc: sqlite3.Connection,
    full: sqlite3.Connection | None,
) -> dict[str, str]:
    source_author = _primary_author(inc, source_row["book_id"])
    target_author = _primary_author(full, target_row["book_id"]) if full is not None and target_row is not None else ""
    source_relative = _relative_from_row(source_row)
    target_relative = _relative_from_row(target_row) if target_row is not None else ""
    metadata_compare = _metadata_compare_fields(inc, full, source_row["book_id"], target_row["book_id"] if target_row is not None else None)
    return {
        "sync_id": _sync_id(batch, source_row["book_id"], source_row["data_id"], reason_code, target_row["data_id"] if target_row is not None else ""),
        "batch": batch,
        "reason_code": reason_code,
        "source_book_id": str(source_row["book_id"]),
        "source_data_id": str(source_row["data_id"]),
        "target_book_id": str(target_row["book_id"]) if target_row is not None else "",
        "target_data_id": str(target_row["data_id"]) if target_row is not None else "",
        "source_title": str(source_row["title"] or ""),
        "target_title": str(target_row["title"] or "") if target_row is not None else "",
        "source_author": source_author,
        "target_author": target_author,
        "source_book_path": str(source_row["book_path"] or ""),
        "target_book_path": str(target_row["book_path"] or "") if target_row is not None else "",
        "source_file_basename": str(source_row["file_basename"] or ""),
        "target_file_basename": str(target_row["file_basename"] or "") if target_row is not None else "",
        "source_ext": str(source_row["format"] or "").lower(),
        "target_ext": str(target_row["format"] or "").lower() if target_row is not None else "",
        "source_bytes": str(source_row["uncompressed_size"] or ""),
        "target_bytes": str(target_row["uncompressed_size"] or "") if target_row is not None else "",
        "source_relative_path": source_relative,
        "target_relative_path": target_relative,
        "source_authors": _linked_values(inc, "authors", "name", "books_authors_link", "author", source_row["book_id"]),
        "target_authors": _linked_values(full, "authors", "name", "books_authors_link", "author", target_row["book_id"]) if full is not None and target_row is not None else "",
        "source_tags": _linked_values(inc, "tags", "name", "books_tags_link", "tag", source_row["book_id"]),
        "target_tags": _linked_values(full, "tags", "name", "books_tags_link", "tag", target_row["book_id"]) if full is not None and target_row is not None else "",
        "source_publisher": _linked_values(inc, "publishers", "name", "books_publishers_link", "publisher", source_row["book_id"]),
        "target_publisher": _linked_values(full, "publishers", "name", "books_publishers_link", "publisher", target_row["book_id"]) if full is not None and target_row is not None else "",
        "source_languages": _linked_values(inc, "languages", "lang_code", "books_languages_link", "lang_code", source_row["book_id"]),
        "target_languages": _linked_values(full, "languages", "lang_code", "books_languages_link", "lang_code", target_row["book_id"]) if full is not None and target_row is not None else "",
        "source_rating": _linked_values(inc, "ratings", "rating", "books_ratings_link", "rating", source_row["book_id"]),
        "target_rating": _linked_values(full, "ratings", "rating", "books_ratings_link", "rating", target_row["book_id"]) if full is not None and target_row is not None else "",
        "source_series": _linked_values(inc, "series", "name", "books_series_link", "series", source_row["book_id"]),
        "target_series": _linked_values(full, "series", "name", "books_series_link", "series", target_row["book_id"]) if full is not None and target_row is not None else "",
        "source_identifiers": _identifier_values(inc, source_row["book_id"]),
        "target_identifiers": _identifier_values(full, target_row["book_id"]) if full is not None and target_row is not None else "",
        "source_comments_len": _comment_length(inc, source_row["book_id"]),
        "target_comments_len": _comment_length(full, target_row["book_id"]) if full is not None and target_row is not None else "",
        "source_metadata": _metadata_summary(inc, source_row["book_id"]),
        "target_metadata": _metadata_summary(full, target_row["book_id"]) if full is not None and target_row is not None else "",
        "source_authors_compare": metadata_compare["authors_compare"],
        "target_authors_compare": metadata_compare["authors_compare"],
        "source_tags_compare": metadata_compare["tags_compare"],
        "target_tags_compare": metadata_compare["tags_compare"],
        "source_publisher_compare": metadata_compare["publisher_compare"],
        "target_publisher_compare": metadata_compare["publisher_compare"],
        "source_languages_compare": metadata_compare["languages_compare"],
        "target_languages_compare": metadata_compare["languages_compare"],
        "source_rating_compare": metadata_compare["rating_compare"],
        "target_rating_compare": metadata_compare["rating_compare"],
        "source_series_compare": metadata_compare["series_compare"],
        "target_series_compare": metadata_compare["series_compare"],
        "source_comments_compare": metadata_compare["comments_compare"],
        "target_comments_compare": metadata_compare["comments_compare"],
        "source_identifiers_compare": metadata_compare["identifiers_compare"],
        "target_identifiers_compare": metadata_compare["identifiers_compare"],
        "source_metadata_compare": metadata_compare["metadata_compare"],
        "target_metadata_compare": metadata_compare["metadata_compare"],
        "source_books_record": _table_row_json(inc, "books", source_row["book_id"]),
        "target_books_record": _table_row_json(full, "books", target_row["book_id"]) if full is not None and target_row is not None else "",
        "source_data_record": _table_row_json(inc, "data", source_row["data_id"]),
        "target_data_record": _table_row_json(full, "data", target_row["data_id"]) if full is not None and target_row is not None else "",
        "recommended_action": action,
        "human_decision": "",
        "human_note": "",
    }


def _full_file_row_map(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        _sync_key(row["title"], row["format"], row["uncompressed_size"]): row
        for row in conn.execute(SQL["book_files_query"]).fetchall()
    }


def _full_book_key_map(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    result: dict[str, sqlite3.Row] = {}
    for row in conn.execute(SQL["book_files_query"]).fetchall():
        result.setdefault(_book_key(row["title"], _primary_author(conn, row["book_id"])), row)
    return result


def _full_title_map(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    result: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(SQL["book_files_query"]).fetchall():
        result.setdefault(_normalize(row["title"]), []).append(row)
    return result


def _target_by_relative(conn: sqlite3.Connection, relative: str) -> sqlite3.Row | None:
    for row in conn.execute(SQL["book_files_query"]).fetchall():
        if _relative_from_row(row) == relative:
            return row
    return None


def _duplicate_scan_decision(
    inc: sqlite3.Connection,
    full: sqlite3.Connection,
    source_book_id: int,
    target_book_id: int,
) -> tuple[str, str]:
    metadata_compare = _metadata_compare_fields(inc, full, source_book_id, target_book_id)
    if _metadata_compare_has_syncable_delta(metadata_compare):
        return "file_duplicate_metadata_missing", "fill_missing_metadata"
    return "file_duplicate", "keep_target"


def _metadata_compare_fields(
    inc: sqlite3.Connection,
    full: sqlite3.Connection | None,
    source_book_id: object,
    target_book_id: object | None,
) -> dict[str, str]:
    if full is None or target_book_id is None:
        return {
            "authors_compare": "target_missing",
            "tags_compare": "target_missing",
            "publisher_compare": "target_missing",
            "languages_compare": "target_missing",
            "rating_compare": "target_missing",
            "series_compare": "target_missing",
            "comments_compare": "target_missing",
            "identifiers_compare": "target_missing",
            "metadata_compare": "target_missing",
        }

    fields = {
        "authors_compare": _compare_multi_link_values(inc, full, "authors", "name", "books_authors_link", "author", source_book_id, target_book_id),
        "tags_compare": _compare_multi_link_values(inc, full, "tags", "name", "books_tags_link", "tag", source_book_id, target_book_id),
        "publisher_compare": _compare_single_link_values(inc, full, "publishers", "name", "books_publishers_link", "publisher", source_book_id, target_book_id),
        "languages_compare": _compare_multi_link_values(inc, full, "languages", "lang_code", "books_languages_link", "lang_code", source_book_id, target_book_id),
        "rating_compare": _compare_multi_link_values(inc, full, "ratings", "rating", "books_ratings_link", "rating", source_book_id, target_book_id),
        "series_compare": _compare_single_link_values(inc, full, "series", "name", "books_series_link", "series", source_book_id, target_book_id),
        "comments_compare": _compare_comments(inc, full, source_book_id, target_book_id),
        "identifiers_compare": _compare_identifiers(inc, full, source_book_id, target_book_id),
    }
    status = _metadata_compare_rollup(fields)
    fields["metadata_compare"] = status
    return fields


def _compare_multi_link_values(
    inc: sqlite3.Connection,
    full: sqlite3.Connection,
    dictionary_table: str,
    value_column: str,
    link_table: str,
    link_column: str,
    source_book_id: object,
    target_book_id: object,
) -> str:
    source_values = _linked_value_set(inc, dictionary_table, value_column, link_table, link_column, source_book_id)
    target_values = _linked_value_set(full, dictionary_table, value_column, link_table, link_column, target_book_id)
    return _compare_sets(source_values, target_values)


def _compare_single_link_values(
    inc: sqlite3.Connection,
    full: sqlite3.Connection,
    dictionary_table: str,
    value_column: str,
    link_table: str,
    link_column: str,
    source_book_id: object,
    target_book_id: object,
) -> str:
    source_values = _linked_value_set(inc, dictionary_table, value_column, link_table, link_column, source_book_id)
    target_values = _linked_value_set(full, dictionary_table, value_column, link_table, link_column, target_book_id)
    if not source_values and not target_values:
        return "same(empty)"
    if source_values and not target_values:
        return f"target_missing: {', '.join(sorted(source_values))}"
    if target_values and not source_values:
        return f"source_missing: {', '.join(sorted(target_values))}"
    if source_values == target_values:
        return f"same: {', '.join(sorted(source_values))}"
    return f"conflict: source={', '.join(sorted(source_values))}; target={', '.join(sorted(target_values))}"


def _compare_sets(source_values: set[str], target_values: set[str]) -> str:
    if not source_values and not target_values:
        return "same(empty)"
    source_only = source_values - target_values
    target_only = target_values - source_values
    if not source_only and not target_only:
        return f"same: {', '.join(sorted(source_values))}"
    parts: list[str] = []
    if source_only:
        parts.append(f"source_only={', '.join(sorted(source_only))}")
    if target_only:
        parts.append(f"target_only={', '.join(sorted(target_only))}")
    return "delta: " + "; ".join(parts)


def _compare_comments(inc: sqlite3.Connection, full: sqlite3.Connection, source_book_id: object, target_book_id: object) -> str:
    source_row = inc.execute("SELECT text FROM comments WHERE book = ?", (source_book_id,)).fetchone()
    target_row = full.execute("SELECT text FROM comments WHERE book = ?", (target_book_id,)).fetchone()
    source_text = str((source_row["text"] if source_row else "") or "").strip()
    target_text = str((target_row["text"] if target_row else "") or "").strip()
    if not source_text and not target_text:
        return "same(empty)"
    if source_text and not target_text:
        return f"target_missing: source_len={len(source_text)}"
    if target_text and not source_text:
        return f"source_missing: target_len={len(target_text)}"
    if source_text == target_text:
        return f"same: len={len(source_text)}"
    return f"conflict: source_len={len(source_text)}; target_len={len(target_text)}"


def _compare_identifiers(inc: sqlite3.Connection, full: sqlite3.Connection, source_book_id: object, target_book_id: object) -> str:
    source_values = _identifier_map(inc, source_book_id)
    target_values = _identifier_map(full, target_book_id)
    if not source_values and not target_values:
        return "same(empty)"
    parts: list[str] = []
    for key in sorted(set(source_values) | set(target_values)):
        source_value = source_values.get(key)
        target_value = target_values.get(key)
        if source_value is None:
            parts.append(f"{key}:source_missing(target={target_value})")
        elif target_value is None:
            parts.append(f"{key}:target_missing(source={source_value})")
        elif source_value == target_value:
            parts.append(f"{key}:same({source_value})")
        else:
            parts.append(f"{key}:conflict(source={source_value},target={target_value})")
    if all(":same(" in part for part in parts):
        return "same: " + "; ".join(parts)
    return "delta: " + "; ".join(parts)


def _metadata_compare_rollup(fields: dict[str, str]) -> str:
    if any("conflict" in value for value in fields.values()):
        return "conflict"
    if any(("target_missing" in value or "source_only=" in value) for value in fields.values()):
        return "target_missing"
    if any(("source_missing" in value or "target_only=" in value) for value in fields.values()):
        return "target_extra"
    return "same"


def _metadata_compare_has_syncable_delta(fields: dict[str, str]) -> bool:
    return any(
        "target_missing" in value or "source_only=" in value
        for key, value in fields.items()
        if key != "metadata_compare"
    )


def _primary_author(conn: sqlite3.Connection | None, book_id: object) -> str:
    if conn is None:
        return ""
    try:
        row = conn.execute(
            "SELECT authors.name FROM authors "
            "JOIN books_authors_link ON books_authors_link.author = authors.id "
            "WHERE books_authors_link.book = ? ORDER BY books_authors_link.id LIMIT 1",
            (book_id,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row["name"] if row else "")


def _metadata_summary(conn: sqlite3.Connection | None, book_id: object) -> str:
    if conn is None:
        return ""
    parts = [
        f"authors={_linked_values(conn, 'authors', 'name', 'books_authors_link', 'author', book_id)}",
        f"tags={_linked_values(conn, 'tags', 'name', 'books_tags_link', 'tag', book_id)}",
        f"publisher={_linked_values(conn, 'publishers', 'name', 'books_publishers_link', 'publisher', book_id)}",
        f"languages={_linked_values(conn, 'languages', 'lang_code', 'books_languages_link', 'lang_code', book_id)}",
        f"rating={_linked_values(conn, 'ratings', 'rating', 'books_ratings_link', 'rating', book_id)}",
        f"series={_linked_values(conn, 'series', 'name', 'books_series_link', 'series', book_id)}",
        f"identifiers={_identifier_values(conn, book_id)}",
        f"comments_len={_comment_length(conn, book_id)}",
    ]
    return " | ".join(parts)


def _linked_values(
    conn: sqlite3.Connection,
    dictionary_table: str,
    value_column: str,
    link_table: str,
    link_column: str,
    book_id: object,
) -> str:
    try:
        rows = conn.execute(
            f"SELECT d.{value_column} AS value FROM {dictionary_table} d "
            f"JOIN {link_table} l ON l.{link_column} = d.id "
            "WHERE l.book = ? ORDER BY d.id",
            (book_id,),
        ).fetchall()
    except sqlite3.Error:
        return ""
    return ";".join(str(row["value"] or "") for row in rows)


def _linked_value_set(
    conn: sqlite3.Connection,
    dictionary_table: str,
    value_column: str,
    link_table: str,
    link_column: str,
    book_id: object,
) -> set[str]:
    values = _linked_values(conn, dictionary_table, value_column, link_table, link_column, book_id)
    return {value.strip() for value in values.split(";") if value.strip()}


def _identifier_values(conn: sqlite3.Connection, book_id: object) -> str:
    try:
        rows = conn.execute(
            "SELECT type, val FROM identifiers WHERE book = ? ORDER BY type",
            (book_id,),
        ).fetchall()
    except sqlite3.Error:
        return ""
    return ";".join(f"{row['type']}={row['val']}" for row in rows)


def _identifier_map(conn: sqlite3.Connection, book_id: object) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT type, val FROM identifiers WHERE book = ? ORDER BY type",
            (book_id,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row["type"]): str(row["val"] or "") for row in rows}


def _comment_length(conn: sqlite3.Connection, book_id: object) -> str:
    try:
        row = conn.execute("SELECT text FROM comments WHERE book = ?", (book_id,)).fetchone()
    except sqlite3.Error:
        return "0"
    return str(len(str(row["text"] or ""))) if row else "0"


def _table_row_json(conn: sqlite3.Connection | None, table: str, row_id: object) -> str:
    if conn is None or row_id in ("", None):
        return ""
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    return json.dumps({key: row[key] for key in row.keys()}, ensure_ascii=False, default=str, sort_keys=True)


def _relative_from_row(row: sqlite3.Row | None) -> str:
    if row is None:
        return ""
    return str(Path(str(row["book_path"] or "")) / f"{row['file_basename']}.{str(row['format']).lower()}")


def _normalize(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _book_key(title: object, author: object) -> str:
    normalized_author = _normalize(author)
    if normalized_author:
        return f"{_normalize(title)}|{normalized_author}"
    return _normalize(title)


def _sync_id(batch: str, source_book_id: object, source_data_id: object, reason_code: str, target_data_id: object) -> str:
    raw = f"{batch}|{source_book_id}|{source_data_id}|{reason_code}|{target_data_id}"
    return uuid.uuid5(uuid.NAMESPACE_URL, raw).hex[:16]


def _bucket_counts(buckets: dict[str, list[dict[str, str]]]) -> str:
    return ", ".join(f"{key}({len(rows)})" for key, rows in buckets.items() if rows) or "无"


def _step5_scan_output_summary(
    rows: list[dict[str, str]],
    *,
    source_side_only: bool = False,
    compare_only: bool = False,
) -> str:
    scoped_rows = _summary_scoped_rows(rows, source_side_only)
    action_counts = Counter((row.get("recommended_action") or "").strip() or "empty" for row in scoped_rows)
    action_summary = _format_action_summary(action_counts)
    pending_sql = 0 if compare_only else _pending_sql_count(scoped_rows)
    return f"{len(scoped_rows)} 条，{action_summary}，待执行SQL {pending_sql}条"


def _summary_scoped_rows(rows: list[dict[str, str]], source_side_only: bool) -> list[dict[str, str]]:
    if not source_side_only:
        return rows
    return [row for row in rows if row.get("compare_side") == "incremental"]


def _format_action_summary(counts: Counter[str]) -> str:
    if not counts:
        return "无动作"
    return "，".join(f"{action} {count}条" for action, count in sorted(counts.items()))


def _pending_sql_count(rows: list[dict[str, str]]) -> int:
    return sum(1 for row in rows if (row.get("recommended_action") or "").strip() in STEP5_EXECUTABLE_ACTIONS)


def _pending_sql_count_for_buckets(buckets: dict[str, list[dict[str, str]]]) -> int:
    return sum(_pending_sql_count(rows) for rows in buckets.values())


def _load_step5_confirmation_rows(step5_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in _scan_output_paths(step5_dir).values():
        for row in read_csv_dicts(path):
            if row.get("compare_side", "incremental") != "incremental":
                continue
            row["_source_file"] = path.name
            rows.append(row)
    rows.sort(key=lambda row: (int(row.get("source_book_id") or 0), int(row.get("source_data_id") or 0), row.get("reason_code", "")))
    return rows


def _resolved_step5_action(row: dict[str, str]) -> str:
    decision = (row.get("human_decision") or "").strip()
    action = decision or (row.get("recommended_action") or "").strip()
    if action in set(STEP5_CONFIG["actions"]["valid"]):
        return action
    return str(STEP5_CONFIG["actions"]["default"])


def _step5_merge_sql_notes(row: dict[str, str], action: str) -> list[str]:
    source_book_id = row.get("source_book_id", "")
    source_data_id = row.get("source_data_id", "")
    target_book_id = row.get("target_book_id", "")
    target_data_id = row.get("target_data_id", "")
    reason_code = row.get("reason_code", "")
    sync_id = row.get("sync_id", "")
    note = (
        f"-- sync_id={sync_id} reason={reason_code} action={action} "
        f"source_book_id={source_book_id} source_data_id={source_data_id} "
        f"target_book_id={target_book_id} target_data_id={target_data_id}"
    )
    if action in STEP5_NOOP_ACTIONS:
        return [note, "-- no-op: keep existing full-library records."]
    return [
        note,
        "-- applied by Step5 apply against existing full-library tables; no intermediate plan table is written.",
    ]


def _execute_merge_plan_sql(full_db: Path, sql_path: Path) -> None:
    with sqlite3.connect(full_db) as conn:
        conn.executescript(sql_path.read_text(encoding="utf-8"))


def _apply_confirmation_rows(
    inc_db: Path,
    incremental_dir: Path,
    full_db: Path,
    full_library_dir: Path,
    rows: list[dict[str, str]],
    metadata_conflict: str,
    force: bool,
) -> list[dict[str, str]]:
    reports: list[dict[str, str]] = []
    manual_files = {str(value) for key, value in STEP5_CONFIG["scan_outputs"].items() if key.startswith("manual_")}
    with sqlite3.connect(inc_db) as inc, sqlite3.connect(full_db) as full:
        inc.row_factory = sqlite3.Row
        full.row_factory = sqlite3.Row
        _register_calibre_functions(full)
        full_keys = _full_key_map(full)
        book_id_map: dict[int, tuple[int, str]] = {}
        for row in rows:
            action = _resolved_step5_action(row)
            if row.get("_source_file") in manual_files and not (row.get("human_decision") or "").strip() and not force:
                reports.append(_merge_report(row.get("sync_id", ""), row.get("source_book_id", ""), row.get("source_data_id", ""), row.get("target_book_id", ""), row.get("target_data_id", ""), action, "manual_required", "not_verified", "", "", "待人工确认 CSV 未填写 human_decision"))
                continue
            if action in STEP5_NOOP_ACTIONS:
                reports.append(_merge_report(row.get("sync_id", ""), row.get("source_book_id", ""), row.get("source_data_id", ""), row.get("target_book_id", ""), row.get("target_data_id", ""), action, "skipped", "not_applicable", "", "", ""))
                continue
            full.execute("BEGIN")
            try:
                report = _apply_merge_action(inc, full, incremental_dir, full_library_dir, row, action, metadata_conflict, full_keys, book_id_map)
                if report["execute_status"] == "failed":
                    full.rollback()
                else:
                    full.commit()
                reports.append(report)
            except Exception as exc:  # noqa: BLE001 - keep per-row merge failures auditable.
                full.rollback()
                reports.append(_merge_report(row.get("sync_id", ""), row.get("source_book_id", ""), row.get("source_data_id", ""), row.get("target_book_id", ""), row.get("target_data_id", ""), action, "failed", "not_verified", "failed", "", str(exc)))
    return reports


def _apply_merge_action(
    inc: sqlite3.Connection,
    full: sqlite3.Connection,
    incremental_dir: Path,
    full_library_dir: Path,
    row: dict[str, str],
    action: str,
    metadata_conflict: str,
    full_keys: dict[str, int],
    book_id_map: dict[int, tuple[int, str]],
) -> dict[str, str]:
    source_book_id = int(row.get("source_book_id") or 0)
    source_data_id = int(row.get("source_data_id") or 0)
    source_rows = _book_file_rows_by_book(inc).get(source_book_id, [])
    source_data_row = inc.execute(SQL["book_files_query"].replace("ORDER BY books.id, data.id", "WHERE data.id = ?"), (source_data_id,)).fetchone()
    if source_data_row is None:
        raise ValueError(f"source_data_id 不存在： {source_data_id}")

    target_book_text = row.get("target_book_id", "")
    if target_book_text.strip().isdigit():
        target_book_id = int(target_book_text)
        book_status = "matched"
    else:
        target_book_id, book_status = _resolve_target_book(inc, full, source_book_id, source_rows or [source_data_row], full_keys, book_id_map)

    target_data_id = row.get("target_data_id", "")
    data_status = "not_applicable"
    file_status = "not_applicable"
    message = ""
    if action in {"insert_book", "insert_data"}:
        target_data_id, data_status, data_message = _sync_data_row(inc, full, source_data_id, target_book_id)
        relative = row.get("source_relative_path") or _relative_from_row(source_data_row)
        file_status, file_message = _copy_entity_file(incremental_dir, full_library_dir, relative)
        message = "; ".join(part for part in (data_message, file_message) if part)
        full_keys[_sync_key(source_data_row["title"], source_data_row["format"], source_data_row["uncompressed_size"])] = target_book_id

    metadata_status = ""
    metadata_details = ""
    if action in {"insert_book", "insert_data", "fill_missing_metadata", "merge_metadata"}:
        policy = STEP5_CONFIG["metadata_conflict"]["overwrite"] if action == "merge_metadata" else metadata_conflict
        metadata_status, metadata_details = _sync_book_metadata(inc, full, source_book_id, target_book_id, str(policy))

    verification = "verified" if file_status != "file_missing" and data_status != "conflict" else "not_verified"
    execute_status = "success" if verification == "verified" else "failed"
    return _merge_report(row.get("sync_id", ""), source_book_id, source_data_id, target_book_id, target_data_id, action, execute_status, verification, metadata_status, metadata_details, message)


def _merge_report(
    sync_id: object,
    source_book_id: object,
    source_data_id: object,
    target_book_id: object,
    target_data_id: object,
    action: object,
    execute_status: object,
    verification_status: object,
    metadata_status: object,
    metadata_details: object,
    error_message: object,
) -> dict[str, str]:
    return {
        "sync_id": str(sync_id),
        "source_book_id": str(source_book_id),
        "source_data_id": str(source_data_id),
        "target_book_id": str(target_book_id),
        "target_data_id": str(target_data_id),
        "recommended_action": str(action),
        "execute_status": str(execute_status),
        "verification_status": str(verification_status),
        "metadata_status": str(metadata_status),
        "metadata_details": str(metadata_details),
        "error_message": str(error_message),
    }


def _merge_status_counts(reports: list[dict[str, str]]) -> str:
    counts: dict[str, int] = {}
    for row in reports:
        key = row["execute_status"]
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{key}({value})" for key, value in sorted(counts.items())) or "无"


def _merge_report_summary(reports: list[dict[str, str]]) -> str:
    status_counts = Counter(row.get("execute_status", "") for row in reports)
    executed = sum(status_counts.get(status, 0) for status in ("success", "already_done"))
    skipped = status_counts.get("skipped", 0)
    manual_required = status_counts.get("manual_required", 0)
    failed = status_counts.get("failed", 0)
    return (
        f"审计明细 {len(reports)}条，"
        f"实际执行 {executed}条，"
        f"跳过 {skipped}条，"
        f"待人工确认 {manual_required}条，"
        f"失败 {failed}条"
    )


def _manual_check_counts(reports: list[dict[str, str]]) -> str:
    counts: dict[str, int] = {}
    for row in reports:
        key = row["check_status"]
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{key}({value})" for key, value in sorted(counts.items())) or "无"


def _target_full_db(full_library_dir: Path, inc_db: Path, batch: str) -> Path:
    """Return the target full DB path.

    If full DB files already exist in the directory, return the most recent one
    (sorted by filename date).  Otherwise return a new path based on the
    incremental DB's date — the caller will copy inc_db there to initialize it.
    """
    config = pipeline_config()
    pattern = str(config["patterns"]["full_db"]).replace("{date}", "*")
    candidates = sorted(full_library_dir.glob(pattern), key=lambda p: p.name, reverse=True)
    if candidates:
        return candidates[0]
    batch_key = _date_key_from_path(inc_db) or batch.replace("-", "")
    return full_library_dir / str(config["patterns"]["full_db"]).format(date=batch_key)


def _date_key_from_path(path: Path) -> str | None:
    match = re.search(r"(\d{8})", path.name)
    return match.group(1) if match else None


def _latest_full_db(full_library_dir: Path, exclude: Path) -> Path | None:
    config = pipeline_config()
    pattern = str(config["patterns"]["full_db"]).replace("{date}", "*")
    candidates = [
        path
        for path in full_library_dir.glob(pattern)
        if path.resolve() != exclude.resolve()
    ]
    return max(candidates, key=lambda path: path.name, default=None)


def _is_usable_full_db(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('books', 'data')"
            ).fetchall()
    except sqlite3.Error:
        return False
    return {str(row[0]) for row in rows} == {"books", "data"}


def _same_database_file(left: Path, right: Path) -> bool:
    if not left.exists() or not right.exists():
        return False
    try:
        return filecmp.cmp(left, right, shallow=False)
    except OSError:
        return False


def _resolve_step5_incremental_db(batch: str, incremental_dir: Path, output_dir: Path) -> Path:
    compact_batch = batch.replace("-", "")
    config = pipeline_config()
    candidates = [
        step_dir(output_dir, "step3") / str(config["step3"]["cleaned_db_filename"]),
        step_dir(output_dir, "step2")
        / str(config["step2"]["repair"]["encoded_db_filename"]).format(date=compact_batch),
    ]
    if incremental_dir.is_file():
        candidates.append(incremental_dir)
    else:
        candidates.append(
            incremental_dir
            / str(config["patterns"]["incremental_db"]).format(date=compact_batch)
        )
        latest_db = latest_incremental_db(incremental_dir)
        if latest_db is not None:
            candidates.append(latest_db)
        candidates.append(incremental_dir / "metadata.db")
    candidates.append(
        Path(str(config["defaults"]["input_dir"]))
        / str(config["patterns"]["incremental_db"]).format(date=compact_batch)
    )
    latest_default_db = latest_incremental_db(Path(str(config["defaults"]["input_dir"])))
    if latest_default_db is not None:
        candidates.append(latest_default_db)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0]


def _preflight_blockers(output_dir: Path, inc_db: Path) -> list[str]:
    blockers: list[str] = []
    step2_config = load_json_config("step2_config.json")
    for path in (
        step_dir(output_dir, "step2") / str(step2_config["outputs"]["sql_filename"]),
        step_dir(output_dir, "step2") / str(step2_config["outputs"]["bat_filename"]),
    ):
        if not path.exists():
            blockers.append(f"缺少 Step2 plan 指令文件： {display_path(path)}")

    if not inc_db.exists():
        blockers.append(f"缺少可用增量预处理库： {display_path(inc_db)}")
    return blockers


def _initial_copy_files(incremental_dir: Path, full_library_dir: Path) -> list[dict[str, str]]:
    reports: list[dict[str, str]] = []
    for path in incremental_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() == ".db":
            continue
        relative = path.relative_to(incremental_dir)
        target = full_library_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == path.stat().st_size:
            status = "already_synced"
        else:
            shutil.copy2(path, target)
            status = "synced"
        reports.append(_sync_report("", "", "", "", "", "", "", str(relative), "initialized", status, "verified", "not_applicable", "", ""))
    return reports


def _initialize_full_library(
    inc_db: Path,
    incremental_dir: Path,
    full_db: Path,
    full_library_dir: Path,
) -> list[dict[str, str]]:
    full_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(inc_db, full_db)
    return _initial_copy_files(incremental_dir, full_library_dir)


def _sync_existing_full_library(
    inc_db: Path,
    incremental_dir: Path,
    full_db: Path,
    full_library_dir: Path,
    metadata_conflict: str,
) -> list[dict[str, str]]:
    reports: list[dict[str, str]] = []
    with sqlite3.connect(inc_db) as inc, sqlite3.connect(full_db) as full:
        inc.row_factory = sqlite3.Row
        full.row_factory = sqlite3.Row
        _register_calibre_functions(full)
        full_keys = _full_key_map(full)
        book_id_map: dict[int, tuple[int, str]] = {}
        rows_by_book = _book_file_rows_by_book(inc)

        for source_book_id, rows in rows_by_book.items():
            full.execute("BEGIN")
            try:
                target_book_id, book_status = _resolve_target_book(
                    inc,
                    full,
                    source_book_id,
                    rows,
                    full_keys,
                    book_id_map,
                )
                metadata_status, metadata_details = _sync_book_metadata(
                    inc,
                    full,
                    source_book_id,
                    target_book_id,
                    metadata_conflict,
                )

                for row in rows:
                    key = _sync_key(row["title"], row["format"], row["uncompressed_size"])
                    relative = str(Path(row["book_path"]) / f"{row['file_basename']}.{str(row['format']).lower()}")
                    target_data_id, data_status, data_message = _sync_data_row(
                        inc,
                        full,
                        row["data_id"],
                        target_book_id,
                    )
                    file_status, file_message = _copy_entity_file(incremental_dir, full_library_dir, relative)
                    full_keys[key] = target_book_id
                    message = "; ".join(part for part in (data_message, file_message) if part)
                    verification = "verified" if file_status != "file_missing" and data_status != "conflict" else "not_verified"
                    reports.append(_sync_report(
                        row["book_id"],
                        target_book_id,
                        row["data_id"],
                        target_data_id,
                        row["title"],
                        row["format"],
                        row["uncompressed_size"],
                        relative,
                        book_status,
                        file_status if data_status != "conflict" else "conflict",
                        verification,
                        metadata_status,
                        metadata_details,
                        message,
                    ))
                full.commit()
            except Exception as exc:  # noqa: BLE001 - report per-row sync failures.
                full.rollback()
                for row in rows:
                    relative = str(Path(row["book_path"]) / f"{row['file_basename']}.{str(row['format']).lower()}")
                    reports.append(_sync_report(
                        row["book_id"],
                        "",
                        row["data_id"],
                        "",
                        row["title"],
                        row["format"],
                        row["uncompressed_size"],
                        relative,
                        "failed",
                        "failed",
                        "not_verified",
                        "failed",
                        "",
                        str(exc),
                    ))
    return reports


def _register_calibre_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("uuid4", 0, lambda: str(uuid.uuid4()))
    conn.create_function("title_sort", 1, lambda value: str(value or ""))


def _book_file_rows_by_book(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    rows_by_book: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(SQL["book_files_query"]).fetchall():
        rows_by_book.setdefault(int(row["book_id"]), []).append(row)
    return rows_by_book


def _full_key_map(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        _sync_key(row["title"], row["format"], row["uncompressed_size"]): int(row["book_id"])
        for row in conn.execute(SQL["book_files_query"]).fetchall()
    }


def _sync_key(title: object, ext: object, size: object) -> str:
    return f"{str(title).strip().lower()}|{str(ext).strip().lower()}|{size}"


def _resolve_target_book(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    source_book_id: int,
    rows: list[sqlite3.Row],
    full_keys: dict[str, int],
    book_id_map: dict[int, tuple[int, str]],
) -> tuple[int, str]:
    if source_book_id in book_id_map:
        return book_id_map[source_book_id]

    for row in rows:
        key = _sync_key(row["title"], row["format"], row["uncompressed_size"])
        target_book_id = full_keys.get(key)
        if target_book_id is not None:
            result = (target_book_id, "already_synced")
            book_id_map[source_book_id] = result
            return result

    target_book_id = _copy_book_row(src, dst, source_book_id)
    result = (target_book_id, "inserted")
    book_id_map[source_book_id] = result
    return result


def _copy_book_row(src: sqlite3.Connection, dst: sqlite3.Connection, book_id: object) -> int:
    src_row = src.execute(SQL["select_book_by_id"], (book_id,)).fetchone()
    columns = [info[1] for info in src.execute(SQL["table_info_books"]).fetchall() if info[1] != "id"]
    values = [src_row[column] for column in columns]
    placeholders = ", ".join("?" for _ in columns)
    dst.execute(f"INSERT INTO books ({', '.join(columns)}) VALUES ({placeholders})", values)
    return int(dst.execute(SQL["last_insert_rowid"]).fetchone()[0])


def _sync_data_row(src: sqlite3.Connection, dst: sqlite3.Connection, data_id: object, new_book_id: int) -> tuple[str, str, str]:
    src_row = src.execute(SQL["select_data_by_id"], (data_id,)).fetchone()
    existing = dst.execute(
        "SELECT * FROM data WHERE book = ? AND format = ?",
        (new_book_id, src_row["format"]),
    ).fetchone()
    if existing:
        if existing["uncompressed_size"] == src_row["uncompressed_size"] and existing["name"] == src_row["name"]:
            return str(existing["id"]), "already_synced", ""
        return str(existing["id"]), "conflict", "目标库已存在同格式 data，但 name 或 size 不一致"

    columns = [info[1] for info in src.execute(SQL["table_info_data"]).fetchall() if info[1] != "id"]
    values = [new_book_id if column == "book" else src_row[column] for column in columns]
    placeholders = ", ".join("?" for _ in columns)
    dst.execute(f"INSERT INTO data ({', '.join(columns)}) VALUES ({placeholders})", values)
    return str(dst.execute(SQL["last_insert_rowid"]).fetchone()[0]), "inserted", ""


def _copy_entity_file(incremental_dir: Path, full_library_dir: Path, relative: str) -> tuple[str, str]:
    source = incremental_dir / relative
    target = full_library_dir / relative
    if not source.exists():
        return "file_missing", "增量实体文件不存在"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size == source.stat().st_size:
        return "already_synced", ""
    shutil.copy2(source, target)
    return "synced", ""


def _sync_book_metadata(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    source_book_id: int,
    target_book_id: int,
    conflict_policy: str,
) -> tuple[str, str]:
    details: list[str] = []
    conflicts = 0
    changed = 0

    for spec in STEP5_CONFIG["metadata_links"]["multi_dictionary"]:
        table_changed, table_conflicts, detail = _sync_multi_dictionary_link(
            src,
            dst,
            source_book_id,
            target_book_id,
            spec["dictionary_table"],
            spec["key_column"],
            spec["link_table"],
            spec["link_column"],
        )
        changed += table_changed
        conflicts += table_conflicts
        details.append(detail)

    for spec in STEP5_CONFIG["metadata_links"]["single_dictionary"]:
        table_changed, table_conflicts, detail = _sync_single_dictionary_link(
            src,
            dst,
            source_book_id,
            target_book_id,
            spec["dictionary_table"],
            spec["key_column"],
            spec["link_table"],
            spec["link_column"],
            conflict_policy,
        )
        changed += table_changed
        conflicts += table_conflicts
        details.append(detail)

    table_changed, table_conflicts, detail = _sync_comments(
        src,
        dst,
        source_book_id,
        target_book_id,
        conflict_policy,
    )
    changed += table_changed
    conflicts += table_conflicts
    details.append(detail)

    table_changed, table_conflicts, detail = _sync_identifiers(
        src,
        dst,
        source_book_id,
        target_book_id,
        conflict_policy,
    )
    changed += table_changed
    conflicts += table_conflicts
    details.append(detail)

    if conflicts:
        status = "conflict"
    elif changed:
        status = "updated"
    else:
        status = "already_synced"
    return status, " | ".join(details)


def _sync_multi_dictionary_link(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    source_book_id: int,
    target_book_id: int,
    dictionary_table: str,
    key_column: str,
    link_table: str,
    link_column: str,
) -> tuple[int, int, str]:
    changed = 0
    source_links = src.execute(
        f"SELECT * FROM {link_table} WHERE book = ?",
        (source_book_id,),
    ).fetchall()
    for source_link in source_links:
        source_item = src.execute(
            f"SELECT * FROM {dictionary_table} WHERE id = ?",
            (source_link[link_column],),
        ).fetchone()
        if source_item is None:
            continue
        target_item_id, inserted = _resolve_dictionary_row(dst, dictionary_table, key_column, source_item)
        changed += int(inserted)
        existing = dst.execute(
            f"SELECT id FROM {link_table} WHERE book = ? AND {link_column} = ?",
            (target_book_id, target_item_id),
        ).fetchone()
        if existing:
            continue
        _insert_link_row(src, dst, link_table, source_link, target_book_id, link_column, target_item_id)
        changed += 1
    return changed, 0, f"{dictionary_table}:{len(source_links)}"


def _sync_single_dictionary_link(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    source_book_id: int,
    target_book_id: int,
    dictionary_table: str,
    key_column: str,
    link_table: str,
    link_column: str,
    conflict_policy: str,
) -> tuple[int, int, str]:
    changed = 0
    conflicts = 0
    source_link = src.execute(
        f"SELECT * FROM {link_table} WHERE book = ?",
        (source_book_id,),
    ).fetchone()
    if source_link is None:
        return 0, 0, f"{dictionary_table}:0"

    source_item = src.execute(
        f"SELECT * FROM {dictionary_table} WHERE id = ?",
        (source_link[link_column],),
    ).fetchone()
    if source_item is None:
        return 0, 0, f"{dictionary_table}:0"

    target_item_id, inserted = _resolve_dictionary_row(dst, dictionary_table, key_column, source_item)
    changed += int(inserted)
    existing = dst.execute(
        f"SELECT * FROM {link_table} WHERE book = ?",
        (target_book_id,),
    ).fetchone()
    if existing is None:
        _insert_link_row(src, dst, link_table, source_link, target_book_id, link_column, target_item_id)
        return changed + 1, 0, f"{dictionary_table}:inserted"
    if existing[link_column] == target_item_id:
        return changed, 0, f"{dictionary_table}:already_synced"
    if conflict_policy == STEP5_CONFIG["metadata_conflict"]["overwrite"]:
        dst.execute(
            f"UPDATE {link_table} SET {link_column} = ? WHERE id = ?",
            (target_item_id, existing["id"]),
        )
        return changed + 1, 0, f"{dictionary_table}:overwritten"
    conflicts += 1
    return changed, conflicts, f"{dictionary_table}:conflict"


def _sync_comments(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    source_book_id: int,
    target_book_id: int,
    conflict_policy: str,
) -> tuple[int, int, str]:
    source_row = src.execute("SELECT * FROM comments WHERE book = ?", (source_book_id,)).fetchone()
    if source_row is None:
        return 0, 0, "comments:0"

    target_row = dst.execute("SELECT * FROM comments WHERE book = ?", (target_book_id,)).fetchone()
    if target_row is None:
        _insert_book_child_row(src, dst, "comments", source_row, target_book_id)
        return 1, 0, "comments:inserted"
    if target_row["text"] == source_row["text"]:
        return 0, 0, "comments:already_synced"
    if conflict_policy == STEP5_CONFIG["metadata_conflict"]["overwrite"]:
        dst.execute("UPDATE comments SET text = ? WHERE id = ?", (source_row["text"], target_row["id"]))
        return 1, 0, "comments:overwritten"
    if conflict_policy == STEP5_CONFIG["metadata_conflict"]["fill_missing"] and not str(target_row["text"] or "").strip():
        dst.execute("UPDATE comments SET text = ? WHERE id = ?", (source_row["text"], target_row["id"]))
        return 1, 0, "comments:filled_missing"
    return 0, 1, "comments:conflict"


def _sync_identifiers(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    source_book_id: int,
    target_book_id: int,
    conflict_policy: str,
) -> tuple[int, int, str]:
    changed = 0
    conflicts = 0
    source_rows = src.execute("SELECT * FROM identifiers WHERE book = ?", (source_book_id,)).fetchall()
    for source_row in source_rows:
        target_row = dst.execute(
            "SELECT * FROM identifiers WHERE book = ? AND type = ?",
            (target_book_id, source_row["type"]),
        ).fetchone()
        if target_row is None:
            _insert_book_child_row(src, dst, "identifiers", source_row, target_book_id)
            changed += 1
            continue
        if target_row["val"] == source_row["val"]:
            continue
        if conflict_policy == STEP5_CONFIG["metadata_conflict"]["overwrite"]:
            dst.execute("UPDATE identifiers SET val = ? WHERE id = ?", (source_row["val"], target_row["id"]))
            changed += 1
            continue
        conflicts += 1
    if conflicts:
        return changed, conflicts, f"identifiers:conflict({conflicts})"
    return changed, 0, f"identifiers:{len(source_rows)}"


def _resolve_dictionary_row(
    dst: sqlite3.Connection,
    table: str,
    key_column: str,
    source_row: sqlite3.Row,
) -> tuple[int, bool]:
    existing = dst.execute(
        f"SELECT id FROM {table} WHERE {key_column} = ? COLLATE NOCASE",
        (source_row[key_column],),
    ).fetchone()
    if existing:
        return int(existing["id"]), False

    columns = _columns_without_id(dst, table)
    values = [source_row[column] for column in columns]
    placeholders = ", ".join("?" for _ in columns)
    dst.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", values)
    return int(dst.execute(SQL["last_insert_rowid"]).fetchone()[0]), True


def _insert_link_row(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    source_row: sqlite3.Row,
    target_book_id: int,
    link_column: str,
    target_item_id: int,
) -> None:
    columns = [column for column in _columns_without_id(src, table)]
    values = [
        target_book_id if column == "book" else target_item_id if column == link_column else source_row[column]
        for column in columns
    ]
    placeholders = ", ".join("?" for _ in columns)
    dst.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", values)


def _insert_book_child_row(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    source_row: sqlite3.Row,
    target_book_id: int,
) -> None:
    columns = [column for column in _columns_without_id(src, table)]
    values = [target_book_id if column == "book" else source_row[column] for column in columns]
    placeholders = ", ".join("?" for _ in columns)
    dst.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", values)


def _columns_without_id(conn: sqlite3.Connection, table: str) -> list[str]:
    return [info[1] for info in conn.execute(f"PRAGMA table_info({table})").fetchall() if info[1] != "id"]


def _sync_report(
    book_id: object,
    target_book_id: object,
    data_id: object,
    target_data_id: object,
    title: object,
    ext: object,
    size: object,
    relative: str,
    book_status: str,
    status: str,
    verification: str,
    metadata_status: str,
    metadata_details: str,
    message: str,
) -> dict[str, str]:
    return {
        "book_id": str(book_id),
        "target_book_id": str(target_book_id),
        "data_id": str(data_id),
        "target_data_id": str(target_data_id),
        "title": str(title),
        "ext": str(ext).lower(),
        "bytes": str(size),
        "relative_path": relative,
        "book_status": book_status,
        "sync_status": status,
        "verification_status": verification,
        "metadata_status": metadata_status,
        "metadata_details": metadata_details,
        "error_message": message,
    }


def _status_counts(reports: list[dict[str, str]]) -> str:
    counts: dict[str, int] = {}
    for row in reports:
        counts[row["sync_status"]] = counts.get(row["sync_status"], 0) + 1
    return ", ".join(f"{key}({value})" for key, value in sorted(counts.items())) or "无"


def _main() -> None:
    parser = argparse.ArgumentParser(description="Step5 compare：比对清洗好的增量预处理库与全量预处理库，输出差异 CSV")
    batch = default_batch()
    parser.add_argument("--batch", default=batch)
    parser.add_argument("--incremental-dir", type=Path, default=default_input_dir())
    parser.add_argument("--full-library-dir", type=Path, default=default_full_db_dir())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=default_log_dir())
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--metadata-conflict", choices=METADATA_CONFLICT_POLICIES, default=STEP5_CONFIG["metadata_conflict"]["default"])
    args = parser.parse_args()

    setup_logger(log_dir=str(args.log_dir))
    run_step5_compare(
        args.batch,
        args.incremental_dir,
        args.full_library_dir,
        args.output_dir or default_output_dir(args.batch),
        args.force,
        args.metadata_conflict,
    )


if __name__ == "__main__":
    _main()
