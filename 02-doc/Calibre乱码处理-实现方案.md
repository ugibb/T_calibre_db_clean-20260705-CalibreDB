# Calibre 增量库乱码处理 · 实现方案

> 版本 **v1** · 2026-09-24 · 状态：**已实现，本机验证通过**
>
> 关联项目：20260705-CalibreDB（Calibre 增量预处理流水线）
>
> 适用范围：`06-src/knowledge_assets/` 的 Step0 与 Step1-1。
>
> **本方案分两期落地**：
>
> - **P0** 改造 Step1-1 的乱码判定（`preprocess/encoding.py` + `step1_1_scan.py`）
> - **P1** 新增 Step0 前置修复（`preprocess/step0_repair.py` + 配置 + CLI 接线）
>
> **P2 不在本期**：`duplicate_title_ext_bytes` 命中率异常（88%）另行诊断，本期未动。
>
> 判定与修复逻辑的操作说明见 [`06-src/knowledge_assets/README.md`](../06-src/knowledge_assets/README.md) 的「乱码（mojibake）处理」与「Step0：前置编码修复」两节；本文档记录**为什么这么设计**。

---

## 0. 一句话

把增量库里「GB 系字节被单字节编码误读」产生的乱码，**在进入 1-2-1 人工清单之前**能自动修的修掉、修不了的写清原因交给人，并且**绝不猜、绝不改不该改的列**。

---

## 1. 问题：两组编码被弄混

### 1.1 机制

乱码来自两组编码被弄混——GB 系（`GB2312` ⊂ `GBK` ≈ `CP936` ⊂ `GB18030`）与单字节系（`UTF-8`、`ISO-8859-1`/latin-1 及其 Windows 变体 `CP1250`/`CP1252`）：

```text
原始 GB 系字节 --用「单字节系」解码--> 存进 DB 的字符串
```

### 1.2 两类结局（实测）

**用哪个解码器误读，决定了乱码长什么样、以及还能不能救回来。** 在真实库 `03-input/01-Calibre/metadata-20260923.db` 上实测，两种路径**同时存在**：

| 误解码器 | 机制 | 例子 | 实测计数 | 能否自动还原 |
| --- | --- | --- | --- | --- |
| `ISO-8859-1` / `CP1250` / `CP1252` | 单字节映射：一个字节必对应一个字符，**永不报错** | `Ã«Ôó¶«Ñ¡¼¯(µÚËÄ¾í)` | **9** | **可以**，逐字节可逆 |
| `UTF-8`（`errors='replace'`） | 多字节校验：凑得成合法序列的字节留下，其余替换为 `U+FFFD` | `ңԶ������` | **146** | **不可以**，丢掉的字节永久消失 |

两者合计 155 条，即该库 `books.title` 的真乱码总数。

### 1.3 为什么第二类救不回来

`U+FFFD` 是**有损替换**的结果，原始字节值已不存在于数据中。这不是「换个编码再解一次」能解决的问题——任何转码都救不回来。**只有第一类才是「上游转码」能救的那部分**，这也正是 P1 的价值所在：那 9 条在 Step0 就被修好，不再挤占人工清单。

### 1.4 两条实现要点

**解码统一用 `gb18030`。** 它是 GB 系的超集，`GB2312` / `GBK` / `CP936` 能编出的字节它都能解。若改用 `GB2312` 解，遇到 GBK 扩展字会直接失败。

**编码侧要试三条链路**（`latin-1` / `cp1250` / `cp1252`）。它们才是当初那个「永不报错」的误解码器，得先按下它把字符编回字节。三者差别只在 `0x80–0x9F` 区间——GBK 次字节常落在这里，用 `latin-1` 编回的是 C1 控制字符，用 `cp1252` 编回的才是 `“”‘’—` 这类可见符号，所以三条都要试：

```python
_RESTORE_CHAINS = (
    ("latin-1", "gb18030"),
    ("cp1250", "gb18030"),
    ("cp1252", "gb18030"),
    ("latin-1", "big5"),
)
```

---

## 2. P0：判定方案从「字符黑名单」改为「编码探针」

### 2.1 旧规则的问题

