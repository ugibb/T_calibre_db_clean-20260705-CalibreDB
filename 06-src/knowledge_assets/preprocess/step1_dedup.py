"""Step1: 前置预处理（去重）。

为什么要有这一步
----------------
在 ``title+ext+bytes`` 三线查重被证明不可靠（``data.format`` 有 62.2% 与真实扩展名不符）后，
改用 ``title+bytes+initPath`` 三线查重。这一步在 Step2 之前执行，目的是：

1. 减少 Step2 阶段人工确认的工作量（重复记录提前剔除）
2. 使用更可靠的去重键：``initPath`` 来自 Windows 文件系统，不受 Calibre 导入编码错误影响

去重键
------
``normalize_title(title) + bytes + initPath``

- ``normalize_title``: 小写化、括号统一（（→(, ）→)）、空白折叠
- ``bytes``: ``data.uncompressed_size``（文件大小）
- ``initPath``: ``custom_column_1`` 聚合值（BookInitPath，来自 Windows 文件系统）

安全边界
--------
1. **绝不修改输入库。** 复制副本后再删除重复记录。
2. **只删除 data 记录与关联的 books 记录。** 若某 book 的所有 data 记录都被删除，
   才删除该 book；否则只删 data 记录。
3. **保留决策：** 每组重复中保留 ``data_id`` 最小的记录（稳定、可预测）。
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, fields
from pathlib import Path, PureWindowsPath

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
    display_path,
    load_json_config,
    pipeline_config,
    resolve_incremental_db,
    step_dir,
)
from knowledge_assets.preprocess.dedup_core import (
    AutoCandidate,
    DedupRecord,
    build_junk_title_candidates,
    build_non_target_candidates,
    group_duplicate_records,
    filter_junk_titles,
    filter_non_target_formats,
    make_title_bytes_initpath_key,
)
from knowledge_assets.utils.logger import get_logger, setup_logger


#: Calibre 打开库时注册的自定义 SQL 函数，维护触发器依赖它们
_CALIBRE_FUNCTION_MARKERS = ("title_sort(", "uuid4(")

#: SQL 标识符白名单
_IDENTIFIER_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def run_step1(
    batch: str,
    incremental_dir: Path,
    output_dir: Path,
) -> Path:
    """对增量库做前置预处理（去重 + 垃圾标题删除 + 非目标格式删除），输出去重后的库副本与 CSV 报告。"""
    logger = get_logger("preprocess.step1")

    source_db = resolve_incremental_db(batch, incremental_dir)
    batch, output_dir = align_batch_to_metadata_db(batch, source_db, output_dir)
    config = pipeline_config()
    step1_config = config.get("step1", {})
    step1_rules = load_json_config("step1_rules.json")
    target_formats = frozenset(str(f).lower() for f in step1_rules["target_formats"])
    junk_keywords = tuple(str(k) for k in step1_rules["junk_title_keywords"])
    out_dir = step_dir(output_dir, "step1")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not source_db.exists():
        raise FileNotFoundError(f"metadata.db 不存在： {source_db}")

    business_log: list[str] = []

    def emit(message: str, *args: object) -> None:
        text = message % args if args else message
        business_log.append(text)
        logger.info(text)

    emit("========== 开始【Step1：前置预处理（去重「title_bytes_initPath」 -> 垃圾标题 -> 非目标格式）】 ==========")
    emit(f"-----> 读取增量库： {display_path(source_db)}")
    source_books, source_data = db_table_counts(source_db)
    emit(f"输入库统计： books {source_books} | data {source_data}")
    emit("")

    records, ext_by_data_id = _load_dedup_records(source_db, step1_config, emit)

    # 1. 去重（先于过滤，确保垃圾/非目标只作用于存活记录）
    groups = group_duplicate_records(records, make_title_bytes_initpath_key)

    data_ids_to_delete: set[int] = set()
    book_ids_to_keep: set[int] = set()

    dup_candidates: list[AutoCandidate] = []
    for group in groups:
        for record in group.records:
            action = "keep" if record.data_id == group.keep_record.data_id else "delete"
            dup_candidates.append(
                AutoCandidate(
                    batch=batch,
                    book_id=record.book_id,
                    data_id=record.data_id,
                    title=record.title,
                    file_basename="",
                    ext=ext_by_data_id.get(record.data_id, ""),
                    bytes=record.bytes,
                    relative_path=record.relative_path,
                    reason_code="duplicate_title_bytes_initpath",
                    reason_detail="重复文件（title + bytes + initPath）",
                    matched_value=group.group_key,
                    recommended_action="delete_file" if action == "delete" else "keep",
                    custom_column_1_value=record.init_path,
                )
            )
            if action == "delete":
                data_ids_to_delete.add(record.data_id)
                book_ids_to_keep.add(group.keep_record.book_id)
            else:
                book_ids_to_keep.add(record.book_id)
    dup_candidates.sort(key=lambda c: (c.matched_value, c.recommended_action != "keep", c.book_id, c.data_id))

    # 去重后存活记录：每组 keep 的记录 + 未参与任何重复组的记录
    dup_deleted_ids = {c.data_id for c in dup_candidates if c.recommended_action != "keep"}
    surviving_records = [r for r in records if r.data_id not in dup_deleted_ids]

    # 2. 垃圾标题过滤（仅对去重后存活记录）
    junk_filtered = filter_junk_titles(surviving_records, junk_keywords)
    junk_data_ids = {f.record.data_id for f in junk_filtered}
    junk_candidates = build_junk_title_candidates(
        junk_filtered, batch=batch, ext_by_data_id=ext_by_data_id,
    )
    for c in junk_candidates:
        data_ids_to_delete.add(c.data_id)

    # 3. 非目标格式过滤（仅对去重+垃圾过滤后存活记录）
    after_junk = [r for r in surviving_records if r.data_id not in junk_data_ids]
    non_target_filtered = filter_non_target_formats(after_junk, target_formats, ext_by_data_id)
    non_target_candidates = build_non_target_candidates(
        non_target_filtered, batch=batch, ext_by_data_id=ext_by_data_id,
    )
    for c in non_target_candidates:
        data_ids_to_delete.add(c.data_id)

    book_ids_to_delete = {
        r.book_id for r in records if r.data_id in data_ids_to_delete
    } - book_ids_to_keep

    deduped_db = out_dir / str(step1_config.get("deduped_db_filename", "metadata-{date}.deduped.db")).format(
        date=batch.replace("-", "")
    )
    _copy_and_delete(source_db, deduped_db, data_ids_to_delete, book_ids_to_delete)

    junk_delete_count = len(junk_candidates)
    junk_update_count = 0
    junk_keep_count = 0
    junk_all_books = len({c.book_id for c in junk_candidates})
    junk_delete_books = junk_all_books
    junk_update_books = 0
    junk_keep_books = 0

    non_target_delete_count = len(non_target_candidates)
    non_target_update_count = 0
    non_target_keep_count = 0
    non_target_all_books = len({c.book_id for c in non_target_candidates})
    non_target_delete_books = non_target_all_books
    non_target_update_books = 0
    non_target_keep_books = 0

    junk_and_non_target_ids = junk_data_ids | {c.data_id for c in non_target_candidates}
    dup_candidates = [c for c in dup_candidates if c.data_id not in junk_and_non_target_ids]

    dup_delete_count = sum(1 for c in dup_candidates if c.recommended_action != "keep")
    dup_keep_count = sum(1 for c in dup_candidates if c.recommended_action == "keep")
    dup_update_count = len(dup_candidates) - dup_delete_count - dup_keep_count

    dup_all_books = len({c.book_id for c in dup_candidates})
    dup_delete_books = len({c.book_id for c in dup_candidates if c.recommended_action != "keep"})
    dup_keep_books = dup_all_books - dup_delete_books
    dup_update_books = 0

    emit("")
    emit(f"【步骤一：去重删除】：\t 总数 {dup_all_books}/{len(dup_candidates)} | 删除 {dup_delete_books}/{dup_delete_count} | 更新 {dup_update_books}/{dup_update_count} | 保留 {dup_keep_books}/{dup_keep_count}")
    emit(f"【步骤二：垃圾文件删除】：\t 总数 {junk_all_books}/{len(junk_candidates)} | 删除 {junk_delete_books}/{junk_delete_count} | 更新 {junk_update_books}/{junk_update_count} | 保留 {junk_keep_books}/{junk_keep_count}")
    emit(f"【步骤三：非目标格式删除】：总数 {non_target_all_books}/{len(non_target_candidates)} | 删除 {non_target_delete_books}/{non_target_delete_count} | 更新 {non_target_update_books}/{non_target_update_count} | 保留 {non_target_keep_books}/{non_target_keep_count}")

    title_auto, title_manual, basename_auto, basename_manual = _scan_title_basename_issues(records)
    title_total = title_auto + title_manual
    title_books = len({r.book_id for r in records if _contains_copy_marker(r.title)})
    basename_total = basename_auto + basename_manual
    basename_markers = _unknown_basename_markers()
    basename_books = len({
        r.book_id for r in records
        if r.relative_path and any(
            marker in PureWindowsPath(r.relative_path.replace("/", "\\")).name
            for marker in basename_markers
        )
    })
    emit(f"【步骤四：副本标题扫描】：\t 总数 {title_books}/{title_total} | 删除 0/0 | 更新 {title_books}/{title_auto} | 保留 0/{title_manual}")
    emit(f"【步骤五：未知标题扫描】：\t 总数 {basename_books}/{basename_total} | 删除 0/0 | 更新 {basename_books}/{basename_auto} | 保留 0/{basename_manual}")
    manual_total = title_manual + basename_manual
    emit("")
    emit("合计： 待删除 %s 本书（%s 条触发记录） | 待人工确认 %s 条",
         len(book_ids_to_delete), len(data_ids_to_delete), manual_total)
    emit("")

    merged_path = out_dir / "step1_all_data_merged.csv"
    _write_merged_csv(merged_path, records, dup_candidates, junk_candidates, non_target_candidates, batch, ext_by_data_id)
    emit(f"生成【全量合并】CSV：\t {display_path(merged_path)} | {len(records)} 条")
  
    emit("")  
    emit(f"-----> 输出预处理后的增量库：\t {display_path(deduped_db)}")
    deduped_books, deduped_data = db_table_counts(deduped_db)
    emit(f"输出库统计： books {deduped_books} | data {deduped_data}")
    emit(f"输入 → 输出： books {source_books} → {deduped_books} | data {source_data} → {deduped_data}")
    total_books = len({r.book_id for r in records})
    if book_ids_to_delete:
        emit(f"表 books 汇总：\t 共 {total_books} 条 -> 删除 {len(book_ids_to_delete)} 条 -> 剩余 {total_books - len(book_ids_to_delete)} 条")
    if data_ids_to_delete:
        emit(f"表 data 汇总：\t 共 {len(records)} 条 -> 删除 {len(data_ids_to_delete)} 条 -> 剩余 {len(records) - len(data_ids_to_delete)} 条")

    emit("========== 完成【Step1：前置预处理】 ==========")

    log_path = out_dir / str(step1_config.get("log_filename", "step1_dedup.log"))
    log_path.write_text("\n".join(business_log) + "\n", encoding="utf-8")

    return deduped_db


def _contains_copy_marker(value: str) -> bool:
    return "副本" in value or "Fu Ben" in value


def _unknown_basename_markers() -> tuple[str, ...]:
    rules = load_json_config("step1_rules.json")
    return tuple(str(m) for m in rules["unknown_basename_markers"])


def _book_init_candidate(
    init_paths: tuple[str, ...],
) -> tuple[str, str, str]:
    """从 BookInitPath 推导修复候选值，返回 (init_path, candidate, reason)。"""
    if not init_paths:
        return "", "", "missing_book_init_path"
    if len(init_paths) > 1:
        return "; ".join(init_paths), "", "ambiguous_book_init_path"
    path = init_paths[0]
    filename = PureWindowsPath(path).name.strip()
    candidate = PureWindowsPath(filename).stem.strip()
    suffix = PureWindowsPath(filename).suffix.removeprefix(".")
    if not candidate or not suffix:
        return path, "", "invalid_book_init_path"
    return path, candidate, ""


def _scan_title_basename_issues(
    records: list[DedupRecord],
) -> tuple[int, int, int, int]:
    """扫描副本标题和未知文件名，返回 (title_auto, title_manual, basename_auto, basename_manual)。"""
    markers = _unknown_basename_markers()
    init_paths_by_book: dict[int, tuple[str, ...]] = {}
    for r in records:
        if r.book_id not in init_paths_by_book and r.init_path:
            init_paths_by_book[r.book_id] = (r.init_path,)

    title_auto = 0
    title_manual = 0
    seen_books: set[int] = set()
    for r in records:
        if r.book_id in seen_books:
            continue
        seen_books.add(r.book_id)
        if not _contains_copy_marker(r.title):
            continue
        _path, candidate, reason = _book_init_candidate(
            init_paths_by_book.get(r.book_id, ()),
        )
        if not reason and _contains_copy_marker(candidate):
            candidate = ""
            reason = "filename_still_copy_marker"
        if candidate:
            title_auto += 1
        else:
            title_manual += 1

    basename_auto = 0
    basename_manual = 0
    for r in records:
        if not r.relative_path:
            continue
        filename = PureWindowsPath(r.relative_path.replace("/", "\\")).name
        if not any(marker in filename for marker in markers):
            continue
        _path, candidate, reason = _book_init_candidate(
            init_paths_by_book.get(r.book_id, ()),
        )
        if candidate:
            basename_auto += 1
        else:
            basename_manual += 1

    return title_auto, title_manual, basename_auto, basename_manual


def _write_merged_csv(
    path: Path,
    records: list[DedupRecord],
    dup_candidates: list[AutoCandidate],
    junk_candidates: list[AutoCandidate],
    non_target_candidates: list[AutoCandidate],
    batch: str,
    ext_by_data_id: dict[int, str],
) -> None:
    """将三份 CSV 的记录与全部 data 记录合并为一份完整 CSV。"""
    flagged: dict[int, AutoCandidate] = {}
    for c in dup_candidates:
        flagged[c.data_id] = c
    for c in junk_candidates:
        flagged[c.data_id] = c
    for c in non_target_candidates:
        flagged[c.data_id] = c

    record_by_data_id = {r.data_id: r for r in records}

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[fld.name for fld in fields(AutoCandidate)])
        writer.writeheader()
        for record in records:
            if record.data_id in flagged:
                c = flagged[record.data_id]
                writer.writerow({
                    "batch": c.batch,
                    "book_id": c.book_id,
                    "data_id": c.data_id,
                    "title": c.title,
                    "file_basename": c.file_basename,
                    "ext": c.ext,
                    "bytes": c.bytes,
                    "relative_path": c.relative_path,
                    "reason_code": c.reason_code,
                    "reason_detail": c.reason_detail,
                    "matched_value": c.matched_value,
                    "recommended_action": c.recommended_action,
                    "custom_column_1_value": c.custom_column_1_value,
                })
            else:
                ext = ext_by_data_id.get(record.data_id, "")
                writer.writerow({
                    "batch": batch,
                    "book_id": record.book_id,
                    "data_id": record.data_id,
                    "title": record.title,
                    "file_basename": "",
                    "ext": ext,
                    "bytes": record.bytes,
                    "relative_path": record.relative_path,
                    "reason_code": "",
                    "reason_detail": "",
                    "matched_value": "",
                    "recommended_action": "keep",
                    "custom_column_1_value": record.init_path,
                })


def _load_dedup_records(
    source_db: Path,
    step1_config: dict[str, object],
    emit: callable,
) -> tuple[list[DedupRecord], dict[int, str]]:
    """从增量库加载所有待处理记录，返回 (records, ext_by_data_id)。"""
    book_init_path_config = step1_config.get("book_init_path", {})
    if not isinstance(book_init_path_config, dict):
        raise ValueError("step1.book_init_path 必须是对象")
    value_table = str(book_init_path_config.get("value_table", "custom_column_1"))
    link_table = str(book_init_path_config.get("link_table", "books_custom_column_1_link"))
    for identifier in (value_table, link_table):
        if not _IDENTIFIER_RE.match(identifier):
            raise ValueError(f"非法 BookInitPath 表名： {identifier}")

    with closing(sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row

        init_path_join = _build_init_path_join(conn, value_table, link_table)

        query = (
            "SELECT books.id AS book_id, books.title AS title, "
            "books.path AS book_path, data.name AS data_name, "
            "data.id AS data_id, data.format AS format, data.uncompressed_size AS bytes, "
            f"{init_path_join.value_expr} AS init_path "
            f"FROM books {init_path_join.join_clause} "
            "JOIN data ON data.book = books.id "
            "ORDER BY books.id, data.id"
        )
        rows = conn.execute(query).fetchall()

    records: list[DedupRecord] = []
    ext_by_data_id: dict[int, str] = {}
    for row in rows:
        ext = str(row["format"] or "").strip().lower()
        data_id = int(row["data_id"])
        book_path = str(row["book_path"] or "")
        data_name = str(row["data_name"] or "")
        if book_path and data_name and ext:
            relative_path = f"{book_path}/{data_name}.{ext}"
        elif book_path and data_name:
            relative_path = f"{book_path}/{data_name}"
        else:
            relative_path = ""
        records.append(
            DedupRecord(
                book_id=int(row["book_id"]),
                data_id=data_id,
                title=str(row["title"] or ""),
                bytes=int(row["bytes"]) if row["bytes"] is not None else 0,
                init_path=str(row["init_path"] or ""),
                relative_path=relative_path,
                sort_key=0,
            )
        )
        ext_by_data_id[data_id] = ext

    all_books_count = len({r.book_id for r in records})
    with_init_path = sum(1 for r in records if r.init_path)
    without_init_path = len(records) - with_init_path
    emit(f" 加载记录：表 books 共 {all_books_count} 条 | 表 data 共 {len(records)} 条 | 有 initPath 共 {with_init_path} 条 | 没有 initPath 共 {without_init_path} 条")
    return records, ext_by_data_id


@dataclass(frozen=True)
class _InitPathJoin:
    join_clause: str
    value_expr: str


def _build_init_path_join(
    conn: sqlite3.Connection,
    value_table: str,
    link_table: str,
) -> _InitPathJoin:
    """构建 custom_column_1 的 LEFT JOIN 子句。

    适配两种表结构：
    - 单值列：custom_column_1(id, book, value)
    - normalized 多值列：custom_column_1(id, value, link) + books_custom_column_1_link(book, value)
    """
    if not _table_exists(conn, value_table):
        return _InitPathJoin(join_clause="", value_expr="NULL")

    columns = _table_columns(conn, value_table)
    if {"book", "value"}.issubset(columns):
        return _InitPathJoin(
            join_clause=f"LEFT JOIN {value_table} ON {value_table}.book = books.id",
            value_expr=f"{value_table}.value",
        )
    if (
        {"id", "value"}.issubset(columns)
        and _table_exists(conn, link_table)
        and {"book", "value"}.issubset(_table_columns(conn, link_table))
    ):
        return _InitPathJoin(
            join_clause=(
                f"LEFT JOIN ("
                f"SELECT link.book AS book, GROUP_CONCAT(cc.value, '; ') AS value "
                f"FROM {link_table} AS link "
                f"JOIN {value_table} AS cc ON cc.id = link.value "
                f"GROUP BY link.book"
                f") AS cc_agg ON cc_agg.book = books.id"
            ),
            value_expr="cc_agg.value",
        )

    return _InitPathJoin(join_clause="", value_expr="NULL")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))


def _copy_and_delete(
    source: Path,
    destination: Path,
    data_ids_to_delete: set[int],
    book_ids_to_delete: set[int],
) -> None:
    """复制数据库并删除重复记录。"""
    if destination.exists():
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)

    if not data_ids_to_delete and not book_ids_to_delete:
        return

    with closing(sqlite3.connect(destination)) as conn:
        with conn:
            triggers = _calibre_managed_triggers(conn, {"books", "data"})
            for name, _sql in triggers:
                conn.execute(f"DROP TRIGGER {name}")

            if data_ids_to_delete:
                placeholders = ",".join("?" * len(data_ids_to_delete))
                conn.execute(f"DELETE FROM data WHERE id IN ({placeholders})", list(data_ids_to_delete))

            if book_ids_to_delete:
                placeholders = ",".join("?" * len(book_ids_to_delete))
                conn.execute(f"DELETE FROM books WHERE id IN ({placeholders})", list(book_ids_to_delete))
                from knowledge_assets.preprocess.common import cleanup_orphan_link_records
                cleanup_orphan_link_records(conn, book_ids_to_delete)

            for _name, sql in triggers:
                conn.execute(sql)


def _calibre_managed_triggers(
    conn: sqlite3.Connection, tables: set[str]
) -> list[tuple[str, str]]:
    """找出依赖 Calibre 运行时函数的触发器。"""
    if not tables:
        return []
    placeholders = ",".join("?" * len(tables))
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        f"WHERE type = 'trigger' AND tbl_name IN ({placeholders})",
        tuple(sorted(tables)),
    ).fetchall()
    found: list[tuple[str, str]] = []
    for name, sql in rows:
        if not sql or not any(marker in sql for marker in _CALIBRE_FUNCTION_MARKERS):
            continue
        if not _IDENTIFIER_RE.match(name):
            raise ValueError(f"非法触发器名： {name}")
        found.append((name, sql))
    return found


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Step1：前置预处理（去重 title+bytes+initPath）")
    parser.add_argument("--batch", default=default_batch())
    parser.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    parser.add_argument("--output-dir", default=None, type=Path)
    parser.add_argument("--log-dir", default=default_log_dir(), type=Path)
    args = parser.parse_args()

    output_dir = args.output_dir or default_output_dir(args.batch)
    setup_logger(log_dir=str(args.log_dir))
    run_step1(
        batch=args.batch,
        incremental_dir=args.incremental_dir,
        output_dir=output_dir,
    )


if __name__ == "__main__":
    _main()
