"""Step2 anomaly: 对增量 metadata.db 中的编码乱码记录执行删除，并处理副本标题与文件名修复。

为什么要有这一步
----------------
增量库里存在「GBK/CP936 字节流被按 UTF-8 解码」的乱码记录。12 个扫描字段中
任何一个出现乱码，说明该书记录已不可信——直接删除整书及关联 data 记录，
保持数据库清洁。副本标题（含「副本」标记）和文件名占位符（Unknown 等）
通过 BookInitPath 候选修复。

安全边界
--------
1. **绝不修改输入库。** ``03-input/`` 是只读输入，本步只复制副本再改副本。
2. **乱码即删除。** 12 个扫描字段中任何一个出现编码乱码，该书记录从副本中删除。
3. **不可逆的乱码同样删除。** 含 ``U+FFFD`` 的记录原始字节已永久丢失，同样删除。
"""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable

_SRC_DIR = Path(__file__).resolve().parents[2]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from knowledge_assets.preprocess import encoding
from knowledge_assets.preprocess.encoding import is_cjk
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
    write_dict_csv,
)
from knowledge_assets.utils.logger import get_logger, setup_logger


#: SQL 标识符白名单。表名/列名来自配置，无法参数化绑定，这里再校验一次，
#: 保证拼进 SQL 的只可能是形如 ``books`` / ``title`` 的普通标识符。
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Calibre 打开库时注册的自定义 SQL 函数，维护触发器依赖它们；裸连接没有这两个函数
_CALIBRE_FUNCTION_MARKERS = ("title_sort(", "uuid4(")

#: 待人工确认的原因码
REASON_ENCODING_MOJIBAKE = "encoding_mojibake"
REASON_BYTES_LOST = "bytes_lost"
REASON_CONSTRAINT_CONFLICT = "constraint_conflict"
REASON_MISSING_BOOK_INIT_PATH = "missing_book_init_path"
REASON_AMBIGUOUS_BOOK_INIT_PATH = "ambiguous_book_init_path"
REASON_INVALID_BOOK_INIT_PATH = "invalid_book_init_path"
REASON_FILENAME_STILL_COPY_MARKER = "filename_still_copy_marker"
REASON_MISSING_SORT_COLUMN = "missing_sort_column"
REASON_NON_CJK_STEM = "non_cjk_stem"

_REASON_LABELS = {
    REASON_ENCODING_MOJIBAKE: "编码乱码，删除不可信记录",
    REASON_BYTES_LOST: "原始字节已丢失（含 U+FFFD），删除不可信记录",
    REASON_CONSTRAINT_CONFLICT: "可无损还原，但还原后会与库中已有记录重名，需人工决定是否合并",
    REASON_MISSING_BOOK_INIT_PATH: "BookInitPath 为空或未配置",
    REASON_AMBIGUOUS_BOOK_INIT_PATH: "BookInitPath 存在多个候选，无法安全选择",
    REASON_INVALID_BOOK_INIT_PATH: "BookInitPath 中无法提取有效文件名",
    REASON_FILENAME_STILL_COPY_MARKER: "BookInitPath 文件名仍含副本标记，无法安全修复标题",
    REASON_MISSING_SORT_COLUMN: "books 表缺少 sort 列，无法同步修复标题排序值",
    REASON_NON_CJK_STEM: "BookInitPath 文件名不含中文字符，无法作为有效书名",
}

_ENCODE_REPAIR_FIELDS = [
    "序号", "表", "列", "说明", "记录ID", "book_id", "data_id", "原值", "修复后", "恢复链路",
    "status", "原因", "还原建议（仅供参考）","book_init_path", 
]
_TITLE_COPY_REPORT_FIELDS = [
    "book_id",
    "data_id",
    "original_title",
    "title_candidate",
    "status",
    "reason_code",
    "reason_detail",
    "book_init_path",
]
_BASENAME_REPORT_FIELDS = [
    "book_id",
    "data_id",
    "original_file_basename",
    "format",
    "file_basename_candidate",
    "status",
    "reason_code",
    "reason_detail",
    "book_init_path",
]


