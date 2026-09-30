# Calibre 增量预处理库自清洗与全量同步技术实现方案

## 1. 目标

对 Calibre 增量预处理库进行可审计、可人工确认、可重复执行的清洗，并在清洗完成后通过可确认、可审计的 SQL 合并流程写入全量预处理库。

整套流程统一拆成两个业务阶段、六个子步骤：

### 1.1 Step1：增量预处理库自清洗阶段

Step1 只处理增量预处理库自身，不写入全量预处理库。

1. `Step1-1 scan`：扫描增量预处理库与实体文件，执行自动规则，输出差异 CSV 文件。
2. `Step1-2 plan`：根据人工确认后的 CSV 文件生成自清洗执行 SQL + BAT。
3. `Step1-3 apply`：执行 SQL，完成增量预处理库自清洗，生成 `metadata.cleaned.db`。

### 1.2 Step2：清洗好的增量预处理库合并到全量预处理库

Step2 只处理“清洗好的增量预处理库”到“最近一次全量预处理库”的同步，不再承担增量库自清洗职责。

1. `Step2-1 scan`：将清洗好的增量预处理库与最近一次全量预处理库进行比对，输出差异 CSV 文件。
2. `Step2-2 plan`：根据人工确认后的 CSV 文件生成同步任务需要的可执行 SQL。
3. `Step2-3 apply`：执行同步 SQL，完成全量预处理库数据的增量更新。

### 1.3 旧阶段兼容映射

当前代码和历史产物中仍存在旧命名，后续代码迁移必须按以下映射保持兼容：

| 新阶段 | 旧阶段/旧命令 | 迁移说明 |
|---|---|---|
| `Step1-1 scan` | 原 `Step1` / `cli step1` | 增量预处理库自清洗扫描。 |
| `Step1-2 plan` | 原 `Step2` / `cli step2` | 根据 Step1-1 CSV 生成自清洗 SQL + BAT。 |
| `Step1-3 apply` | 原 `Step3` / `cli step3` | 执行自清洗 SQL，产出 `metadata.cleaned.db`。 |
| `Step2-1 scan` | 原 `Step4 scan` / `Step4-1` | 比对清洗好的增量库与最近一次全量库。 |
| `Step2-2 plan` | 原 `Step4 plan` / `Step4-3` | 根据确认 CSV 生成全量同步 SQL。 |
| `Step2-3 apply` | 原 `Step4 apply` / `Step4-4` | 执行同步 SQL，更新全量预处理库。 |
| `validate` | 原 `Step4 validate` / `Step4-2` | 不再作为编号阶段；作为 `Step2-2 plan` 的前置校验或独立辅助命令保留。 |

所有子步骤都必须支持独立执行和重复执行。任何一步重复执行时，不应产生重复记录、重复删除、重复同步或不可恢复的副作用。

## 2. 目录与产物约定

以批次日期 `{batch}` 为隔离单位，例如 `2026-07-07`。

```text
03-input/
  metadata-YYYYMMDD.db                # 增量预处理库，例如 metadata-20260707.db
  ...                                 # Calibre 实体文件目录

04-output/{batch}/
  step1/
    step1_scan.log                    # Step1-1 自清洗扫描日志，内容通过 06-src/utils/logger.py 同步写入统一日志
    2-1-1：系统确认-非电子书格式.csv
    2-1-2：系统确认-title命中垃圾关键词.csv
    2-1-3：系统确认-重复文件-title_ext_bytes匹配.csv
    2-2-1：待人工确认-title乱码.csv
    2-2-2：待人工确认-title副本.csv
    2-2-4：待人工确认-basename未正确识别.csv
    step1_scan_snapshot.json          # Step1-1 规则命中快照

  step2/
    step2_cleaning_instructions.sql   # Step1-2 自清洗 SQL 指令文件（旧文件名兼容）
    step2_cleaning_instructions.bat   # Step1-2 自清洗批处理指令文件（旧文件名兼容）

  step3/
    step3_execute_cleaning.log        # Step1-3 自清洗执行日志（旧目录兼容）
    step3_execution_report.csv        # Step1-3 指令执行结果明细（旧文件名兼容）
    metadata.cleaned.db               # Step1-3 清洗后的 metadata.db 工作副本

  step4/
    step4_sync.log                    # Step2-1/2-2/2-3 比对、同步与验证日志（旧目录兼容）
    4-1-1：系统确认-全量库不存在可新增.csv
    4-1-2：系统确认-文件级重复可跳过.csv
    4-2-3：待人工确认-疑似重复书籍.csv
    step4_manual_check_report.csv     # Step2-2 人工确认检查报告（旧文件名兼容）
    step4_merge_instructions.sql      # Step2-2 全量同步 SQL（旧文件名兼容）
    step4_merge_report.csv            # Step2-3 同步执行与验证明细（旧文件名兼容）
```

全量预处理库路径建议通过命令参数传入，例如：

```text
--full-library-dir 04-output/00-full-db/
```

Step2 默认在 `04-output/00-full-db/` 中维护日期化全量预处理库：

```text
04-output/00-full-db/metadata-full-YYYYMMDD.db
```

Step2 目标全量库日期跟随批次日期；如果未提供批次，则可从增量预处理库文件名提取日期。例如批次为 `2026-07-07` 或输入为 `03-input/metadata-20260707.db`，输出为 `04-output/00-full-db/metadata-full-20260707.db`。

如果目标库不存在，Step2-1 会先复制目录中最新的 `metadata-full-*.db` 作为本批次目标库，再进入“比对扫描 -> CSV 确认 -> SQL 同步”流程。

Step2 读取增量库时按以下优先级自动解析：

1. `04-output/{batch}/step3/metadata.cleaned.db`
2. 命令传入的具体 DB 文件，如 `03-input/metadata-20260707.db`
3. `{incremental_dir}/metadata.db`
4. `03-input/metadata-YYYYMMDD.db`

当 `03-input/` 下存在多个 `metadata-YYYYMMDD.db` 时，默认选择日期最新的一个。若最新增量库对应的全量预处理库已经存在，例如 `03-input/metadata-20260707.db` 对应 `04-output/00-full-db/metadata-full-20260707.db`，Step1-1 直接跳过，不再重复扫描和生成候选文件。

## 3. 总体状态流

