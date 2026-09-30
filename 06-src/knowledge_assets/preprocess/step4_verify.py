"""Step4 verify: check if data table records have corresponding physical files."""

from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path, PureWindowsPath

_SRC_DIR = Path(__file__).resolve().parents[2]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from knowledge_assets.preprocess.common import (
    default_batch,
    default_input_dir,
    default_log_dir,
    default_output_dir,
    display_path,
    resolve_incremental_db,
    step_dir,
)
from knowledge_assets.utils.logger import get_logger, setup_logger


def run_step4_verify(
    batch: str,
    output_dir: Path,
    incremental_dir: Path | None = None,
    library_dir: Path | None = None,
) -> None:
    logger = get_logger("preprocess.step4")
    
    # Step4 verify should use the cleaned database after step4 SQL execution
    # which is in step3 directory (metadata.cleaned.db)
    step3_dir = step_dir(output_dir, "step3")
    step4_dir = step_dir(output_dir, "step4")
    step4_dir.mkdir(parents=True, exist_ok=True)
    
    # Use step3's metadata.cleaned.db as the source
    metadata_db = step3_dir / "metadata.cleaned.db"

    logger.info("========== 开始【Step4 verify：验证 cleaned.db 中 data 记录的物理文件是否存在】 ==========")
    logger.info("读取增量 metadata.db：%s", display_path(metadata_db))
    if not metadata_db.exists():
        raise FileNotFoundError(f"metadata.db 不存在： {metadata_db}")

    # Default library directory for verification
    if library_dir is None:
        library_dir = Path("E:/98-Calibre-books-new")
    
    logger.info("电子书库目录：%s", library_dir)

    # Read data table from cleaned database
    with sqlite3.connect(metadata_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT 
                d.id as data_id,
                d.book,
                d.name as data_name,
                d.format as data_format,
                b.id as book_id,
                b.title as book_title,
                b.path as book_path
            FROM data d
            JOIN books b ON d.book = b.id
            ORDER BY b.id, d.id
        """).fetchall()

    total_count = len(rows)
    logger.info("读取 data 表记录：%d 条", total_count)

    # Check file existence
    missing_files = []
    existing_files = []

    for row in rows:
        data_id = row["data_id"]
        book_id = row["book_id"]
        book_path = str(row["book_path"] or "")
        data_name = str(row["data_name"] or "")
        data_format = str(row["data_format"] or "").lower()

        # Construct relative path like step1_dedup.py does
        if book_path and data_name and data_format:
            relative_path = f"{book_path}/{data_name}.{data_format}"
        elif book_path and data_name:
            relative_path = f"{book_path}/{data_name}"
        else:
            relative_path = ""

        # Check if file exists
        if relative_path:
            # Convert to Windows path separators
            windows_relative = relative_path.replace("/", "\\")
            absolute_path = library_dir / windows_relative
            
            if absolute_path.exists():
                existing_files.append({
                    "data_id": data_id,
                    "book_id": book_id,
                    "book_title": row["book_title"],
                    "relative_path": relative_path,
                    "absolute_path": str(absolute_path),
                    "status": "exists",
                })
            else:
                missing_files.append({
                    "data_id": data_id,
                    "book_id": book_id,
                    "book_title": row["book_title"],
                    "relative_path": relative_path,
                    "absolute_path": str(absolute_path),
                    "status": "missing",
                })
        else:
            missing_files.append({
                "data_id": data_id,
                "book_id": book_id,
                "book_title": row["book_title"],
                "relative_path": "",
                "absolute_path": "",
                "status": "no_path",
            })

    existing_count = len(existing_files)
    missing_count = len(missing_files)
    no_path_count = sum(1 for m in missing_files if m["status"] == "no_path")
    actual_missing = missing_count - no_path_count

    logger.info("验证结果：总数 %d | 文件存在 %d | 文件缺失 %d (其中路径为空 %d)", 
                total_count, existing_count, missing_count, no_path_count)

    # Write verification report CSV
    report_path = step4_dir / "step4_verification_report.csv"
    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "data_id", "book_id", "book_title", "relative_path", "absolute_path", "status"
        ])
        writer.writeheader()
        for record in missing_files + existing_files:
            writer.writerow(record)

    logger.info("输出验证报告：%s | 缺失 %d 条 | 存在 %d 条", 
                display_path(report_path), missing_count, existing_count)

    # Write detailed log for missing files
    log_path = step4_dir / "step4_verification.log"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Step4 验证报告\n")
        f.write(f"批次: {batch}\n")
        f.write(f"数据库: {metadata_db.name}\n")
        f.write(f"电子书库目录: {library_dir}\n")
        f.write(f"\n统计摘要:\n")
        f.write(f"  总记录数: {total_count}\n")
        f.write(f"  文件存在: {existing_count}\n")
        f.write(f"  文件缺失: {missing_count}\n")
        f.write(f"    - 路径为空: {no_path_count}\n")
        f.write(f"    - 实际缺失: {actual_missing}\n")
        f.write(f"\n缺失文件详情:\n")
        for record in missing_files:
            if record["status"] == "no_path":
                f.write(f"  [data_id={record['data_id']}] book_id={record['book_id']} 路径为空\n")
            else:
                f.write(f"  [data_id={record['data_id']}] book_id={record['book_id']} {record['relative_path']}\n")
                f.write(f"    期望路径: {record['absolute_path']}\n")

    logger.info("输出详细日志：%s", display_path(log_path))
    logger.info("========== 完成【Step4 verify：验证 cleaned.db 中 data 记录的物理文件是否存在】 ==========")


def batch_from_batch(batch: str) -> str | None:
    """Extract compact batch from batch string like 2026-09-23 → 20260923."""
    return batch.replace("-", "")


def batch_from_metadata_path(metadata_db: Path) -> str | None:
    """Extract batch from metadata db filename like metadata-2026-09-23.db."""
    stem = metadata_db.stem
    if stem.startswith("metadata-"):
        return stem[len("metadata-"):]
    return None


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Step4 verify：验证 cleaned.db 中 data 记录的物理文件是否存在")
    batch = default_batch()
    parser.add_argument("--batch", default=batch)
    parser.add_argument("--incremental-dir", type=Path, default=default_input_dir())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=default_log_dir())
    parser.add_argument("--library-dir", type=Path, default=None, help="Calibre 电子书库目录（默认 E:/98-Calibre-books-new）")
    args = parser.parse_args()

    setup_logger(log_dir=str(args.log_dir))
    run_step4_verify(
        batch=args.batch,
        output_dir=args.output_dir or default_output_dir(args.batch),
        incremental_dir=args.incremental_dir,
        library_dir=args.library_dir,
    )


if __name__ == "__main__":
    _main()
