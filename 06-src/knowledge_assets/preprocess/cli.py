"""Command line entrypoint for the Calibre preprocessing pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[2]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from knowledge_assets.utils.logger import get_logger, setup_logger

from knowledge_assets.preprocess.step1_dedup import run_step1
from knowledge_assets.preprocess.step2_anomaly import (
    run_step2_anomaly,
    run_step2_anomaly_scan,
    run_step2_anomaly_apply,
)
from knowledge_assets.preprocess.step3_regression import run_step3_regression
from knowledge_assets.preprocess.step4_sql import run_step4_sql
from knowledge_assets.preprocess.step4_apply import run_step4_apply
from knowledge_assets.preprocess.step4_verify import run_step4_verify
from knowledge_assets.preprocess.step5_compare import (
    run_step5,
    run_step5_compare,
    run_step5_plan,
    run_step5_apply,
    run_step5_validate_manual,
)
from knowledge_assets.preprocess.common import (
    default_batch,
    default_full_db_dir,
    default_input_dir,
    default_log_dir,
    default_output_dir,
    load_json_config,
)


def _build_parser() -> argparse.ArgumentParser:
    step5_config = load_json_config("step4_config.json")
    batch = default_batch()
    parser = argparse.ArgumentParser(
        prog="knowledge_assets.preprocess",
        description="Calibre 增量预处理库清洗流水线",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── Step1: 前置预处理 ────────────────────────────────────────────────────
    step1 = subparsers.add_parser(
        "step1",
        help="Step1：前置预处理（去重「title_bytes_initPath」→ 垃圾标题 → 非目标格式），自动执行，无闸口",
    )
    step1.add_argument("--batch", default=batch)
    step1.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step1.add_argument("--output-dir", default=None, type=Path)
    step1.add_argument("--log-dir", default=default_log_dir(), type=Path)

    # ── Step2: 异常处理 ──────────────────────────────────────────────────────
    step2 = subparsers.add_parser(
        "step2",
        help="Step2：异常处理【乱码→副本标题→未知标题】，需要人工确认，有闸口",
    )
    step2.add_argument("--batch", default=batch)
    step2.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step2.add_argument("--output-dir", default=None, type=Path)
    step2.add_argument("--log-dir", default=default_log_dir(), type=Path)
    step2_subparsers = step2.add_subparsers(dest="step2_phase")

    step2_scan = step2_subparsers.add_parser(
        "scan",
        help="Step2 scan：只读扫描，输出异常处理 CSV",
    )
    step2_scan.add_argument("--batch", default=batch)
    step2_scan.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step2_scan.add_argument("--output-dir", default=None, type=Path)
    step2_scan.add_argument("--log-dir", default=default_log_dir(), type=Path)

    step2_apply = step2_subparsers.add_parser(
        "apply",
        help="Step2 apply：读取 CSV 并执行异常修复",
    )
    step2_apply.add_argument("--batch", default=batch)
    step2_apply.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step2_apply.add_argument("--output-dir", default=None, type=Path)
    step2_apply.add_argument("--log-dir", default=default_log_dir(), type=Path)

    # ── Step3: 回归验证 ──────────────────────────────────────────────────────
    step3 = subparsers.add_parser(
        "step3",
        help="Step3：回归验证【去重→垃圾标题→非目标格式→乱码→副本标题→未知标题】，需要人工确认，有闸口",
    )
    step3.add_argument("--batch", default=batch)
    step3.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step3.add_argument("--output-dir", default=None, type=Path)
    step3.add_argument("--log-dir", default=default_log_dir(), type=Path)

    # ── Step4: 自清洗 SQL 生成 + 执行 ────────────────────────────────────────
    step4 = subparsers.add_parser(
        "step4",
        help="Step4：汇总所有已确认 CSV 生成自清洗 SQL + BAT，自动执行，无闸口",
    )
    step4.add_argument("--batch", default=batch)
    step4.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step4.add_argument("--output-dir", default=None, type=Path)
    step4.add_argument("--log-dir", default=default_log_dir(), type=Path)
    step4_subparsers = step4.add_subparsers(dest="step4_phase")

    step4_sql = step4_subparsers.add_parser(
        "sql",
        help="Step4 sql：根据确认 CSV 生成自清洗 SQL + BAT",
    )
    step4_sql.add_argument("--batch", default=batch)
    step4_sql.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step4_sql.add_argument("--output-dir", default=None, type=Path)
    step4_sql.add_argument("--log-dir", default=default_log_dir(), type=Path)

    step4_apply = step4_subparsers.add_parser(
        "apply",
        help="Step4 apply：执行自清洗 SQL 并验证",
    )
    step4_apply.add_argument("--batch", default=batch)
    step4_apply.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step4_apply.add_argument("--output-dir", default=None, type=Path)
    step4_apply.add_argument("--log-dir", default=default_log_dir(), type=Path)

    step4_verify = step4_subparsers.add_parser(
        "verify",
        help="Step4 verify：验证 cleaned.db 中 data 记录的物理文件是否存在",
    )
    step4_verify.add_argument("--batch", default=batch)
    step4_verify.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step4_verify.add_argument("--output-dir", default=None, type=Path)
    step4_verify.add_argument("--log-dir", default=default_log_dir(), type=Path)
    step4_verify.add_argument("--library-dir", default=None, type=Path, help="Calibre 电子书库目录（默认 E:/98-Calibre-books-new）")

    # ── Step5: 比对 ──────────────────────────────────────────────────────────
    step5 = subparsers.add_parser(
        "step5",
        help="Step5：比对清洗好的增量预处理库与全量预处理库，自动执行，无闸口",
    )
    step5.add_argument("--batch", default=batch)
    step5.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step5.add_argument("--full-library-dir", default=default_full_db_dir(), type=Path)
    step5.add_argument("--output-dir", default=None, type=Path)
    step5.add_argument("--log-dir", default=default_log_dir(), type=Path)
    step5_subparsers = step5.add_subparsers(dest="step5_phase")

    step5_scan = step5_subparsers.add_parser(
        "scan",
        help="Step5 scan：比对清洗好的增量库与全量库，生成差异 CSV",
    )
    step5_scan.add_argument("--batch", default=batch)
    step5_scan.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step5_scan.add_argument("--full-library-dir", default=default_full_db_dir(), type=Path)
    step5_scan.add_argument("--output-dir", default=None, type=Path)
    step5_scan.add_argument("--log-dir", default=default_log_dir(), type=Path)
    step5_scan.add_argument("--force", action="store_true")
    step5_scan.add_argument(
        "--metadata-conflict",
        choices=tuple(step5_config["metadata_conflict"]["valid"]),
        default=step5_config["metadata_conflict"]["default"],
    )

    step5_validate = step5_subparsers.add_parser(
        "validate",
        help="Step5 validate：检查待人工确认 CSV 是否已填写完整",
    )
    step5_validate.add_argument("--batch", default=batch)
    step5_validate.add_argument("--output-dir", default=None, type=Path)
    step5_validate.add_argument("--log-dir", default=default_log_dir(), type=Path)

    step5_plan = step5_subparsers.add_parser(
        "plan",
        help="Step5 plan：根据确认 CSV 生成合并 SQL",
    )
    step5_plan.add_argument("--batch", default=batch)
    step5_plan.add_argument("--output-dir", default=None, type=Path)
    step5_plan.add_argument("--log-dir", default=default_log_dir(), type=Path)

    step5_apply = step5_subparsers.add_parser(
        "apply",
        help="Step5 apply：执行合并 SQL 并更新全量库",
    )
    step5_apply.add_argument("--batch", default=batch)
    step5_apply.add_argument("--incremental-dir", default=default_input_dir(), type=Path)
    step5_apply.add_argument("--full-library-dir", default=default_full_db_dir(), type=Path)
    step5_apply.add_argument("--output-dir", default=None, type=Path)
    step5_apply.add_argument("--log-dir", default=default_log_dir(), type=Path)
    step5_apply.add_argument("--force", action="store_true")
    step5_apply.add_argument(
        "--metadata-conflict",
        choices=tuple(step5_config["metadata_conflict"]["valid"]),
        default=step5_config["metadata_conflict"]["default"],
    )

    return parser


def _log_compat(message: str) -> None:
    get_logger("preprocess.cli").info(message)


def _resolve_default_output_dir(args: argparse.Namespace) -> None:
    if getattr(args, "output_dir", None) is None:
        args.output_dir = default_output_dir(args.batch)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    _resolve_default_output_dir(args)
    setup_logger(log_dir=str(args.log_dir))

    # ── Step1: 前置预处理 ────────────────────────────────────────────────────
    if args.command == "step1":
        run_step1(
            batch=args.batch,
            incremental_dir=args.incremental_dir,
            output_dir=args.output_dir,
        )
        return

    # ── Step2: 异常处理 ──────────────────────────────────────────────────────
    if args.command == "step2":
        phase = getattr(args, "step2_phase", None)

        if phase == "scan":
            run_step2_anomaly_scan(
                batch=args.batch,
                incremental_dir=args.incremental_dir,
                output_dir=args.output_dir,
            )
            return

        if phase == "apply":
            run_step2_anomaly_apply(
                batch=args.batch,
                incremental_dir=args.incremental_dir,
                output_dir=args.output_dir,
            )
            return

        if phase is None:
            _log_compat("step2 默认执行完整流程（scan + apply）")
            run_step2_anomaly(
                batch=args.batch,
                incremental_dir=args.incremental_dir,
                output_dir=args.output_dir,
            )
            return

        parser.error(f"Unsupported step2 phase: {phase}")

    # ── Step3: 回归验证 ──────────────────────────────────────────────────────
    if args.command == "step3":
        run_step3_regression(
            batch=args.batch,
            incremental_dir=args.incremental_dir,
            output_dir=args.output_dir,
        )
        return

    # ── Step4: 自清洗 SQL 生成 + 执行 ────────────────────────────────────────
    if args.command == "step4":
        phase = getattr(args, "step4_phase", None)

        if phase == "sql":
            run_step4_sql(
                batch=args.batch,
                output_dir=args.output_dir,
                incremental_dir=args.incremental_dir,
            )
            return

        if phase == "apply":
            run_step4_apply(
                batch=args.batch,
                incremental_dir=args.incremental_dir,
                output_dir=args.output_dir,
            )
            return

        if phase == "verify":
            run_step4_verify(
                batch=args.batch,
                output_dir=args.output_dir,
                incremental_dir=args.incremental_dir,
                library_dir=args.library_dir,
            )
            return

        parser.error(f"Unsupported step4 phase: {phase}")

    # ── Step5: 比对 ──────────────────────────────────────────────────────────
    if args.command == "step5":
        phase = getattr(args, "step5_phase", None) or "scan"

        if phase == "scan":
            run_step5_compare(
                batch=args.batch,
                incremental_dir=args.incremental_dir,
                full_library_dir=args.full_library_dir,
                output_dir=args.output_dir,
                force=args.force,
                metadata_conflict=args.metadata_conflict,
            )
            return

        if phase == "validate":
            run_step5_validate_manual(
                batch=args.batch,
                output_dir=args.output_dir,
            )
            return

        if phase == "plan":
            run_step5_plan(
                batch=args.batch,
                output_dir=args.output_dir,
            )
            return

        if phase == "apply":
            run_step5_apply(
                batch=args.batch,
                incremental_dir=args.incremental_dir,
                full_library_dir=args.full_library_dir,
                output_dir=args.output_dir,
                force=args.force,
                metadata_conflict=args.metadata_conflict,
            )
            return

        if getattr(args, "step5_phase", None) is None:
            _log_compat("step5 默认执行 scan（比对增量库与全量库，生成差异 CSV）")
            run_step5(
                batch=args.batch,
                incremental_dir=args.incremental_dir,
                full_library_dir=args.full_library_dir,
                output_dir=args.output_dir,
                force=args.force,
                metadata_conflict=args.metadata_conflict,
            )
            return

        parser.error(f"Unsupported step5 phase: {phase}")

    parser.error(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
