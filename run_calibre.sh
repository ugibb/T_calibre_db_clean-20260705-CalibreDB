#!/usr/bin/env bash
#
# Calibre 增量预处理流水线执行脚本
#
# 用法：
#   ./run_calibre.sh                           # 不带参数 = auto，连跑非破坏性步骤
#   ./run_calibre.sh status                    # 查看当前批次进度与下一步人工动作
#   ./run_calibre.sh 1                         # Step1  前置预处理：去重→垃圾标题→非目标格式
#   ./run_calibre.sh step2                     # Step2  异常处理：乱码→副本标题→未知标题（scan → 确认 → apply）
#   ./run_calibre.sh step2 scan                #          只读扫描，输出异常处理 CSV
#   ./run_calibre.sh step2 apply               #          读取 CSV 并执行异常修复
#   ./run_calibre.sh 3                         # Step3  回归验证：去重→垃圾标题→非目标格式→乱码→副本标题→未知标题
#   ./run_calibre.sh step4                     # Step4  汇总已确认 CSV 生成自清洗 SQL + BAT → 执行
#   ./run_calibre.sh step4 sql                 #          根据确认 CSV 生成自清洗 SQL + BAT
#   ./run_calibre.sh step4 apply               #          执行自清洗 SQL 并验证
#   ./run_calibre.sh 5                         # Step5  比对清洗好的增量预处理库与全量预处理库
#   ./run_calibre.sh step5 scan                #          比对清洗好的增量库与全量库
#   ./run_calibre.sh step5 validate            #          检查待人工确认 CSV 是否已填写完整
#   ./run_calibre.sh step5 plan                #          根据确认 CSV 生成合并 SQL
#   ./run_calibre.sh step5 apply -y            #          执行合并 SQL 并更新全量库
#   ./run_calibre.sh auto                      # 连跑 Step1 + Step2 scan + Step3，停在人工确认处
#
# 设计要点：
#   - 始终切到项目根目录再执行（cli.py 的相对路径与日志显示依赖 cwd）
#   - 目录约定从 06-src/template/pipeline_config.json 读取，不在脚本里硬编码
#   - 批次默认由 03-input/01-Calibre/metadata-*.db 中日期最新的文件推断
#   - step5 apply 会修改全量预处理库，需 -y/--yes 显式确认
#
# 详见 06-src/knowledge_assets/README.md

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"
CLI="06-src/knowledge_assets/preprocess/cli.py"
CONFIG="06-src/template/pipeline_config.json"

# ── 辅助函数 ────────────────────────────────────────────────────────────────

die() {
    printf '错误： %s\n' "$1" >&2
    exit 1
}

_cfg() {
    "$PYTHON" - "$CONFIG" "$1" "$2" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
print(config[sys.argv[2]][sys.argv[3]])
PY
}

# 从 input_dir 下日期最新的 metadata-YYYYMMDD.db 推断批次（YYYY-MM-DD）
_detect_batch() {
    local latest name key
    latest="$(_latest_metadata_db)"
    if [ -n "$latest" ]; then
        name="$(basename "$latest")"
        key="${name#metadata-}"
        key="${key%.db}"
        printf '%s-%s-%s\n' "${key:0:4}" "${key:4:2}" "${key:6:2}"
    else
        _cfg defaults batch
    fi
}

# 目录不存在时 find 会返回非零，必须兜住，否则 set -e 会静默终止脚本
_latest_metadata_db() {
    { find "$INPUT_DIR" -maxdepth 1 -type f -name 'metadata-*.db' 2>/dev/null || true; } | sort | tail -n 1
}

_latest_full_db() {
    { find "$FULL_DB_DIR" -maxdepth 1 -type f -name 'metadata-full-*.db' 2>/dev/null || true; } | sort | tail -n 1
}

_count_files() {
    { find "$1" -maxdepth 1 -type f -name "$2" 2>/dev/null || true; } | wc -l | tr -d ' '
}

_human_size() {
    "$PYTHON" - "$1" <<'PY'
import sys
from pathlib import Path

size = Path(sys.argv[1]).stat().st_size
for unit in ("B", "KB", "MB", "GB"):
    if size < 1024 or unit == "GB":
        print(f"{size:.1f} {unit}")
        break
    size /= 1024
PY
}

_progress_line() {
    if [ -e "$2" ]; then
        printf '  [✓] %-10s %s\n' "$1" "$2"
    else
        printf '  [ ] %-10s  缺少 %s\n' "$1" "$2"
    fi
}

# 透传给 cli.py 的统一入口：$1=step $2=phase，其余为额外参数
# 用户自己传了 --batch 就不再注入默认值，避免出现两个 --batch
_run_cli() {
    local step="$1" phase="$2" arg has_batch=0
    shift 2
    for arg in "$@"; do
        case "$arg" in
            --batch | --batch=*) has_batch=1 ;;
        esac
    done

    local args=()
    if [ "$has_batch" -eq 0 ]; then
        args=(--batch "$BATCH")
    fi
    if [ "$#" -gt 0 ]; then
        args+=("$@")
    fi

    # phase 传空串时不能把它当位置参数递给 argparse
    local cmd=("$step")
    if [ -n "$phase" ]; then
        cmd+=("$phase")
    fi

    printf '\n───── python3 %s %s %s\n\n' "$CLI" "${cmd[*]}" "${args[*]}"
    "$PYTHON" "$CLI" "${cmd[@]}" ${args[@]+"${args[@]}"}
}

