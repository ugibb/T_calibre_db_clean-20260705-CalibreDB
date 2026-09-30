# BookInitPath 驱动的 Step0 前置修复与预处理方案

## 1. 目标

将以下两类人工项前移到 **Step0**，优先使用 `custom_column_1_value` 所对应的 **BookInitPath** 原始路径生成候选并完成安全处理：

| Step1 人工清单 | 触发条件 | 目标字段 |
| --- | --- | --- |
| `1-2-2：待人工确认-title副本.csv` | `books.title` 含「副本」或 `Fu Ben` | `books.title` |
| `1-2-4：待人工确认-basename未正确识别.csv` | `data.name` 含 `Unknown`、`Wei Zhi` 或 `Untitled` | `data.name` 与对应实体文件名 |

只有 Step0 无法安全完成的记录，才进入对应 Step1 人工清单，并明确标注候选、处理状态和未恢复原因。

Step0 绝不写入 `03-input` 的原始 `metadata-YYYYMMDD.db`、原始实体文件、原始目录、Calibre 路径或 BookInitPath。默认模式只创建并修改数据库副本；涉及文件名的实际修复仅能在经过验证的**隔离书库副本**中执行。

## 2. 现状与依据

- Step0 目前仅对可无损还原的乱码执行修复；`books.title` 已在扫描目标中，但「副本」并不是编码乱码，所以不会进入现有修复分支。
- Step1-1 已优先读取 Step0 的副本；因此 Step0 修复后的标题或文件名不会再命中相应人工检测。
- Step1-1 的 `file_basename` 直接来自 `data.name`；其 `relative_path` 也由 `books.path` 与 `data.name` 组合得到。
- `custom_column_1` 当前是 BookInitPath，且应同时兼容 Calibre 的单值表及 `books_custom_column_1_link` 关联表结构。
- 当前批次（2026-09-23）有 69 条 `title_copy`：69 条均有 BookInitPath，68 条末级文件名可作为候选，1 条的文件名仍为 `副本 副本.epub`，应保留人工确认。
- 当前 `file_basename_unknown` 记录通常也带有 BookInitPath，例如 `迷航昆仑墟 - Unknown.mobi` 可从 BookInitPath 推导出候选 `迷航昆仑墟`。但 `data.name` 与磁盘实体文件绑定，不能像标题一样只更新数据库副本。

## 3. 推荐流程

```text
原始 metadata.db + 原始 Calibre 实体文件（只读）
        │
        ▼
Step0：复制 metadata.db 为 metadata-*.encoded.db
        │
        ├─ 现有无损乱码修复
        │
        ├─ BookInitPath 标题恢复
        │     ├─ 候选合格：更新副本 books.title / books.sort
        │     └─ 无法恢复：写入标题交接清单
        │
        └─ BookInitPath basename 预处理
              ├─ 生成、校验候选与实体文件同步计划
              ├─ 已验证隔离书库副本：原子改名实体文件，再更新副本 data.name
              └─ 未具备隔离副本或任一校验失败：不改 data.name，写入 basename 交接清单
        │
        ▼
Step1-1：扫描 Step0 副本并读取交接清单
        ├─ 仅仍含副本标记的 title 进入 1-2-2
        └─ 仅仍含未知 basename 的 data.name 进入 1-2-4
           两类记录均携带 Step0 状态、原因和候选
```

### 3.1 Step0 的处理顺序

1. 复制输入库为 Step0 数据库副本。
2. 在副本中执行既有的可逆编码修复。
3. 重新读取副本中的 `books.title` 和 `data.name`，仅处理仍命中上述占位标记的记录。
4. 查询每条记录对应的 BookInitPath，并由末级文件名生成标题及 basename 候选。
5. 对可安全处理的标题，仅更新副本的 `books.title` 和 `books.sort`。
6. 对可安全处理的 basename，仅在已验证的隔离书库副本中先完成实体文件改名、再更新副本 `data.name`。
7. 无法处理的记录不修改目标字段，写入对应的 Step0 交接清单。

