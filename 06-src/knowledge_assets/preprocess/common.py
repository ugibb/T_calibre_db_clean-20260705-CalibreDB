"""Shared helpers for Calibre preprocessing steps."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Iterable, TypeVar


T = TypeVar("T")

_SRC_DIR = Path(__file__).resolve().parents[2]
_PIPELINE_CONFIG_PATH = _SRC_DIR / "template" / "pipeline_config.json"


def load_json_config(filename: str) -> dict[str, object]:
    with (_SRC_DIR / "template" / filename).open(encoding="utf-8") as handle:
        return json.load(handle)


def pipeline_config() -> dict[str, object]:
    with _PIPELINE_CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def default_batch() -> str:
    config = pipeline_config()
    latest_db = latest_incremental_db(Path(str(config["defaults"]["input_dir"])))
    latest_batch = batch_from_metadata_path(latest_db) if latest_db is not None else None
    return latest_batch or str(config["defaults"]["batch"])


def default_input_dir() -> Path:
    return Path(str(pipeline_config()["defaults"]["input_dir"]))


def default_output_dir(batch: str | None = None) -> Path:
    config = pipeline_config()
    selected_batch = batch or default_batch()
    return Path(str(config["defaults"]["output_root"])) / selected_batch


def default_full_db_dir() -> Path:
    return Path(str(pipeline_config()["defaults"]["full_db_dir"]))


def default_log_dir() -> Path:
    return Path(str(pipeline_config()["defaults"]["log_dir"]))


def step_dir(output_dir: Path, step_name: str) -> Path:
    return output_dir / str(pipeline_config()["step_dirs"][step_name])


def display_path(path: Path) -> str:
    """Return a cwd-relative path for logs when possible."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def resolve_incremental_db(
    batch: str,
    incremental_dir: Path | None,
    output_dir: Path | None = None,
    *,
    include_step2_repair: bool = False,
    include_step1: bool = False,
    include_step2_apply: bool = True,
) -> Path:
    """按优先级挑出本批要处理的增量库。

    ``include_step1`` 打开时，把 Step1 的去重副本排在 Step2 之前——它是原始输入
    去重后的版本，Step2 应读它而不是原始输入。

    ``include_step2_repair`` 打开时，把 Step2 的编码修复副本排在**最前**——它是输入库的
    无损修复版，元数据比原始输入更可信；文件名带日期，只有「本批」才会命中，
    不会用旧批次的产物顶替新库。Step2 repair 自身必须用默认值（``False``）调用，否则会
    读到自己上一次的产物。

    ``include_step2_apply`` 控制是否把 Step2 apply 的 ``metadata.cleaned.db`` 纳入候选，
    默认与历史行为一致。
    """
    compact_batch = batch.replace("-", "")
    config = pipeline_config()
    candidates: list[Path] = []
    if include_step2_repair and output_dir is not None:
        candidates.append(
            step_dir(output_dir, "step2")
            / str(config["step2"]["repair"]["encoded_db_filename"]).format(date=compact_batch)
        )
    if include_step1 and output_dir is not None:
        step1_config = config.get("step1", {})
        candidates.append(
            step_dir(output_dir, "step1")
            / str(step1_config.get("deduped_db_filename", "metadata-{date}.deduped.db")).format(date=compact_batch)
        )
    if incremental_dir is not None:
        if incremental_dir.is_file():
            candidates.append(incremental_dir)
        else:
            latest_db = latest_incremental_db(incremental_dir)
            if latest_db is not None:
                candidates.append(latest_db)
            candidates.append(incremental_dir / "metadata.db")
    if output_dir is not None and include_step2_apply:
        candidates.append(step_dir(output_dir, "step3") / str(config["step3"]["cleaned_db_filename"]))
    latest_default_db = latest_incremental_db(Path(str(config["defaults"]["input_dir"])))
    if latest_default_db is not None:
        candidates.append(latest_default_db)
    candidates.append(
        Path(str(config["defaults"]["input_dir"]))
        / str(config["patterns"]["incremental_db"]).format(date=compact_batch)
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0]