@dataclass(frozen=True)
class RepairTarget:
    """一个待检查的库列。"""

    table: str
    column: str
    label: str
    #: 该列是否绑定实体文件位置；为真时只报告、绝不修改
    filesystem_bound: bool

    @property
    def qualified_name(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class UnrepairableHit:
    """一条本步不动、需要人工看的记录。"""

    target: RepairTarget
    row_id: int
    value: str
    reason: str
    #: 给人工的还原建议：bytes_lost 时是「部分还原 + ◻ 缺口」，其余情况是完整还原结果。
    #: 纯提示，不参与任何写库。
    restore_suggestion: str = ""
    #: 补充说明（如唯一约束冲突的原文），仅写进报告便于追查
    detail: str = ""
    #: BookInitPath 原始值，仅 mojibake 报告使用
    book_init_path: str = ""


@dataclass(frozen=True)
class DeletionCandidate:
    """12 个扫描字段中任一个出现编码乱码，整书删除。"""

    target: RepairTarget
    row_id: int
    original: str
    book_id: int
    reason: str = REASON_ENCODING_MOJIBAKE
    book_init_path: str = ""
    book_title: str = ""


@dataclass(frozen=True)
class TitleCopyHandoff:
    book_id: int
    original_title: str
    book_init_path: str
    title_candidate: str
    status: str
    reason_code: str = ""
    reason_detail: str = ""


@dataclass(frozen=True)
class BasenameHandoff:
    book_id: int
    data_id: int
    original_file_basename: str
    format: str
    book_init_path: str
    file_basename_candidate: str
    status: str
    reason_code: str = ""
    reason_detail: str = ""


def run_step2_anomaly(
    batch: str,
    incremental_dir: Path,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> None:
    """扫描增量库、复制副本、在副本上删除乱码记录并修复副本标题/文件名。"""
    logger = get_logger("preprocess.step2")

    resolved_output_dir = output_dir or default_output_dir(batch)
    source_db = resolve_incremental_db(batch, incremental_dir, resolved_output_dir, include_step1=True)
    batch, output_dir = align_batch_to_metadata_db(batch, source_db, output_dir)
    config = pipeline_config()
    repair_config = config["step2"]["repair"]
    out_dir = step_dir(output_dir, "step2")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not source_db.exists():
        raise FileNotFoundError(f"metadata.db 不存在： {source_db}")

    targets = _load_targets(repair_config)

    business_log: list[str] = []

    def emit(message: str, *args: object) -> None:
        text = message % args if args else message
        business_log.append(text)
        logger.info(text)

    emit("========== 开始【Step2 scan：异常处理扫描】 ==========")
    emit(f"只读输入增量库： {display_path(source_db)}")
    source_books, source_data = db_table_counts(source_db)
    emit(f"输入库统计： books {source_books} | data {source_data}")
    if dry_run:
        emit("dry-run 模式：只扫描并输出报告，不执行删除")

    with closing(sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)) as conn:
        paths_by_book = _load_book_init_paths(conn, repair_config)
        data_ids_by_book = _build_data_ids_by_book(conn)
        deletion_candidates = _scan(conn, targets, paths_by_book)
        title_handoffs, basename_handoffs = _scan_book_init_handoffs(conn, repair_config)
        affected_book_ids = {c.book_id for c in deletion_candidates}
        if affected_book_ids:
            ph = ",".join("?" * len(affected_book_ids))
            total_data_affected = conn.execute(
                f"SELECT COUNT(*) FROM data WHERE book IN ({ph})",
                tuple(affected_book_ids),
            ).fetchone()[0]
        else:
            total_data_affected = 0

    deleted_book_ids = {c.book_id for c in deletion_candidates}

    title_handoffs = [h for h in title_handoffs if h.book_id not in deleted_book_ids]
    basename_handoffs = [h for h in basename_handoffs if h.book_id not in deleted_book_ids]

    encoded_db = out_dir / str(repair_config["encoded_db_filename"]).format(date=batch.replace("-", ""))
    if dry_run:
        title_handoffs = _mark_title_handoffs_dry_run(title_handoffs)
        basename_handoffs = _mark_basename_handoffs_dry_run(basename_handoffs)
        if encoded_db.exists():
            emit(f"注意： 已存在上一次的修复库，下游仍会读到它： {display_path(encoded_db)}")
    else:
        _copy_database(source_db, encoded_db)
        _apply_book_deletions(encoded_db, deletion_candidates)
        title_handoffs = _apply_title_copy_repairs(encoded_db, title_handoffs)
        basename_handoffs = _apply_basename_repairs(encoded_db, basename_handoffs)

    title_unresolved_count = sum(1 for h in title_handoffs if h.status == "manual_confirming")
    basename_unresolved_count = sum(1 for h in basename_handoffs if h.status == "manual_confirming")
    title_update_count = sum(1 for h in title_handoffs if h.status in ("update_file", "updated"))
    basename_update_count = sum(1 for h in basename_handoffs if h.status in ("update_file", "updated"))

    manual_hits: list[UnrepairableHit] = []
    title_target = _target_for(targets, "books", "title", "书名")
    for h in title_handoffs:
        if h.status == "manual_confirming":
            manual_hits.append(
                UnrepairableHit(
                    target=title_target,
                    row_id=h.book_id,
                    value=h.original_title,
                    reason=h.reason_code,
                    restore_suggestion=h.title_candidate,
                    detail=h.reason_detail,
                )
            )
    basename_target = _target_for(targets, "data", "name", "文件名")
    for h in basename_handoffs:
        if h.status == "manual_confirming":
            manual_hits.append(
                UnrepairableHit(
                    target=basename_target,
                    row_id=h.data_id,
                    value=h.original_file_basename,
                    reason=h.reason_code,
                    restore_suggestion=h.file_basename_candidate,
                    detail=h.reason_detail,
                )
            )

    title_manual_hits = [h for h in manual_hits if h.target.table == "books"]
    basename_manual_hits = [h for h in manual_hits if h.target.table == "data"]

    _emit_scan_summary(emit, targets, deletion_candidates, manual_hits)
    if dry_run:
        emit("（dry-run 不写库，唯一约束冲突要等真正执行时才能检出）")
    else:
        emit("")
        emit(
            "输出删除后的增量库： %s | 已删除 %s 本书",
            display_path(encoded_db),
            len(deletion_candidates),
        )
        encoded_books, encoded_data = db_table_counts(encoded_db)
        emit(f"输出库统计： books {encoded_books} | data {encoded_data}")
        emit(f"输入 → 输出： books {source_books} → {encoded_books} | data {source_data} → {encoded_data}")

    repair_path = out_dir / str(repair_config["repair_report_filename"])
    title_copy_path = out_dir / str(repair_config["title_copy_repair_filename"])
    basename_path = out_dir / str(repair_config["basename_repair_filename"])

    _write_encode_repair_report(repair_path, deletion_candidates, data_ids_by_book=data_ids_by_book)
    _write_title_copy_report(title_copy_path, title_handoffs, data_ids_by_book=data_ids_by_book)
    _write_basename_report(basename_path, basename_handoffs)

    emit("")
    encode_total = len(deletion_candidates) + len(manual_hits)
    encode_delete_books = len({c.book_id for c in deletion_candidates})
    encode_manual_books = len({h.row_id for h in title_manual_hits})
    encode_total_books = len(
        {c.book_id for c in deletion_candidates} | {h.row_id for h in title_manual_hits}
    )
    encode_total_data = total_data_affected
    encode_delete_data = total_data_affected
    encode_manual_data = 0
    emit(
        "【步骤一：乱码扫描】：\t 总数 %s/%s | 删除 %s/%s | 更新 0/0 | 保留 %s/%s",
        encode_total_books, encode_total_data,
        encode_delete_books, encode_delete_data,
        encode_manual_books, encode_manual_data,
    )
    title_total = len(title_handoffs)
    title_books = len({h.book_id for h in title_handoffs})
    emit(
        "【步骤二：副本标题扫描】：\t 总数 %s/%s | 删除 0/0 | 更新 %s/%s | 保留 %s/%s",
        title_books, title_total,
        len({h.book_id for h in title_handoffs if h.status in ("update_file", "updated")}), title_update_count,
        len({h.book_id for h in title_handoffs if h.status == "manual_confirming"}), title_unresolved_count,
    )
    basename_total = len(basename_handoffs)
    basename_books = len({h.book_id for h in basename_handoffs})
    emit(
        "【步骤三：未知标题扫描】：\t 总数 %s/%s | 删除 0/0 | 更新 %s/%s | 保留 %s/%s",
        basename_books, basename_total,
        len({h.book_id for h in basename_handoffs if h.status in ("update_file", "updated")}), basename_update_count,
        len({h.book_id for h in basename_handoffs if h.status == "manual_confirming"}), basename_unresolved_count,
    )
    emit(
        "生成【乱码扫描】CSV：\t %s | %s 条",
        display_path(repair_path), encode_total,
    )
    emit(
        "生成【副本标题扫描】CSV：\t %s | %s 条",
        display_path(title_copy_path), title_total,
    )
    emit(
        "生成【未知标题扫描】CSV：\t %s | %s 条",
        display_path(basename_path), basename_total,
    )
    emit("========== 完成【Step2 scan：异常处理扫描】 ==========")

    log_path = out_dir / str(repair_config["log_filename"])
    log_path.write_text("\n".join(business_log) + "\n", encoding="utf-8")


def run_step2_anomaly_scan(
    batch: str,
    incremental_dir: Path,
    output_dir: Path,
) -> None:
    """只读扫描增量库，输出三份 CSV（status=delete_file/keep），不复制删除库。"""
    logger = get_logger("preprocess.step2")

    resolved_output_dir = output_dir or default_output_dir(batch)
    source_db = resolve_incremental_db(batch, incremental_dir, resolved_output_dir, include_step1=True)
    batch, output_dir = align_batch_to_metadata_db(batch, source_db, output_dir)
    config = pipeline_config()
    repair_config = config["step2"]["repair"]
    out_dir = step_dir(output_dir, "step2")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not source_db.exists():
        raise FileNotFoundError(f"metadata.db 不存在： {source_db}")

    targets = _load_targets(repair_config)

    business_log: list[str] = []

    def emit(message: str, *args: object) -> None:
        text = message % args if args else message
        business_log.append(text)
        logger.info(text)

    emit("========== 开始【Step2：异常处理【乱码->副本标题->未知标题】】 ==========")
    emit(f"-----> 只读输入已预处理的增量库： {display_path(source_db)}")

    with closing(sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)) as conn:
        paths_by_book = _load_book_init_paths(conn, repair_config)
        data_ids_by_book = _build_data_ids_by_book(conn)
        deletion_candidates = _scan(conn, targets, paths_by_book)
        title_handoffs, basename_handoffs = _scan_book_init_handoffs(conn, repair_config)
        affected_book_ids = {c.book_id for c in deletion_candidates}
        if affected_book_ids:
            ph = ",".join("?" * len(affected_book_ids))
            total_data_affected = conn.execute(
                f"SELECT COUNT(*) FROM data WHERE book IN ({ph})",
                tuple(affected_book_ids),
            ).fetchone()[0]
        else:
            total_data_affected = 0

    deleted_book_ids = {c.book_id for c in deletion_candidates}
    title_handoffs = [h for h in title_handoffs if h.book_id not in deleted_book_ids]
    basename_handoffs = [h for h in basename_handoffs if h.book_id not in deleted_book_ids]

    manual_hits: list[UnrepairableHit] = []
    title_target = _target_for(targets, "books", "title", "书名")
    for h in title_handoffs:
        if h.status == "manual_confirming":
            manual_hits.append(
                UnrepairableHit(
                    target=title_target, row_id=h.book_id,
                    value=h.original_title, reason=h.reason_code,
                    restore_suggestion=h.title_candidate,
                    detail=h.reason_detail,
                )
            )
    basename_target = _target_for(targets, "data", "name", "文件名")
    for h in basename_handoffs:
        if h.status == "manual_confirming":
            manual_hits.append(
                UnrepairableHit(
                    target=basename_target, row_id=h.data_id,
                    value=h.original_file_basename, reason=h.reason_code,
                    restore_suggestion=h.file_basename_candidate,
                    detail=h.reason_detail,
                )
            )

    title_manual_hits = [h for h in manual_hits if h.target.table == "books"]
    basename_manual_hits = [h for h in manual_hits if h.target.table == "data"]

    _emit_scan_summary(emit, targets, deletion_candidates, manual_hits)

    repair_path = out_dir / str(repair_config["repair_report_filename"])
    title_copy_path = out_dir / str(repair_config["title_copy_repair_filename"])
    basename_path = out_dir / str(repair_config["basename_repair_filename"])

    _write_encode_repair_report(repair_path, deletion_candidates, data_ids_by_book=data_ids_by_book)
    _write_title_copy_report(title_copy_path, title_handoffs, data_ids_by_book=data_ids_by_book)
    _write_basename_report(basename_path, basename_handoffs)

    title_unresolved_count = sum(1 for h in title_handoffs if h.status == "manual_confirming")
    basename_unresolved_count = sum(1 for h in basename_handoffs if h.status == "manual_confirming")
    title_update_count = sum(1 for h in title_handoffs if h.status == "update_file")
    basename_update_count = sum(1 for h in basename_handoffs if h.status == "update_file")

    emit("")
    encode_total = len(deletion_candidates) + len(manual_hits)
    encode_delete_books = len({c.book_id for c in deletion_candidates})
    encode_manual_books = len({h.row_id for h in title_manual_hits})
    encode_total_books = len(
        {c.book_id for c in deletion_candidates} | {h.row_id for h in title_manual_hits}
    )
    encode_total_data = total_data_affected
    encode_delete_data = total_data_affected
    encode_manual_data = 0
    emit(
        "【步骤一：乱码扫描】：\t 总数 %s/%s | 删除 %s/%s | 更新 0/0 | 保留 %s/%s",
        encode_total_books, encode_total_data,
        encode_delete_books, encode_delete_data,
        encode_manual_books, encode_manual_data,
    )
    title_total = len(title_handoffs)
    title_books = len({h.book_id for h in title_handoffs})
    emit(
        "【步骤二：副本标题扫描】：\t 总数 %s/%s | 删除 0/0 | 更新 %s/%s | 保留 %s/%s",
        title_books, title_total,
        len({h.book_id for h in title_handoffs if h.status in ("update_file", "updated")}), title_update_count,
        len({h.book_id for h in title_handoffs if h.status == "manual_confirming"}), title_unresolved_count,
    )
    basename_total = len(basename_handoffs)
    basename_books = len({h.book_id for h in basename_handoffs})
    emit(
        "【步骤三：未知标题扫描】：\t 总数 %s/%s | 删除 0/0 | 更新 %s/%s | 保留 %s/%s",
        basename_books, basename_total,
        len({h.book_id for h in basename_handoffs if h.status in ("update_file", "updated")}), basename_update_count,
        len({h.book_id for h in basename_handoffs if h.status == "manual_confirming"}), basename_unresolved_count,
    )
    emit(
        "生成【乱码扫描】CSV：\t %s | %s 条",
        display_path(repair_path), encode_total,
    )
    emit(
        "生成【副本标题扫描】CSV：\t %s | %s 条",
        display_path(title_copy_path), title_total,
    )
    emit(
        "生成【未知标题扫描】CSV：\t %s | %s 条",
        display_path(basename_path), basename_total,
    )
    emit("========== 完成【Step2：异常处理】 ==========")

    log_path = out_dir / str(repair_config["log_filename"])
    log_path.write_text("\n".join(business_log) + "\n", encoding="utf-8")