先做编码修复后再检查占位标记，避免本可由无损编码还原解决的内容被 BookInitPath 规则覆盖。

### 3.2 BookInitPath → 候选规则

对命中占位标记的记录：

1. 只接受已确认标签为 `BookInitPath` 的 `custom_column_1` 值；需兼容单值表和关联表结构。
2. 用 `PureWindowsPath` 提取末级文件名，不能把 macOS/Linux 上的反斜杠误作普通字符。
3. 只删除最后一个扩展名，例如 `思考, 快与慢.epub` → `思考, 快与慢`；不自动清理作者、版本、序号、括号或宣传语。
4. 候选去首尾空白后必须非空，且不得包含 NUL、换行或路径分隔符。
5. 标题候选不能仍含「副本」或 `Fu Ben`，规范化后不能与当前标题相同。
6. basename 候选不能含 `Unknown`、`Wei Zhi`、`Untitled`。basename 与格式无关，BookInitPath 提取的候选可跨格式复用（同一本书的 epub/mobi/azw3 共用同一候选 basename，各自保留原扩展名）。
7. 同一本书的同格式 `data` 记录若存在多个不一致的 BookInitPath 候选，全部降级为人工确认，不任选其一。
8. 任一校验失败都不更新副本，并记录精确原因。

BookInitPath 是可追溯的原始导入文件名，但不是书目权威来源；本方案仅在当前字段已明确为占位符时使用它替换占位符，绝不对正常元数据批量覆盖。

### 3.3 标题修复的写入范围与安全约束

- 仅更新 Step0 数据库副本的 `books.title`、`books.sort`。
- 复用 Step0 已有的事务、Savepoint、Calibre 触发器临时摘除与恢复机制。
- 任一行更新出现约束冲突时，只回滚该行，并归入标题交接清单；不影响同批其他记录。
- 不更新 `data.name`、`books.path`、实体文件、目录名、BookInitPath 或任何全量库。
- `--dry-run` 只输出预计修复与未恢复清单，不创建或修改 Step0 副本。

### 3.4 basename 预处理与实体文件同步边界

`data.name` 不是普通展示字段。Calibre 通过 `books.path` 与 `data.name + '.' + data.format` 定位实际电子书文件；只更新数据库副本会使其指向不存在的文件。因此 basename 处理分为两种明确模式：

| 模式 | 前提 | 允许动作 | 结果 |
| --- | --- | --- | --- |
| 默认预处理 | Step0 只有数据库副本，或未提供可验证的实体文件副本 | 生成并校验候选，写交接清单 | 不更新 `data.name`，记录 `filesystem_sync_required` |
| 隔离副本修复 | 已创建完整的隔离 Calibre 书库副本，且可从副本根目录精确解析并校验目标文件 | 原子改名实体文件，再更新同一隔离副本的 `data.name` | 标记 `repaired`，后续 Step1 不再生成该行 |

隔离副本修复必须同时满足以下条件：

1. 书库副本根目录与 Step0 数据库副本明确绑定；不得把数据库副本指向原始书库。
2. 由当前副本的 `books.path`、`data.name` 与 `data.format` 解析出的源文件存在，且是常规文件；不可跟随符号链接离开副本根目录。
3. 新文件名在同一目录中不存在；存在同名文件时不覆盖，降级为 `target_file_exists`。
4. 文件改名与 `data.name` 更新必须处于可回滚的操作单元：数据库更新失败时恢复原文件名；文件改名失败时不得写数据库。
5. 只允许改名当前 `data.id` 对应的一份实体文件；不改 `books.path`，不重命名书籍目录，不批量迁移其他格式。
6. 成功后重新校验数据库记录解析出的新路径存在，且旧路径不存在，再写入 `repaired` 交接结果。
7. 任何一步无法验证、无法回滚或出现并发变更，都保留原数据库和原实体文件，降级为人工确认。