原实现的判定依据是 `step1_rules.json` 里的两个键：

```json
"mojibake_fragments": ["°Ù", "Ã«", "£¨", "����"],
"mojibake_chars": "ÃÄÖÐ¹£µË¾íÔóÎÑ§Ł"
```

即「命中片段 + 异常字符占比 ≥ 0.3」。这是**在用乱码的特征反推乱码**，两个方向都会错：

| | 后果 |
| --- | --- |
| 特征表不全 | 漏判：新的乱码形态没人加进表里就永远检不出 |
| 特征与正常字符重叠 | 误判：日文假名、装饰符号被计入「异常占比」 |

在真实库上的实测结果：

| | 旧规则 | 编码探针 |
| --- | --- | --- |
| `books.title` 判定数 | 228 | 155 |
| 其中**日文标题误报** | **47** | **0** |
| 其中真乱码 | 181 | 155 |

47 条误报是真实存在的（如 `昼顔～平日午後３時の恋人たち～`），它们会让本该自动处理的记录无谓地进入人工清单。

### 2.2 新判据：编码探针

`encoding.is_mojibake()` 只做一件事——**尝试把字符串按各条恢复链编回字节、再用真实编码解码**：

```python
def is_mojibake(text: str) -> bool:
    if not text:
        return False
    if is_replacement_damaged(text):   # 含 U+FFFD → 字节已丢，是乱码
        return True
    return is_repairable(text)          # 能无损还原 → 是乱码
```

判定原则是**宁可漏判，不可误判**。正常的中文/日文标题（含假名、装饰符号、罗马数字、全角 ASCII）**无法被 latin-1/cp1252 编码**，探针自然失败，因此不会被判为乱码。这一条把「误报」从 47 降到 0，而且**不需要维护任何特征表**。

还原结果的可信度另有一道闸（`_is_plausible_restore`），要求同时满足：候选结果不为空、不含 `U+FFFD`、CJK 占比 ≥ 0.5、且 CJK 字符数**净增**于原文。最后一条是防止把本就正常的多语言文本改坏。

`step1_rules.json` 里的 `mojibake_fragments` / `mojibake_chars` 两个键已删除。

### 2.3 P0 的另一半：把乱码分流提到重复判定之前

**这是容易被忽略但影响更大的一处改动。**

`_build_ordered_outputs()` 原先的顺序是「非目标格式 → 垃圾标题 → 重复文件判定 → 其余人工清单」。乱码排在最后，意味着：

> 乱码记录的 title 恰好一致时，会被 `duplicate_title_ext_bytes` 当作**正常重复项**直接判定去留，人工确认清单（1-2-1）就此丢失这批记录。

因此在 `_build_ordered_outputs()` 里把 `title_mojibake` **上提到重复判定之前**，并用 `emitted_data_ids` 保证一条记录只进一张表：

```python
# title 乱码意味着「元数据本身不可信」，必须先于重复判定分流
collect_manual_reviews(MOJIBAKE_REASON_CODE)

duplicate_comparison = _build_duplicate_comparison_candidates(files, emitted_data_ids)
```

`MOJIBAKE_REASON_CODE = "title_mojibake"` 提升为模块常量——它在三处被引用，且承担「先于重复判定分流」的语义，避免各处字面量漂移。

---

## 3. P1：Step0 前置编码修复

### 3.1 定位

`step0` 在 Step1 之前跑，把 1.2 节里第一类（可无损还原）的乱码**在源头修好**，让下游所有步骤看到的都是正确中文。逻辑在 `preprocess/step0_repair.py`。

要改哪些列由 `pipeline_config.json` 的 `step0.targets` 声明，每项带一个 `filesystem_bound` 标志：

```json
{"table": "books", "column": "title", "label": "书名", "filesystem_bound": false}
```

覆盖 `books.title/sort/author_sort`、`authors.name/sort`、`series.name/sort`、`publishers.name/sort`、`tags.name`，以及两个**只报告**的列（见 3.3）。

### 3.2 安全边界一：绝不修改输入库

`03-input/metadata-YYYYMMDD.db` 是**只读输入，任何步骤都不会修改它**。本步：

