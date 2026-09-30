# Step0 文件系统绑定字段：删除替代修复方案

## 背景

Step0 扫描 12 个字段（来源：`pipeline_config.json` → `step0.targets`），其中 2 个标记为 `filesystem_bound: true`：

| 说明 | 表.列 | filesystem_bound | 当前行为 |
|------|-------|:----------------:|----------|
| 书名 | `books.title` | - | 编码修复（UPDATE） |
| 书名排序值 | `books.sort` | - | 编码修复（UPDATE） |
| 作者排序值 | `books.author_sort` | - | 编码修复（UPDATE） |
| 作者 | `authors.name` | - | 编码修复（UPDATE） |
| 作者排序值 | `authors.sort` | - | 编码修复（UPDATE） |
| 丛书 | `series.name` | - | 编码修复（UPDATE） |
| 丛书排序值 | `series.sort` | - | 编码修复（UPDATE） |
| 出版社 | `publishers.name` | - | 编码修复（UPDATE） |
| 出版社排序值 | `publishers.sort` | - | 编码修复（UPDATE） |
| 标签 | `tags.name` | - | 编码修复（UPDATE） |
| **文件名** | **`data.name`** | **是** | **只报告 → 改为删除** |
| **书库路径** | **`books.path`** | **是** | **只报告 → 改为删除** |

当前行为：编码探针发现 filesystem_bound 字段有乱码时，**只报告不修复**（status=unresolved, reason=filesystem_bound），因为修改 DB 中的路径/文件名会导致 Calibre 无法寻址到磁盘上的实际文件。

问题：这些乱码记录留在库里也没用——Calibre 已经无法通过乱码路径找到文件，属于「事实上的死数据」。留着只会：
1. 污染后续 step1/step2 的比对结果
2. 增加人工确认的噪音
3. 最终合并到全量库后成为无法访问的幽灵记录

## 方案：以删除替代修复

**核心逻辑**：既然乱码路径已经导致 Calibre 无法寻址，不如直接删除这些不可访问的记录，保持数据库清洁。

### 删除范围

| 乱码字段 | 删除范围 | 理由 |
|----------|----------|------|
| `books.path` | 删除整书：该 book 下所有 data 记录 + book 本身 | Calibre 通过 `books.path` 定位书籍目录，path 乱码 = 该书的**所有格式**都无法访问 |
| `data.name` | 仅删除该条 data 记录（单个格式文件） | 只影响一种格式，其他格式不受影响；若删除后该书无任何 data 记录，则级联删除 book |

### 去重规则

同一本书可能同时出现 `books.path` 乱码和 `data.name` 乱码：
- `books.path` 乱码 → 整书已标记删除，其下所有 data 记录都会被删除
- 此时不再为同一本书的 `data.name` 乱码单独生成删除条目，避免重复

实现：扫描时先处理 `books.path`，收集乱码 book_id 集合；处理 `data.name` 时跳过这些 book_id。

### 删除操作涉及的表

删除一本书或一条格式记录时，需要清理以下表中引用 `books.id` 的数据：

#### 直接删除（本方案执行）

| 表 | 外键列 | 删除整书时 | 仅删格式时 | 说明 |
|----|--------|:----------:|:----------:|------|
| `data` | `data.book` | 删除该书所有 data 行 | 删除该条 data 行 | 格式文件记录（文件名、扩展名、大小） |
| `books` | — | 删除 book 行 | 仅当该书无剩余 data 时删除 | 书籍主记录（title、path、sort 等） |

#### 关联表（本方案**不**清理，与 step00/step1 保持一致）

删除 book 行后，以下表会残留以该 `book_id` 为外键的孤儿记录。本方案**不主动清理**这些表，理由：
- step00（去重）和 step1（自清洗）的删除逻辑同样不清理关联表
- encoded.db 是流水线中间产物，最终合并到全量库时由 step2 统一管理
- Calibre 打开库时会自行处理孤儿关联

| 表 | 外键列 | 关联目标 | 说明 |
|----|--------|----------|------|
| `books_authors_link` | `book` | `authors.id` | 书 ↔ 作者 |
| `books_tags_link` | `book` | `tags.id` | 书 ↔ 标签 |
| `books_series_link` | `book` | `series.id` | 书 ↔ 丛书 |
| `books_publishers_link` | `book` | `publishers.id` | 书 ↔ 出版社 |
| `books_languages_link` | `book` | `languages.id` | 书 ↔ 语言 |
| `books_ratings_link` | `book` | `ratings.id` | 书 ↔ 评分 |
| `comments` | `book` | — | 书籍简介/评论文本 |
| `identifiers` | `book` | — | ISBN、DOI、UUID 等标识符 |
| `custom_column_1` | `book`（单值结构）或 `books_custom_column_1_link`（多值结构） | — | BookInitPath 等自定义列 |

#### 不涉及的内容

| 内容 | 理由 |
|------|------|
| 磁盘上的实际文件 | Step0 只操作数据库副本，不涉及文件删除 |
| 非 filesystem_bound 字段的乱码 | 继续走原有的编码修复路径（UPDATE 修复） |

## CSV 输出变化

### step0_1_encode_repair.csv

filesystem_bound 乱码记录从 `status=unresolved`（仅报告）改为 `status=ready`（可执行删除）：