这意味着当前仅复制 `metadata.db` 的 Step0 运行方式可完成 basename **预处理**，但不能安全自动修复 basename。只有未来显式提供“隔离书库副本”能力后，才允许走实际修复分支；禁止为了提高自动修复率直接改动输入书库中的文件。

## 4. Step0 → Step1 的交接

### 4.1 新增 Step0 标题交接清单

新增 `step0_title_copy_report.csv`，字段至少包括：

| 字段 | 含义 |
| --- | --- |
| `book_id` | Calibre `books.id` |
| `original_title` | Step0 处理前的副本标记标题 |
| `book_init_path` | 读取到的 BookInitPath 原值 |
| `title_candidate` | 从末级文件名提取的候选；不可用时为空 |
| `status` | `repaired` 或 `unresolved` |
| `reason_code` | 如 `missing_book_init_path`、`filename_still_copy_marker`、`invalid_candidate`、`constraint_conflict` |
| `reason_detail` | 可读的补充说明 |

`step0_repair_report.csv` 继续承载既有的编码修复明细；标题交接清单专门说明 BookInitPath 标题恢复，避免依赖展示型中文列名或混入无关的乱码人工项。

### 4.2 新增 Step0 basename 交接清单

新增 `step0_file_basename_report.csv`，每个 `data.id` 一行，字段至少包括：

| 字段 | 含义 |
| --- | --- |
| `book_id` / `data_id` | Calibre `books.id` 与 `data.id` |
| `original_file_basename` | Step0 处理前的 `data.name` |
| `format` | `data.format` |
| `relative_path` | 当前副本中由 Calibre 元数据解析出的路径 |
| `book_init_path` | 读取到的 BookInitPath 原值 |
| `file_basename_candidate` | 从末级文件名提取的候选；不可用时为空 |
| `status` | `repaired`、`unresolved` 或 `dry_run` |
| `reason_code` | 如 `filesystem_sync_required`、`missing_book_init_path`、`source_file_missing`、`target_file_exists`、`rename_failed` |
| `reason_detail` | 可读的补充说明 |

在默认预处理模式下，候选通过全部内容校验但未提供隔离书库副本时，状态应为 `unresolved`，原因为 `filesystem_sync_required`。这不是候选不可信，而是明确告知不能在仅有数据库副本时破坏数据库与实体文件的一致性。

### 4.3 `1-2-2：待人工确认-title副本.csv` 的标记

Step1-1 从同批 `step0_title_copy_report.csv` 按 `book_id` 关联仍未恢复的记录，并在 `ManualReview` 中新增：

| 字段 | 值 |
| --- | --- |
| `step0_title_repair_status` | `unresolved` |
| `step0_title_repair_reason` | Step0 的 `reason_code` |
| `step0_title_repair_note` | Step0 的 `reason_detail` |
| `step0_title_candidate` | Step0 已提取但未采用的候选；无候选则为空 |

因此，`1-2-2` 不再是所有副本标题的预填清单，而是 **Step0 无法可靠处理的例外清单**。这些行的 `recommended_action` 初始值仍为 `keep`，由人工决定后续动作。

### 4.4 `1-2-4：待人工确认-basename未正确识别.csv` 的标记

Step1-1 从同批 `step0_file_basename_report.csv` 按 `data_id` 关联仍未恢复的记录，并在 `ManualReview` 中新增：

| 字段 | 值 |
| --- | --- |
| `step0_basename_repair_status` | `unresolved` |
| `step0_basename_repair_reason` | Step0 的 `reason_code` |
| `step0_basename_repair_note` | Step0 的 `reason_detail` |
| `step0_basename_candidate` | Step0 已提取但未采用的候选；无候选则为空 |

默认模式下，`filesystem_sync_required` 的行仍出现在 `1-2-4`，但人工不必再从 BookInitPath 手工摘取文件名；该行已携带可核对的候选及“必须同步实体文件”的原因。隔离副本修复成功的记录不会进入 `1-2-4`。