```text
Step1：增量预处理库自清洗阶段

Step1-1 scan：扫描与规则命中
  -> 输出候选删除文件、待人工确认文件、扫描日志

人工编辑 Step1-1 CSV 的 recommended_action
  -> 明确每条记录建议操作

Step1-2 plan：读取 Step1-1 所有 CSV
  -> 根据 recommended_action 生成 step2_cleaning_instructions.sql
  -> 根据 recommended_action 生成 step2_cleaning_instructions.bat

人工复核 step2_cleaning_instructions.sql / step2_cleaning_instructions.bat
  -> 确认 SQL 和批处理清洗指令可执行

Step1-3 apply：执行 SQL 清洗指令
  -> 不自动执行 BAT，BAT 由 Windows 本地人工执行
  -> 更新 metadata.db
  -> 验证每条指令是否完整执行

Step2：清洗好的增量预处理库合并到全量预处理库

Step2-1 scan：比对清洗好的增量预处理库与最近一次全量预处理库
  -> 输出系统确认 CSV 和待人工确认 CSV
  -> 人工处理待确认 CSV

Step2-2 plan：读取确认后的 CSV
  -> 生成 step4_merge_instructions.sql（同步任务可执行 SQL，旧文件名兼容）

Step2-3 apply：执行同步 SQL
  -> 更新全量预处理库，并复制需要新增的实体文件
  -> 验证合并数量、文件存在性、数据一致性
```

## 4. Step1-1：增量预处理库自清洗扫描

### 4.1 职责

Step1-1 只读取增量预处理库和实体文件，不修改 `metadata.db`，不删除实体文件。

Step1-1 负责：

- 扫描 Calibre 数据表。
- 扫描实体文件。
- 统计 book 总数、文件总数、文件类型分布。
- 执行自动清洗规则。
- 输出系统确认删除候选。
- 输出待人工确认文件。
- 输出标准清洗日志。

### 4.2 输入

- `03-input/metadata-YYYYMMDD.db`
- 增量实体文件目录，默认与增量库同级或通过 `--incremental-dir` 指向的目录解析
- `06-src/template/step1_rules.json`

### 4.3 输出

- `04-output/{batch}/step1/step1_scan.log`
- `04-output/{batch}/step1/2-1-1：系统确认-非电子书格式.csv`
- `04-output/{batch}/step1/2-1-2：系统确认-title命中垃圾关键词.csv`
- `04-output/{batch}/step1/2-1-3：系统确认-重复文件-title_ext_bytes匹配.csv`
- `04-output/{batch}/step1/2-2-1：待人工确认-title乱码.csv`
- `04-output/{batch}/step1/2-2-2：待人工确认-title副本.csv`
- `04-output/{batch}/step1/2-2-4：待人工确认-basename未正确识别.csv`
- `04-output/{batch}/step1/step1_scan_snapshot.json`

### 4.4 系统确认删除候选规则

#### 4.4.1 非目标电子书格式

目标格式白名单：

- `azw`
- `azw3`
- `epub`
- `mobi`
- `pdf`
- `txt`

凡 `data.format` 不在白名单内，优先输出为系统确认记录 `2-1-1：系统确认-非电子书格式.csv`，默认 `recommended_action=delete_file`，先于 title 垃圾关键词和重复文件规则执行，避免重复进入后续人工确认。

#### 4.4.2 title 命中垃圾关键词

Step1-1 的规则和输出文件命名统一放在 `06-src/template/step1_rules.json`：

```json
{
  "target_formats": ["azw", "azw3", "epub", "mobi", "pdf", "txt"],
  "junk_title_keywords": ["资源分享", "资料 教程 学习 资源汇总"],
  "mojibake_fragments": ["°Ù", "Ã«", "£¨", "����"],
  "mojibake_chars": "ÃÄÖÐ¹£µË¾íÔóÎÑ§Ł",
  "unknown_basename_markers": ["Wei Zhi", "Unknown", "Untitled"],
  "manual_review_outputs": [
    {"reason_code": "title_mojibake", "filename": "2-2-1：待人工确认-title字段出现乱码.csv"}
  ],
  "auto_delete_outputs": [
    {"reason_code": "non_target_format", "filename": "2-1-1：系统确认-非电子书格式.csv"},
    {"reason_code": "title_junk_keyword", "filename": "2-1-2：系统确认-title命中垃圾关键词标记待删除.csv"}
  ]
}
```

实际文件保留完整关键词和完整输出文件列表，代码不再在 `step1_1_scan.py` 中硬编码这些规则。

#### 4.4.3 重复文件

重复判定键：

```text
normalized_title + normalized_ext + bytes
```

字段来源：

- `normalized_title`：`books.title` 规范化后结果。
- `normalized_ext`：`lower(data.format)`。
- `bytes`：优先使用 `data.uncompressed_size`，为空时读取实体文件大小。

每组重复文件稳定保留 1 个，并输出完整重复组对比文件：

- 保留项也输出到 `2-1-3：系统确认-重复文件-title_ext_bytes对比.csv`。
- 保留项的 `recommended_action` 固定为 `keep`。
- 其余重复项的 `recommended_action` 为 `delete_file`。
- `custom_column_1_value` 仅作为参考字段展示，不作为文件物理地址的强认证条件；即使同组 `custom_column_1_value` 不一致，也仍在 `2-1-3` 中按重复文件规则统一给出 `keep/delete_file` 建议。

保留优先级：

1. 优先保留未命中其他删除规则的记录。
2. 优先保留路径更短、文件名更规范的记录。
3. 优先保留 `book_id`、`data_id` 更小的记录，确保重复执行结果稳定。

### 4.5 待人工确认规则

#### 4.5.1 title 出现乱码

组合判定：

- 命中典型乱码片段：`°Ù`、`Ã«`、`£¨`、`����`。
- 连续出现 2 个及以上 `�`。
- Latin-1 误解码常见字符密集出现，例如 `Ã`、`Ä`、`Ö`、`Ð`、`¹`、`£`。
- 非中文、非英文、非数字、非常规标点字符占比超过阈值，例如 30%。

#### 4.5.2 title 出现副本

命中条件：

- `title` 包含 `副本`。
- `title` 包含 `Fu Ben`。

#### 4.5.3 file_basename 未正确识别

`file_basename` 默认来自 `data.name`。

命中条件：

- 包含 `Wei Zhi`
- 包含 `Unknown`
- 包含 `Untitled`

### 4.6 Step1-1 输出 CSV 字段

系统确认 CSV 共 3 个，按执行顺序独立输出：

- `2-1-1：系统确认-非电子书格式.csv`
- `2-1-2：系统确认-title命中垃圾关键词标记待删除.csv`
- `2-1-3：系统确认-重复文件-title_ext_bytes对比.csv`

字段统一为：

```text
candidate_id,batch,book_id,data_id,title,file_basename,ext,bytes,relative_path,reason_code,reason_detail,matched_value,recommended_action
```

