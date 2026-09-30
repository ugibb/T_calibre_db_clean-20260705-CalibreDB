# courses.db 增量数据处理技术方案

> 仅涉及本项目内文件，不跨项目操作

---

## 一、上下文

### 问题

`03-input/02-courses/courses-YYYYMMDD.csv`（394,861 行）是文件路径清单的全量快照。现有单体脚本缺乏增量审计、比对机制、人工确认环和幂等执行。

### 输入

| 文件 | 说明 |
|---|---|
| `03-input/02-courses/courses-YYYYMMDD.csv` | 课程文件路径清单（全量快照） |
| `04-output/02-courses/{batch}/step1/courses-augmented.csv` | Step1 输出 → 人工已校正（含 source/source_category/channel/title/model/dir_leaf） |

### 目标

参考 Calibre DB 增量处理范式，实现两阶段增量处理流水线。

---

## 二、数据流总览

```
03-input/02-courses/courses-YYYYMMDD.csv
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Step1-0: CSV full_path → 按 \ 拆分为 dir_1~dir_N         │
│   输出: courses-augmented.csv（人工校正 source/channel/   │
│         title/model/dir_leaf 字段）                       │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────┐
│ Step1-1: 读取人工校正后 CSV → courses_incremental.db      │
│   ① 按 (source,channel,title) 分组为课程                  │
│   ② 提取讲师（从 title 的 · 前）→ lecturers 表            │
│   ③ source 去重 → platforms 表                            │
│   ④ channel 去重 → course_categories 表                   │
│   ⑤ 写入 courses + course_files                          │
│   ⑥ 创建 FTS5 索引                                      │
└──────────────────────────┬───────────────────────────────┘
                           ▼
        courses_incremental.db    +    全量 courses-full-YYYYMMDD.db
                           │                  │
                           ▼                  ▼
┌──────────────────────────────────────────────────────────┐
│ Step2: 增量合并（incremental → full）                     │
│  Step2-1 scan: 比对增量与全量 → 差异 CSV                  │
│  Step2-2 plan: 生成同步 SQL                              │
│  Step2-3 apply: 执行同步                                 │
└──────────────────────┬───────────────────────────────────┘
                       ▼
         04-output/00-full-db/courses-full-YYYYMMDD.db
```

---

## 三、数据库 ER 模型

### 3.1 实体关系

```
┌────────────┐     ┌──────────────────────────┐     ┌──────────────┐
│  lecturers │     │  courses                 │     │ course_files │
├────────────┤     ├──────────────────────────┤     ├──────────────┤
│ id (PK)    │←────│ lecturer_id              │────→│ course_id    │
│ name       │     │ id (PK)                  │     │ id (PK)      │
│ title      │     │ title                    │     │ filename     │
│ img        │     │ source                   │     │ extension    │
│ _desc      │     │ channel                  │     │ full_path    │
└────────────┘     │ platform_id (FK)         │     │ file_size    │
                   │ category_id              │     │ file_type    │
┌────────────────┐ │ lecturer_id (FK)         │     │ module_name  │
│  platforms     │ │ score                    │     │ sort_order   │
├────────────────┤ │ phase_num                │     │ created_at   │
│ id (PK)        │ │ learn_user_count         │     └──────────────┘
│ name           │ │ online_time              │
│ channel        │ │ index_img                │            ┌──────────────────┐
└────────────────┘ │ horizontal_img           │            │ course_category  │
                   │ cover_path               │            │ _links           │
┌────────────────┐ │ has_cover                │────→       ├──────────────────┤
│ course_categories│ │ file_count              │            │ course_id (FK)   │
├────────────────┤ │ total_size               │            │ category_id (FK) │
│ id (PK)        │ │ description              │            │ type (nav/tag)   │
│ name           │ │ tags                     │            └──────────────────┘
│ parent_id(FK)  │ │ created_at               │
│ sort_order     │ └──────────────────────────┘
│ source_type    │
│ source_value   │
│ source_group   │
└────────────────┘
```

### 3.2 数据库 DDL

```sql
-- 讲师表
CREATE TABLE IF NOT EXISTS lecturers (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL,
    title TEXT DEFAULT '',
    img   TEXT DEFAULT '',
    _desc TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lecturers_name ON lecturers(name);

-- 平台表
CREATE TABLE IF NOT EXISTS platforms (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    channel TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_platforms_name ON platforms(name);

-- 分类表
CREATE TABLE IF NOT EXISTS course_categories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    parent_id     INTEGER REFERENCES course_categories(id),
    sort_order    INTEGER DEFAULT 0,
    course_count  INTEGER DEFAULT 0,
    source_type   TEXT DEFAULT 'channel',
    source_value  TEXT DEFAULT '',
    source_group  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cc_parent ON course_categories(parent_id);

-- 课程表
CREATE TABLE IF NOT EXISTS courses (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    title                 TEXT NOT NULL,
    platform_id           INTEGER DEFAULT 0,
    lecturer_id           INTEGER DEFAULT 0,
    category_id           INTEGER DEFAULT NULL,
    description           TEXT DEFAULT '',
    score                 TEXT DEFAULT '',
    phase_num             INTEGER DEFAULT 0,
    learn_user_count      INTEGER DEFAULT 0,
    online_time           TEXT DEFAULT '',
    index_img             TEXT DEFAULT '',
    horizontal_img        TEXT DEFAULT '',
    cover_path            TEXT DEFAULT '',
    has_cover             INTEGER DEFAULT 0,
    file_count            INTEGER DEFAULT 0,
    total_size            INTEGER DEFAULT 0,
    tags                  TEXT DEFAULT '',
    created_at            TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_courses_title ON courses(title);
CREATE INDEX IF NOT EXISTS idx_courses_platform_id ON courses(platform_id);
CREATE INDEX IF NOT EXISTS idx_courses_lecturer_id ON courses(lecturer_id);

-- 课程-分类关联表
CREATE TABLE IF NOT EXISTS course_category_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES course_categories(id) ON DELETE CASCADE,
    link_type   TEXT DEFAULT 'channel',
    UNIQUE(course_id, category_id)
);

-- 课程文件表
CREATE TABLE IF NOT EXISTS course_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id     INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    extension     TEXT DEFAULT '',
    full_path     TEXT NOT NULL,
    display_path  TEXT DEFAULT '',
    file_size     INTEGER DEFAULT 0,
    file_type     TEXT DEFAULT '',
    duration      TEXT DEFAULT '',
    module_name   TEXT DEFAULT '',
    is_primary    INTEGER DEFAULT 0,
    sort_order    INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cf_course ON course_files(course_id);

-- FTS5 索引
CREATE VIRTUAL TABLE IF NOT EXISTS courses_fts USING fts5(
    title, lecturer, tags,
    content='courses',
    content_rowid='id',
    tokenize='unicode61'
);

-- 用户功能表
CREATE TABLE IF NOT EXISTS course_subscriptions (...);
CREATE TABLE IF NOT EXISTS course_downloads (...);
```

