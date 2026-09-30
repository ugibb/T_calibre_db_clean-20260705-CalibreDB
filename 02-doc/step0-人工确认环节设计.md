# Step0 人工确认环节设计

## 背景

当前 Step0 是一步到位：扫描 → 复制副本 → 修复 → 写 CSV 报告。`status=ready` 的记录直接修了，人工没有机会在修复前审阅或干预。

需求：增加人工确认闸口——先扫描出 CSV，人工编辑后确认，再执行修复。

## 现状分析

Step0 当前产出三类 CSV：

| 文件 | 内容 | 当前行为 |
|------|------|----------|
| `step0_1_encode_repair.csv` | 编码探针修复 + BookInitPath 回补 + 不可修复项 | `repaired` 已写库，`unresolved` 仅报告 |
| `step0_2_title_copy_repair.csv` | 副本标题修复（BookInitPath → title） | `ready` 已写库，`unresolved` 仅报告 |
| `step0_3_basename_repair.csv` | 未知文件名修复（BookInitPath → data.name） | `ready` 已写库，`unresolved` 仅报告 |

## 设计方案

### 拆分为 scan + apply 两阶段

```
step0 scan   →  扫描 + 输出 CSV（所有 status 写初始值，不写修复库）
  ↓ 人工编辑 CSV（修正 title_candidate、改 status 等）
  ↓ 控制台输入 yes
step0 apply  →  读取编辑后的 CSV，按 status 执行修复，输出 encoded.db
```

### CSV 格式调整

三份 CSV 统一增加/调整 `status` 列语义：

| status 值 | 含义 | apply 行为 |
|-----------|------|------------|
| `ready` | scan 预填，表示系统认为可自动修复 | 执行修复 |
| `skip` | 人工标记跳过 | 不修复，保留原始值 |
| `unresolved` | 系统无法判断 | 不修复 |
| `repaired` | apply 执行后回写 | 最终状态 |

**人工可编辑的列：**

- `step0_1_encode_repair.csv`：`修复后`、`status`
- `step0_2_title_copy_repair.csv`：`title_candidate`、`status`
- `step0_3_basename_repair.csv`：`file_basename_candidate`、`status`

人工可以把 `status` 改为 `skip` 来跳过某条修复，也可以修正候选值后再让 apply 执行。

### scan 阶段行为

1. 只读输入库，扫描所有乱码/副本/未知文件名
2. 输出三份 CSV，所有可修复项 `status=ready`，不可修复项 `status=unresolved`
3. **不复制副本、不写修复库**
4. 输出扫描摘要（已发现 X 条可修复、Y 条待人工）

### apply 阶段行为

1. 读取三份 CSV
2. 复制输入库为 `encoded.db`
3. 对 `status=ready` 的记录执行修复（使用 CSV 中的候选值，人工可能已修改）
4. 修复成功的回写 `status=repaired`
5. 唯一约束冲突的降级为 `status=unresolved`，原因写 `constraint_conflict`
6. 输出最终摘要 + 更新 CSV

### CLI 结构

```
cli.py step0 scan   [--batch ...] [--incremental-dir ...] [--output-dir ...]
cli.py step0 apply  [--batch ...] [--incremental-dir ...] [--output-dir ...]
```

`run_step0()` 拆为 `run_step0_scan()` 和 `run_step0_apply()`。

保留 `--dry-run` 语义不变（仅 scan 阶段有效，apply 阶段忽略）。

### run_calibre.sh 集成

```bash
cmd_step0_full() {
    printf '──── Step0 完整流程：scan → 人工确认 → apply ────\n'
    _run_cli step0 scan "$@"
    _gate_notice
    printf '\n请编辑 step0/step0_1/2/3 CSV 后输入 yes 执行修复： '
    read -r answer
    [ "$answer" = "yes" ] || die "已取消。"
    _run_cli step0 apply
}
```

数字快捷键 `0` 走完整流程，`step0` 走子阶段模式（与 step1/step2 一致）。

### 向后兼容

- `step0` 不带子阶段时默认走 `scan`（与 step1/step2 一致）
- `--dry-run` 等价于 `step0 scan`（scan 本身就不写库）
- 下游 step1 读取 `encoded.db` 的逻辑不变

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `step0_repair.py` | 拆 `run_step0` → `run_step0_scan` + `run_step0_apply`；新增 `_read_csv_handoffs` 读取人工编辑后的 CSV |
| `cli.py` | step0 增加子解析器 `scan` / `apply` |
| `run_calibre.sh` | `cmd_step0` 改为子阶段模式，新增 `cmd_step0_full` |
| `pipeline_config.json` | 无变更（CSV 文件名已配好） |

## 执行顺序

1. 改造 `step0_repair.py`：拆 scan/apply，apply 从 CSV 读修复指令
2. 更新 `cli.py`：step0 子命令
3. 更新 `run_calibre.sh`：完整流程 + 确认闸口
4. 测试：scan → 检查 CSV → apply → 验证 encoded.db
