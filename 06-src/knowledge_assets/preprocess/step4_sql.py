"""Step4 sql: generate SQL and batch cleaning instructions from confirmed CSV files."""

from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import sys
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
    read_csv_dicts,
    resolve_incremental_db,
    step_dir,
)
from knowledge_assets.utils.logger import get_logger, setup_logger

_RULES_PATH = _SRC_DIR / "template" / "step1_rules.json"
_CONFIG_PATH = _SRC_DIR / "template" / "step2_config.json"


def run_step4_sql(batch: str, output_dir: Path, incremental_dir: Path | None = None, metadata_db: Path | None = None) -> None:
    logger = get_logger("preprocess.step4")
    if metadata_db is None:
        metadata_db = resolve_incremental_db(
            batch, incremental_dir, output_dir, include_step2_repair=True, include_step2_apply=False
        )
    resolved_batch = batch_from_metadata_path(metadata_db)
    if resolved_batch is not None and output_dir.name == batch:
        output_dir = output_dir.parent / resolved_batch
        batch = resolved_batch
    step3_dir = step_dir(output_dir, "step3")
    step4_dir = step_dir(output_dir, "step4")
    step4_dir.mkdir(parents=True, exist_ok=True)

    logger.info("========== 开始【Step4 sql：根据确认 CSV 生成自清洗 SQL + BAT】 ==========")
    logger.info("读取增量 metadata.db：%s", display_path(metadata_db))
    if not metadata_db.exists():
        raise FileNotFoundError(f"metadata.db 不存在： {metadata_db}")

    config = _load_config()
    sql_path = step4_dir / config["outputs"]["sql_filename"]
    bat_path = step4_dir / config["outputs"]["bat_filename"]
    _remove_legacy_outputs(step4_dir, config)

    rules = _load_rules()
    rows: list[dict[str, str]] = []
    for source, specs in (
        ("[系统确认]", rules["auto_delete_outputs"]),
        ("[人工确认]", rules["manual_review_outputs"]),
    ):
        for spec in specs:
            path = step3_dir / spec["filename"]
            records = read_csv_dicts(path)
            logger.info(
                "读取 %s 输入：%s | %s",
                source,
                path.name,
                _format_action_counts_short(_action_counts(records, config), config),
            )
            for record in records:
                record["_source"] = source
                rows.append(record)

    rows.sort(key=lambda row: (int(row.get("book_id") or 0), int(row.get("data_id") or 0), row.get("reason_code", "")))

    sql_lines, sql_stats = _build_sql(batch, rows, metadata_db, config)
    bat_lines = _build_bat(batch, rows, config)
    rollback_lines = _build_rollback_bat(batch, rows, config)
    sql_path.write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    bat_path.write_text("\r\n".join(bat_lines) + "\r\n", encoding="utf-8")
    rollback_path = step4_dir / config["outputs"]["rollback_bat_filename"]
    rollback_path.write_text("\r\n".join(rollback_lines) + "\r\n", encoding="utf-8")

    action_counts = _action_counts(rows, config)
    sql_delete_total = sql_stats["delete_data_count"] + sql_stats["delete_book_count"]
    bat_move_count = action_counts.get("delete_file", 0)
    bat_noop_count = sum(count for action, count in action_counts.items() if action != "delete_file")
    rollback_book_count = len({
        row.get("book_id") for row in rows if _action_for(row, config) == "delete_file" and row.get("relative_path")
    })
    logger.info("recommended_action 统计：%s", _format_counts(action_counts))
    logger.info(
        "SQL 删除计划：DELETE  %s (data)/  %s (books) | 保留 book %s 个",
        sql_stats["delete_data_count"],
        sql_stats["delete_book_count"],
        sql_stats["partial_delete_book_count"],
    )
    logger.info(
        "输出 SQL 清洗指令：%s | DELETE %s (data) / DELETE %s (books) ",
        display_path(sql_path),
        sql_stats["delete_data_count"],
        sql_stats["delete_book_count"],
    )
    logger.info(
        "输出 BAT 清洗指令：%s | %s ",
        display_path(bat_path),
        _format_action_counts_short(action_counts, config),
    )
    logger.info(
        "输出 BAT 回退指令：%s | 回退 %s 个 book 目录",
        display_path(rollback_path),
        rollback_book_count,
    )
    logger.info("动作含义：%s", _format_action_descriptions(config))
    logger.info("========== 完成【Step4 sql：根据确认 CSV 生成自清洗 SQL + BAT】 ==========")