_gate_notice() {
    cat <<'TXT'

────────────────────────────────────────────────────────────────
人工闸口：
  闸口 1：确认 step2/ 异常处理 CSV（乱码→副本标题→未知标题）
  闸口 2：确认 step3/ 回归验证 CSV
  闸口 3：填写 step5/ 待人工确认 CSV 的 human_decision 列
详见 06-src/knowledge_assets/README.md 的「人工闸口」一节
────────────────────────────────────────────────────────────────
TXT
}

_require_file() {
    [ -e "$1" ] || die "缺少 $1，先执行： $2"
}

# ── 命令 ────────────────────────────────────────────────────────────────────

cmd_status() {
    local out_dir latest_full
    out_dir="$OUTPUT_ROOT/$BATCH"
    latest_full="$(_latest_full_db)"

    printf '批次          %s\n' "$BATCH"
    if [ -n "$(_latest_metadata_db)" ]; then
        local input_db
        input_db="$(_latest_metadata_db)"
        printf '输入增量库    %s (%s)\n' "$input_db" "$(_human_size "$input_db")"
    else
        printf '输入增量库    （%s 下没有 metadata-*.db）\n' "$INPUT_DIR"
    fi
    printf '输出目录      %s\n' "$out_dir"
    if [ -n "$latest_full" ]; then
        printf '全量库基准    %s ← %s\n' "$FULL_DB_DIR" "$(basename "$latest_full")"
    else
        printf '全量库基准    %s（目录内没有 metadata-full-*.db）\n' "$FULL_DB_DIR"
    fi

    printf '\n进度：\n'
    _progress_line 'Step1' "$out_dir/step1/metadata-${BATCH//-/}.deduped.db"
    _progress_line 'Step2 anomaly' "$out_dir/step2/metadata-${BATCH//-/}.encoded.db"
    _progress_line 'Step3 regression' "$out_dir/step3/step3_scan_snapshot.json"
    _progress_line 'Step4 sql' "$out_dir/step4/step4_cleaning_instructions.sql"
    _progress_line 'Step4 apply' "$out_dir/step3/metadata.cleaned.db"
    _progress_line 'Step5 scan' "$out_dir/step5/5-1-1：系统确认-全量库不存在可新增.csv"
    _progress_line 'Step5 plan' "$out_dir/step5/step5_merge_instructions.sql"

    printf '\n'
    if [ ! -e "$out_dir/step1/metadata-${BATCH//-/}.deduped.db" ]; then
        printf '下一步： ./run_calibre.sh 1（前置预处理，去重+垃圾标题+非目标格式）\n'
    elif [ ! -e "$out_dir/step2/metadata-${BATCH//-/}.encoded.db" ]; then
        printf '下一步： ./run_calibre.sh step2（异常处理：乱码→副本标题→未知标题）\n'
    elif [ ! -e "$out_dir/step3/step3_scan_snapshot.json" ]; then
        printf '下一步： ./run_calibre.sh 3（回归验证扫描）\n'
    elif [ ! -e "$out_dir/step4/step4_cleaning_instructions.sql" ]; then
        printf '下一步： ./run_calibre.sh step4 sql（生成自清洗 SQL + BAT）\n'
    elif [ ! -e "$out_dir/step3/metadata.cleaned.db" ]; then
        printf '下一步： 核对 step4/ SQL 后 ./run_calibre.sh step4 apply\n'
    elif [ ! -e "$out_dir/step5/step5_merge_instructions.sql" ]; then
        printf '下一步： 填写 step5/ 待人工确认 CSV 后 ./run_calibre.sh step5 plan\n'
    else
        printf '下一步： 核对 step5/step5_merge_instructions.sql 后 ./run_calibre.sh step5 apply -y\n'
    fi
}

cmd_step1() {
    _run_cli step1 "" "$@"
}

cmd_step2() {
    local phase="${1:-}"
    if [[ "$phase" == -* ]] || [ -z "$phase" ]; then
        _run_cli step2 "" "$@"
        return
    fi
    shift
    case "$phase" in
        scan) _run_cli step2 scan "$@" ;;
        apply) _run_cli step2 apply "$@" ;;
        *) die "step2 需要子阶段：scan / apply（或直接 step2 执行完整流程）" ;;
    esac
}

cmd_step3() {
    _run_cli step3 "" "$@"
}

cmd_step4() {
    local phase="${1:-}"
    if [[ "$phase" == -* ]] || [ -z "$phase" ]; then
        # 默认连跑 sql + apply
        _run_cli step4 sql "$@"
        _run_cli step4 apply "$@"
        return
    fi
    shift
    case "$phase" in
        sql) _run_cli step4 sql "$@" ;;
        apply) _run_cli step4 apply "$@" ;;
        *) die "step4 需要子阶段：sql / apply（或直接 step4 连跑）" ;;
    esac
}