def run_step2_anomaly_apply(
    batch: str,
    incremental_dir: Path,
    output_dir: Path,
) -> None:
    """读取 scan 产出的 CSV，复制副本并按 status=delete_file 执行删除。"""
    logger = get_logger("preprocess.step2")

    resolved_output_dir = output_dir or default_output_dir(batch)
    source_db = resolve_incremental_db(batch, incremental_dir, resolved_output_dir, include_step1=True)
    batch, output_dir = align_batch_to_metadata_db(batch, source_db, output_dir)
    config = pipeline_config()
    repair_config = config["step2"]["repair"]
    out_dir = step_dir(output_dir, "step2")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not source_db.exists():
        raise FileNotFoundError(f"metadata.db 不存在： {source_db}")

    targets = _load_targets(repair_config)

    repair_path = out_dir / str(repair_config["repair_report_filename"])
    title_copy_path = out_dir / str(repair_config["title_copy_repair_filename"])
    basename_path = out_dir / str(repair_config["basename_repair_filename"])

    for csv_path in (repair_path, title_copy_path, basename_path):
        if not csv_path.exists():
            raise FileNotFoundError(f"缺少 scan 产出 CSV： {csv_path}，请先执行 step2 repair scan")

    business_log: list[str] = []

    def emit(message: str, *args: object) -> None:
        text = message % args if args else message
        business_log.append(text)
        logger.info(text)

    emit("========== 开始【Step2 apply：执行【乱码+副本标题+未知标题】修复】 ==========")
    emit(f"-----> 重新只读输入已预处理的增量库： {display_path(source_db)}")
    source_books, source_data = db_table_counts(source_db)
    emit(f"输入库统计： books {source_books} | data {source_data}")

    encode_rows, ready_deletions = _read_encode_repair_csv(repair_path, targets)
    title_handoffs = _read_title_copy_csv(title_copy_path)
    basename_handoffs = _read_basename_csv(basename_path)

    deleted_book_ids = {c.book_id for c in ready_deletions}
    title_handoffs = [h for h in title_handoffs if h.book_id not in deleted_book_ids]
    basename_handoffs = [h for h in basename_handoffs if h.book_id not in deleted_book_ids]

    with closing(sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)) as conn:
        total_books = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        total_data = conn.execute("SELECT COUNT(*) FROM data").fetchone()[0]
        data_ids_by_book = _build_data_ids_by_book(conn)
        if deleted_book_ids:
            placeholders = ",".join("?" * len(deleted_book_ids))
            data_to_delete = conn.execute(
                f"SELECT COUNT(*) FROM data WHERE book IN ({placeholders})",
                tuple(deleted_book_ids),
            ).fetchone()[0]
        else:
            data_to_delete = 0

    keep_title_count = sum(1 for h in title_handoffs if h.status == "keep")
    keep_basename_count = sum(1 for h in basename_handoffs if h.status == "keep")
    emit(
        "从 CSV 读取删除指令： 待删除 %s 本书 | 副本标题保留 %s 条 | 文件名保留 %s 条",
        len(ready_deletions), keep_title_count, keep_basename_count,
    )

    encoded_db = out_dir / str(repair_config["encoded_db_filename"]).format(date=batch.replace("-", ""))
    _copy_database(source_db, encoded_db)

    _apply_book_deletions(encoded_db, ready_deletions)
    title_results = _apply_title_copy_repairs(encoded_db, title_handoffs)
    basename_results = _apply_basename_repairs(encoded_db, basename_handoffs)

    for row in encode_rows:
        if row["status"] == "delete_file":
            row["status"] = "deleted"
        if not row.get("data_id"):
            book_id_val = row.get("book_id")
            if book_id_val:
                row["data_id"] = data_ids_by_book.get(int(book_id_val), "")
    write_dict_csv(repair_path, [dict(row) for row in encode_rows], _ENCODE_REPAIR_FIELDS)
    _write_title_copy_report(title_copy_path, title_results, data_ids_by_book=data_ids_by_book)
    _write_basename_report(basename_path, basename_results)

    title_repaired_count = sum(1 for h in title_results if h.status == "updated")
    basename_repaired_count = sum(1 for h in basename_results if h.status == "updated")

    emit("")
    emit(
        "-----> 输出删除后的增量库： %s | 已删除 %s 本书 | 副本标题修复 %s 条 | 文件名修复 %s 条",
        display_path(encoded_db),
        len(ready_deletions),
        title_repaired_count,
        basename_repaired_count,
    )
    encoded_books, encoded_data = db_table_counts(encoded_db)
    emit(f"输出库统计： books {encoded_books} | data {encoded_data}")
    emit(f"输入 → 输出： books {source_books} → {encoded_books} | data {source_data} → {encoded_data}")
    if deleted_book_ids:
        emit(f"表 books 汇总：\t 共 {total_books} 条 -> 删除 {len(deleted_book_ids)} 条 -> 剩余 {total_books - len(deleted_book_ids)} 条")
        emit(f"表 data 汇总：\t 共 {total_data} 条 -> 删除 {data_to_delete} 条 -> 剩余 {total_data - data_to_delete} 条")

    step1_manual_book_ids = {
        int(row["记录ID"]) for row in encode_rows
        if row["status"] == "manual_confirming"
        and row["表"] == "books"
        and int(row["记录ID"]) not in deleted_book_ids
    }
    step1_total_books = len(deleted_book_ids) + len(step1_manual_book_ids) + (total_books - len(deleted_book_ids) - len(step1_manual_book_ids))
    step1_total_data = total_data
    step2_update_books = len({h.book_id for h in title_results if h.status == "updated"})
    step2_manual_books = len({h.book_id for h in title_results if h.status == "manual_confirming"})
    step3_update_books = len({h.book_id for h in basename_results if h.status == "updated"})
    step3_manual_books = len({h.book_id for h in basename_results if h.status == "manual_confirming"})
    emit(
        "【步骤一：乱码扫描】：\t 总数 %s/%s | 删除 %s/%s | 更新 0/0 | 保留 %s/%s",
        total_books, step1_total_data,
        len(deleted_book_ids), data_to_delete,
        len(step1_manual_book_ids), 0,
    )
    emit(
        "【步骤二：副本标题扫描】：\t 总数 %s/%s | 删除 0/0 | 更新 %s/%s | 保留 %s/%s",
        len({h.book_id for h in title_results}), len(title_results),
        step2_update_books, title_repaired_count,
        step2_manual_books, sum(1 for h in title_results if h.status == "manual_confirming"),
    )
    emit(
        "【步骤三：未知标题扫描】：\t 总数 %s/%s | 删除 0/0 | 更新 %s/%s | 保留 %s/%s",
        len({h.book_id for h in basename_results}), len(basename_results),
        step3_update_books, basename_repaired_count,
        step3_manual_books, sum(1 for h in basename_results if h.status == "manual_confirming"),
    )

    total_downgraded = sum(
        1 for h in title_results if h.status == "manual_confirming" and h.reason_code == REASON_CONSTRAINT_CONFLICT
    ) + sum(
        1 for h in basename_results if h.status == "manual_confirming" and h.reason_code == REASON_CONSTRAINT_CONFLICT
    )
    if total_downgraded:
        emit("  唯一约束冲突降级 %s 条（已更新 CSV 为 manual_confirming）", total_downgraded)
    emit("========== 完成【Step2 apply：执行【乱码+副本标题+未知标题】修复】 ==========")

    log_path = out_dir / str(repair_config["log_filename"])
    log_path.write_text("\n".join(business_log) + "\n", encoding="utf-8")


