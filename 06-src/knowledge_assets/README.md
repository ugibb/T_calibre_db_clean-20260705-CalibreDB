# Calibre 增量预处理流水线

把 Calibre 导出的 `metadata-YYYYMMDD.db` 清洗成「干净、无重复、格式收敛」的预处理库，再增量合并进全量预处理库。

设计蓝本见 [01-init/Calibre.db增量数据处理技术实现方案.md](../../01-init/Calibre.db增量数据处理技术实现方案.md)。

---

## 快速开始

在项目根目录执行：

```bash
./run_calibre.sh status          # 看当前批次进度 + 下一步该做什么
./run_calibre.sh 1               # Step1：前置去重
./run_calibre.sh step2           # Step2：编码修复+自清洗完整流程
./run_calibre.sh auto            # 连跑非破坏性步骤，停在人工确认处
```

`auto` 会依次执行 Step1 去重 → Step2 repair scan → Step2 scan → Step2 plan，然后打印人工闸口提示。
**Step3 apply（写全量库）不会被 `auto` 触发**，必须显式执行且需要确认。

---

## 流水线总览

三个步骤、十个子阶段。**只有 Step3 apply 会写全量库**，其余步骤都不动全量库。

| 步骤 | 子阶段 | 命令 | 输入 | 输出 | 人工闸口 | 破坏性 |
| --- | --- | --- | --- | --- | --- | --- |
| **Step1** 前置去重 | — | `step1` | `03-input/metadata-YYYYMMDD.db`（只读） | `step1/metadata-YYYYMMDD.deduped.db` + 3 个 CSV | 过一眼 3 份 CSV 的删除/保留数量 | 否（只写副本） |
| **Step2** 编码修复 | repair scan | `step2 repair scan` | `step1/metadata-YYYYMMDD.deduped.db`（只读） | `step2/metadata-YYYYMMDD.encoded.db` + 3 个 CSV | — | 否（只写副本） |
| | repair apply | `step2 repair apply` | 上面的 CSV | `step2/metadata-YYYYMMDD.encoded.db`（更新） | 确认 3 份 CSV | 否（只改副本） |
| | scan | `step2 scan` | `step2/` 的修复副本 | `step2/step2_scan/` 下 6 个 CSV + `step2_scan_snapshot.json` | 校核 `recommended_action` | 否 |
| | plan | `step2 plan` | `step2/step2_scan/*.csv` | `step2/step2_cleaning_instructions.{sql,bat}` | 核对 SQL/BAT | 否 |
| | apply | `step2 apply` | 增量库 + 上面的 SQL | `step2/metadata.cleaned.db` + `step3_execution_report.csv` | — | 否（只改副本） |
| **Step3** 增量合并 | scan | `step3 scan` | 清洗后的增量库 + 全量库 | `step3/step3_scan/` 下 3 个确认 CSV + 8 个元数据比对 CSV | 填写 `human_decision` | 否 |
| | validate | `step3 validate` | `step3/step3_scan/*.csv` | `step3/step3_manual_check_report.csv` | — | 否 |
| | plan | `step3 plan` | `step3/step3_scan/*.csv` | `step3/step3_merge_instructions.sql`（逐行动作计划文档） | 核对计划内容 | 否 |
| | apply | `step3 apply -y` | 增量库 + 全量库 + 上面的计划 | 全量库 + `step3_merge_report.csv` | 执行前确认 | **是** |

Step1 是「前置去重」：title+bytes+initPath 三线查重，减少后续人工确认量。
Step2 是「编码修复+自清洗」：先修乱码，再删掉非目标格式、垃圾标题、重复文件。
Step3 是「增量合并进全量库」：把清洗后的增量库并入全量预处理库，并复制需要新增的实体文件。

### 旧编号对照

流水线从旧的 Step00/Step0/Step1/Step2/Step3/Step4 重编号为现在的 3 步结构。看到旧编号的文件名或日志，说明那个批次早于这次重编号：

| 旧编号 | 新编号 | 说明 |
| --- | --- | --- |
| Step00 | Step1 | 前置去重 |
| Step0 | Step2 repair | 编码修复 |
| Step1 | Step2 scan/plan/apply | 自清洗 |
| Step2/Step3/Step4 | Step3 scan/validate/plan/apply | 增量合并 |

---

## 目录与产物

