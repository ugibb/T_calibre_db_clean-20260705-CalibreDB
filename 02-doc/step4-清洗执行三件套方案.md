# Step4 清洗执行方案

> 日期：2026-09-29
> 状态：待确认

---

## 背景

Step4 正向 BAT（`step2_cleaning_instructions.bat`）将书籍目录从书库移动到 quarantine。
需要配套一个回退 BAT（撤销移动）。

验证逻辑不需要单独设计——`step4 verify`（已实现）检查 `metadata.cleaned.db` 中所有 data 记录对应的物理文件是否存在，执行前后各跑一次即可。

---

## 一、正向 BAT（已有）

**文件**：`step2_cleaning_instructions.bat`

- 从书库 → quarantine
- 按 book_id 整目录移动
- 移动后清理空父目录

---

## 二、回退 BAT（新增）

**文件**：`step2_cleaning_instructions_rollback.bat`

### 设计

- 从 quarantine → 书库（正向的反转）
- 只包含正向 BAT 中实际执行了 `delete_file` 的 book_id
- 结构完全对称：同样的路径变量、同样的子程序模式（处理括号路径）
- src/dst 互换
- 不需要 `cleanup_empty_parent`（移回书库时不会留空目录）

### 示例结构

```bat
@echo off
chcp 65001 >nul
REM Rollback: quarantine → library
REM batch: 2026-09-23
setlocal enabledelayedexpansion

if not defined LIBRARY_DIR set "LIBRARY_DIR=E:\98-Calibre-books-new"
if not defined QUARANTINE_DIR set "QUARANTINE_DIR=E:\98-Calibre-books-new\quarantine\2026-09-23"

REM action=rollback_book book_id=2
set "book_rel=【德】赫尔曼·黑塞, 丁君君, 谢莹莹, ePUBw.COM/德米安：彷徨少年时 (2)"
set "src=%QUARANTINE_DIR%\%book_rel%"
set "dst=%LIBRARY_DIR%\%book_rel%"
echo [2] 回退：!src!
echo      到：!dst!
call :do_move_book "!src!" "!dst!"

goto :eof

:do_move_book
if not exist %1 goto :eof
call :ensure_dir %2
move /Y %1 %2
goto :eof

:ensure_dir
set "dirpath=%~dp1"
if not exist "%dirpath%" mkdir "%dirpath%"
goto :eof
```

### 生成逻辑

在 `step4_sql.py` 的 `run_step4_sql()` 中，与正向 BAT 同时生成。
遍历 rows，收集所有 `delete_file` 的 book_dir，生成反向移动指令。

---

## 三、验证（已有，无需新增）

**命令**：`step4 verify`

检查 `metadata.cleaned.db` 中所有 data 记录对应的物理文件是否存在。

- 执行前跑一次：确认基线（所有保留文件都在书库）
- 执行后跑一次：确认最终状态（DB 中的每条记录都有对应文件）

无需区分"哪些该删、哪些该留"——DB 里留下的就是该留的，只要它们都在就行。

---

## 四、执行顺序

```
① step4 verify                        → 执行前基线
② step4 apply                         → 执行 SQL（删除 DB 记录）
③ step2_cleaning_instructions.bat     → 正向移动（书库 → quarantine）
④ step4 verify                        → 确认最终状态
⑤ （出问题）step2_cleaning_instructions_rollback.bat → 回退（quarantine → 书库）
```

---

## 五、实现计划

### 修改文件

| 文件 | 改动 |
|------|------|
| `06-src/knowledge_assets/preprocess/step4_sql.py` | 新增 `_build_bat_rollback()` 函数，在 `run_step4_sql()` 中同时生成回退 BAT |
| `06-src/template/step2_config.json` | `outputs` 中新增 `rollback_bat_filename` |

### 输出文件清单

| 文件 | 目录 | 说明 |
|------|------|------|
| `step2_cleaning_instructions.bat` | step4/ | 正向（已有） |
| `step2_cleaning_instructions_rollback.bat` | step4/ | 回退（新增） |
| `step2_cleaning_instructions.sql` | step4/ | SQL（已有） |
| `step4_verification_report.csv` | step4/ | 验证报告（已有） |
| `step4_verification.log` | step4/ | 验证日志（已有） |

---

## 六、待确认问题

1. **回退 BAT** 是否需要在回退后同时清理 quarantine 中的空目录？
2. 文件名/输出路径是否 OK？