1. 用 `resolve_incremental_db()` 定位输入库
2. 以 `mode=ro` 只读扫描
3. 用 **SQLite 备份 API**（`src.backup(dst)`）复制出 `step0/metadata-YYYYMMDD.encoded.db`
4. 所有改动只落在副本上

复制用备份 API 而非直接拷文件，是为了避免在源库有未提交事务时拿到不一致的快照。

### 3.3 安全边界二：只改元数据，不改文件系统绑定列

`books.path` 与 `data.name` 直接参与实体文件定位：

```text
DB 中：  books.path = '毛泽东/毛泽东选集(第四卷) (509)'
磁盘上： 03-input/01-Calibre/毛泽东/Ã«Ôó¶«Ñ¡¼¯(µÚËÄ¾í) (509)/
```

只改库、不改磁盘上的目录名，Calibre 就找不到文件。因此这两列在配置里标 `filesystem_bound: true`，**只报告、绝不修改**，条目照样进 `step0_unrepairable.csv` 并附还原建议，由人工连同实体重命名一起处理。

**判定优先级**：先判「能不能还原」，再判「是不是绑实体文件」。所以一条既绑实体文件、字节又已丢失的记录，报的是 `bytes_lost` 而不是 `filesystem_bound`——否则会给人「还能自动修」的错觉。

### 3.4 安全边界三：唯一约束冲突降级而不强改

`authors.name` / `series.name` / `publishers.name` 有 `UNIQUE` 约束。若还原后的名字与库里已有的正确记录重名（真实案例：乱码的 `Ã«Ôó¶«` 还原成 `毛泽东`，而 `id=465` 已是 `毛泽东`），那是**「同一个作者的两条记录」**，要不要合并得人来定，本步不擅自决定：

- 该条**不写入**，降级进 `step0_unrepairable.csv`，`原因` 标注冲突、`还原建议` 给出完整还原结果
- 逐条用 `SAVEPOINT` 写入，单条失败只回滚该条，不影响同批其它记录

```python
conn.execute("SAVEPOINT repair_row")
try:
    conn.execute(statement, (hit.repaired, hit.row_id))
except sqlite3.IntegrityError as error:
    conn.execute("ROLLBACK TO repair_row")
    downgraded.append(UnrepairableHit(..., reason=REASON_CONSTRAINT_CONFLICT, ...))
else:
    applied.append(hit)
finally:
    conn.execute("RELEASE repair_row")
```

### 3.5 Calibre 触发器：`no such function: title_sort`

`books` / `series` 上的维护触发器调用了 `title_sort()` / `uuid4()`。真实库（`metadata-20260923.db`）里有 **4 个**这样的触发器：

```sql
CREATE TRIGGER books_insert_trg AFTER INSERT ON books
        BEGIN
            UPDATE books SET sort=title_sort(NEW.title),uuid=uuid4() WHERE id=NEW.id;
        END

CREATE TRIGGER books_update_trg
            AFTER UPDATE ON books
            BEGIN
            UPDATE books SET sort=title_sort(NEW.title)
                         WHERE id=NEW.id AND OLD.title <> NEW.title;
            END

CREATE TRIGGER series_insert_trg
        AFTER INSERT ON series
        BEGIN
          UPDATE series SET sort=title_sort(NEW.name) WHERE id=NEW.id;
        END

CREATE TRIGGER series_update_trg
        AFTER UPDATE ON series
        BEGIN
          UPDATE series SET sort=title_sort(NEW.name) WHERE id=NEW.id;
        END
```

这两个函数由 Calibre 打开库时用 `create_function()` 注册，**裸 sqlite3 连接没有**，直接 `UPDATE` 会抛：

```text
sqlite3.OperationalError: no such function: title_sort
```

注意库里触发器**总数是 44**，但只有上面 4 个依赖 Calibre 运行时函数。`_calibre_managed_triggers()` 用 `title_sort(` / `uuid4(` 两个标记筛出这 4 个（`INSERT` 触发器其实不会被我们的 `UPDATE` 触发，但一并摘除再装回更简单，也不会有副作用）。

