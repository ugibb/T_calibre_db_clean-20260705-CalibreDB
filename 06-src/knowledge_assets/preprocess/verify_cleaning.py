"""Step3 清洗验证脚本：验证 cleaned.db 完整性、删除正确性、CSV↔SQL↔DB 一致性。"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[2]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from knowledge_assets.preprocess.common import pipeline_config, step_dir

DELETE_DATA_RE = re.compile(r"^\s*DELETE\s+FROM\s+data\s+WHERE\s+id\s*=\s*(\d+)\s*;\s*$", re.IGNORECASE)
DELETE_BOOK_RE = re.compile(r"^\s*DELETE\s+FROM\s+books\s+WHERE\s+id\s*=\s*(\d+)\s+AND\s+NOT\s+EXISTS\b", re.IGNORECASE)

BASE_DIR = Path(__file__).resolve().parents[3]
BATCH = "2026-09-23"
OUTPUT_DIR = BASE_DIR / "04-output" / BATCH


def main() -> None:
    config = pipeline_config()
    step3_dir = step_dir(OUTPUT_DIR, "step3")
    step4_dir = step_dir(OUTPUT_DIR, "step4")

    merged_db = step3_dir / "metadata.merged.db"
    cleaned_db = step3_dir / str(config["step3"]["cleaned_db_filename"])
    sql_path = step4_dir / "step2_cleaning_instructions.sql"

    if not merged_db.exists():
        print(f"ERROR: merged.db 不存在: {merged_db}")
        return
    if not cleaned_db.exists():
        print(f"ERROR: cleaned.db 不存在: {cleaned_db}")
        return
    if not sql_path.exists():
        print(f"ERROR: SQL 不存在: {sql_path}")
        return

    print("========== Step3 清洗验证报告 ==========")
    print()

    ok = True
    ok &= layer1(cleaned_db)
    print()
    ok &= layer2(merged_db, cleaned_db, sql_path)
    print()
    ok &= layer3(step3_dir, sql_path, cleaned_db)
    print()

    print("第四层：物理文件一致性（需 Windows 侧执行）")
    print("  [跳过 — 需在 Windows 上跑 BAT 后验证]")
    print()

    if ok:
        print("========== 验证完成：全部通过 ==========")
    else:
        print("========== 验证完成：存在失败项 ==========")


def layer1(cleaned_db: Path) -> bool:
    print("第一层：cleaned.db 内部完整性")
    all_ok = True
    with sqlite3.connect(cleaned_db) as conn:
        orphan_data = conn.execute("SELECT COUNT(*) FROM data WHERE book NOT IN (SELECT id FROM books)").fetchone()[0]
        all_ok &= _check("孤儿 data", orphan_data, 0)

        empty_books = conn.execute("SELECT COUNT(*) FROM books WHERE id NOT IN (SELECT DISTINCT book FROM data)").fetchone()[0]
        all_ok &= _check("空 book", empty_books, 0)

        link_tables = conn.execute(
            "SELECT m.name FROM sqlite_master m "
            "JOIN pragma_table_info(m.name) i "
            "WHERE m.type='table' AND i.name='book' "
            "GROUP BY m.name"
        ).fetchall()
        link_table_names = [t[0] for t in link_tables if t[0] != "data"]
        total_orphans = 0
        for tname in link_table_names:
            orphans = conn.execute(f'SELECT COUNT(*) FROM "{tname}" WHERE book NOT IN (SELECT id FROM books)').fetchone()[0]
            total_orphans += orphans
        all_ok &= _check(f"孤儿 link（{len(link_table_names)} 张表）", total_orphans, 0)

        dupes = conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT book, format, COUNT(*) c FROM data GROUP BY book, format HAVING c > 1"
            ")"
        ).fetchone()[0]
        all_ok &= _check("重复 format", dupes, 0)

    return all_ok


def layer2(merged_db: Path, cleaned_db: Path, sql_path: Path) -> bool:
    print("第二层：cleaned.db 删除正确性")
    all_ok = True

    with sqlite3.connect(merged_db) as conn:
        merged_books = {r[0] for r in conn.execute("SELECT id FROM books").fetchall()}
        merged_data = {r[0] for r in conn.execute("SELECT id FROM data").fetchall()}

    with sqlite3.connect(cleaned_db) as conn:
        cleaned_books = {r[0] for r in conn.execute("SELECT id FROM books").fetchall()}
        cleaned_data = {r[0] for r in conn.execute("SELECT id FROM data").fetchall()}

    added_books = cleaned_books - merged_books
    added_data = cleaned_data - merged_data
    removed_books = merged_books - cleaned_books
    removed_data = merged_data - cleaned_data

    all_ok &= _check("merged books", len(merged_books), None, display=f"{len(merged_books)}")
    all_ok &= _check("cleaned books", len(cleaned_books), None, display=f"{len(cleaned_books)}")
    all_ok &= _check("删除 books", len(removed_books), None, display=f"{len(removed_books)}")
    all_ok &= _check("merged data", len(merged_data), None, display=f"{len(merged_data)}")
    all_ok &= _check("cleaned data", len(cleaned_data), None, display=f"{len(cleaned_data)}")
    all_ok &= _check("删除 data", len(removed_data), None, display=f"{len(removed_data)}")
    all_ok &= _check("意外新增 books", len(added_books), 0)
    all_ok &= _check("意外新增 data", len(added_data), 0)

    sql_text = sql_path.read_text(encoding="utf-8")
    sql_delete_data_ids = {int(m.group(1)) for line in sql_text.splitlines() if (m := DELETE_DATA_RE.match(line))}
    sql_delete_book_ids = {int(m.group(1)) for line in sql_text.splitlines() if (m := DELETE_BOOK_RE.match(line))}

    all_ok &= _check("SQL DELETE books = 实际删除 books", len(sql_delete_book_ids), len(removed_books),
                      display=f"{len(sql_delete_book_ids)} vs {len(removed_books)}")

    return all_ok


def layer3(step3_dir: Path, sql_path: Path, cleaned_db: Path) -> bool:
    print("第三层：CSV ↔ SQL ↔ DB 一致性")
    all_ok = True

    import csv
    csv_delete_ids: set[int] = set()
    for csv_file in sorted(step3_dir.glob("2-1-*.csv")):
        with csv_file.open(encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                action = (row.get("recommended-action") or row.get("recommended_action") or "").strip()
                if action == "delete_file":
                    data_id = row.get("data_id", "")
                    if data_id:
                        csv_delete_ids.add(int(data_id))

    sql_text = sql_path.read_text(encoding="utf-8")
    sql_delete_ids: set[int] = set()
    for line in sql_text.splitlines():
        m = DELETE_DATA_RE.match(line)
        if m:
            sql_delete_ids.add(int(m.group(1)))

    all_ok &= _check("CSV delete → SQL DELETE", len(csv_delete_ids), len(sql_delete_ids),
                      display=f"{len(csv_delete_ids)} / {len(sql_delete_ids)}")

    csv_not_in_sql = csv_delete_ids - sql_delete_ids
    sql_not_in_csv = sql_delete_ids - csv_delete_ids
    all_ok &= _check("CSV 有但 SQL 无", len(csv_not_in_sql), 0)
    all_ok &= _check("SQL 有但 CSV 无", len(sql_not_in_csv), 0)

    with sqlite3.connect(cleaned_db) as conn:
        remaining = 0
        if sql_delete_ids:
            sorted_ids = sorted(sql_delete_ids)
            chunk_size = 900
            for i in range(0, len(sorted_ids), chunk_size):
                chunk = sorted_ids[i:i + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                count = conn.execute(f"SELECT COUNT(*) FROM data WHERE id IN ({placeholders})", chunk).fetchone()[0]
                remaining += count
    all_ok &= _check("SQL DELETE 已实际执行（data 残留）", remaining, 0)

    sql_book_delete_ids = {int(m.group(1)) for line in sql_text.splitlines() if (m := DELETE_BOOK_RE.match(line))}
    with sqlite3.connect(cleaned_db) as conn:
        remaining_books = 0
        if sql_book_delete_ids:
            sorted_ids = sorted(sql_book_delete_ids)
            chunk_size = 900
            for i in range(0, len(sorted_ids), chunk_size):
                chunk = sorted_ids[i:i + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                count = conn.execute(f"SELECT COUNT(*) FROM books WHERE id IN ({placeholders})", chunk).fetchone()[0]
                remaining_books += count
    all_ok &= _check("SQL DELETE books 已实际执行（books 残留）", remaining_books, 0)

    return all_ok


def _check(label: str, actual: int, expected: int | None, display: str | None = None) -> bool:
    if expected is None:
        print(f"  {label}：{display or actual}")
        return True
    ok = actual == expected
    mark = "✓" if ok else "✗"
    print(f"  {label}：{display or actual} {mark}")
    return ok


if __name__ == "__main__":
    main()
