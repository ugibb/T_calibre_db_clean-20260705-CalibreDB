# 跨 book_id 同书合并 + 物理文件清理方案

> 日期：2026-09-29
> 状态：待确认

---

## 一、问题定义

### 现状

`metadata.cleaned.db` 中，同一本书的多种格式散落在不同 `book_id` 下：

| book_id | books.path | data 格式 |
|---------|-----------|----------|
| 676 | 马伯庸/长安的荔枝 (676) | AZW3, EPUB, MOBI |
| 10011 | 马伯庸/长安的荔枝 (10011) | PDF |
| 29023 | 马伯庸/长安的荔枝 (29023) | MOBI |
| 29107 | 马伯庸/长安的荔枝 (29107) | EPUB |
| 29300 | 马伯庸/长安的荔枝 (29300) | AZW3 |
| 35208 | 马伯庸/长安的荔枝 (35208) | EPUB, MOBI, PDF |

每个 `book_id` 对应一个独立物理目录，目录内只有部分格式的文件。

### 数据规模

| 指标 | 数值 |
|------|------|
| books 总数 | 17,518 |
| data 总数 | 46,119 |
| 同书多 book_id 组数 | 1,769 |
| 涉及 book_id 数 | 4,341（24.8%）|
| 其中 winner（保留） | 1,769 |
| 其中 loser（删除） | 2,572 |
| loser 的 data 记录数 | 4,569 |
| 其中格式重复（删除） | 3,475（76.0%）|
| 其中格式互补（移动） | 1,094（24.0%）|

### 根因

step1 dedup 的 key 是 `normalize_title + bytes + init_path`，只能捕获 **完全相同的文件**（同 title、同字节数、同原始路径）。不同格式（EPUB vs PDF）的 bytes 和 init_path 不同，不会被去重。同一本书在不同时间被多次导入 Calibre，每次生成新的 `book_id`，格式散落在不同目录下。

---

## 二、「同一本书」的认定逻辑

### 核心发现

Calibre 的 `books.path` 格式为 `作者/书名 (book_id)`，其中 `(book_id)` 是 Calibre 自动追加的后缀。**去掉 `(book_id)` 后缀后的部分（以下称 `path_prefix`），就是 Calibre 对「同一本书」的标识。**

验证结果：
- 100% 的 `books.path` 以 ` ({id})` 结尾（0 条例外）
- 1,769 个多 book_id 组中，仅 2 组有微小 title 差异（同一版本的不同 subtitle 截断），path_prefix 完全一致
- 89 个 `未知/` 前缀组是垃圾数据（title 为 "008"、"01" 等无意义编号），排除后剩余 1,680 组 100% 为同一本书

### path_prefix 提取

```python
# books.path 格式："{author}/{title} ({book_id})"
# path_prefix = 去掉末尾 " ({book_id})" 的部分
path_prefix = path[:-(len(str(book_id)) + 2)]
# 例："马伯庸/长安的荔枝 (676)" → "马伯庸/长安的荔枝"
```

### 排除规则

`path_prefix` 以 `未知/` 开头的组不自动合并。原因：
- 这些书的 title 是无意义的编号（"008"、"01" 等），不同书可能碰巧有相同编号
- 共 89 组、381 个 book_id，全部为单格式
- 输出到待人工确认 CSV，由人工判断

### 为什么不用其他方案

| 方案 | 问题 |
|------|------|
| `normalize_title + author_sort` | 14.1% 的组 author_sort 不一致（同一作者的不同标注方式），会漏合并 |
| `normalize_title + normalize_author` | normalize 规则难以覆盖所有变体，有误合并风险 |
| BookInitPath（原始导入路径） | 同一本书来自不同来源（微信TOP200、豆瓣年度榜单等），无法关联 |
| **path_prefix** | **100% 准确，零误判，零漏判（排除未知/后）** |

---

## 三、目标

1. **DB 合并**：同一本书的所有格式统一归属到一个 `book_id`（winner）
2. **物理文件合并**：loser 目录中 winner 缺少的格式文件移动到 winner 目录
3. **清理冗余目录**：loser 的空目录删除