`recommended_action` 必须使用全局动作字典 `06-src/template/recommended_actions.json` 中定义的英文枚举。Step1-1 系统确认 CSV 只使用以下动作：

- `delete_file`
- `keep`

Step1-1 待人工确认 CSV 的人工处理可将 `recommended_action` 改为：

- `delete_file`
- `update_metadata`
- `keep`

其中 `keep` 表示保留当前增量预处理库记录，不生成 SQL/BAT 变更；`keep_target` 仅用于 Step2 全量库同步场景，Step1 不使用。

待人工确认 CSV 共 4 个，在系统确认之后按执行顺序独立输出：

- `2-2-1：待人工确认-title字段出现乱码.csv`
- `2-2-2：待人工确认-title字段出现副本.csv`
- `2-2-4：待人工确认-file_basename未正确识别.csv`

字段统一为：

```text
review_id,batch,book_id,data_id,title,file_basename,ext,bytes,relative_path,reason_code,reason_detail,matched_value,recommended_action,human_decision,human_title,human_file_basename,human_note
```

人工需要填写：

- `human_decision`
- `human_title`
- `human_file_basename`
- `human_note`

所有待人工确认记录默认：

- `recommended_action=keep`

`human_decision` 支持：

- `delete_file`
- `update_metadata`
- `keep`

### 4.7 Step1-1 日志格式与统一 logger

所有 Step 的运行日志统一使用 `06-src/utils/logger.py`：

- 启动命令时调用 `setup_logger(log_dir="05-log")`。
- 业务模块通过 `get_logger("preprocess.step1")`、`get_logger("preprocess.step2")`、`get_logger("preprocess.step3")`、`get_logger("preprocess.step4")` 获取子 logger。
- 终端输出 INFO 级别，统一日志文件写入 DEBUG 级别。
- 统一日志文件路径为 `05-log/YYYY-MM-DD.log`。
- 统一日志格式为：`时间戳 | 级别 | 模块名 | 消息`。
- Step 专属交付日志分别输出到 `04-output/{batch}/step1/`、`step2/`、`step3/`、`step4/`，但内容必须由同一 logger 消息流生成，避免终端日志、统一日志和交付日志三套口径不一致。

```text
========== 开始【Step1-1：扫描增量预处理库并生成差异 CSV】 ==========
读取增量 metadata.db：03-input/metadata-20260707.db
扫描实体文件目录：03-input/
Calibre 扫描结果1：文件总数：83556 | book总数：32969
Calibre 扫描结果2：文件类型统计：azw(10000), azw3(9000), epub(8000), mobi(7000), pdf(6000), txt(5000), zip(1)

【2-1-1:系统确认】：title命中垃圾关键词标记待删除：总文件数：1325 | DOC(900), DOCX(320), azw(100), azw3(78), epub(23), mobi(3)
【2-1-1:系统确认】：非电子书格式标记待删除：总文件数：1325 | DOCX(1325)
【2-1-2:系统确认】：title命中垃圾关键词标记待删除：总文件数：0 | 无
【2-1-3:系统确认】：重复文件（title + ext + bytes）对比：重复文件 17847 组 | 对比文件 66676 个 | 保留 17847 个 | 待删除 48829 个

【2-2-1:待人工确认】：「title」字段出现乱码：共 17847 组 | 待确认文件 48829 个
【2-2-2:待人工确认】：「title」字段出现副本：共 120 组 | 待确认文件 180 个
【2-2-4:待人工确认】：「file_basename」未正确识别（「Wei Zhi」、「Unknown」、「Untitled」）：共 17847 组 | 待确认文件 48829 个

输出系统确认删除候选[non_target_format]：04-output/2026-07-07/2-1-1：系统确认-非电子书格式.csv
输出系统确认删除候选[title_junk_keyword]：04-output/2026-07-07/2-1-2：系统确认-title命中垃圾关键词.csv
输出系统确认删除候选[duplicate_title_ext_bytes]：04-output/2026-07-07/2-1-3：系统确认-重复文件-title_ext_bytes对比.csv
输出待人工确认文件[title_mojibake]：04-output/2026-07-07/2-2-1：待人工确认-title字段出现乱码.csv
输出待人工确认文件[title_copy]：04-output/2026-07-07/2-2-2：待人工确认-title字段出现副本.csv
输出待人工确认文件[file_basename_unknown]：04-output/2026-07-07/2-2-4：待人工确认-file_basename未正确识别.csv
========== 完成【Step1-1：扫描增量预处理库并生成差异 CSV】 ==========
```

### 4.8 Step1-1 幂等要求

- Step1-1 不修改数据库和文件系统。
- 重复执行时覆盖生成同名输出文件。
- 每条候选记录的 `candidate_id` 和 `review_id` 使用稳定键生成。
- 稳定键建议为：`batch + book_id + data_id + reason_code + matched_value_hash`。
- Step1-1 按 `2-1-1 -> 2-1-2 -> 2-1-3 -> 2-2-1 -> 2-2-2 -> 2-2-4` 顺序输出 CSV。
- 同一 `data_id` 只允许出现在最早命中的一个 CSV 中；如果已在前序 CSV 出现，后续 CSV 必须跳过，避免重复操作和重复人工处理。

## 5. Step1-2：根据确认 CSV 生成自清洗 SQL + BAT

### 5.1 职责

Step1-2 读取 Step1-1 输出 CSV，按每条记录的 `recommended_action` 字段生成 SQL 和批处理清洗指令。

Step1-2 不执行删除、不修改 `metadata.db`、不移动实体文件。

### 5.2 输入

- `04-output/{batch}/step1/2-1-1：系统确认-非电子书格式.csv`
- `04-output/{batch}/step1/2-1-2：系统确认-title命中垃圾关键词.csv`
- `04-output/{batch}/step1/2-1-3：系统确认-重复文件-title_ext_bytes匹配.csv`
- `04-output/{batch}/step1/2-2-1：待人工确认-title乱码.csv`
- `04-output/{batch}/step1/2-2-2：待人工确认-title副本.csv`
- `04-output/{batch}/step1/2-2-4：待人工确认-basename未正确识别.csv`

### 5.3 输出

- `04-output/{batch}/step2/step2_cleaning_instructions.sql`
- `04-output/{batch}/step2/step2_cleaning_instructions.bat`

Step1-2 不再单独输出 `step2_instruction_update.log`。Step1-2 的运行日志统一写入 `05-log/YYYY-MM-DD.log`。

Step1-2 的动作枚举、默认动作、日志短标签、输出文件名和 BAT 默认目录统一配置在 `06-src/template/step2_config.json`，代码不得重复硬编码动作集合。