def _load_targets(repair_config: dict[str, object]) -> tuple[RepairTarget, ...]:
    targets: list[RepairTarget] = []
    for raw in repair_config["targets"]:  # type: ignore[union-attr]
        target = RepairTarget(
            table=str(raw["table"]),
            column=str(raw["column"]),
            label=str(raw["label"]),
            filesystem_bound=bool(raw["filesystem_bound"]),
        )
        if not _IDENTIFIER_RE.match(target.table) or not _IDENTIFIER_RE.match(target.column):
            raise ValueError(f"非法表名/列名： {target.qualified_name}")
        targets.append(target)
    return tuple(targets)


def _build_row_to_book_mapping(
    conn: sqlite3.Connection, targets: Iterable[RepairTarget]
) -> dict[str, dict[int, int]]:
    """Build mapping from (table, row_id) to book_id for all target tables."""
    mapping: dict[str, dict[int, int]] = {}
    tables = {t.table for t in targets}

    for table in tables:
        if table == "books":
            continue
        if not _table_exists(conn, table):
            continue

        if table == "data":
            columns = _table_columns(conn, table)
            if "book" in columns:
                rows = conn.execute(f"SELECT id, book FROM {table}").fetchall()
                mapping[table] = {int(row_id): int(book) for row_id, book in rows if book}
            continue

        for link_table in _list_tables(conn):
            if not link_table.startswith("books_") or not link_table.endswith(f"_{table}_link"):
                continue
            link_cols = _table_columns(conn, link_table)
            if "book" not in link_cols:
                continue
            entity_col = next((c for c in link_cols if c != "book"), None)
            if not entity_col:
                continue
            rows = conn.execute(f"SELECT {entity_col}, book FROM {link_table}").fetchall()
            mapping[table] = {int(entity_id): int(book) for entity_id, book in rows if book}
            break

    return mapping