def _load_rules() -> dict[str, object]:
    with _RULES_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_config() -> dict[str, object]:
    with _CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _remove_legacy_outputs(step4_dir: Path, config: dict[str, object]) -> None:
    for filename in config["outputs"]["legacy_filenames"]:
        path = step4_dir / filename
        if path.exists():
            path.unlink()


def _build_sql(
    batch: str,
    rows: list[dict[str, str]],
    metadata_db: Path,
    config: dict[str, object],
) -> tuple[list[str], dict[str, int]]:
    book_data_totals = _book_data_totals(metadata_db)
    data_delete_ids_by_book = _data_delete_ids_by_book(rows, config)
    book_delete_ids = {
        book_id
        for book_id, data_ids in data_delete_ids_by_book.items()
        if book_data_totals.get(book_id, 0) == len(data_ids)
    }
    pre_existing_empty = _pre_existing_empty_books(metadata_db)
    book_delete_ids |= pre_existing_empty
    partial_delete_book_ids = set(data_delete_ids_by_book) - book_delete_ids
    lines = [
        "-- Auto-generated by Step2 plan. Review before running Step2 apply.",
        f"-- batch: {batch}",
        "BEGIN TRANSACTION;",
    ]
    for row in rows:
        action = _action_for(row, config)
        data_id = row.get("data_id", "")
        book_id = row.get("book_id", "")
        reason = row.get("reason_code", "")
        if action == "delete_file":
            lines.extend(
                [
                    f"-- action=delete_file reason={reason} book_id={book_id} data_id={data_id}",
                    f"DELETE FROM data WHERE id = {int(data_id)};",
                ]
            )
        elif action == "update_metadata":
            title = row.get("new_title", "")
            basename = row.get("new_file_basename", "")
            lines.append(f"-- action=update_metadata reason={reason} book_id={book_id} data_id={data_id}")
            if title:
                lines.append(f"UPDATE books SET title = {_sql_quote(title)} WHERE id = {int(book_id)};")
            if basename:
                lines.append(f"UPDATE data SET name = {_sql_quote(basename)} WHERE id = {int(data_id)};")
            if not title and not basename:
                lines.append("-- skipped update_metadata: no new_title/new_file_basename provided")
        else:
            lines.append(f"-- action={action} reason={reason} book_id={book_id} data_id={data_id}; no SQL operation")
    lines.append("-- delete empty books confirmed by metadata.db")
    for book_id in sorted(book_delete_ids):
        lines.extend(
            [
                f"-- action=delete_book book_id={book_id}; all data rows are marked delete_file",
                f"DELETE FROM books WHERE id = {book_id} AND NOT EXISTS (SELECT 1 FROM data WHERE book = {book_id});",
            ]
        )
    link_tables = _link_tables_with_book_column(metadata_db)
    for table in link_tables:
        if table == "data":
            continue
        lines.append(f"-- cleanup orphan records in {table}")
        lines.append(f'DELETE FROM "{table}" WHERE book NOT IN (SELECT id FROM books);')
    lines.append("COMMIT;")
    return lines, {
        "delete_data_count": sum(len(data_ids) for data_ids in data_delete_ids_by_book.values()),
        "delete_book_count": len(book_delete_ids),
        "partial_delete_book_count": len(partial_delete_book_ids),
    }


def _book_data_totals(metadata_db: Path) -> dict[int, int]:
    with sqlite3.connect(metadata_db) as conn:
        rows = conn.execute("SELECT book, COUNT(*) FROM data GROUP BY book").fetchall()
    return {int(book_id): int(count) for book_id, count in rows}


def _pre_existing_empty_books(metadata_db: Path) -> set[int]:
    with sqlite3.connect(metadata_db) as conn:
        rows = conn.execute("SELECT id FROM books WHERE id NOT IN (SELECT DISTINCT book FROM data)").fetchall()
    return {int(r[0]) for r in rows}