### 5.4 指令生成规则

Step1-2 不再做人工决策转换、冲突合并或额外判断，只按 `recommended_action` 生成指令：

- 读取 Step1-1 所有输出 CSV。
- `recommended_action=delete_file`：在 `.sql` 中生成删除 `data` 记录和空 `book` 记录的 SQL；在 `.bat` 中生成供 Windows 本地人工执行的文件移动命令。
- `recommended_action=keep`：在 `.sql` 和 `.bat` 中只生成注释，不产生破坏性操作。
- `recommended_action=update_metadata`：在 `.sql` 中生成元数据更新语句；若无新值，则只生成注释。
- `recommended_action` 为空或无法识别时按 `keep` 处理，只生成注释。

### 5.5 指令文件格式

`step2_cleaning_instructions.sql`：

```sql
-- Auto-generated by Step1-2. Review before running Step1-3.
BEGIN TRANSACTION;
DELETE FROM data WHERE id = 11;
DELETE FROM books WHERE id = 8 AND NOT EXISTS (SELECT 1 FROM data WHERE book = 8);
COMMIT;
```

`step2_cleaning_instructions.bat`：

```bash
#!/usr/bin/env bash
LIBRARY_DIR="${LIBRARY_DIR:-03-input}"
QUARANTINE_DIR="${QUARANTINE_DIR:-04-output/2026-07-07/step3/quarantine}"
```

`.bat` 文件是 Windows 本地人工执行的批处理清洗指令文件，系统不会在 Step1-3 自动执行它。

### 5.6 Step1-2 幂等要求

- 重复执行 Step1-2 时覆盖生成同名 `.sql` 和 `.bat` 文件。
- 重复执行 Step1-2 时会清理旧版 `step2_cleaning_instructions.csv` 和 `step2_instruction_update.log`，避免使用者误读。
- Step1-2 不修改 Step1-1 输出 CSV。

## 6. Step1-3：执行自清洗 SQL 并验证

### 6.1 职责

Step1-3 读取人工确认后的 `step2_cleaning_instructions.sql` 和 `step2_cleaning_instructions.bat`，但只自动执行 SQL 文件并生成验证报告。`.bat` 文件只用于 Windows 本地人工执行，Step1-3 不自动执行。

Step1-3 不直接修改 `03-input/metadata-YYYYMMDD.db`。执行前先复制一份到 `04-output/{batch}/step3/metadata.cleaned.db`，后续 SQL 只在该工作副本上执行。

Step1-3 可以重复执行。重复执行时，已完成的指令应被识别为已完成，不重复删除、不重复更新。

### 6.2 输入

- `03-input/metadata-YYYYMMDD.db`
- 增量实体文件目录，默认与增量库同级或通过 `--incremental-dir` 指向的目录解析
- `04-output/{batch}/step2/step2_cleaning_instructions.sql`
- `04-output/{batch}/step2/step2_cleaning_instructions.bat`

### 6.3 输出

- 清洗后的 `04-output/{batch}/step3/metadata.cleaned.db`
- 被删除或隔离的实体文件
- `04-output/{batch}/step3/step3_execute_cleaning.log`
- `04-output/{batch}/step3/step3_execution_report.csv`
- `04-output/{batch}/step3/metadata.cleaned.db`

### 6.4 删除策略

默认不直接物理删除，优先移动到隔离目录：

```text
04-output/{batch}/step3/quarantine/
```

隔离后再更新 Calibre 数据表。这样 Step1-3 可恢复、可审计。

`delete_file` 执行内容：

1. SQL 删除或标记 `data` 表中对应记录。
2. 如果该 `book_id` 下已无任何 `data` 记录，可选择删除 `books` 记录，或标记为空书。建议第一版删除空书。
3. 实体文件移动由 `.bat` 文件在 Windows 本地人工执行。
4. 写入执行结果。

### 6.5 元数据更新策略

`update_metadata` 执行内容：

1. 如果 `new_title` 非空，更新 `books.title`。
2. 如果 `new_file_basename` 非空，更新 `data.name`。
3. 必要时同步实体文件名。第一版建议只更新 `data.name`，不重命名实体文件，降低风险。

### 6.6 执行审计表

推荐在增量库中创建独立执行审计表：

```sql
CREATE TABLE IF NOT EXISTS cleaning_instruction_runs (
    instruction_id TEXT PRIMARY KEY,
    batch TEXT NOT NULL,
    book_id INTEGER NOT NULL,
    data_id INTEGER,
    action TEXT NOT NULL,
    execute_status TEXT NOT NULL,
    executed_at TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    error_message TEXT
);
```

`execute_status`：

- `success`
- `already_done`
- `failed`
- `skipped`

`verification_status`：

- `verified`
- `not_verified`
- `not_applicable`

### 6.7 Step1-3 验证规则

每条指令执行后立即验证：

`delete_file`：

- 原路径文件不存在。
- 隔离目录中存在对应文件，或记录为执行前已不存在。
- `data` 表中对应 `data_id` 不存在。
- 若删除空书，`books` 表中对应 `book_id` 不存在。

`update_metadata`：

- `books.title` 等于 `new_title`，如果该字段被要求更新。
- `data.name` 等于 `new_file_basename`，如果该字段被要求更新。

`keep`：

- 不执行破坏性操作。
- 报告中记录为 `skipped`。

### 6.8 Step1-3 幂等要求

- 每次执行前从 `03-input/metadata-YYYYMMDD.db` 复制生成 `metadata.cleaned.db`，SQL 只作用于该副本。
- `instruction_id` 是幂等键。
- 执行前先查 `cleaning_instruction_runs`。
- 如果已成功且验证通过，重复执行时返回 `already_done`。
- 如果文件已不在原路径但在隔离目录，且数据库记录已删除，视为 `already_done`。
- 如果上次失败，允许重复执行并覆盖失败状态。

## 7. Step2：清洗好的增量预处理库合并到全量预处理库

### 7.1 职责

Step2 不再直接把清洗完成的增量预处理库自动写入全量预处理库，而是参考增量预处理库的自清洗处理逻辑，先完成“比对扫描 -> 系统确认 CSV + 待人工确认 CSV -> 人工处理 -> 同步 SQL -> 执行同步 -> 验证”的闭环。

Step2 必须在 Step1-3 验证通过后执行；如果仍存在未确认或未执行的破坏性指令，应阻止进入全量同步流程，除非显式传入 `--force`。

Step2 的同步范围必须覆盖 Calibre 核心书籍记录、实体文件和关键元数据，包括 `books`、`data`、作者、标签、出版社、语言、评分、系列、简介、标识符等。