```text
03-input/
└── metadata-YYYYMMDD.db            # 增量输入库（Calibre 导出）

04-output/
├── 00-full-db/                     # 全量预处理库目录（step3 apply 的目标）
│   └── metadata-full-YYYYMMDD.db
└── {batch}/                        # batch = 2026-09-23
    ├── step1/                      # Step1 前置去重产物
    │   ├── metadata-YYYYMMDD.deduped.db   # 去重后的副本
    │   ├── step1_1_duplicate.csv         # 重复文件（保留 + 删除）
    │   ├── step1_2_title_junk_keyword.csv # 垃圾标题关键词命中
    │   ├── step1_3_non_target_format.csv # 非目标格式
    │   └── step1_dedup.log               # 去重日志
    ├── step2/                      # Step2 编码修复+自清洗产物
    │   ├── metadata-YYYYMMDD.encoded.db   # 编码修复副本（下游优先读它）
    │   ├── step2_1_encode_repair.csv      # 编码修复明细
    │   ├── step2_2_title_copy_repair.csv  # 书名「副本」修复
    │   ├── step2_3_basename_repair.csv    # basename 修复
    │   ├── step2_encode_repair.log        # 修复日志
    │   ├── step2_scan/                    # Step2 scan 产物
    │   │   ├── 2-1-1：系统确认-title命中垃圾关键词.csv
    │   │   ├── 2-1-2：系统确认-重复文件-title_ext_bytes匹配.csv
    │   │   ├── 2-1-3：系统确认-非电子书格式.csv
    │   │   ├── 2-2-1：待人工确认-title乱码.csv
    │   │   ├── 2-2-2：待人工确认-title副本.csv
    │   │   ├── 2-2-4：待人工确认-basename未正确识别.csv
    │   │   └── step2_scan_snapshot.json
    │   ├── step2_cleaning_instructions.sql
    │   ├── step2_cleaning_instructions.bat
    │   ├── metadata.cleaned.db     # 清洗后的增量库（增量库的副本，不是原件）
    │   ├── step3_execution_report.csv
    │   └── quarantine/             # 隔离/备份区
    └── step3/                      # Step3 增量合并产物
        ├── step3_scan/
        │   ├── 3-1-1：系统确认-全量库不存在可新增.csv
        │   ├── 3-1-2：系统确认-文件级重复可跳过.csv
        │   ├── 3-2-3：待人工确认-疑似重复书籍.csv
        │   └── 3-3-1 ~ 3-3-8：元数据比对-*.csv
        ├── step3_manual_check_report.csv
        ├── step3_merge_instructions.sql
        └── step3_merge_report.csv

05-log/{YYYY-MM-DD}.log             # 运行日志（含业务流水与逐条处理明细）
```

`03-input/metadata-YYYYMMDD.db` 是**只读输入，任何步骤都不会修改它**。Step2 apply 改的是 `step2/metadata.cleaned.db` 这个副本。

---

## 使用方法

### 方式一：`run_calibre.sh`（推荐）

脚本在项目根目录，自动读取 `06-src/template/pipeline_config.json` 的目录约定，并按 `03-input/` 里日期最新的库推断批次。

```bash
./run_calibre.sh status                    # 进度总览 + 下一步建议
./run_calibre.sh 1                         # Step1：前置去重
./run_calibre.sh step2 repair scan         # Step2 repair：只扫描编码修复 CSV
./run_calibre.sh step2 repair apply        # Step2 repair：读取 CSV 执行编码修复
./run_calibre.sh step2 scan                # Step2 scan：扫描增量预处理库
./run_calibre.sh step2 plan                # Step2 plan：生成自清洗 SQL + BAT
./run_calibre.sh step2 apply               # Step2 apply：执行自清洗 SQL
./run_calibre.sh step3 scan                # Step3 scan：比对增量库与全量库
./run_calibre.sh step3 validate            # Step3 validate：检查待人工确认 CSV
./run_calibre.sh step3 plan                # Step3 plan：生成合并 SQL
./run_calibre.sh step3 apply -y            # Step3 apply（-y 跳过交互确认）
./run_calibre.sh auto                      # 连跑非破坏性步骤
./run_calibre.sh help                      # 帮助
```

除命令名之外的参数原样透传给 `cli.py`，可覆盖默认批次：

```bash
./run_calibre.sh step2 scan --batch 2026-07-13
./run_calibre.sh step3 apply --metadata-conflict overwrite
./run_calibre.sh step3 scan --log-dir 05-log
```

脚本自带三道防护，缺产物时直接拒绝执行，而不是跑一半再失败：