---

## 四、设计方案

### 4.1 合并分组

```python
# 按 path_prefix 分组
groups = {}
for book in books:
    if book.path.startswith("未知/"):
        continue  # 排除，待人工确认
    path_prefix = extract_path_prefix(book.path, book.id)
    groups.setdefault(path_prefix, []).append(book)

# 只处理有多 book_id 的组
merge_groups = {k: v for k, v in groups.items() if len(v) > 1}
# 结果：1,680 组
```

### 4.2 Winner 选择

```python
winner = max(book_ids, key=lambda bid: (data_count[bid], -bid))
```

即：**data 记录最多的 book_id 胜出**；记录数相同时，**book_id 最小的胜出**。

选择理由：
- 最多格式 → winner 持有最完整的格式集合，减少移动操作
- 最小 book_id → 结果确定性强，最早导入的版本通常质量更好

### 4.3 格式重叠分析

同一格式（如 EPUB）可能存在于多个 book_id 中，且 `data.name` 相同但 `uncompressed_size` 不同。这是合并的核心复杂性。

**实际数据**：
- 1,680 个合并组中，1,337 组（79.6%）存在格式重叠
- loser 的 4,569 条 data 中：
  - 3,475 条（76.0%）的格式在 winner 中已存在 → **重复，删除**
  - 1,094 条（24.0%）的格式在 winner 中不存在 → **互补，移动到 winner**

### 4.4 DB 合并操作

对每个 merge group：

```python
winner_book_id = 选出的 winner
winner_formats = {d.format for d in winner.data_records}

for loser_book_id in losers:
    for data_record in loser.data_records:
        if data_record.format in winner_formats:
            # 格式重复 → 删除 loser 的 data 记录
            DELETE FROM data WHERE id = data_record.id
        else:
            # 格式互补 → 移动到 winner
            UPDATE data SET book = winner_book_id WHERE id = data_record.id
            winner_formats.add(data_record.format)
    
    # 删除 loser 的标签/系列等关联
    DELETE FROM books_tags_link WHERE book = loser_book_id
    DELETE FROM books_series_link WHERE book = loser_book_id
    DELETE FROM books_publishers_link WHERE book = loser_book_id
    DELETE FROM books_languages_link WHERE book = loser_book_id
    # ... 其他 custom_column link 表
    
    # 删除 loser book 记录
    DELETE FROM books WHERE id = loser_book_id
```

### 4.5 物理文件操作（BAT 层面）

BAT 操作的对象是 **loser 目录中 winner 缺少的格式文件**。

```
winner_dir = E:\98-Calibre-books-new\{winner_path}\

for each loser_book_id:
    loser_dir = E:\98-Calibre-books-new\{loser_path}\
    
    for each data_record in loser (合并前快照):
        if data_record.format 在 winner 中已存在:
            → 不做任何操作（文件留在 loser 目录，随目录一起删除）
        else:
            → move "{loser_dir}\{data.name}.{ext}" 到 "{winner_dir}\"
    
    → 删除空的 loser_dir（含残留的重复格式文件）
```

**关键细节**：
- 文件格式 = `data.name + "." + data.format.lower()`
- 重复格式的文件不需要移动，直接随 loser 目录一起删除
- 互补格式的文件需要移动到 winner 目录

**BAT 示例**：

```bat
REM === 跨 book_id 合并：马伯庸/长安的荔枝 ===
REM winner: book_id=676, formats=AZW3,EPUB,MOBI
REM loser: book_id=29300, formats=AZW3

set "winner_dir=%LIBRARY_DIR%\马伯庸\长安的荔枝 (676)"
set "loser_dir=%LIBRARY_DIR%\马伯庸\长安的荔枝 (29300)"

REM AZW3 格式在 winner 中已存在 → 不移动，随 loser 目录删除

REM 清理 loser 目录（含残留文件）
if exist "!loser_dir!" (
    rd /s /q "!loser_dir!"
)
```