本步的做法是在**同一个事务内**先 `DROP TRIGGER`，改完按 `sqlite_master` 里存的原文重建：

```python
with conn:  # 事务：任何未捕获的失败都整体回滚，副本保持刚复制出来的状态
    triggers = _calibre_managed_triggers(conn, {table for table, _ in grouped})
    for name, _sql in triggers:
        conn.execute(f"DROP TRIGGER {name}")
    ...  # 写库
    for _name, sql in triggers:
        conn.execute(sql)   # 原样装回
```

这样是安全的，理由有两条：

1. **语义一致**——我们本来就自己写 `sort` 列（用同一套编码还原），与触发器 `sort=title_sort(title)` 的意图相同，摘掉不会让 `sort` 与 `title` 脱节。
2. **DDL 是事务性的**——SQLite 的 `DROP TRIGGER` 可回滚，中途失败时被摘掉的触发器会随事务一并恢复。

只摘「SQL 里出现 `title_sort(` 或 `uuid4(` 且在受影响表上」的触发器，且触发器名要过 `^[A-Za-z_][A-Za-z0-9_]*$` 白名单才允许拼进 SQL。

---

## 4. 产物

### 4.1 文件

```text
04-output/{batch}/step0/
├── metadata-YYYYMMDD.encoded.db   # 编码修复副本（下游优先读它）
├── step0_repair_report.csv        # 已修复明细：原值 → 修复后 → 恢复链路
├── step0_unrepairable.csv         # 待人工确认清单（只报告，不改库）
└── step0_encode_repair.log
```

### 4.2 `step0_unrepairable.csv` 的三个原因码

| 原因码 | 含义 | 该给人什么 |
| --- | --- | --- |
| `bytes_lost` | 含 `U+FFFD`，原始字节已丢失，不可自动还原 | `partial_hint` 部分还原提示 |
| `filesystem_bound` | 可无损还原，但该列绑实体文件名/路径 | 完整还原结果，供配合重命名 |
| `constraint_conflict` | 可无损还原，但还原后会与已有记录重名 | 完整还原结果 + 冲突原文，供决定是否合并 |

### 4.3 `partial_hint` 的语义

对含 `U+FFFD` 的记录，逐字符尝试还原：能还原的正常还原，缺口处以 `◻` 标注。

```text
ңԶ������  →  遥远◻◻◻◻◻◻
```

一眼可知原书名以「遥远」开头、共 8 字。**这是给人工判断用的线索，不是还原结果**，流水线绝不据此改库。

（`◻` 在 `partial_hint` 里的还原路径是：单字符先 `encode('utf-8')` 再按 gb18030/big5 解——`ң` 的 UTF-8 字节是 `D2 A3`，按 gb18030 解正是「遥」。这条路径与 1.4 节的多字节链路不同，因为第二类乱码是**按 UTF-8 解码**出来的。）

---

## 5. 真实库运行结果

数据源：`03-input/01-Calibre/metadata-20260923.db`（64,787 本）。

> **⚠️ 数据来源说明**：以下数字来自**验证跑**（`step0` 跑在真实批次目录 `04-output/2026-09-23/step0/`，
> `step1 scan` 跑在临时输出目录）。
>
> **批次 `2026-09-23` 的 `step1/` 产物截至本文档撰写时尚未用新代码重扫**——磁盘上还是
> P0 之前的版本（生成于 2026-09-23 23:21，`1-2-1` 仍为 19 行，表头里没有 `partial_hint` 列）。
> 要让判定改造在本批生效，需重跑 `./run_calibre.sh step1 scan`（见 6.4 节）。

### 5.1 修复统计

| 列 | 类型 | 已修复 | 待人工 |
| --- | --- | --- | --- |
| `books.title` | 可修复 | 9 | 146 |
| `books.sort` | 可修复 | 9 | 146 |
| `books.author_sort` | 可修复 | 19 | 39 |
| `authors.name` | 可修复 | 2 | 21 |
| `authors.sort` | 可修复 | 3 | 20 |
| `tags.name` | 可修复 | 2 | 1 |
| `data.name` | **只报告** | 0 | 157 |
| `books.path` | **只报告** | 0 | 157 |
| **合计** | | **44** | **687** |