### 7.2 输入

- 清洗后的增量库：`04-output/{batch}/step3/metadata.cleaned.db`
- 若 Step1-3 清洗库不存在，回退读取：`03-input/metadata-YYYYMMDD.db`
- 清洗后的增量实体目录：默认与增量库同级，或由 `--incremental-dir` 指向的目录解析
- 全量预处理库目录：`{full_library_dir}/`，默认 `04-output/00-full-db/`
- 本次目标全量库：`{full_library_dir}/metadata-full-YYYYMMDD.db`
- 全量实体目录：`{full_library_dir}/`
- `04-output/{batch}/step2/step2_cleaning_instructions.sql`
- `04-output/{batch}/step2/step2_cleaning_instructions.bat`
- `04-output/{batch}/step3/step3_execution_report.csv`
- Step2-1 人工确认后的 CSV 文件
- 元数据冲突策略参数：`--metadata-conflict fill-missing|keep-target|overwrite|report-only`

### 7.3 输出

- Step2-1 比对扫描日志：`04-output/{batch}/step4/step4_sync.log`（旧目录和文件名兼容）
- Step2-1 系统确认 CSV：`04-output/{batch}/step4/4-1-*.csv`（旧编号兼容）
- Step2-1 待人工确认 CSV：`04-output/{batch}/step4/4-2-*.csv`（旧编号兼容）
- Step2-2 人工确认检查报告：`04-output/{batch}/step4/step4_manual_check_report.csv`
- Step2-2 同步 SQL：`04-output/{batch}/step4/step4_merge_instructions.sql`；不得创建 `step4_merge_plan` 等中间计划表，写库动作必须直接作用于全量预处理库既有业务表。
- Step2-3 同步执行报告：`04-output/{batch}/step4/step4_merge_report.csv`；这是确认记录的审计明细，可能包含 `keep_target/skipped` 等未执行写库的记录，不等同于实际 SQL 执行条数。
- 更新后的全量预处理库：`04-output/00-full-db/metadata-full-YYYYMMDD.db`
- 合并后的全量实体文件

### 7.4 处理流程

Step2 分为三个编号子步骤和一个辅助校验命令：

1. `Step2-1 scan`：读取清洗后的增量库和最近一次全量库，执行跨库比对，只生成确认 CSV，不修改数据库和实体文件。
2. 人工处理：人工检查待确认 CSV，并填写 `recommended_action`、`human_decision`、`human_note` 等字段。
3. `validate`：辅助校验命令，检查待人工确认 CSV 是否已填写明确动作，输出 `step4_manual_check_report.csv`；不再作为编号阶段。
4. `Step2-2 plan`：读取系统确认 CSV 与人工确认 CSV，生成同步任务需要的可执行 SQL 文件 `step4_merge_instructions.sql`。
5. `Step2-3 apply`：人工确认 SQL 后执行，将增量库同步到全量库，并生成同步报告和验证结果。

每个子阶段都必须可以独立执行和重复执行：

- `Step2-1 scan` 可重复执行并覆盖系统生成字段，但必须按 `sync_id` 保留已有 `human_decision`、`human_note` 等人工字段。
- 人工处理只修改 `4-2-*` CSV，不直接操作数据库。
- `validate` 可在人工处理后单独重复执行，只输出检查报告，不修改数据库和实体文件。
- `Step2-2 plan` 可在人工处理后单独重复执行，每次根据当前 CSV 重新生成 `step4_merge_instructions.sql`。
- `Step2-3 apply` 只读取已生成的 SQL 和当前 CSV 执行同步；已完成记录重复执行时返回 `already_done` 或 `skipped`，未确认人工项返回 `manual_required`。
- `Step2-3 apply` 日志必须区分审计明细行数和实际执行行数：`step4_merge_report.csv` 可保留所有确认记录作为审计明细，但实际执行数量只统计 `success/already_done` 等写库或已写库结果，`keep_target/skipped` 只计入跳过数量。

当目标全量库不存在但存在历史全量库时，Step2-1 只读取最新 `metadata-full-*.db` 作为比对基准，不复制、不创建当天目标库；当天目标库必须等到 Step2-3 执行同步 SQL 时才由最新历史全量库复制生成。

当不存在任何历史全量库时，Step2-1 scan 只记录“无可比对全量库”并清理旧比对 CSV，不复制 `metadata.cleaned.db`，不复制实体文件；全量初始化必须等到 Step2-3 执行时完成。

当用于比对的全量库与当前 `metadata.cleaned.db` 内容完全一致时，Step2-1 scan 只记录日志并清理旧的比对 CSV，不再生成新的 4-* CSV。

### 7.5 比对规则与确认文件

由于增量预处理库与全量预处理库表结构一致，但主键 ID 不能跨库直接复用，因此全量库已存在时必须采用“按业务唯一键定位 + ID 映射 + 关联表重建”的方式，而不是直接覆盖全量库。

文件级唯一键：

```text
normalized_title + ext + bytes
```

书籍级定位优先使用：

```text
normalized_title + primary_author_name
```

如果作者缺失，退化为 `normalized_title`。

Step2-1 系统确认 CSV：

- `4-1-1：系统确认-全量库不存在可新增.csv`：增量记录在全量库中未命中，可自动插入 `books/data` 并复制实体文件。
- `4-1-2：系统确认-文件级重复可跳过.csv`：按 `normalized_title + ext + bytes` 命中同一文件，可跳过 `data` 插入，只补齐缺失元数据；如果目标实体路径已存在但文件属性不一致，也以全量库现有数据为准，归入系统跳过。

Step2-1 待人工确认 CSV：

- `4-2-3：待人工确认-疑似重复书籍.csv`：书籍级定位命中但文件级唯一键未命中，需要人工确认是否同一本书的不同格式或误匹配。

`4-1-2`、`4-2-3` 必须同时列出源增量库与目标全量库的基础文件字段和元数据原值；元数据逐表差异另行输出到 `4-3-*` 独立比对 CSV，方便按表横向判断。

基础比对 CSV 中同一 `sync_id` 的 `compare_side=incremental/full` 两行必须展示相同的 `recommended_action`，避免全量库对照行出现空动作造成误判；执行阶段仍只读取 `compare_side=incremental` 行，`compare_side=full` 仅用于人工对照。

Step2-1 CSV 必须包含稳定字段：

```text
sync_id,batch,reason_code,compare_side,
source_book_id,source_data_id,target_book_id,target_data_id,
book_id,data_id,title,author,book_path,file_basename,ext,bytes,relative_path,
authors,tags,publisher,languages,rating,series,identifiers,comments_len,
metadata,books_record,data_record,
recommended_action,human_decision,human_note
```