```bat
REM === 跨 book_id 合并：马伯庸/长安的荔枝 ===
REM winner: book_id=676, formats=AZW3,EPUB,MOBI
REM loser: book_id=10011, formats=PDF

set "winner_dir=%LIBRARY_DIR%\马伯庸\长安的荔枝 (676)"
set "loser_dir=%LIBRARY_DIR%\马伯庸\长安的荔枝 (10011)"

REM PDF 格式在 winner 中不存在 → 移动到 winner
if exist "!loser_dir!\长安的荔枝.pdf" (
    move /Y "!loser_dir!\长安的荔枝.pdf" "!winner_dir!\"
)

REM 清理 loser 目录
if exist "!loser_dir!" (
    rd /s /q "!loser_dir!"
)
```

### 4.6 文件名冲突分析

**移动的文件不会产生命名冲突**。原因：

- 移动的文件 = loser 中 winner 缺少的格式
- 文件名 = `{data.name}.{format_ext}`
- 同组内不同 book_id 的同一格式，`data.name` 通常相同（如都叫 "长安的荔枝"）
- 但移动的格式在 winner 中不存在 → 不会有同名同扩展名的文件

**唯一可能的冲突**：loser 的 `data.name` 与 winner 目录中已有文件恰好同名同扩展名。但这意味着同一格式在 winner 中已存在 → 该文件属于「重复格式」→ 不会被移动。

**结论：无需处理文件名冲突。**

---

## 五、边界情况处理

### 5.1 「未知/」前缀组

89 组，381 个 book_id，全部单格式。

**处理策略**：
- 不自动合并
- 输出到 `3-2-1：待人工确认-未知前缀组.csv`
- 人工确认后手动处理

### 5.2 纯重复组

部分组的所有 book_id 持有完全相同的格式组合。

**处理策略**：
- Winner 保留一份完整格式
- Loser 的所有 data 记录全部标记为重复 → DELETE
- BAT 中直接删除 loser 目录（无需移动任何文件）

### 5.3 data.name 不一致

同一格式在不同 book_id 下 `data.name` 不同：

```
book_id=676:  data.name = "changanshilizhi"  (拼音)
book_id=10011: data.name = "长安的荔枝"       (中文)
```

**处理策略**：
- 如果该格式在 winner 中已存在 → loser 的记录删除，文件随目录删除
- 如果该格式在 winner 中不存在 → 移动文件到 winner 目录，保留原文件名，data 记录 UPDATE book 指向 winner
- Calibre 用 `data.name + data.format` 定位文件，只要文件在 winner 目录下即可正常工作

### 5.4 Title 微小差异

2 个组的 path_prefix 相同但 title 有微小差异（同一版本的不同 subtitle 截断）：

```
book_id=2185: title = "呼兰河传(全本未删节插图珍藏本，1941年萧红定稿版)(作家榜推荐)"
book_id=5850: title = "呼兰河传(全本未删节插图珍藏本，1941年萧红定稿版)(作家榜推荐) (双桅船名家经典读本)"
```

**处理策略**：正常合并，title 以 winner 为准。

### 5.5 超大合并组

存在极端组：1 个 58 book_id 的组、1 个 52 book_id 的组、1 个 37 book_id 的组。

**处理策略**：无特殊处理，逻辑与小组相同。Winner 选出后，其余全部合并。

---

## 六、Pipeline 改动

### 6.1 Step3 新增：跨 book_id 合并

在 `step3_regression.py` 的现有回归扫描之后、生成 `metadata.cleaned.db` 之前，新增一个阶段：

```
step3_regression.py 执行流程：
  ① 现有回归扫描（dedup + junk + format + mojibake + copy + basename）
  ② 【新增】跨 book_id 合并扫描
     → 输出合并计划 CSV
  ③ 【新增】应用合并到 DB
     → 生成 metadata.cleaned.db
```

**新增输出文件**：

| 文件 | 说明 |
|------|------|
| `step3/3-1-1：系统确认-跨book_id同书合并.csv` | 自动合并计划（1,680 组） |
| `step3/3-2-1：待人工确认-未知前缀组.csv` | 未知/ 前缀组（89 组） |

**合并计划 CSV 字段**：

