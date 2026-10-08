"""Step4 apply: execute confirmed self-cleaning instructions and verify outcomes."""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[2]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from knowledge_assets.preprocess.common import (
    batch_from_metadata_path,
    default_batch,
    default_input_dir,
    default_log_dir,
    default_output_dir,
    display_path,
    load_json_config,
    pipeline_config,
    resolve_incremental_db,
    step_dir,
)
from knowledge_assets.utils.logger import get_logger, setup_logger

DELETE_DATA_RE = re.compile(r"^\s*DELETE\s+FROM\s+data\s+WHERE\s+id\s*=\s*(\d+)\s*;\s*$", re.IGNORECASE)
DELETE_BOOK_RE = re.compile(r"^\s*DELETE\s+FROM\s+books\s+WHERE\s+id\s*=\s*(\d+)\s+AND\s+NOT\s+EXISTS\b", re.IGNORECASE)


@dataclass(frozen=True)
class SqlPlan:
    data_delete_ids: tuple[int, ...]
    book_delete_ids: tuple[int, ...]


def run_step4_apply(batch: str, incremental_dir: Path, output_dir: Path, metadata_db: Path | None = None) -> None:
    logger = get_logger("preprocess.step4")
    if metadata_db is None:
        # 优先使用 merged.db（Step3 的产物），如果不存在则回退到 encoded.db
        step3_dir = step_dir(output_dir, "step3")
        merged_db = step3_dir / "metadata.merged.db"
        if merged_db.exists():
            metadata_db = merged_db
            logger.info(f"使用 Step3 合并后的数据库： {display_path(metadata_db)}")
        else:
            metadata_db = resolve_incremental_db(
                batch, incremental_dir, output_dir, include_step2_repair=True, include_step2_apply=False
            )
            logger.info(f"merged.db 不存在，回退到增量库： {display_path(metadata_db)}")
    resolved_batch = batch_from_metadata_path(metadata_db)
    if resolved_batch is not None and output_dir.name == batch:
        output_dir = output_dir.parent / resolved_batch
        batch = resolved_batch
    config = pipeline_config()
    step4_config = load_json_config("step2_config.json")
    step3_dir = step_dir(output_dir, "step3")
    step3_dir.mkdir(parents=True, exist_ok=True)
    cleaned_db = step3_dir / str(config["step3"]["cleaned_db_filename"])
    step4_dir = step_dir(output_dir, "step4")
    step4_dir.mkdir(parents=True, exist_ok=True)
    _remove_legacy_outputs(step4_dir, config)
    sql_path = step4_dir / str(step4_config["outputs"]["sql_filename"])
    bat_path = step4_dir / str(step4_config["outputs"]["bat_filename"])

    def emit(message: str) -> None:
        logger.info(message)

    emit("========== 开始【Step4 apply：执行自清洗 SQL 并验证】 ==========")
    emit(f"获取增量 metadata.db： {display_path(metadata_db)}")
    # emit(f"复制数据库副本用于清洗： {display_path(cleaned_db)}")
    # emit(f"读取 BAT 清洗指令（仅供 Windows 本地人工执行）： {display_path(bat_path)}")
    emit(f"读取清洗 SQL 指令： {display_path(sql_path)}")

    missing = [path for path in (bat_path, sql_path) if not path.exists()]
    if missing:
        for path in missing:
            emit(f"指令文件缺失： {display_path(path)}")
        emit("========== 中止【Step4 apply：执行自清洗 SQL 并验证】 ==========")
        return

    sql_text = sql_path.read_text(encoding="utf-8")
    plan = _parse_sql_plan(sql_text)
    emit(
        "SQL 预期删除统计："
        f"DELETE data  {len(plan.data_delete_ids)} 条 | "
        f"DELETE books {len(plan.book_delete_ids)} 条 "
    )

    if cleaned_db.exists():
        cleaned_db.unlink()
    shutil.copy2(metadata_db, cleaned_db)
    emit(f"复制数据库副本用于清洗： {display_path(cleaned_db)}")

    try:
        verification = _execute_and_verify_sql(sql_text, cleaned_db, plan)
    except Exception as exc:  # noqa: BLE001 - keep Step2 apply failure visible in unified log.
        emit(f"SQL 执行失败： {exc}")
        emit("========== 失败【Step4 apply：执行自清洗 SQL 并验证】 ==========")
        return

    emit(
        "data  删除验证："
        f" {verification['data_before']} (共) - {verification['actual_data_deleted']} (已删除) = {verification['data_after']} (剩余) | "
        f"尚未删除数{verification['remaining_data_ids']} 个"
    )
    emit(
        "books 删除验证："
        f" {verification['books_before']} (共) - {verification['actual_books_deleted']} (已删除) = {verification['books_after']} (剩余) | "
        # f"条件检查涉及 {verification['book_delete_candidates']} 个 book_id | "
        # f"预期实际可删除 {verification['expected_books_deleted']} 个 | "
        f"尚未删除数{verification['remaining_book_ids']} 个"
    )
    if verification["fully_executed"]:
        emit("【完整性验证：OK】：删除指令完整执行，增量库已完成清洗。" + display_path(cleaned_db))        
    else:
        emit("【完整性验证：NOK】：删除指令与数据库副本实际变更不一致。" + display_path(cleaned_db))
    # emit(f"清洗后数据库副本： {display_path(cleaned_db)}")
    emit("========== 完成【Step4 apply：执行自清洗 SQL 并验证】 ==========")