def _link_tables_with_book_column(metadata_db: Path) -> list[str]:
    with sqlite3.connect(metadata_db) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT m.name FROM sqlite_master m "
                "JOIN pragma_table_info(m.name) i "
                "WHERE m.type='table' AND i.name='book' "
                "GROUP BY m.name"
            ).fetchall()
        ]


def _data_delete_ids_by_book(rows: list[dict[str, str]], config: dict[str, object]) -> dict[int, set[int]]:
    grouped: dict[int, set[int]] = {}
    for row in rows:
        if _action_for(row, config) != "delete_file":
            continue
        book_id = int(row.get("book_id") or 0)
        data_id = int(row.get("data_id") or 0)
        grouped.setdefault(book_id, set()).add(data_id)
    return grouped


def _build_bat(batch: str, rows: list[dict[str, str]], config: dict[str, object]) -> list[str]:
    bat_config = config["bat"]
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "REM Auto-generated by Step4 sql. Review before running on Windows.",
        f"REM batch: {batch}",
        "setlocal enabledelayedexpansion",
        "",
        f'if not defined LIBRARY_DIR set "LIBRARY_DIR={bat_config["library_dir_default"]}"',
        f'if not defined QUARANTINE_DIR set "QUARANTINE_DIR={bat_config["quarantine_dir_template"].format(batch=batch)}"',
        "",
        'if not exist "%QUARANTINE_DIR%" mkdir "%QUARANTINE_DIR%"',
        "",
    ]
    
    book_dirs_processed = {}
    for row in rows:
        action = _action_for(row, config)
        relative = row.get("relative_path", "")
        book_id = row.get("book_id", "")
        data_id = row.get("data_id", "")
        
        if action == "delete_file" and relative:
            book_dir = str(Path(relative).parent).replace("\\", "/")
            if book_dir not in book_dirs_processed:
                book_dirs_processed[book_dir] = book_id
                escaped_book_dir = _bat_quote(book_dir)
                lines.extend([
                    f"REM action=delete_book book_id={book_id}",
                    f'set "book_rel={escaped_book_dir}"',
                    f'set "src=%LIBRARY_DIR%\\%book_rel%"',
                    f'set "dst=%QUARANTINE_DIR%\\%book_rel%"',
                    f'echo [{book_id}] 移动：!src!',
                    f'echo      到：!dst!',
                    'call :do_move_book "!src!" "!dst!"',
                    "",
                ])
        else:
            lines.append(f"REM action={action} book_id={book_id} data_id={data_id}; no file operation")

    lines.extend([
        "goto :eof",
        "",
        ":do_move_book",
        "if not exist %1 goto :eof",
        "call :ensure_dir %2",
        "move /Y %1 %2",
        "call :cleanup_empty_parent %1",
        "goto :eof",
        "",
        ":ensure_dir",
        "set \"dirpath=%~dp1\"",
        "if not exist \"%dirpath%\" mkdir \"%dirpath%\"",
        "goto :eof",
        "",
        ":cleanup_empty_parent",
        "set \"filepath=%~dp1\"",
        "for /f \"delims=\" %%a in (\"%filepath:~0,-1%\") do set \"parent=%%~dpa\"",
        "dir /b \"%parent%\" 2>nul | find /c /v \"\" >nul",
        "if errorlevel 1 rmdir \"%parent%\" 2>nul",
        "goto :eof",
    ])
    return lines