| 字段 | 说明 |
|------|------|
| merge_group | 合并组编号 |
| path_prefix | 同书标识（如 "马伯庸/长安的荔枝"） |
| winner_book_id | 胜出的 book_id |
| winner_path | winner 的 books.path |
| winner_format_count | winner 的格式数量 |
| loser_book_id | 被合并的 book_id |
| loser_path | loser 的 books.path |
| loser_formats | loser 持有的格式列表 |
| moved_data_ids | 移动到 winner 的 data_id 列表（格式互补） |
| duplicate_data_ids | 将被删除的 data_id 列表（格式重复） |

### 6.2 Step4 改动：BAT 增加文件合并指令

`step4_sql.py` 的 `_build_bat()` 读取合并计划 CSV，在 BAT 中生成文件合并 + 目录清理指令。

**BAT 执行顺序**（在现有清理指令之前）：

```
① 跨 book_id 文件合并（移动互补格式文件到 winner 目录）
② 删除 loser 空目录（含残留的重复格式文件）
③ 现有清理逻辑（dedup 文件删除、junk 目录清理等）
```

### 6.3 执行顺序

```
① step3 scan              → 回归扫描 + 跨 book_id 合并扫描
② step3 apply（内部）      → 应用合并 + 删除 → 生成 metadata.cleaned.db
③ step4 sql               → 读取合并计划 + 确认 CSV → 生成 SQL + BAT
④ step4 verify            → 验证 cleaned.db 中所有 data 的物理文件存在性
⑤ step2_cleaning_instructions.bat → 执行：文件合并 + 冗余清理
⑥ step4 verify            → 再次验证：确认所有文件在 winner 目录下
```

---

## 七、数据流变化

### 合并前

```
books 表: 17,518 条
data 表:  46,119 条
物理目录: ~17,518 个（每个 book_id 一个目录）
```

### 合并后（预估）

```
books 表: ~14,946 条  (减少 2,572 个 loser book_id)
data 表:  ~42,644 条  (减少 3,475 个 duplicate data)
物理目录: ~14,946 个  (loser 目录被清理)
```

### 对 Step5 的影响

合并后，增量库中每本书只有一个 book_id，step5 的合并逻辑更简单：
- 不再出现「同一 title 多个 book_id 需要分别匹配」的情况
- `insert_book` / `insert_data` 的分类更准确
- 元数据冲突减少（winner 持有最完整的格式集合）

---

## 八、实现计划

### 修改文件

| 文件 | 改动 |
|------|------|
| `step3_regression.py` | 新增 `_scan_cross_bookid_merge()` + `_apply_cross_bookid_merge()` |
| `step4_sql.py` | `_build_bat()` 读取合并计划，生成文件合并 + 目录清理指令 |
| `step1_rules.json` | 新增 `cross_bookid_merge_outputs` 配置 |
| `step2_config.json` | 新增合并相关输出文件名 |

### 新增文件

| 文件 | 说明 |
|------|------|
| `step3/3-1-1：系统确认-跨book_id同书合并.csv` | 自动合并计划（1,680 组） |
| `step3/3-2-1：待人工确认-未知前缀组.csv` | 未知/ 前缀组（89 组） |

### 实现步骤

1. **step3 扫描逻辑**：`_scan_cross_bookid_merge()` — 按 path_prefix 分组、选 winner、分析格式重叠、输出合并计划 CSV
2. **step3 应用逻辑**：`_apply_cross_bookid_merge()` — 对 DB 执行合并（UPDATE data、DELETE data、DELETE books、清理 link 表）
3. **step4 BAT 生成**：读取合并计划 CSV，生成文件移动 + 目录清理指令
4. **验证**：合并前后分别运行 step4 verify，确认文件存在性

---

## 九、待确认问题

1. **Winner 选择策略**：最多格式 + 最小 book_id 是否合理？还是有其他偏好（如优先选有 BookInitPath 的）？
2. **未知/ 前缀组**：排除自动合并、输出待人工确认，是否可接受？
3. **实现顺序**：是否先实现 step3 的 DB 合并，验证无误后再做 step4 的 BAT 文件合并？