def latest_incremental_db(input_dir: Path) -> Path | None:
    candidates = [path for path in input_dir.glob("metadata-*.db") if date_key_from_metadata_path(path)]
    return max(candidates, key=lambda path: date_key_from_metadata_path(path) or "", default=None)


def date_key_from_metadata_path(path: Path) -> str | None:
    """从 ``metadata-YYYYMMDD[.标记].db`` 提取日期。

    允许中间夹一段标记，是为了认下 Step0 的产物 ``metadata-20260923.encoded.db``
    —— 它替代原始输入进入下游，若取不到日期，``matching_full_db`` 会直接报错。
    """
    match = re.search(r"metadata-(\d{8})(?:\.[A-Za-z0-9]+)*\.db$", path.name)
    return match.group(1) if match else None


def matching_full_db(metadata_db: Path, full_library_dir: Path | None = None) -> Path:
    config = pipeline_config()
    full_library_dir = full_library_dir or Path(str(config["defaults"]["full_db_dir"]))
    date_key = date_key_from_metadata_path(metadata_db)
    if date_key is None:
        raise ValueError(f"无法从增量库文件名提取日期： {metadata_db}")
    return full_library_dir / str(config["patterns"]["full_db"]).format(date=date_key)


def batch_from_metadata_path(metadata_db: Path) -> str | None:
    date_key = date_key_from_metadata_path(metadata_db)
    if date_key is None:
        return None
    return f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:]}"


def align_batch_to_metadata_db(batch: str, metadata_db: Path, output_dir: Path) -> tuple[str, Path]:
    """把 ``batch`` / ``output_dir`` 对齐到实际读到的增量库日期。

    显式传 ``--batch`` 时可能与输入目录里最新那个库的日期不符；此时以库为准，
    否则产物会写进一个与数据不匹配的批次目录。``output_dir`` 不是以 ``batch``
    结尾（调用方自定了路径）时保持原样。
    """
    resolved_batch = batch_from_metadata_path(metadata_db)
    if resolved_batch is not None and output_dir.name == batch:
        return resolved_batch, output_dir.parent / resolved_batch
    return batch, output_dir


def resolve_incremental_entity_dir(incremental_dir: Path | None, metadata_db: Path) -> Path:
    if incremental_dir is not None and incremental_dir.is_dir():
        return incremental_dir.resolve()
    if incremental_dir is not None and incremental_dir.is_file():
        return incremental_dir.parent.resolve()
    return metadata_db.parent.resolve()


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_dict_csv(path: Path, rows: Iterable[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def dataclass_fieldnames(cls: type[T]) -> list[str]:
    if not is_dataclass(cls):
        raise TypeError(f"{cls!r} is not a dataclass")
    return [field.name for field in fields(cls)]


def row_value(row: dict[str, str], key: str, default: str = "") -> str:
    value = row.get(key, default)
    return value if value is not None else default


def normalize_title(value: str) -> str:
    """Normalize title for dedup grouping: lowercase, unify parentheses, collapse whitespace."""
    normalized = value.strip().lower()
    normalized = normalized.replace("（", "(").replace("）", ")")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def cleanup_orphan_link_records(conn: "sqlite3.Connection", book_ids: list[int] | set[int]) -> None:
    """Delete link-table records whose book no longer exists in books table."""
    if not book_ids:
        return
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT m.name FROM sqlite_master m "
            "JOIN pragma_table_info(m.name) i "
            "WHERE m.type='table' AND i.name='book' "
            "GROUP BY m.name"
        ).fetchall()
    ]
    placeholders = ",".join("?" * len(book_ids))
    for table in tables:
        if table == "data":
            continue
        conn.execute(f'DELETE FROM "{table}" WHERE book IN ({placeholders})', list(book_ids))


def db_table_counts(db_path: Path) -> tuple[int, int]:
    """Return (books_count, data_count) for a Calibre metadata database."""
    import sqlite3
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        books = int(conn.execute("SELECT COUNT(*) FROM books").fetchone()[0])
        data = int(conn.execute("SELECT COUNT(*) FROM data").fetchone()[0])
    return books, data