当存在源库与全量库对比关系时，同一个 `sync_id` 必须连续输出两行：

- `compare_side=incremental`：增量库数据。
- `compare_side=full`：全量库数据。

`Step2-2 plan`、`Step2-3 apply` 只读取 `compare_side=incremental` 行；`compare_side=full` 只用于人工对照，但仍必须保留 `recommended_action`。

Step2-1 scan 日志中的每个 CSV 数量必须按“增量库侧关注记录”统计：基础确认 CSV 按原始增量记录数统计；`4-3-*` 元数据比对 CSV 只统计 `compare_side=incremental` 行，不把 `compare_side=full` 对照行和 `target_only` 全量库既有行计入主数量。日志必须同时输出 `recommended_action` 分布和待执行 SQL 数，例如 `keep_target 2563条，待执行SQL 0条`。

Step2-1 元数据逐表比对 CSV 独立输出：

- `4-3-1：元数据比对-authors_compare.csv`
- `4-3-2：元数据比对-tags_compare.csv`
- `4-3-3：元数据比对-publisher_compare.csv`
- `4-3-4：元数据比对-languages_compare.csv`
- `4-3-5：元数据比对-rating_compare.csv`
- `4-3-6：元数据比对-series_compare.csv`
- `4-3-7：元数据比对-comments_compare.csv`
- `4-3-8：元数据比对-identifiers_compare.csv`

元数据比对 CSV 必须包含稳定字段：

```text
batch,metadata_scope,metadata_key,type,pair_key,
compare_status,compare_side,
record_id,field_value,record_json,
recommended_action,detail
```

元数据比对文件必须逐项标识：

- `authors_compare`、`tags_compare`、`publisher_compare`、`languages_compare`、`rating_compare`、`series_compare`：按字典表业务键全量比对增量库与全量库表数据；`same`、`conflict` 输出 `compare_side=incremental/full` 两行，`source_only` 只输出增量库行，`target_only` 只输出全量库行，不为缺失侧额外生成空行。
- `comments_compare`：按 `comments` 表全量比对增量库与全量库数据，以书籍映射关系定位同一本书，按存在侧分行展示，不为缺失侧额外生成空行。
- `identifiers_compare`：按 `identifiers` 表全量比对增量库与全量库数据，以书籍映射关系 + `identifiers.type` 定位同一标识，按存在侧分行展示，不为缺失侧额外生成空行；必须单独展示 `type` 字段，便于区分 `isbn`、`asin`、`mobi-asin` 等编号类型。
- 不再生成 `metadata_compare` 汇总 CSV；各元数据表已独立全量比对，汇总表不参与同步，避免造成重复同步含义。
- `target_only` 的 `recommended_action` 为 `keep_target`，表示全量库已有而增量库没有，无需处理。
- `source_only` 的 `recommended_action` 为 `insert`，表示增量库新增元数据，需要插入全量库。
- 字典表（`authors`、`tags`、`publisher`、`languages`、`rating`、`series`）中，`same` 且增量库元数据 ID 与全量库元数据 ID 相同的 `recommended_action` 为 `keep_target`，表示无需处理。
- 字典表中，`same` 但增量库元数据 ID 与全量库元数据 ID 不同的 `recommended_action` 为 `relink_metadata`，表示需要把增量库关联表中的元数据 ID 映射为全量库对应元数据 ID。
- 子表（`comments`、`identifiers`）中，`same` 表示同一本书的业务内容已经一致，即使记录 ID 不同也应为 `keep_target`；子表 ID 不是可跨库复用的元数据 ID，不应触发 `relink_metadata`。
- `conflict` 的 `recommended_action` 为 `merge_metadata`，表示业务键相同但非 ID 字段不一致，需要按冲突策略处理。

### 7.6 合并动作

Step2-2 同步 SQL 只从系统确认 CSV 和人工确认后的 CSV 生成，不允许直接根据扫描结果写库；不得新增 `step4_merge_plan` 等中间计划表。实际同步必须直接更新全量预处理库中既有的 `books`、`data`、元数据字典表、关联表和子表。

`recommended_action` 统一使用全局数据字典 `06-src/template/recommended_actions.json`。各 Step 只能使用自己所属的动作子集：Step1 使用自清洗动作，Step2 分为可执行同步动作和元数据比对标签。

Step2 可执行同步动作只保留以下 6 个，只有这些动作允许进入 `Step2-2 plan` / `Step2-3 apply`：

| action | scope | effect | 定义 |
|---|---|---|---|
| `insert_book` | `book_file` | `write` | 全量库不存在对应书籍，插入 `books`，建立 `source_book_id -> target_book_id` 映射，并插入对应 `data`。 |
| `insert_data` | `book_file` | `write` | 目标书籍存在但确认需要作为新增格式/文件，插入 `data` 并复制实体文件；疑似重复书籍默认不得使用该动作，必须由人工明确改为该动作。 |
| `fill_missing_metadata` | `metadata` | `write` | 只补齐全量库缺失的元数据字段或关联。 |
| `merge_metadata` | `metadata` | `manual_or_policy_write` | 业务键相同但非 ID 字段不一致，需要按人工确认或冲突策略合并。 |
| `keep_target` | `target_record` | `noop` | 保留全量库现有记录，不写入增量库值；适用于文件级重复、`target_only`、目标路径已存在等以全量库为准的场景。 |
| `report_only` | `manual_review` | `noop` | 只记录需要人工判断的对比结果，不写入全量库。 |

4-3 元数据比对 CSV 的 `recommended_action` 是比对标签，不直接进入 `Step2-2 plan` / `Step2-3 apply`：

| action | scope | effect | 定义 |
|---|---|---|---|
| `insert` | `metadata_compare` | `compare_only` | 元数据只存在于增量库；实际同步由 `insert_book`、`insert_data` 或 `fill_missing_metadata` 执行。 |
| `relink_metadata` | `metadata_compare` | `compare_only` | 元数据业务键相同但 ID 不同；实际执行时由元数据同步逻辑映射为全量库元数据 ID。 |
| `keep_target` | `metadata_compare` | `compare_only` | 保留全量库现有元数据；比对表中只作标识。 |
| `merge_metadata` | `metadata_compare` | `compare_only` | 元数据字段冲突；只有当基础确认 CSV 选择可执行 `merge_metadata` 时才会按策略合并。 |