---

## 四、Step1-1：从人工校正 CSV 直接入库

### 4.1 职责

读取 Step1-0 输出并经人工校正的 `courses-augmented.csv`，直接完成全量建库。

### 4.2 字段映射

| CSV 列 | 写入表 | 写入字段 | 处理 |
|---|---|---|---|
| `source` | `platforms` | `name` | 去重 INSERT OR IGNORE |
| `source` | `courses` | `source` | 直接写入 |
| `channel` | `courses` | `channel` | 直接写入 |
| `title` | `courses` | `title` | 直接写入 |
| `model` | `course_files` | `module_name` | 直接写入 |
| `dir_leaf` | `course_files` | `filename` | 直接写入 |
| `full_path` | `course_files` | `full_path` | 直接写入 |
| `file_type` | `course_files` | `file_type` | 直接写入 |
| `ex_name` | `course_files` | `extension` | 直接写入 |
| `bytes` | `course_files` | `file_size` | 直接写入 |
| `title` | `lecturers` | `name` | 从 title 提取 `·` 前文本 |
| `No1` | `course_files` | `sort_order` | 按 No1 升序排列 |

### 4.3 讲师提取

title 包含 `·` 时提取：
- `"王路·深度思考日课"` → lecturer_name="王路"
- `"36-香帅的北大金融学课"` → 不提取（无 `·`）

### 4.4 分类初始化

channel 列去重后插入 `course_categories`（source_type='channel'），每课程按 channel 关联。
source_category 列（人工维护，如 `得到：大师课`）按 `：` 拆分两级后以 `source_type='source_category'` 独立入库，
课程经 `courses.source_category_id` 关联（二级优先，无二级则一级）。

---

## 五、目录与输出

```
06-src/courses_import_tool/
├── __init__.py / common.py / cli.py
├── step1_prepare.py           # Step1: CSV 路径拆分 → courses-augmented.csv
├── step2_build_db.py          # Step2: 读取校正 CSV → courses_incremental.db（含 source_category）
├── step3_scan_diff.py         # Step3: 增量比对 → 差异 CSV
├── step4_plan_sql.py          # Step4: 生成增量 SQL
├── step5_apply_sql.py         # Step5: 执行同步 → 新全量库
├── import_data.py             # 旧版导入工具（temp.txt → courses.db，历史）
├── courses_step1_rules.json / courses_step2_config.json  # 模板位于 06-src/template/

04-output/02-courses/{batch}/
├── step1/courses-augmented.csv # 待人工校正（含 source_category 列，默认空）
├── step2/
│   ├── courses_incremental.db  # 产物：增量课程 DB（source_category 两级结构）
│   └── step1_apply_report.csv
├── step3/
│   ├── 2-1-* / 2-2-*        # 比对 CSV
├── step4/
│   ├── step4_merge_instructions.sql
│   └── step4_merge_report.csv
└── step5/courses-full-YYYYMMDD.db  # 新全量库产物

04-output/00-full-db/courses-full-YYYYMMDD.db  # 历史基准库，step5 后不再更新
```

---

## 六、CLI 设计

```bash
# Step1: CSV 预处理 → courses-augmented.csv（含 source_category 列，默认空）
PYTHONPATH=06-src python -m courses_import_tool.cli step1 \
  --input 03-input/02-courses/courses-20260715.csv

# Step2: 读取人工校正 CSV → courses_incremental.db
PYTHONPATH=06-src python -m courses_import_tool.cli step2 \
  --augmented-csv 04-output/02-courses/2026-07-15/step1/courses-augmented.csv

# Step3/4/5: 比对 → SQL → 新全量库（自动检测全量库：优先 step5/，否则 00-full-db/）
PYTHONPATH=06-src python -m courses_import_tool.cli step3
PYTHONPATH=06-src python -m courses_import_tool.cli step4
PYTHONPATH=06-src python -m courses_import_tool.cli step5
```

---

## 七、验证

```bash
sqlite3 04-output/02-courses/2026-07-15/step2/courses_incremental.db "
  SELECT 'courses:', COUNT(*) FROM courses;
  SELECT 'files:', COUNT(*) FROM course_files;
  SELECT 'platforms:', COUNT(*) FROM platforms;
  SELECT 'categories:', COUNT(*) FROM course_categories;
  SELECT 'lecturers:', COUNT(*) FROM lecturers;
  SELECT 'fts:', COUNT(*) FROM courses_fts;
"
```