- `step2 apply` 前要求 `step2/step2_cleaning_instructions.sql` 存在
- `step3 scan` / `step3 apply` 前要求 `step2/metadata.cleaned.db` 存在
- `step3 apply` 会打印全量库目录与当前基准，要求输入 `yes` 或传 `-y`

### 方式二：直接调用 `cli.py`

```bash
python3 06-src/knowledge_assets/preprocess/cli.py step1 --batch 2026-09-23
python3 06-src/knowledge_assets/preprocess/cli.py step2 repair scan --batch 2026-09-23
python3 06-src/knowledge_assets/preprocess/cli.py step2 scan --batch 2026-09-23
python3 06-src/knowledge_assets/preprocess/cli.py step2 plan --batch 2026-09-23
python3 06-src/knowledge_assets/preprocess/cli.py step2 apply --batch 2026-09-23
python3 06-src/knowledge_assets/preprocess/cli.py step3 scan --batch 2026-09-23
python3 06-src/knowledge_assets/preprocess/cli.py step3 plan --batch 2026-09-23
python3 06-src/knowledge_assets/preprocess/cli.py step3 apply --batch 2026-09-23
```

`cli.py` 会自行把 `06-src` 加入 `sys.path`，不需要设 `PYTHONPATH`；但**必须在项目根目录执行**，因为配置文件与相对路径按 cwd 解析。