cmd_step5() {
    local phase="${1:-scan}"
    if [[ "$phase" == -* ]]; then
        _run_cli step5 scan "$@"
        return
    fi
    [ "$#" -eq 0 ] || shift
    case "$phase" in
        scan)
            _run_cli step5 scan "$@"
            ;;
        validate | plan) _run_cli step5 "$phase" "$@" ;;
        apply)
            _confirm_full_db_write "$@"
            ;;
        *) die "step5 需要子阶段：scan / validate / plan / apply" ;;
    esac
}

# step5 apply 是唯一会写全量预处理库的步骤，单独加一道确认
_confirm_full_db_write() {
    local assume_yes=0 arg latest answer
    local passthru=()
    for arg in "$@"; do
        case "$arg" in
            -y | --yes) assume_yes=1 ;;
            *) passthru+=("$arg") ;;
        esac
    done

    latest="$(_latest_full_db)"
    printf '\n⚠️   Step5 apply 会直接修改全量预处理库，并复制需要新增的实体文件。\n'
    printf '    全量库目录： %s\n' "$FULL_DB_DIR"
    printf '    当前基准  ： %s\n' "${latest:-（无，Step5 会初始化一个）}"
    if [ "$assume_yes" -ne 1 ]; then
        printf '    确认继续请输入 yes 并回车： '
        read -r answer
        [ "$answer" = "yes" ] || die "已取消。"
    fi
    _run_cli step5 apply ${passthru[@]+"${passthru[@]}"}
}

# 连跑所有非破坏性步骤：Step1 + Step2 scan + Step3，停在人工确认处
cmd_auto() {
    printf '批次 %s：执行 Step1 + Step2 scan + Step3（均为非破坏性步骤）\n' "$BATCH"
    _run_cli step1 ""
    _run_cli step2 scan
    _run_cli step3 ""
    _gate_notice
}

# 数字快捷方式：跑完该步骤的完整流程，在破坏性操作前暂停确认
_has_help_flag() {
    local arg
    for arg in "$@"; do
        case "$arg" in -h | --help) return 0 ;; esac
    done
    return 1
}

cmd_step2_full() {
    if _has_help_flag "$@"; then
        _run_cli step2 scan "$@"
        return
    fi
    printf '──── Step2 完整流程：scan → 确认 → apply ────\n'
    _run_cli step2 scan "$@"
    _gate_notice
    printf '\n请确认 step2/ 异常处理 CSV（乱码→副本标题→未知标题）。\n'
    printf '确认无误后输入 yes 执行修复（在库副本上操作，不改原始库）： '
    local answer
    read -r answer
    [ "$answer" = "yes" ] || die "已取消。"
    _run_cli step2 apply "$@"
}

cmd_step5_full() {
    if _has_help_flag "$@"; then
        _run_cli step5 scan "$@"
        return
    fi
    printf '──── Step5 完整流程：scan → validate → plan → 确认 → apply ────\n'
    _require_file "$OUTPUT_ROOT/$BATCH/step3/metadata.cleaned.db" \
        './run_calibre.sh step4 apply'
    _run_cli step5 scan "$@"

    local step5_dir="$OUTPUT_ROOT/$BATCH/step5"
    local csv_count
    csv_count="$(_count_files "$step5_dir" '*.csv')"
    if [ "$csv_count" -eq 0 ]; then
        printf '\n──── Step5 scan 完成（无比对目标全量库） ────\n'
        printf '未找到历史全量库，scan 未生成比对 CSV。\n'
        printf '所有记录将在 apply 时直接初始化全量库。\n'
        printf '确认后将直接执行 apply，跳过 validate / plan。\n'
        _confirm_full_db_write
        return
    fi

    _run_cli step5 validate
    _run_cli step5 plan
    _gate_notice
    _confirm_full_db_write
}

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

# ── 入口 ────────────────────────────────────────────────────────────────────

[ -f "$CLI" ] || die "找不到 CLI： $CLI"
command -v "$PYTHON" >/dev/null 2>&1 || die "找不到解释器： $PYTHON（可用 PYTHON=... 覆盖）"

INPUT_DIR="$(_cfg defaults input_dir)"
OUTPUT_ROOT="$(_cfg defaults output_root)"
FULL_DB_DIR="$(_cfg defaults full_db_dir)"
BATCH="$(_detect_batch)"

case "${1:-}" in
    status) shift; cmd_status ;;
    step1) shift; cmd_step1 "$@" ;;
    1) shift; cmd_step1 "$@" ;;
    step2) shift; cmd_step2 "$@" ;;
    2) shift; cmd_step2_full "$@" ;;
    step3) shift; cmd_step3 "$@" ;;
    3) shift; cmd_step3 "$@" ;;
    step4) shift; cmd_step4 "$@" ;;
    4) shift; cmd_step4 "$@" ;;
    step5) shift; cmd_step5 "$@" ;;
    5) shift; cmd_step5_full "$@" ;;
    auto | '') shift || true; cmd_auto "$@" ;;
    help | -h | --help) usage ;;
    *) die "未知命令： $1（可用 help 查看用法）" ;;
esac