def _remove_legacy_outputs(step4_dir: Path, config: dict[str, object]) -> None:
    apply_config = config["step4"]["apply"]
    for filename in (apply_config["report_filename"], apply_config["log_filename"]):
        path = step4_dir / filename
        if path.exists():
            path.unlink()


def _parse_sql_plan(sql_text: str) -> SqlPlan:
    data_delete_ids: list[int] = []
    book_delete_ids: list[int] = []
    for line in sql_text.splitlines():
        data_match = DELETE_DATA_RE.match(line)
        if data_match:
            data_delete_ids.append(int(data_match.group(1)))
            continue
        book_match = DELETE_BOOK_RE.match(line)
        if book_match:
            book_delete_ids.append(int(book_match.group(1)))
    return SqlPlan(
        data_delete_ids=tuple(data_delete_ids),
        book_delete_ids=tuple(book_delete_ids),
    )


def _execute_and_verify_sql(sql_text: str, metadata_db: Path, plan: SqlPlan) -> dict[str, int | bool]:
    with sqlite3.connect(metadata_db) as conn:
        data_before = _table_count(conn, "data")
        books_before = _table_count(conn, "books")
        existing_data_ids = _existing_ids(conn, "data", plan.data_delete_ids)
        expected_books_deleted = _expected_book_deletes(conn, plan.book_delete_ids, existing_data_ids)

        conn.executescript(sql_text)
        conn.commit()

        data_after = _table_count(conn, "data")
        books_after = _table_count(conn, "books")
        remaining_data_ids = _existing_ids(conn, "data", plan.data_delete_ids)
        remaining_book_ids = _existing_ids(conn, "books", expected_books_deleted)

    actual_data_deleted = data_before - data_after
    actual_books_deleted = books_before - books_after
    expected_data_deleted_count = len(existing_data_ids)
    expected_books_deleted_count = len(expected_books_deleted)
    fully_executed = (
        actual_data_deleted == expected_data_deleted_count
        and actual_books_deleted == expected_books_deleted_count
        and len(remaining_data_ids) == 0
        and len(remaining_book_ids) == 0
    )
    return {
        "data_before": data_before,
        "data_after": data_after,
        "expected_data_deleted": expected_data_deleted_count,
        "actual_data_deleted": actual_data_deleted,
        "remaining_data_ids": len(remaining_data_ids),
        "books_before": books_before,
        "books_after": books_after,
        "book_delete_candidates": len(set(plan.book_delete_ids)),
        "expected_books_deleted": expected_books_deleted_count,
        "actual_books_deleted": actual_books_deleted,
        "remaining_book_ids": len(remaining_book_ids),
        "fully_executed": fully_executed,
    }


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _existing_ids(conn: sqlite3.Connection, table: str, ids: tuple[int, ...] | set[int]) -> set[int]:
    if not ids:
        return set()
    existing: set[int] = set()
    ordered_ids = sorted(ids)
    chunk_size = 900
    for index in range(0, len(ordered_ids), chunk_size):
        chunk = ordered_ids[index : index + chunk_size]
        placeholders = ", ".join("?" for _ in chunk)
        rows = conn.execute(f"SELECT id FROM {table} WHERE id IN ({placeholders})", chunk).fetchall()
        existing.update(int(row[0]) for row in rows)
    return existing


def _expected_book_deletes(
    conn: sqlite3.Connection,
    book_delete_ids: tuple[int, ...],
    existing_data_delete_ids: set[int],
) -> set[int]:
    if not book_delete_ids:
        return set()

    conn.execute("DROP TABLE IF EXISTS temp.step2_apply_expected_data_delete")
    conn.execute("DROP TABLE IF EXISTS temp.step2_apply_book_delete_candidate")
    conn.execute("CREATE TEMP TABLE step2_apply_expected_data_delete (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TEMP TABLE step2_apply_book_delete_candidate (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO step2_apply_expected_data_delete (id) VALUES (?)",
        ((data_id,) for data_id in existing_data_delete_ids),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO step2_apply_book_delete_candidate (id) VALUES (?)",
        ((book_id,) for book_id in book_delete_ids),
    )
    rows = conn.execute(
        """
        SELECT candidate.id
        FROM step2_apply_book_delete_candidate AS candidate
        JOIN books ON books.id = candidate.id
        WHERE NOT EXISTS (
            SELECT 1
            FROM data
            WHERE data.book = candidate.id
              AND NOT EXISTS (
                  SELECT 1
                  FROM step2_apply_expected_data_delete AS expected
                  WHERE expected.id = data.id
              )
        )
        """
    ).fetchall()
    return {int(row[0]) for row in rows}


def _main() -> None:
    parser = argparse.ArgumentParser(description="Step4 apply：执行自清洗 SQL 并验证")
    batch = default_batch()
    parser.add_argument("--batch", default=batch)
    parser.add_argument("--incremental-dir", type=Path, default=default_input_dir())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=default_log_dir())
    args = parser.parse_args()

    setup_logger(log_dir=str(args.log_dir))
    run_step4_apply(args.batch, args.incremental_dir, args.output_dir or default_output_dir(args.batch))


if __name__ == "__main__":
    _main()