687 条待人工里，314 条是 `books.path` + `data.name`（需要配合磁盘重命名），1 条是唯一约束冲突降级。

`books.author_sort` 修了 19 条而 `books.title` 只修了 9 条——因为 `author_sort` 里存的是作者名，同一作者的乱码会在其所有书上各出现一次。

### 5.2 一致性核验（输入库 vs 修复库）

| 检查项 | 结果 |
| --- | --- |
| 输入库 SHA256 | 运行前后一致，未改动 |
| 行数（books / authors / series / publishers / tags / data） | 64787 / 12622 / 152 / 887 / 1515 / 171230，**逐表相等** |
| 触发器数量 | 44 → 44，无缺失、无多余（含 3.5 节那 4 个被摘除又装回的） |
| `PRAGMA integrity_check` | ok |
| `PRAGMA foreign_key_check` | 0 条违例 |
| `id=509` 的 `title` / `sort` | 均还原为 `毛泽东选集(第四卷)`（同步） |
| `id=509` 的 `path` | 保持乱码（刻意不动） |
| 残留 `U+FFFD` 的 title | 146 条（与 1.2 节第二类计数吻合） |

### 5.3 端到端

`step0 → step1 scan`：`step1` 自动解析到 `/…/step0/metadata-20260923.encoded.db`，`1-2-1：待人工确认-title乱码.csv` 由 **155 行降到 146 行**（降掉的正是 Step0 修好的那 9 条）。

### 5.4 判定改造的产物变化（P0）

| 产物 | 改前（旧黑名单规则） | 改后（编码探针） | 说明 |
| --- | --- | --- | --- |
| `1-2-1：待人工确认-title乱码.csv` | 19 行 | 155 行 | 旧的 19 行是「黑名单碰巧命中」的部分；现在 155 条真乱码全部进清单 |
| `1-1-3：…重复文件-title_ext_bytes匹配.csv` | 151,273 | 151,129 | 144 条乱码不再被当正常重复项吞掉 |

> 这两个「改后」数字取自验证跑。磁盘上本批的 `1-2-1` / `1-1-3` 仍是「改前」值（见本节开头说明）。

---

## 6. 接线

### 6.1 CLI

`cli.py` 新增 `step0` 子命令（无子阶段）：

```bash
python3 06-src/knowledge_assets/preprocess/cli.py step0 --batch 2026-09-23
python3 06-src/knowledge_assets/preprocess/cli.py step0 --dry-run    # 只出报告，不写修复库
```

`--dry-run` 下不复制、不写库，但**报告照出**——否则试跑没有意义。注意 dry-run 检不出唯一约束冲突（那要真正写库才会触发），日志里会明确提示这一点。

### 6.2 `run_calibre.sh`

```bash
./run_calibre.sh step0                     # 前置编码修复
./run_calibre.sh step0 --dry-run
```

`_run_cli` 原来硬传 `$1=step $2=phase` 两个位置参数；step0 没有子阶段，因此改为：phase 为空串时不把它拼进命令，避免 argparse 收到多余的空位置参数。

`cmd_auto` 现在**先跑 step0**（4 步 → 5 步）。理由：`step1 scan` 已无条件优先读 step0 产物，不把它纳入 `auto` 的话这个修复在默认流程里永远不生效。

### 6.3 下游读取

```python
# step1_1_scan.py
# include_step0：若本批已跑过 Step0 的编码修复，优先扫那份副本，乱码更少。
# include_step3=False：保持历史行为——Step1 只认输入库，不认 Step1-3 的产物。
metadata_db = resolve_incremental_db(
    batch, incremental_dir, output_dir, include_step0=True, include_step3=False
)
```

`resolve_incremental_db()` 新增两个关键字参数（默认值与历史行为一致，不影响既有调用方）：

- `include_step0=False`——打开时把 step0 的修复副本排在候选**最前**。文件名带日期，只有本批才会命中，不会用旧批次产物顶替新库。**Step0 自身必须用默认值调用**，否则会读到自己上一次的产物。
- `include_step3=True`——历史行为开关。