若用户跳过 Step0 而直接运行 Step1-1，两类人工项仍保留现有扫描能力，但对应字段标为 `step0_not_run`，提示先运行 Step0，不能误称为已尝试但失败。

## 5. 确认后实施范围

1. 在 `06-src/knowledge_assets/preprocess/step0_repair.py` 增加 BookInitPath 查询、候选提取、校验、标题单行更新及标题交接清单写入。
2. 在 Step0 增加 basename 交接清单生成；默认模式只预处理，不修改 `data.name` 或实体文件。
3. 仅在另行明确确认“隔离书库副本”输入、输出位置与回滚策略后，才实现 basename 的实体文件原子改名与 `data.name` 同步更新；该能力不得隐式启用。
4. 在 `06-src/template/pipeline_config.json` 配置两个交接清单文件名、BookInitPath 列标识及隔离副本修复的显式开关，避免把输出文件名或列标签散落在代码中。
5. 扩展 `06-src/knowledge_assets/preprocess/step1_1_scan.py` 的 `ManualReview` CSV 架构，读取两个 Step0 交接清单，并只为仍未恢复的相应行写入状态、原因和候选。
6. 更新 `06-src/knowledge_assets/README.md`：说明 Step0 先处理可验证的副本占位标题、预处理未知 basename，以及 `data.name` 的实体文件同步约束；`update_metadata` 继续仅用于人工确认的例外，不用于 Step0 已修复项。
7. 不实施“只更新 `data.name` 而不改实体文件”的路径，也不再实施“将所有 `title_copy` 记录预填 `new_title`、再由人工改为 `update_metadata`”的路径。

## 6. 测试与验收

新增或扩展测试，覆盖：

- Step0 从 Windows 风格 BookInitPath 提取末级文件名，并只删除最后一个扩展名。
- 合格标题候选仅更新 Step0 副本的 `books.title` 和 `books.sort`；输入库校验和不变。
- 空 BookInitPath、无扩展名、NUL/换行、仍含副本标记、候选等于当前标题、关联表结构异常及约束冲突均不写标题，并产生 `unresolved` 交接记录。
- basename 候选的占位标记、空值及路径字符校验；默认模式不得更新 `data.name`，并输出 `filesystem_sync_required`。basename 候选可跨格式复用，不要求 BookInitPath 扩展名与 `data.format` 一致。
- 隔离副本模式下，源文件缺失、目标已存在、符号链接越界、改名失败与数据库更新失败均回滚并保持元数据与实体文件一致。
- 隔离副本模式成功时，实体文件名和 `data.name` 同步更新，副本解析出的新文件路径存在，输入书库内容不变。
- 已被 Step0 修复的标题不会出现在 `1-2-2`；已完成隔离副本同步的 basename 不会出现在 `1-2-4`。
- Step0 未恢复项分别出现在 `1-2-2`、`1-2-4`，且携带 `unresolved`、原因和候选。
- 跳过 Step0 时，两类人工项明确标为 `step0_not_run`。
- `--dry-run` 不修改数据库副本或隔离实体文件副本，但输出预计的 `repaired` / `unresolved` 交接结果。

以当前批次验收标题项时，预期 69 条副本标题中 68 条在 Step0 数据库副本中得到恢复，`副本 副本.epub` 对应的 1 条仍进入 `1-2-2` 并标记 `filename_still_copy_marker`；实际数量以执行前的规则校验结果为准。basename 项在未提供隔离书库副本时仅输出候选与 `filesystem_sync_required`，不改 `data.name`。

## 7. 不纳入本次范围

- 自动判断原始文件名是否为权威或规范书名；
- 通过网络书目服务补全元数据；
- 修改输入库、输入实体文件、输入目录、BookInitPath 或全量库；
- 在没有隔离书库副本的情况下修改 `data.name`；
- 自动删除副本记录；
- 自动重命名 `books.path` 对应目录，或对同书其他格式做推断式批量迁移；
- 对 `title_mojibake` 等其他人工清单复用本规则。