def _scan(
    conn: sqlite3.Connection,
    targets: Iterable[RepairTarget],
    paths_by_book: dict[int, tuple[str, ...]],
) -> list[DeletionCandidate]:
    """逐列扫描 12 个目标字段，任何编码乱码 → 整书删除。

    去重：books.path 乱码已标记整书删除，同一本书的 data.name 乱码不再单独出条目。
    """
    candidates: list[DeletionCandidate] = []
    row_to_book = _build_row_to_book_mapping(conn, targets)

    def resolve_book_id(table: str, row_id: int) -> int | None:
        if table == "books":
            return row_id
        return row_to_book.get(table, {}).get(row_id)

    def resolve_book_init_path(table: str, row_id: int) -> str:
        book_id = resolve_book_id(table, row_id)
        if book_id is None:
            return ""
        paths = paths_by_book.get(book_id, ())
        return paths[0] if len(paths) == 1 else ""

    garbled_path_book_ids: set[int] = set()
    targets_list = list(targets)
    if _table_exists(conn, "books") and "path" in _table_columns(conn, "books"):
        for row_id, value in conn.execute("SELECT id, path FROM books").fetchall():
            text = value or ""
            if text and encoding.restore_text(text) is not None:
                garbled_path_book_ids.add(int(row_id))

    for target in targets_list:
        if not _table_exists(conn, target.table):
            continue
        if target.column not in _table_columns(conn, target.table):
            continue
        query = f"SELECT id, {target.column} FROM {target.table}"
        for row_id, value in conn.execute(query).fetchall():
            text = value or ""
            if not text:
                continue
            book_id = resolve_book_id(target.table, int(row_id))
            if (
                target.table == "data"
                and target.column == "name"
                and book_id is not None
                and book_id in garbled_path_book_ids
            ):
                continue
            reason: str | None = None
            suggestion = ""
            if encoding.restore_text(text) is not None:
                reason = REASON_ENCODING_MOJIBAKE
            elif encoding.is_replacement_damaged(text):
                reason = REASON_BYTES_LOST
                suggestion = encoding.partial_hint(text)
            if reason is None:
                continue
            book_init_path = resolve_book_init_path(target.table, int(row_id))
            book_title = ""
            if book_id is not None and _table_exists(conn, "books"):
                row = conn.execute(
                    "SELECT title FROM books WHERE id = ?", (book_id,)
                ).fetchone()
                if row:
                    book_title = str(row[0] or "")
            candidates.append(
                DeletionCandidate(
                    target=target,
                    row_id=int(row_id),
                    original=text,
                    book_id=book_id if book_id is not None else int(row_id),
                    reason=reason,
                    book_init_path=book_init_path,
                    book_title=book_title,
                )
            )
    return candidates