配套改动：`date_key_from_metadata_path()` 的正则放宽为 `metadata-(\d{8})(?:\.[A-Za-z0-9]+)*\.db$`，让它能认下 `metadata-20260923.encoded.db` 这类带中间标记的名字——否则 `matching_full_db()` 会在 Step1 里直接抛 `ValueError`。

另外新增 `align_batch_to_metadata_db()`：显式传 `--batch` 可能与实际读到的库日期不符，此时以库为准修正批次与输出目录。

### 6.4 幂等性

`step0` 每次从**输入库**重新复制副本再改，可安全重跑；两份报告与日志覆盖重建。它只认输入库，不会拿上一次的修复库当输入。

**⚠️ 一个必须知情的顺序效应**：`step1 scan` 的输入优先取 step0 的副本，所以「先跑 step0 再跑 step1」与「不跑 step0 直接跑 step1」会得到不同的 CSV。若本批已跑过 step1、之后才补跑 step0，需要**重跑 `step1 scan`** 才能让修复生效（已有产物会被覆盖重建，这是预期行为）。

---

## 7. 已知边界与未做的事

### 7.1 `books.path` / `data.name` 只是报告，不是修复

这 314 条是**真正需要配合实体重命名**的活儿。只改库会让 Calibre 找不到文件，因此本方案选择不碰，把决策留给人。若将来要做完整的「库 + 磁盘」联动重命名，需要一个新的、明确带磁盘写权限的步骤，不能塞进 Step0。

### 7.2 `step1_3_apply` 存在同样的 `title_sort` 隐患（既有，未修）

`step1 plan` 会生成 `UPDATE books SET title = ...`（`update_metadata` 动作），`step1 apply` 执行时会撞上和 3.5 节一样的 `no such function: title_sort` 问题。这是**本次改动之前就存在**的问题，不在本方案范围内，仅在此备案。

### 7.3 P2 未动（按用户要求）

`duplicate_title_ext_bytes` 的命中率异常（88%）本期**未做任何改动**，需另行诊断后单独出方案。上面 5.4 节里该产物从 151,273 降到 151,129 是 P0 分流的副作用，不是对 P2 的修复。

### 7.4 本机没有 Calibre

本机不是 Calibre 运行环境，因此 3.5 节的触发器处理是**按源码事实 + 真实库结构**实现的，只在真实 `metadata-20260923.db` 上做过离线验证，未在 Calibre 进程内验证过。装机验收请在 Windows 上做。

---

## 8. 验证策略

### 8.1 测试

`06-src/tests/test_step0_repair.py`（13 项）+ `test_encoding.py`（9 项），共 **22 项通过**。运行：

```bash
cd 06-src && ./calibre_plugin/BookDedup4Del/.venv/bin/pytest tests/ -q
```

step0 的测试用 mini Calibre 库复刻真实库里真正会绊住修复的三件事——依赖 `title_sort()` 的触发器、`authors.name UNIQUE` 约束、绑定实体文件的 `books.path` / `data.name`——然后逐条断言：

| 测试 | 守的是什么 |
| --- | --- |
| `test_input_db_is_never_modified` | 安全边界一：SHA256 前后一致 |
| `test_calibre_triggers_are_restored` | 摘掉触发器后必须原样装回 |
| `test_filesystem_bound_columns_are_never_touched` | 安全边界二：绑实体文件的列一字不改 |
| `test_bound_column_with_lost_bytes_reports_the_harder_reason` | 判定优先级：报 `bytes_lost` 而非 `filesystem_bound` |
| `test_unique_conflict_is_downgraded_not_forced` | 安全边界三：降级而不强改 |
| `test_unrepairable_report_flags_lost_bytes_with_hint` | `bytes_lost` 附 `◻` 提示 |
| `test_repair_report_records_before_and_after` | 修复明细含原值/修复后/恢复链路 |
| `test_dry_run_writes_no_encoded_db` | dry-run 不写库但出报告 |
| `test_title_and_sort_are_restored_together` | `title` 与 `sort` 同改，否则 Calibre 排序错乱 |