def _build_rollback_bat(batch: str, rows: list[dict[str, str]], config: dict[str, object]) -> list[str]:
    bat_config = config["bat"]
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "REM Auto-generated by Step4 sql. Rollback: move books from quarantine back to library.",
        f"REM batch: {batch}",
        "setlocal enabledelayedexpansion",
        "",
        f'if not defined LIBRARY_DIR set "LIBRARY_DIR={bat_config["library_dir_default"]}"',
        f'if not defined QUARANTINE_DIR set "QUARANTINE_DIR={bat_config["quarantine_dir_template"].format(batch=batch)}"',
        "",
        'if not exist "%QUARANTINE_DIR%" (',
        '    echo QUARANTINE_DIR does not exist: %QUARANTINE_DIR%',
        "    echo Nothing to rollback.",
        "    goto :eof",
        ")",
        "",
    ]

    book_dirs_seen: dict[str, str] = {}
    for row in rows:
        if _action_for(row, config) != "delete_file":
            continue
        relative = row.get("relative_path", "")
        if not relative:
            continue
        book_dir = str(Path(relative).parent).replace("\\", "/")
        if book_dir in book_dirs_seen:
            continue
        book_dirs_seen[book_dir] = row.get("book_id", "")
        book_id = row.get("book_id", "")
        escaped_book_dir = _bat_quote(book_dir)
        lines.extend([
            f"REM rollback book_id={book_id}: quarantine → library",
            f'set "book_rel={escaped_book_dir}"',
            f'set "src=%QUARANTINE_DIR%\\%book_rel%"',
            f'set "dst=%LIBRARY_DIR%\\%book_rel%"',
            f'echo [{book_id}] 回退：!src!',
            f'echo       到：!dst!',
            'call :do_rollback_book "!src!" "!dst!"',
            "",
        ])

    lines.extend([
        "echo Rollback complete.",
        "goto :eof",
        "",
        ":do_rollback_book",
        "if not exist %1 (",
        "    echo SKIP: source not found %1",
        "    goto :eof",
        ")",
        "call :ensure_dir %2",
        "move /Y %1 %2",
        "call :cleanup_empty_quarantine_parent %1",
        "goto :eof",
        "",
        ":ensure_dir",
        "set \"dirpath=%~dp1\"",
        "if not exist \"%dirpath%\" mkdir \"%dirpath%\"",
        "goto :eof",
        "",
        ":cleanup_empty_quarantine_parent",
        "set \"filepath=%~dp1\"",
        "for /f \"delims=\" %%a in (\"%filepath:~0,-1%\") do set \"parent=%%~dpa\"",
        "dir /b \"%parent%\" 2>nul | find /c /v \"\" >nul",
        "if errorlevel 1 rmdir \"%parent%\" 2>nul",
        "goto :eof",
    ])
    return lines


def _bat_quote(path: str) -> str:
    """Quote a path for Windows batch, escaping special characters."""
    special_chars = set('&|<>^%')
    if any(c in special_chars for c in path):
        path = path.replace('%', '%%')
        path = path.replace('^', '^^')
        path = path.replace('&', '^&')
        path = path.replace('|', '^|')
        path = path.replace('<', '^<')
        path = path.replace('>', '^>')
    return path


def _action_for(row: dict[str, str], config: dict[str, object]) -> str:
    action = (row.get("recommended_action") or "").strip()
    if action in set(config["actions"]["valid"]):
        return action
    return str(config["actions"]["default"])


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _action_counts(rows: list[dict[str, str]], config: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        action = _action_for(row, config)
        counts[action] = counts.get(action, 0) + 1
    return counts


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}({value})" for key, value in sorted(counts.items())) or "无"


def _format_action_counts_short(counts: dict[str, int], config: dict[str, object]) -> str:
    labels = config["actions"]["short_labels"]
    order = config["actions"]["display_order"]
    parts = [
        f"{labels[action]}({counts[action]})"
        for action in order
        if counts.get(action, 0)
    ]
    parts.extend(
        f"{action}({count})"
        for action, count in sorted(counts.items())
        if action not in labels and count
    )
    return " / ".join(parts) or "无"


def _format_action_descriptions(config: dict[str, object]) -> str:
    labels = config["actions"]["short_labels"]
    descriptions = config["actions"].get("descriptions", {})
    order = config["actions"]["display_order"]
    parts = [
        f"{labels.get(action, action)}={descriptions[action]}"
        for action in order
        if descriptions.get(action)
    ]
    return "；".join(parts) or "未配置"


def _main() -> None:
    parser = argparse.ArgumentParser(description="Step4 sql：根据确认 CSV 生成自清洗 SQL + BAT")
    batch = default_batch()
    parser.add_argument("--batch", default=batch)
    parser.add_argument("--incremental-dir", type=Path, default=default_input_dir())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=default_log_dir())
    args = parser.parse_args()

    setup_logger(log_dir=str(args.log_dir))
    run_step4_sql(batch=args.batch, output_dir=args.output_dir or default_output_dir(args.batch), incremental_dir=args.incremental_dir)


if __name__ == "__main__":
    _main()