def _scan_book_init_handoffs(
    conn: sqlite3.Connection, repair_config: dict[str, object]
) -> tuple[list[TitleCopyHandoff], list[BasenameHandoff]]:
    paths_by_book = _load_book_init_paths(conn, repair_config)
    if not _table_exists(conn, "books"):
        return [], []

    book_columns = _table_columns(conn, "books")
    titles: list[TitleCopyHandoff] = []
    for book_id, title in conn.execute("SELECT id, title FROM books ORDER BY id"):
        original_title = str(title or "")
        if not _contains_copy_marker(original_title):
            continue
        book_init_path, candidate, _suffix, reason_code, reason_detail = _book_init_candidate(
            paths_by_book.get(int(book_id), ())
        )
        if not reason_code and _contains_copy_marker(candidate):
            candidate = ""
            reason_code = REASON_FILENAME_STILL_COPY_MARKER
            reason_detail = "BookInitPath 文件名仍包含副本标记"
        if not reason_code and "sort" not in book_columns:
            candidate = ""
            reason_code = REASON_MISSING_SORT_COLUMN
            reason_detail = "无法同步更新 books.sort"
        titles.append(
            TitleCopyHandoff(
                book_id=int(book_id),
                original_title=original_title,
                book_init_path=book_init_path,
                title_candidate=candidate,
                status="update_file" if candidate else "manual_confirming",
                reason_code=reason_code,
                reason_detail=reason_detail,
            )
        )

    basenames: list[BasenameHandoff] = []
    if not _table_exists(conn, "data"):
        return titles, basenames
    query = (
        "SELECT data.id, data.book, data.name, data.format "
        "FROM data JOIN books ON books.id = data.book ORDER BY data.book, data.id"
    )
    for data_id, book_id, basename, file_format in conn.execute(query):
        original_basename = str(basename or "")
        if not any(marker in original_basename for marker in _unknown_basename_markers()):
            continue
        ext = str(file_format or "").strip().lower()
        book_init_path, candidate, _source_ext, reason_code, reason_detail = _book_init_candidate(
            paths_by_book.get(int(book_id), ())
        )
        basenames.append(
            BasenameHandoff(
                book_id=int(book_id),
                data_id=int(data_id),
                original_file_basename=original_basename,
                format=ext,
                book_init_path=book_init_path,
                file_basename_candidate=candidate,
                status="update_file" if candidate else "manual_confirming",
                reason_code=reason_code,
                reason_detail=reason_detail,
            )
        )
    return titles, basenames


def _load_book_init_paths(
    conn: sqlite3.Connection, repair_config: dict[str, object]
) -> dict[int, tuple[str, ...]]:
    raw_config = repair_config.get("book_init_path", {})
    if not isinstance(raw_config, dict):
        raise ValueError("step2.repair.book_init_path 必须是对象")
    value_table = str(raw_config.get("value_table", "custom_column_1"))
    link_table = str(raw_config.get("link_table", "books_custom_column_1_link"))
    for identifier in (value_table, link_table):
        if not _IDENTIFIER_RE.match(identifier):
            raise ValueError(f"非法 BookInitPath 表名： {identifier}")
    if not _table_exists(conn, value_table):
        return {}

    value_columns = _table_columns(conn, value_table)
    rows: list[tuple[object, object]] = []
    if {"book", "value"}.issubset(value_columns):
        rows = conn.execute(
            f"SELECT book, value FROM {value_table} WHERE value IS NOT NULL"
        ).fetchall()
    elif (
        {"id", "value"}.issubset(value_columns)
        and _table_exists(conn, link_table)
        and {"book", "value"}.issubset(_table_columns(conn, link_table))
    ):
        rows = conn.execute(
            f"SELECT link.book, source.value FROM {link_table} AS link "
            f"JOIN {value_table} AS source ON source.id = link.value "
            "WHERE source.value IS NOT NULL"
        ).fetchall()

    grouped: dict[int, list[str]] = {}
    for book_id, value in rows:
        text = str(value or "").strip()
        if text:
            grouped.setdefault(int(book_id), []).append(text)
    return {book_id: tuple(dict.fromkeys(values)) for book_id, values in grouped.items()}


def _book_init_candidate(paths: tuple[str, ...]) -> tuple[str, str, str, str, str]:
    if not paths:
        return "", "", "", REASON_MISSING_BOOK_INIT_PATH, "未找到 BookInitPath"
    if len(paths) > 1:
        return "; ".join(paths), "", "", REASON_AMBIGUOUS_BOOK_INIT_PATH, "同一本书存在多个 BookInitPath"
    path = paths[0]
    filename = PureWindowsPath(path).name.strip()
    candidate = PureWindowsPath(filename).stem.strip()
    suffix = PureWindowsPath(filename).suffix.removeprefix(".")
    if not candidate or not suffix:
        return path, "", "", REASON_INVALID_BOOK_INIT_PATH, "BookInitPath 未包含可用的文件名和扩展名"
    return path, candidate, suffix, "", ""


def _contains_copy_marker(value: str) -> bool:
    return "副本" in value or "Fu Ben" in value


def _unknown_basename_markers() -> tuple[str, ...]:
    rules = load_json_config("step1_rules.json")
    return tuple(str(marker) for marker in rules["unknown_basename_markers"])


def _mark_title_handoffs_dry_run(handoffs: Iterable[TitleCopyHandoff]) -> list[TitleCopyHandoff]:
    return [handoff for handoff in handoffs if handoff.status != "update_file"]