| 字段 | books.path 乱码 | data.name 乱码 |
|------|-----------------|----------------|
| 表 | books | data |
| 列 | path | name |
| 记录ID | books.id | data.id |
| 原值 | 乱码路径 | 乱码文件名 |
| 修复后 | `delete_book`（含 N 条格式记录） | `delete_data` |
| 恢复链路 | `filesystem_delete` | `filesystem_delete` |
| status | `ready` | `ready` |
| 原因 | 文件系统绑定乱码，删除不可访问记录 | 文件系统绑定乱码，删除不可访问记录 |
| 还原建议 | 原始乱码值（供参考） | 原始乱码值（供参考） |

人工审阅时可以：
- 把 `status` 改为 `skip` 跳过某条删除
- 确认后保持 `ready`，apply 执行删除

### apply 后状态回写

| 原 status | apply 后 | 说明 |
|-----------|----------|------|
| `ready` | `deleted` | 成功删除 |
| `skip` | `skip` | 人工跳过，保留原记录 |

## Apply 阶段执行顺序

`run_step0_apply()` 的执行顺序调整：

```
1. 复制源 DB → encoded.db
2. 编码探针修复（UPDATE 非 filesystem_bound 字段）
3. BookInitPath title 回补（UPDATE books.title）
4. 副本标题修复（UPDATE books.title）
5. 文件名修复（UPDATE data.name）— 跳过即将删除的 data_id
6. **新增：文件系统绑定删除**
   a. 先收集所有 status=ready 的 filesystem_bound 记录
   b. DELETE FROM data WHERE id IN (data.name 乱码的 data_id)
   c. 计算需要删除的 book_id：
      - books.path 乱码的 book_id
      - 删除 data 后无剩余 data 记录的 book_id
   d. DELETE FROM books WHERE id IN (book_ids)
   e. 回写 CSV：ready → deleted
```

步骤 5 中跳过即将删除的 data_id，避免无意义的 UPDATE。

删除操作需要临时 DROP / RECREATE Calibre 管理的触发器（与 step00 一致）。

## 代码改动清单

| 文件 | 改动 |
|------|------|
| `step0_repair.py` `_scan()` | filesystem_bound + 可还原 → 不再进 unrepairable，改为输出删除候选（新增 `DeletionCandidate` 数据类或复用 `RepairHit`） |
| `step0_repair.py` `_scan()` | 先去重：先扫 books.path，收集乱码 book_id；扫 data.name 时跳过 |
| `step0_repair.py` `_write_encode_repair_report()` | 接收删除候选列表，输出为 status=ready + 恢复链路=filesystem_delete |
| `step0_repair.py` `run_step0_scan()` | 传递删除候选给报告函数 |
| `step0_repair.py` 新增 `_read_deletion_candidates()` | 从 CSV 读取 status=ready + 恢复链路=filesystem_delete 的记录 |
| `step0_repair.py` 新增 `_apply_filesystem_deletions()` | 执行 data + book 删除，回写 CSV 状态 |
| `step0_repair.py` `run_step0_apply()` | 在 basename 修复后调用删除函数 |
| `step0_repair.py` `_emit_scan_summary()` | 新增删除候选的统计行 |

## 安全考虑

1. **只操作副本**：删除在 encoded.db（副本）上执行，原始增量库不受影响
2. **人工闸口**：scan → 人工编辑 CSV → 控制台输入 yes → apply 执行删除
3. **可审计**：所有删除操作记录在 step0_1 CSV 中，包含原值、删除范围、原因
4. **文件不受影响**：只删除 DB 记录，磁盘上的实际文件保持原样（后续可重新导入）
5. **幂等性**：重复执行 apply 不会产生额外副作用（已删除的记录不会再次删除）

## 示例场景

### 场景 A：books.path 乱码

```
books.id=509, books.path="Ã«Ôó¶«Ñ¡¼¯(µÚËÄ¾卷)"
  → data 表有 3 条记录（EPUB、PDF、MOBI）
  → CSV 输出一行：表=books, 列=path, 记录ID=509, 修复后=delete_book（含 3 条格式记录）, status=ready
  → apply 执行：DELETE FROM data WHERE book=509; DELETE FROM books WHERE id=509
```

### 场景 B：data.name 乱码（该书有其他正常格式）

```
books.id=100, books.path="正常路径"
  → data.id=201, data.name="ÂÒÂëÎÄ¼þ" (EPUB) ← 乱码
  → data.id=202, data.name="正常文件名" (PDF) ← 正常
  → CSV 输出一行：表=data, 列=name, 记录ID=201, 修复后=delete_data, status=ready
  → apply 执行：DELETE FROM data WHERE id=201
  → book 100 仍有 data.id=202，不删除 book
```

### 场景 C：data.name 乱码（该书仅此一个格式）

```
books.id=200, books.path="正常路径"
  → data.id=301, data.name="ÂÒÂëÎÄ¼þ" (EPUB) ← 乱码，且是唯一格式
  → CSV 输出一行：表=data, 列=name, 记录ID=301, 修复后=delete_data, status=ready
  → apply 执行：DELETE FROM data WHERE id=301
  → book 200 无剩余 data 记录 → DELETE FROM books WHERE id=200
```