`keep`、`keep_target`、`report_only` 都是不写库动作，但含义不同：`keep` 表示 Step1 自清洗阶段保留增量预处理库当前记录；`keep_target` 表示 Step2 同步阶段以全量库现有记录为准，不采纳增量侧变更；`report_only` 表示只输出人工判断或审计信息。文件级重复使用 `reason_code=file_duplicate` + `recommended_action=keep_target` 表达。

第一版默认冲突策略为 `fill-missing`：

- 目标为空时写入增量库元数据。
- 目标已有相同元数据时跳过。
- 目标已有不同元数据时不覆盖，也不再单独生成元数据冲突待人工确认 CSV。

必须同步的 Calibre 元数据范围：

| 元数据 | 字典表 | 关联/子表 | 同步方式 |
|---|---|---|---|
| 作者 | `authors` | `books_authors_link` | 按 `authors.name` 复用或插入，再重建关联 |
| 标签 | `tags` | `books_tags_link` | 按 `tags.name` 复用或插入，再重建关联 |
| 出版社 | `publishers` | `books_publishers_link` | 按 `publishers.name` 复用或插入，再重建关联 |
| 语言 | `languages` | `books_languages_link` | 按 `languages.lang_code` 复用或插入，再重建关联 |
| 评分 | `ratings` | `books_ratings_link` | 按 `ratings.rating` 复用或插入，再重建关联 |
| 系列 | `series` | `books_series_link` | 按 `series.name` 复用或插入，再重建关联 |
| 简介 | - | `comments` | 按 `book` 子表复制/合并 |
| 标识符 | - | `identifiers` | 按 `book + type` 复制/合并 |

### 7.7 Step2 前置检查

执行合并前必须检查：

- Step1-2 已生成 `step2_cleaning_instructions.sql` 和 `step2_cleaning_instructions.bat`。
- Step1-3 报告中 SQL 指令文件执行状态为 `success` 或 `already_done`；BAT 指令文件允许为 `manual_only`。
- 增量库中不存在已隔离但仍被 `data` 表引用的文件。
- 增量库中的 `data` 记录均能找到对应实体文件。
- 增量库与全量库的关键表结构兼容：`books`、`data`、`authors`、`tags`、`publishers`、`languages`、`ratings`、`series`、`comments`、`identifiers` 以及对应 link 表均存在。
- `Step2-3 apply` 执行前，所有 `4-2-*` 待人工确认 CSV 均已填写明确动作。
- `step4_merge_instructions.sql` 必须由当前批次最新 CSV 生成，避免人工修改 CSV 后未重新生成 SQL。
- `step4_merge_instructions.sql` 不得包含 `CREATE TABLE step4_merge_plan` 或向计划表写入的 SQL；无需处理的动作只能保留为 no-op，需处理的动作必须由 `Step2-3 apply` 直接写入全量预处理库既有表。

### 7.8 Step2 验证规则

合并后验证：

- 增量库中所有有效 `books` 记录在全量库中存在对应记录。
- 增量库中所有有效 `data` 记录在全量库中存在对应记录。
- 每个合并后的实体文件在全量目录存在。
- 文件大小一致。
- 每个合并后的目标书籍具备对应的作者、标签、出版社、语言、评分、系列、简介、标识符记录；若因冲突策略未写入，必须在报告中标记 `conflict`。
- 所有 `sync_id` 在 `step4_merge_report.csv` 中有唯一执行结果：`success`、`already_done`、`skipped`、`manual_required` 或 `failed`。
- 可选：计算 hash，验证内容一致。大库第一版可只对新增文件抽样 hash，后续再全量 hash。

### 7.9 Step2 幂等要求

- 重复执行时不重复插入相同 book 或 data。
- 文件已存在且大小一致时跳过复制。
- 字典表按业务唯一键复用，不重复插入同名作者、标签、出版社、语言、评分、系列。
- 关联表按 `target_book_id` 重建或补齐，重复执行不产生重复关联。
- `comments`、`identifiers` 按目标书籍和唯一约束去重。
- 已合并成功的记录在 `step4_merge_report.csv` 中标记为 `already_done`。
- 冲突记录不自动覆盖，重复执行仍保持冲突状态，直到人工处理。

## 8. 数据库适配设计

### 8.1 Calibre 关键表

Calibre 常见结构如下：

- `books`：书籍主表，包含 `id`、`title`、`path` 等字段。
- `data`：文件数据表，包含 `id`、`book`、`format`、`name`、`uncompressed_size` 等字段。
- `authors`、`books_authors_link`：作者关系表。

实际实现时应先通过 `sqlite_master` 和 `PRAGMA table_info(table_name)` 动态检测字段，避免不同 Calibre 版本字段轻微差异导致失败。

### 8.2 统一扫描模型

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class BookFile:
    batch: str
    book_id: int
    data_id: int
    title: str
    file_basename: str
    ext: str
    bytes: int | None
    relative_path: str | None
```

规则命中模型：

```python
@dataclass(frozen=True)
class RuleHit:
    batch: str
    book_id: int
    data_id: int | None
    status: str
    reason_code: str
    reason_detail: str
    matched_value: str | None = None
```

清洗指令模型：

```python
@dataclass(frozen=True)
class CleaningInstruction:
    instruction_id: str
    batch: str
    book_id: int
    data_id: int | None
    action: str
    relative_path: str | None
    new_title: str | None
    new_file_basename: str | None
    reason_codes: tuple[str, ...]
    confirm_status: str
```

## 9. 推荐代码结构

```text
06-src/
  knowledge_assets/
    preprocess/
      __init__.py
      cli.py
      common.py
      db.py
      scanner.py
      rules.py
      step1_1_scan.py
      step1_2_plan.py
      step1_3_apply.py
      step2_1_scan.py
      step2_2_plan.py
      step2_3_apply.py
      csv_io.py
      verifier.py
      logger.py
06-src/
  template/
    pipeline_config.json
    step1_rules.json
    step2_config.json
    step3_sql.json
    step4_config.json
    step4_sql.json
tests/
  test_preprocess_rules.py
  test_duplicate_detector.py
  test_step1_2_instruction_merge.py
  test_step1_3_idempotency.py
  test_step2_1_scan_verification.py
  test_log_formatter.py