def _mark_basename_handoffs_dry_run(handoffs: Iterable[BasenameHandoff]) -> list[BasenameHandoff]:
    return [handoff for handoff in handoffs if handoff.status != "update_file"]


def _apply_title_copy_repairs(
    encoded_db: Path, handoffs: Iterable[TitleCopyHandoff]
) -> list[TitleCopyHandoff]:
    results = list(handoffs)
    pending = [handoff for handoff in results if handoff.status == "update_file"]
    if not pending:
        return results

    result_by_book = {handoff.book_id: handoff for handoff in results}
    with closing(sqlite3.connect(encoded_db)) as conn:
        with conn:
            triggers = _calibre_managed_triggers(conn, {"books"})
            for name, _sql in triggers:
                conn.execute(f"DROP TRIGGER {name}")
            for handoff in pending:
                conn.execute("SAVEPOINT title_copy")
                try:
                    conn.execute(
                        "UPDATE books SET title = ?, sort = ? WHERE id = ?",
                        (handoff.title_candidate, handoff.title_candidate, handoff.book_id),
                    )
                except sqlite3.IntegrityError as error:
                    conn.execute("ROLLBACK TO title_copy")
                    result_by_book[handoff.book_id] = replace(
                        handoff,
                        status="manual_confirming",
                        reason_code=REASON_CONSTRAINT_CONFLICT,
                        reason_detail=str(error),
                    )
                else:
                    result_by_book[handoff.book_id] = replace(handoff, status="updated")
                finally:
                    conn.execute("RELEASE title_copy")
            for _name, sql in triggers:
                conn.execute(sql)
    return [result_by_book[handoff.book_id] for handoff in results]


def _apply_basename_repairs(
    encoded_db: Path, handoffs: Iterable[BasenameHandoff]
) -> list[BasenameHandoff]:
    results = list(handoffs)
    pending = [handoff for handoff in results if handoff.status == "update_file"]
    if not pending:
        return results

    result_by_data = {handoff.data_id: handoff for handoff in results}
    with closing(sqlite3.connect(encoded_db)) as conn:
        with conn:
            for handoff in pending:
                conn.execute("SAVEPOINT basename_repair")
                try:
                    conn.execute(
                        "UPDATE data SET name = ? WHERE id = ?",
                        (handoff.file_basename_candidate, handoff.data_id),
                    )
                except sqlite3.IntegrityError as error:
                    conn.execute("ROLLBACK TO basename_repair")
                    result_by_data[handoff.data_id] = replace(
                        handoff,
                        status="manual_confirming",
                        reason_code=REASON_CONSTRAINT_CONFLICT,
                        reason_detail=str(error),
                    )
                else:
                    result_by_data[handoff.data_id] = replace(handoff, status="updated")
                finally:
                    conn.execute("RELEASE basename_repair")
    return [result_by_data[handoff.data_id] for handoff in results]


def _apply_book_deletions(
    encoded_db: Path, candidates: list[DeletionCandidate]
) -> None:
    """在副本上删除乱码书籍：先删 data 行，再删 books 行。"""
    book_ids = list({c.book_id for c in candidates})
    if not book_ids:
        return

    with closing(sqlite3.connect(encoded_db)) as conn:
        with conn:
            triggers = _calibre_managed_triggers(conn, {"books", "data"})
            for name, _sql in triggers:
                conn.execute(f"DROP TRIGGER {name}")

            ph = ",".join("?" * len(book_ids))
            conn.execute(f"DELETE FROM data WHERE book IN ({ph})", book_ids)
            conn.execute(f"DELETE FROM books WHERE id IN ({ph})", book_ids)
            from knowledge_assets.preprocess.common import cleanup_orphan_link_records
            cleanup_orphan_link_records(conn, book_ids)

            for _name, sql in triggers:
                conn.execute(sql)


def _target_for(
    targets: Iterable[RepairTarget], table: str, column: str, label: str
) -> RepairTarget:
    return next(
        (target for target in targets if target.table == table and target.column == column),
        RepairTarget(table=table, column=column, label=label, filesystem_bound=column in {"name", "path"}),
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()]


def _copy_database(source: Path, destination: Path) -> None:
    """用 SQLite 备份 API 复制，避免直接拷文件拿到不一致的快照。"""
    if destination.exists():
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)


def _calibre_managed_triggers(
    conn: sqlite3.Connection, tables: set[str]
) -> list[tuple[str, str]]:
    """找出依赖 Calibre 运行时函数、会挡住我们改库的触发器。

    Calibre 在打开库时用 ``create_function`` 注册了 ``title_sort()`` / ``uuid4()``，
    ``books`` / ``series`` 上的维护触发器依赖它们（如
    ``UPDATE books SET sort=title_sort(NEW.title)``）。裸 sqlite3 连接没有这些函数，
    直接 UPDATE 会抛 ``no such function``。

    这里把相关触发器摘掉，改完再由调用方按 ``sqlite_master`` 里存的原文重建——
    我们本来就是自己写 ``sort`` 列（用同一套编码还原），语义与触发器一致，
    摘掉不会让 sort 和 title 脱节。
    """
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


def _emit_scan_summary(
    emit: Callable[..., None],
    targets: tuple[RepairTarget, ...],
    deletion_candidates: list[DeletionCandidate],
    manual_hits: list[UnrepairableHit],
) -> None:
    emit("")
    emit("【步骤一：乱码扫描】：扫描范围共 %s 列", len(targets))
    for target in targets:
        n_delete = sum(1 for c in deletion_candidates if c.target == target)
        if not n_delete:
            continue
        emit("  %-22s [待删除] %s 条", target.qualified_name, n_delete)
    unique_books = len({c.book_id for c in deletion_candidates})
    emit(
        "合计： 待删除 %s 本书（%s 条触发记录）",
        unique_books, len(deletion_candidates),
    )
    # if deletion_candidates:
    #     emit("")
    #     emit("删除示例（最多 10 条）：")
    #     for cand in deletion_candidates[:10]:
    #         emit(
    #             "  %s id=%s book=%s  %s",
    #             cand.target.qualified_name, cand.row_id, cand.book_id, cand.original,
    #         )