通用参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--batch` | `03-input/` 里最新库的日期 | 输出批次目录名，`YYYY-MM-DD` |
| `--incremental-dir` | `03-input` | 增量库所在目录（也可直接传库文件路径） |
| `--output-dir` | `04-output/{batch}` | 输出根目录 |
| `--full-library-dir` | `04-output/00-full-db` | 全量库目录（`step3 scan` / `apply`） |
| `--log-dir` | `05-log` | 日志目录 |
| `--force` | 关 | `step3 scan`：跳过前置检查继续执行；`step3 apply`：允许对 `human_decision` 为空的待人工确认行强制执行 |
| `--metadata-conflict` | `fill-missing` | 元数据冲突策略：`fill-missing` / `keep-target` / `overwrite` / `report-only` |

批次以**增量库文件名的日期**为准：即使 `--batch` 传了别的值，`run_step1` 也会按文件名里的 `YYYYMMDD` 修正输出目录。

---

## 人工闸口

流水线有五处必须人工判断的关口。

### 闸口 1：Step1 之后的 `step1/*.csv`

Step1 生成 3 份 CSV，每份都是「总数 = 删除 + 更新 + 保留」的结构：

- **`step1_1_duplicate.csv`**：重复文件（title + bytes + initPath 匹配）
- **`step1_2_title_junk_keyword.csv`**：垃圾标题关键词命中
- **`step1_3_non_target_format.csv`**：非目标格式

过一眼各 CSV 的删除/保留数量，确认无误后继续执行 Step2。

### 闸口 2a：Step2 repair scan 之后的 `step2/*.csv`

Step2 repair scan 生成 3 份 CSV：

- **`step2_1_encode_repair.csv`**：编码修复明细
- **`step2_2_title_copy_repair.csv`**：书名「副本」修复
- **`step2_3_basename_repair.csv`**：basename 修复

过一眼各 CSV 的修复数量，确认无误后执行 `step2 repair apply`。

### 闸口 2b：Step2 scan 之后的 `step2/step2_scan/*.csv`

每个 CSV 都有 `recommended_action`（`scan` 预填）和 `human_decision`（默认空）两列。分两类：

- **`2-1-*`（系统确认，自动删除）**：非电子书格式、标题命中垃圾关键词、`title + ext + bytes` 三要素完全相同的重复文件。判定规则来自 `step1_rules.json`，置信度高。
- **`2-2-*`（待人工确认）**：标题乱码、标题含「副本」、文件名未被正确识别。这类需要人看。

Step2 repair 会先处理这三类的上游信息：编码修复结果记录在 `step2_1_encode_repair.csv`；书名「副本」修复和 basename 修复分别记录在 `step2_2_title_copy_repair.csv` 和 `step2_3_basename_repair.csv`。

> ⚠️ **Step2 阶段要改判定，请直接改 `recommended_action` 列**。
> `step2 plan` 只读 `recommended_action`，**完全不看 `human_decision`**——改后者不会有任何效果。
> 合法取值只有 `delete_file` 和 `keep`（见 `step1_rules.json` 的 `actions`）；取值不在白名单内时按默认动作处理。
>
> `human_decision` 是为 Step3 阶段准备的列，在 Step2 的 CSV 里留空即可。

### 闸口 2c：Step2 plan 之后的 `step2/step2_cleaning_instructions.sql`

两个文件职责不同，会**同时**生成：

- **`.sql`**：真正被 `step2 apply` 执行的清洗指令，主体是 `DELETE FROM data WHERE id = N;` 加上 `DELETE FROM books`（整本书的文件全被删时删书）。执行前过一眼影响行数。
- **`.bat`**（内容其实是 bash 脚本）：把将被删除的实体文件 `mv` 进 `step2/quarantine/`，**不会**被 `step2 apply` 执行，留给人工在本地跑。它默认 `LIBRARY_DIR=03-input`，而那下面只有数据库、没有 Calibre 实体目录，所以每步都被 `if [ -e "$src" ]` 跳过——要让隔离真正生效，得传对实体库路径：

  ```bash
  LIBRARY_DIR=/path/to/Calibre\ Library ./04-output/{batch}/step2/step2_cleaning_instructions.bat
  ```

两者缺一 `step2 apply` 就拒绝执行——虽然只用到 `.sql`，但这个检查是刻意的：保证「数据库记录已删」和「实体文件已隔离」两件事不会只做一半而不自知。

### 闸口 3：Step3 scan 之后的 `step3/step3_scan/` 待人工确认 CSV

只有 `3-2-3：待人工确认-疑似重复书籍.csv`（文件名以 `manual_` 开头的那一类）需要人工填写 `human_decision`：

```bash
./run_calibre.sh step3 validate     # 检查是否填全，输出 step3_manual_check_report.csv
```

这里的 `human_decision` 与 Step2 不同，**是真的会被读取的**：

| `human_decision` | `step3 plan` 生成 SQL 时 | `step3 apply` 执行时 |
| --- | --- | --- |
| 已填合法动作 | 用该动作 | 执行该动作 |
| 留空 | 回退用 `recommended_action` | **跳过该行**，报告标 `manual_required` |
| 填了非法值 | 回退用 `recommended_action` | 按回退后的动作执行 |

也就是说留空的行不会报错，但会**静默不合并**——所以填完务必跑一次 `step3 validate`，确认没有 `check_status = manual_required` 的行。真要带着空值强行执行，只有 `--force` 一条路。

合法动作见 `step4_config.json`：`insert_book`、`insert_data`、`fill_missing_metadata`、`merge_metadata`、`keep_target`、`report_only`。其中 `keep_target` 与 `report_only` 是空操作，只记录不动库；两者都不在合法集合内时用默认动作 `report_only`。

`3-1-1` / `3-1-2` 属于系统确认类，不需要填写。
`3-3-1 ~ 3-3-8` 是元数据比对明细（authors / tags / publisher / languages / rating / series / comments / identifiers），只读参考，也不需要填写。

### 闸口 4：Step3 plan 之后的 `step3/step3_merge_instructions.sql`，然后才 `step3 apply`

这是最后一道关，也是唯一会改全量库的动作。

> 注意这个文件**不是可执行的 SQL**。`step3 plan` 生成的是「逐行动作计划文档」：
> `BEGIN TRANSACTION;` + 每行一段 `-- sync_id=... reason=... action=...` 注释 + `COMMIT;`，正文里没有任何写语句。
> 真正改库的语句在 `step3 apply` 阶段由 Python 逐行组装执行（`_apply_merge_action`）。
>
> 所以审阅时看的是**动作与理由**：哪条被判定为新增、哪条跳过、哪条保留目标记录。行数多的时候直接看 `plan` 日志里的统计行
> （`待执行SQL N条`）和 `step3_manual_check_report.csv` 更快。

---

## 全量库语义

`step3 apply`（Step3 apply）的目标库由 `_target_full_db()` 决定：

1. `--full-library-dir` 里**已有** `metadata-full-*.db` → 取文件名日期**最新**的那个作为目标库，把本批增量合并进去。
2. 目录里**没有** → 新建 `metadata-full-{增量库日期}.db`：
   - 若别处还存在历史全量库，先**复制**它为这个新目标库，再在新目标库上执行合并（历史库本身不动，形成日期快照链）；
   - 若一个历史全量库都找不到，则用清洗后的增量库**直接初始化**全量库。

这意味着「历史基准 + 每批一个新日期快照」是默认行为，旧快照不会被覆盖。

> ⚠️ **当前配置下的注意点**
>
> `pipeline_config.json` 里 `full_db_dir` 是 `04-output/00-full-db`，而**这个目录目前不存在/为空**；
> 真正的历史基准在 `04-output-/00-full-db/metadata-full-20260713.db`（8,970 本书）。
>
> 因此现在直接跑 `step3 apply`，会走上面的分支 2 的第二种子情况——**用本批清洗后的增量库直接初始化全量库**
> （扫描时规模为 171,230 个文件 / 64,787 本书，清洗后更少），而不会增量合并到那份 8,970 本的历史基准上。
>
> 两种处理方式，按实际意图选一个：
>
> ```bash
> # A. 想延续历史基准（推荐先确认那份库是不是你要的基准）
> mkdir -p 04-output/00-full-db
> cp 04-output-/00-full-db/metadata-full-20260713.db 04-output/00-full-db/
> # 之后 step3 apply 会以它为基准，产出 metadata-full-20260923.db
>
> # B. 想以本批结果重建全量库 → 什么都不用做，直接 step3 apply
> ```
>
> 另外 `step2 scan` 有一条幂等保护：只要 `04-output/00-full-db/metadata-full-{本批日期}.db` 已存在，它就整体跳过并提示「对应全量预处理库已存在」。
> 这条保护只认文件名，不核实合并是否真的发生过——所以别手工往那个目录里放同名文件。

---

## 数据库适配：`custom_column_1` 的两种表结构

Calibre 会按自定义列的类型生成两套完全不同的表结构，`step2_scan.py` 必须都适配。自定义列 `#1` 的值以 `custom_column_1_value` 列出现在扫描结果里。

**单值列**（`datatype` 为 `comments` 等）：值直接挂在 `book` 上。

```sql
custom_column_1(id, book, value)
-- → LEFT JOIN custom_column_1 ON custom_column_1.book = books.id
```

**normalized 多值列**（`datatype=text, normalized=1`）：值放在字典表，经关联表连接。

```sql
custom_column_1(id, value, link)
books_custom_column_1_link(book, value)
-- → LEFT JOIN (SELECT link.book, GROUP_CONCAT(cc.value, '; ') ...
--              FROM books_custom_column_1_link link JOIN custom_column_1 cc ON cc.id = link.value
--              GROUP BY link.book)
```

多值结构必须先按 `book` 聚合再关联，否则一条 `data` 记录会被多个值放大成多行，扫描结果行数会虚高。

表结构由 `_custom_column_join()` 用 `PRAGMA table_info` 现场探测，两种都不匹配时记一条 warning 并让该列为空，不会中断流程。扫描 SQL 的注入点由 `_BOOK_FILES_SELECT` 常量与 `step1_rules.json` 的 `book_files_query` 共同决定，**改动其中一处必须同步另一处**，否则会抛 `ValueError` 而不是静默产出空列。

---

## 模板配置

全部在 `06-src/template/`，改配置不用改代码。

| 文件 | 作用 |
| --- | --- |
| `pipeline_config.json` | 目录约定、文件名 pattern、批次默认值 |
| `step1_rules.json` | 目标格式白名单、垃圾关键词、扫描 SQL、CSV 产物清单 |
| `step2_config.json` | Step2 plan 生成清洗 SQL 的规则 |
| `step3_sql.json` | Step2 apply 执行阶段的 SQL 模板 |
| `step4_config.json` | 元数据冲突策略、扫描产物清单、可执行动作白名单 |
| `step4_sql.json` | Step3 plan 生成同步 SQL 的模板 |
| `recommended_actions.json` | `recommended_action` 取值定义 |

当前 Step2 的关键判定依据：

- 目标格式白名单：`azw` / `azw3` / `epub` / `mobi`，其余一律进 `2-1-3` 待删
- 垃圾关键词 27 条（网盘推广、资源导航类标题）
- 乱码判定：走 `preprocess/encoding.py` 的**编码探针**，不再用字符黑名单（见下节）
- 未识别文件名特征：`Wei Zhi` / `Unknown` / `Untitled`

---

## 乱码（mojibake）处理

### 成因：两组编码被弄混了

乱码来自**两组编码被弄混**——GB 系（`GB2312` ⊂ `GBK` ≈ `CP936` ⊂ `GB18030`）与单字节系
（`UTF-8`、`ISO-8859-1`/latin-1 及其 Windows 变体 `CP1250`/`CP1252`）：

```text
原始 GB 系字节 --用「单字节系」解码--> 存进 DB 的字符串
```

**用哪个解码器误读，决定了乱码长什么样、以及还能不能救回来。** 真实的
`metadata-20260923.db` 里两种都有：

| 误解码器 | 机制 | 例子 | 本库计数 | 能否自动还原 |
| --- | --- | --- | --- | --- |
| `ISO-8859-1` / `CP1250` / `CP1252` | 单字节映射：一个字节必对应一个字符，**永不报错** | `Ã«Ôó¶«Ñ¡¼¯(µÚËÄ¾í)` | 9 | **可以**，逐字节可逆 |
| `UTF-8`（`errors='replace'`） | 多字节校验：凑得成合法序列的字节留下，其余替换为 `U+FFFD` | `ңԶ` | 146 | **不可以**，丢掉的字节永久消失 |

第二类的字节一旦被 `U+FFFD` 顶替，原始字节值就不存在了——这不是「换个编码再解一次」
能解决的问题，任何转码都救不回来。**第一类才是「上游转码」能救的那部分**，也正因
如此，那 9 条在 Step2 repair 就被修好、不会出现在人工清单里；剩下的 146 条只能人工处理。

> **解码统一用 `gb18030` 的原因**：它是 GB 系的超集，`GB2312` / `GBK` / `CP936` 能编出的
> 字节它都能解。若改用 `GB2312` 解，遇到 GBK 扩展字就会直接失败。
>
> **编码侧为什么有三个候选**（`latin-1` / `cp1250` / `cp1252`）：它们才是当初那个「永不报错」
> 的误解码器，得先按下它把字符编回字节。三者的差别只在 `0x80–0x9F` 区间——GBK 次字节常落在
> 这里，用 `latin-1` 编回的是 C1 控制字符，用 `cp1252` 编回的才是 `""''—` 这类可见符号，
> 所以在 `_RESTORE_CHAINS` 里三条都要试。

### 判定原则：宁可漏判，不可误判

`encoding.is_mojibake()` 只做一件事——**尝试把字符串按各条恢复链编回字节再用真实编码解码**。
正常的中文/日文标题（含假名、装饰符号、罗马数字）**无法被 latin-1/cp1252 编码**，探针自然失败，
因此不会被判为乱码。

这是相对旧规则（`mojibake_chars` 字符黑名单 + 异常字符占比 ≥ 0.3）的关键改进：

| | 旧规则 | 编码探针 |
| --- | --- | --- |
| `metadata-20260923.db` 的 `books.title` 判定数 | 228 | 155 |
| 其中日文标题误报 | 47 | **0** |
| 其中真乱码 | 181 | 155 |

### 产物字段

`2-2-1：待人工确认-title乱码.csv` 比其它人工清单多一列 `partial_hint`：

- 对第二类（含 `U+FFFD`）：能还原的部分已还原，**丢失的字节以 `◻` 标注**。
  例如 `ңԶ` → `遥远◻◻◻◻◻◻`，一眼可知原书名以「遥远」开头、共 8 字。
  这是给人工判断用的**线索，不是还原结果**，流水线绝不会据此改库。
- 对第一类：该列为空（Step2 repair 会在上游直接修好，正常情况下它们不会出现在这张表里）。

`partial_hint` 只写 CSV、不参与任何 SQL 生成；`recommended_action` 一律为 `keep`，
即**乱码记录默认不动**，是否修、怎么修由人工决定。

---

## Step2 repair：前置编码修复

`step2 repair` 在 Step1 之后、Step2 scan 之前跑，把上面第一类（可无损还原）的乱码**在源头修好**，让下游所有步骤
看到的都是正确中文。核心逻辑在 `preprocess/step2_repair.py`。

### 安全边界（三条，缺一不可）

**1. 绝不修改输入库。** `step2 repair` 读取 `step1/metadata-YYYYMMDD.deduped.db`（只读），
用 SQLite 备份 API 复制出 `step2/metadata-YYYYMMDD.encoded.db`，所有改动只落在副本上。
（复制用备份 API 而非直接拷文件，是为了避免拿到不一致的快照。）

**2. 只改元数据，不改文件系统绑定列。** `books.path` 与 `data.name` 直接参与实体文件定位：

```text
DB 中：  books.path = '毛泽东/毛泽东选集(第四卷) (509)'
磁盘上： 03-input/01-Calibre/毛泽东/Ã«Ôó¶«Ñ¡¼¯(µÚËÄ¾í) (509)/
```

只改库、不改磁盘上的目录名，Calibre 就找不到文件。因此这两列**只报告、绝不修改**
（配置里 `filesystem_bound: true`），条目记录在 `step2_3_basename_repair.csv` 并附还原建议，
由人工连同实体重命名一起处理。

**3. 不可逆的乱码不猜。** 含 `U+FFFD` 的记录原始字节已丢失，记录在 `step2_1_encode_repair.csv` 并附
`partial_hint`，不做任何猜测性写库。

### BookInitPath 标题与 basename 预处理

Step2 repair 从 `custom_column_1` 的 BookInitPath 取文件名。若 `books.title` 含「副本」且候选文件名
不含副本标记，会在副本中同步更新 `books.title` 与 `books.sort`；结果写入 `step2_2_title_copy_repair.csv`。

对于 `data.name` 含 `Wei Zhi`、`Unknown`、`Untitled` 的记录，Step2 repair 只根据同扩展名的
BookInitPath 提供 basename 候选，结果写入 `step2_3_basename_repair.csv`：必须先在隔离的实体书库副本中完成文件
改名，才能同步更新 `data.name`；当前流水线绝不单独修改该列。

业务日志 `step2_encode_repair.log` 会列出 3 份 CSV 的路径及记录数。

### 唯一约束冲突：降级而不强改

`authors.name` / `series.name` / `publishers.name` 有 `UNIQUE` 约束。若还原后的名字与库里
已有的正确记录重名（例如乱码的 `Ã«Ôó¶«` 还原成 `毛泽东`，而 `毛泽东` 已存在），那是
「同一个作者的两条记录」，要不要合并得人来定，本步不擅自决定：

- 该条**不写入**，记录在 `step2_1_encode_repair.csv`，标注冲突原因与还原建议
- 逐条用 `SAVEPOINT` 写入，单条失败只回滚该条，不影响同批其它记录

### 与 Calibre 触发器的关系

`books` / `series` 上的维护触发器调用了 `title_sort()` / `uuid4()`——这两个函数由 Calibre
打开库时用 `create_function()` 注册，**裸 sqlite3 连接没有**，直接 `UPDATE` 会抛
`no such function: title_sort`。

本步的做法是：在同一个事务里先 `DROP TRIGGER`，改完按 `sqlite_master` 里存的原文重建。
这样做语义上是安全的——我们本来就自己写 `sort` 列（用同一套编码还原），与触发器
`sort=title_sort(title)` 的意图一致，摘掉不会让 `sort` 和 `title` 脱节。SQLite 的 DDL 是
事务性的，中途失败回滚时被摘掉的触发器也会一并恢复。

> ⚠️ 同样的 `title_sort()` 依赖在 Step2 apply 也存在（`step2 plan` 会生成
> `UPDATE books SET title = ...`）。这是既有行为，未在本次改动范围内。

---

## 幂等性与重跑

| 步骤 | 重跑行为 |
| --- | --- |
| `step1` | 每次从**输入库**重新复制副本再去重，可安全重跑；3 份 CSV 与日志覆盖重建 |
| `step2 repair` | 每次从 `step1` 的 `deduped.db` 重新复制副本再修复，可安全重跑；3 份 CSV 与日志覆盖重建。注意它只认 `deduped.db`，不会拿上一次的修复库当输入 |
| `step2 scan` | 若 `04-output/00-full-db/metadata-full-{本批日期}.db` 已存在则整体跳过；否则覆盖重建 CSV 与 snapshot |
| `step2 plan` | 覆盖重建 SQL/BAT，以当前 CSV 内容为准 |
| `step2 apply` | 每次从增量库**重新复制**一份 `metadata.cleaned.db` 再执行，可安全重跑 |
| `step3 scan` | 覆盖重建比对 CSV，但会**保留**已填写的 `human_decision` / `human_note` 两列（`_preserve_human_fields`），填过的人工判定不会被重扫冲掉 |
| `step3 plan` | 覆盖重建计划文档；前置的 Step3 scan 比对 CSV 缺失时直接报错 |
| `step3 apply` | 有去重保护：已存在的同格式 `data` 按 `name + uncompressed_size` 判定为 `already_synced`，实体文件大小一致也跳过复制，因此重跑通常不会重复写入。但元数据会按 `--metadata-conflict` 策略**再套一遍**，所以仍不建议把它当成可随意重跑的步骤 |

**`step2 scan` 的输入会优先取 `step2/` 的修复副本**（`resolve_incremental_db(..., include_step2_repair=True)`），
所以「先跑 step2 repair 再跑 step2 scan」和「不跑 step2 repair 直接跑 step2 scan」会得到不同的 CSV——前者 `2-2-1`
更短。若本批已经跑过 step2 scan，之后才补跑 step2 repair，需要重跑 `step2 scan` 才能让修复生效
（已有产物会被覆盖重建，这是预期行为）。

`step2 scan` 的跳过保护是按**文件名日期**判断的，只保证「本批已并库就不重复扫描」，不代表内容等价。要强制重扫，只能把 `04-output/00-full-db/metadata-full-{本批日期}.db` 移走或改名（这条判断读的是 `pipeline_config.json` 的 `full_db_dir`，传 `--full-library-dir` 不影响它）。

`step3 apply` 执行前建议给全量库留一份副本。它不是「跑错了再跑一次就好」的步骤：判读结果请看 `step3_merge_report.csv` 的 `execute_status` / `verification_status` / `metadata_status`，而不是靠重跑观察差异。

---

## 已知问题

1. **多值 `custom_column_1` 的拼接顺序不固定**
   `GROUP_CONCAT` 未加 `ORDER BY`，同一本书多个值的拼接顺序是任意的。该列的作用范围很窄：只有同一重复组里出现了多个不同值时，它才会被拼进 `reason_detail`（并且明确标注「仅供参考」）。它**不参与**保留决策（由 `_duplicate_keep_key` 决定），也**不进** `candidate_id`（由 `matched_value`，即 `title|ext|size` 分组键参与 sha1）。所以顺序抖动只会让 CSV 里那句提示文字的排列不同，不影响删除/保留结果。当前库里该自定义列是每本 1:1，未触发。

2. **重复文件保留策略实际只看路径长度**
   `_duplicate_keep_key` 的首要判据是 `file_exists`（文件是否真实存在），但 `03-input/` 下只有 `metadata-*.db`、没有 Calibre 实体目录，所以这一位恒为 `False`。实际保留顺序退化为：basename 已识别 > `relative_path` 更短 > `data_id` 更小。也就是说**同一组重复文件里，路径最短的那份会被保留**。如果希望以「文件真实存在」为准，需要把实体目录一并放进 `03-input/`。

3. **`step3 apply` 的基准库依赖目录内容**
   见上文「全量库语义」的警告框——`04-output/00-full-db/` 为空时不会自动去找 `04-output-/00-full-db/` 的历史基准。

4. **`04-output-/`（结尾带短横线）是归档目录**
   存放历史批次产物与旧的全量基准，当前代码与配置都不引用它。它下面的 `01-Calibre/`、`02-courses/`、`03-film/`、`step3~step5/` 属于不同流水线的历史产物，别和 `04-output/` 混淆。

---

## 排障

**`Invalid -W option ignored: invalid module name: 'urllib3.exceptions'`**
环境里 `PYTHONWARNINGS` 含非法模块名，与本流水线无关，不影响执行。`run_calibre.sh` 的输出里可以忽略。

**`no such column: custom_column_1.book`**
增量库的自定义列结构变了，且两种已知结构都不匹配。看日志里 `custom_column_1 表结构无法识别（实际列： ...）` 那行的实际列名，据此在 `step2_scan.py` 的 `_custom_column_join()` 里补一条适配。

**`无法在扫描 SQL 中注入 custom_column_1_value`**
`step1_rules.json` 的 `book_files_query` SELECT 段被改过，与 `step2_scan.py` 的 `_BOOK_FILES_SELECT` 常量不再匹配。两处一起改。

**`Step3 scan CSV 不存在： 04-output/{batch}/step3/step3_scan`**
`--batch` 与 `--output-dir` 指向的批次对不上，或该批次还没跑过 `step3 scan`。

**`Step3 同步 SQL 不存在` / `Step3 清洗指令不存在`**
前置步骤没跑完。按 `./run_calibre.sh status` 的「下一步」提示走。

**批次目录不是预期的日期**
批次取自增量库文件名里的 `YYYYMMDD`，不是 `--batch` 的值。检查 `03-input/` 下是不是有更新的 `metadata-*.db` 被选中了（只扫描顶层，不进子目录）。