```

目标命令行建议：

```bash
PYTHONPATH=06-src python -m knowledge_assets.preprocess.cli step1 scan --batch 2026-07-07 --incremental-dir 03-input --output-dir 04-output/2026-07-07
PYTHONPATH=06-src python -m knowledge_assets.preprocess.cli step1 plan --batch 2026-07-07 --output-dir 04-output/2026-07-07
PYTHONPATH=06-src python -m knowledge_assets.preprocess.cli step1 apply --batch 2026-07-07 --incremental-dir 03-input --output-dir 04-output/2026-07-07
PYTHONPATH=06-src python -m knowledge_assets.preprocess.cli step2 scan --batch 2026-07-07 --incremental-dir 03-input --full-library-dir 04-output/00-full-db --output-dir 04-output/2026-07-07 --metadata-conflict fill-missing
PYTHONPATH=06-src python -m knowledge_assets.preprocess.cli step2 validate --batch 2026-07-07 --output-dir 04-output/2026-07-07
PYTHONPATH=06-src python -m knowledge_assets.preprocess.cli step2 plan --batch 2026-07-07 --output-dir 04-output/2026-07-07
PYTHONPATH=06-src python -m knowledge_assets.preprocess.cli step2 apply --batch 2026-07-07 --full-library-dir 04-output/00-full-db --output-dir 04-output/2026-07-07
```

旧命令必须保留兼容一段时间，并在日志中提示迁移目标：

```text
旧 cli step1 -> 新 cli step1 scan
旧 cli step2 -> 新 cli step1 plan
旧 cli step3 -> 新 cli step1 apply
旧 cli step4 scan -> 新 cli step2 scan
旧 cli step4 validate -> 新 cli step2 validate
旧 cli step4 plan -> 新 cli step2 plan
旧 cli step4 apply -> 新 cli step2 apply
```

Step2 仅为包含复杂参数的阶段保留独立可执行脚本；人工确认检查通过统一 CLI 的 `step2 validate` 执行，不再保留单独的空壳脚本。

```bash
python 06-src/knowledge_assets/preprocess/step2_1_scan.py --batch 2026-07-07 --incremental-dir 03-input --full-library-dir 04-output/00-full-db --output-dir 04-output/2026-07-07
python 06-src/knowledge_assets/preprocess/step2_2_plan.py --batch 2026-07-07 --output-dir 04-output/2026-07-07
python 06-src/knowledge_assets/preprocess/step2_3_apply.py --batch 2026-07-07 --incremental-dir 03-input --full-library-dir 04-output/00-full-db --output-dir 04-output/2026-07-07
```

## 10. 统一幂等策略

所有 Step 统一遵守：

- 使用稳定 ID：候选、人工确认、清洗指令、合并记录都必须有稳定 ID。
- 破坏性动作前先验证当前状态。
- 已完成动作重复执行时返回 `already_done` 或 `already_synced`。
- 输出日志和报告可以覆盖生成，但人工编辑字段不得被覆盖。
- 数据库更新必须使用事务。
- Step1-3、Step2-3 执行前必须做备份或具备可恢复路径。

## 11. 验收标准

### 11.1 Step1-1：增量预处理库自清洗扫描

- 能读取指定 `metadata.db` 并输出 book 总数、文件总数、文件类型统计。
- 能扫描实体文件，并报告数据库记录与实体文件不一致的情况。
- 能识别 title 乱码和“副本”。
- 能识别 file_basename 中的 `Wei Zhi`、`Unknown`、`Untitled`。
- 能识别非目标电子书格式。
- 能识别 title 垃圾关键词。
- 能按 `title + ext + bytes` 找出重复文件。
- 重复执行输出结果稳定。

### 11.2 Step1-2：根据确认 CSV 生成自清洗 SQL + BAT

- 能读取 Step1-1 所有 CSV。
- 能直接根据 `recommended_action` 生成 SQL 清洗指令。
- 能直接根据 `recommended_action` 生成 BAT 批处理清洗指令。
- 不生成单独 Step1-2 日志文件，运行日志统一进入 `05-log/YYYY-MM-DD.log`。

### 11.3 Step1-3：执行增量预处理库自清洗

- 不自动执行 Step1-2 生成的 BAT 文件；BAT 文件仅供 Windows 本地人工执行。
- 能执行 Step1-2 生成的 SQL 文件，更新 `metadata.cleaned.db`。
- 能生成 Step1-3 执行报告。
- 支持重复执行，执行前创建数据库备份。

### 11.4 Step2：清洗好的增量预处理库合并到全量预处理库

- 能读取清洗后的增量预处理库和目标全量预处理库，完成跨库比对扫描。
- 能输出 `4-1-*` 系统确认 CSV 和 `4-2-*` 待人工确认 CSV。
- 能独立检查人工确认 CSV 并输出 `step4_manual_check_report.csv`。
- 能在人工确认后生成 `step4_merge_instructions.sql`。
- 能通过统一 SQL 完成 `books/data` 与元数据合并。
- 能复制新增实体文件，并跳过已存在且大小一致的实体文件。
- 能识别已合并记录并标记为 `already_done`。
- 能输出冲突记录，且第一版不自动覆盖全量库已有冲突元数据。
- 能验证合并数量、数据库记录、元数据关联和实体文件一致性。

## 12. 风险与处理

- Calibre schema 版本差异：通过动态 schema 检测和字段适配处理。
- 乱码误判：Step1-1 只进入人工确认，不自动删除。
- 关键词误杀：系统确认删除仍需在 Step1-2 指令文件中二次确认后由 Step1-3 执行。
- 重复文件误删：只在 Step1-3 执行已确认指令，且默认移动到隔离目录。
- 文件大小缺失：优先读 `data.uncompressed_size`，缺失时读取实体文件大小，仍缺失则不参与重复删除规则。
- Step2 同步冲突：第一版只输出待人工确认或报告冲突，不自动覆盖全量库现有记录。
- 重复执行风险：所有破坏性动作都通过稳定 ID、执行审计表和状态验证实现幂等。

## 13. 后续实现顺序

1. 先完成文档、CLI、日志和配置中的阶段命名统一：新增 `step1 scan/plan/apply` 与 `step2 scan/validate/plan/apply` 语义入口。
2. 保留旧 `cli step1/step2/step3/step4` 与 `step4_*.py` 脚本兼容入口，但运行日志必须提示对应的新阶段名称。
3. 更新 Step1-1/Step1-2/Step1-3 的输出日志、报告标题和动作字典引用，确保自清洗阶段内部口径一致。
4. 更新 Step2-1/Step2-2/Step2-3 的输出日志、报告标题、SQL 头部注释和统计口径，确保同步阶段只统计真实待执行 SQL。
5. 补充或更新单元测试和小样本集成测试，覆盖旧入口兼容、新入口调用、空 SQL 报告、`keep_target` 统计和元数据比对统计。
6. 用真实批次重新跑 Step1 与 Step2，抽样确认 CSV、SQL、执行报告和统一日志是否符合新命名与新阶段职责。