`test_encoding.py` 里有一条硬约束测试 `test_normal_titles_are_never_flagged`，用真实日文与装饰符号标题断言**零误报**——这正是 2.1 节要解决的问题。

### 8.2 变异验证

为确认「触发器」相关用例不是空转，做过一次变异：临时摘掉 `_apply_repairs` 里的 `DROP TRIGGER` 两行，11 个用例转为 ERROR，报错正是 `sqlite3.OperationalError: no such function: title_sort`。随后已还原并通过。

### 8.3 诚实声明

- 6 号边界（`.venv` 里的 pytest）之外，本机没有 pytest，测试通过复用插件目录下的 venv 运行。
- `Invalid -W option ignored: invalid module name: 'urllib3.exceptions'` 是本机环境问题，与本流水线无关，可忽略（`PYTHONWARNINGS=` 可抑制）。

---

## 9. 复现

```bash
cd <项目根目录>

# 1. 试跑：只出报告，不写库
./run_calibre.sh step0 --dry-run

# 2. 真跑：复制副本并修复
./run_calibre.sh step0

# 3. 核验输入库未被改动
shasum -a 256 03-input/01-Calibre/metadata-20260923.db

# 4. 核验修复库：行数/触发器/完整性
python3 - <<'PY'
import sqlite3
src = "03-input/01-Calibre/metadata-20260923.db"
enc = "04-output/2026-09-23/step0/metadata-20260923.encoded.db"
for label, db in (("输入库", src), ("修复库", enc)):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    print(label,
          "books=", conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
          "触发器=", len(conn.execute(
              "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()),
          conn.execute("PRAGMA integrity_check").fetchone()[0])
PY

# 5. 端到端：step1 应自动读到 step0 的副本，1-2-1 由 155 降到 146
./run_calibre.sh step1 scan
```

判定逻辑可直接在真实库上复现（无需跑全流程）：

```bash
python3 - <<'PY'
import sqlite3, sys
sys.path.insert(0, "06-src")
from knowledge_assets.preprocess import encoding as E

conn = sqlite3.connect("file:03-input/01-Calibre/metadata-20260923.db?mode=ro", uri=True)
titles = [r[0] or "" for r in conn.execute("SELECT title FROM books")]
a = [t for t in titles if E.is_mojibake(t) and not E.is_replacement_damaged(t)]
b = [t for t in titles if E.is_replacement_damaged(t)]
print("可无损还原：", len(a), " 字节已丢失：", len(b))   # → 9 与 146
PY
```

---

## 附录 A：改动清单

| 文件 | 改动 |
| --- | --- |
| `06-src/knowledge_assets/preprocess/encoding.py` | **新增**（P0）：纯标准库的编码检测与还原 |
| `06-src/knowledge_assets/preprocess/step0_repair.py` | **新增**（P1）：Step0 主体 |
| `06-src/knowledge_assets/preprocess/step1_1_scan.py` | 判定改用编码探针；`title_mojibake` 提前分流；`partial_hint` 入 CSV |
| `06-src/knowledge_assets/preprocess/common.py` | `resolve_incremental_db` 增两个开关；新增 `align_batch_to_metadata_db`；放宽日期正则 |
| `06-src/knowledge_assets/preprocess/cli.py` | 新增 `step0` 子命令与分发 |
| `06-src/template/pipeline_config.json` | 新增 `step0` 段（文件名、报告名、12 个 targets）；`step_dirs` 增 `step0` |
| `06-src/template/step1_rules.json` | 删除 `mojibake_fragments` / `mojibake_chars` 两个死键 |
| `06-src/tests/test_encoding.py` | **新增**：9 项，含零误报硬约束 |
| `06-src/tests/test_step0_repair.py` | **新增**：13 项，含三条安全边界 |
| `run_calibre.sh` | 新增 `step0` 命令；`_run_cli` 支持无子阶段；`auto` 纳入 step0；`status` 增 Step0 进度行 |
| `06-src/knowledge_assets/README.md` | 乱码章节改写（两组成因、实测计数）；新增 Step0 章节；幂等性表 |

`06-src/template/courses_step1_rules.json` 里也有 `mojibake_chars` 等键，但它属于**另一条流水线**，本次未动。