def _build_data_ids_by_book(conn: sqlite3.Connection) -> dict[int, str]:
    """返回 {book_id: "data_id1, data_id2, ..."} 映射。"""
    if not _table_exists(conn, "data"):
        return {}
    rows = conn.execute(
        "SELECT book, GROUP_CONCAT(id, ', ') FROM data GROUP BY book"
    ).fetchall()
    return {int(book): data_ids for book, data_ids in rows if book}


def _write_encode_repair_report(
    path: Path,
    deletion_candidates: Iterable[DeletionCandidate],
    status: str = "delete_file",
    data_ids_by_book: dict[int, str] | None = None,
) -> None:
    """输出 step2_1 CSV：所有乱码记录均为待删除。"""
    ids_map = data_ids_by_book or {}
    rows: list[dict[str, object]] = []
    for cand in deletion_candidates:
        rows.append({
            "序号": 0,
            "表": cand.target.table,
            "列": cand.target.column,
            "说明": cand.target.label,
            "记录ID": cand.row_id,
            "book_id": cand.book_id,
            "data_id": ids_map.get(cand.book_id, ""),
            "原值": cand.original,
            "修复后": f"delete_book(id={cand.book_id})",
            "恢复链路": "encoding_delete",
            "book_init_path": cand.book_init_path,
            "status": status,
            "原因": _REASON_LABELS.get(cand.reason, cand.reason),
            "还原建议（仅供参考）": cand.original,
        })
    for index, row in enumerate(rows, start=1):
        row["序号"] = index
    write_dict_csv(path, rows, _ENCODE_REPAIR_FIELDS)


def _write_title_copy_report(
    path: Path,
    handoffs: Iterable[TitleCopyHandoff],
    data_ids_by_book: dict[int, str] | None = None,
) -> None:
    ids_map = data_ids_by_book or {}
    rows = [
        {
            "book_id": handoff.book_id,
            "data_id": ids_map.get(handoff.book_id, ""),
            "original_title": handoff.original_title,
            "title_candidate": handoff.title_candidate,
            "status": handoff.status,
            "reason_code": handoff.reason_code,
            "reason_detail": handoff.reason_detail,
            "book_init_path": handoff.book_init_path,
        }
        for handoff in handoffs
    ]
    write_dict_csv(path, rows, _TITLE_COPY_REPORT_FIELDS)


def _write_basename_report(path: Path, handoffs: Iterable[BasenameHandoff]) -> None:
    rows = [
        {
            "book_id": handoff.book_id,
            "data_id": handoff.data_id,
            "original_file_basename": handoff.original_file_basename,
            "format": handoff.format,
            "file_basename_candidate": handoff.file_basename_candidate,
            "status": handoff.status,
            "reason_code": handoff.reason_code,
            "reason_detail": handoff.reason_detail,
            "book_init_path": handoff.book_init_path,
        }
        for handoff in handoffs
    ]
    write_dict_csv(path, rows, _BASENAME_REPORT_FIELDS)


def _read_encode_repair_csv(
    path: Path, targets: tuple[RepairTarget, ...]
) -> tuple[list[dict], list[DeletionCandidate]]:
    """读取 step2_1 CSV，返回 (所有行, status=delete_file 的 DeletionCandidate)。"""
    if not path.exists():
        return [], []
    target_map = {f"{t.table}.{t.column}": t for t in targets}
    all_rows: list[dict] = []
    deletions: list[DeletionCandidate] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            all_rows.append(row)
            if row["status"] != "delete_file":
                continue
            table = row["表"]
            column = row["列"]
            target = target_map.get(
                f"{table}.{column}",
                RepairTarget(
                    table=table, column=column,
                    label=row.get("说明", ""),
                    filesystem_bound=False,
                ),
            )
            repaired_text = row.get("修复后", "")
            book_id_str = row.get("book_id", "")
            book_id_match = re.search(r"book_id=(\d+)", repaired_text)
            if book_id_str:
                book_id = int(book_id_str)
            elif book_id_match:
                book_id = int(book_id_match.group(1))
            else:
                book_id = int(row["记录ID"])
            deletions.append(
                DeletionCandidate(
                    target=target,
                    row_id=int(row["记录ID"]),
                    original=row["原值"],
                    book_id=book_id,
                    reason=REASON_ENCODING_MOJIBAKE,
                    book_init_path=row.get("book_init_path", ""),
                )
            )
    return all_rows, deletions


def _read_title_copy_csv(path: Path) -> list[TitleCopyHandoff]:
    if not path.exists():
        return []
    handoffs: list[TitleCopyHandoff] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            handoffs.append(
                TitleCopyHandoff(
                    book_id=int(row["book_id"]),
                    original_title=row["original_title"],
                    book_init_path=row.get("book_init_path", ""),
                    title_candidate=row["title_candidate"],
                    status=row["status"],
                    reason_code=row.get("reason_code", ""),
                    reason_detail=row.get("reason_detail", ""),
                )
            )
    return handoffs


def _read_basename_csv(path: Path) -> list[BasenameHandoff]:
    if not path.exists():
        return []
    handoffs: list[BasenameHandoff] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            handoffs.append(
                BasenameHandoff(
                    book_id=int(row["book_id"]),
                    data_id=int(row["data_id"]),
                    original_file_basename=row["original_file_basename"],
                    format=row["format"],
                    book_init_path=row.get("book_init_path", ""),
                    file_basename_candidate=row["file_basename_candidate"],
                    status=row["status"],
                    reason_code=row.get("reason_code", ""),
                    reason_detail=row.get("reason_detail", ""),
                )
            )
    return handoffs


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Step2 anomaly：增量库编码修复")
    parser.add_argument("--batch", default=default_batch())
    parser.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    parser.add_argument("--output-dir", default=None, type=Path)
    parser.add_argument("--log-dir", default=default_log_dir(), type=Path)
    parser.add_argument("--dry-run", action="store_true", help="只扫描并输出报告，不执行删除")
    args = parser.parse_args()

    output_dir = args.output_dir or default_output_dir(args.batch)
    setup_logger(log_dir=str(args.log_dir))
    run_step2_anomaly(
        batch=args.batch,
        incremental_dir=args.incremental_dir,
        output_dir=output_dir,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    _main()
