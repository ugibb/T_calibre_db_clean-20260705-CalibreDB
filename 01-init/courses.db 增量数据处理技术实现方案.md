# 课程资源门户 — 二次开发技术实施方案

## 一、架构总览

### 1.1 数据流

```
┌─────────────────────────────────────────────────────────────────────┐
│                         temp.txt (55MB, 39万行)                      │
│                   F:\01-得到\01-专栏\课程名\第01讲.mp3               │
└──────────────────────┬──────────────────────────────────────────────┘
                       │ python -m cps.courses.import_data
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      00-db/courses.db (SQLite)                       │
│  ┌────────────┐  ┌──────────────┐  ┌──────────────────┐            │
│  │  courses   │  │ course_files │  │ course_categories │            │
│  │  (课程主表)  │  │ (课程文件表)   │  │ (分类字典表)       │            │
│  └─────┬──────┘  └──────┬───────┘  └──────────────────┘            │
│        │                │                                           │
│  ┌─────┴────────────────┴───────┐                                   │
│  │   course_subscriptions       │                                   │
│  │   (用户订阅表, FK→user.id)   │                                   │
│  └─────────────────────────────┘                                   │
└──────────────────────┬──────────────────────────────────────────────┘
                       │ cps/courses/ Blueprint
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Flask Web 层 (Calibre-Web 扩展)                    │
│                                                                     │
│  cps/courses/                                                       │
│  ├── __init__.py     → Blueprint, url_prefix=/courses               │
│  ├── models.py       → SQLAlchemy 模型 + FTS5                       │
│  ├── import_data.py  → 数据导入管道                                   │
│  ├── routes.py       → 6 个路由视图                                   │
│  └── templates/      → Jinja2 模板（extends layout.html）            │
│                                                                     │
│  复用的 Calibre-Web 基础设施：                                        │
│  ├── @user_login_required    (cps/usermanagement.py)                │
│  ├── Pagination 类           (cps/pagination.py)                    │
│  ├── Bootstrap 3 + jQuery    (cps/static/)                          │
│  └── layout.html 布局        (cps/templates/layout.html)            │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 新增文件清单

| 文件 | 说明 |
|------|------|
| `cps/courses/__init__.py` | Blueprint 定义 |
| `cps/courses/models.py` | 数据库模型 + FTS5 |
| `cps/courses/import_data.py` | temp.txt → courses.db 导入脚本 |
| `cps/courses/routes.py` | 所有路由视图 |
| `cps/courses/templates/courses_layout.html` | 基础布局（extends layout.html） |
| `cps/courses/templates/index.html` | 课程列表首页 |
| `cps/courses/templates/detail.html` | 课程详情 + 文件列表 |
| `cps/courses/templates/sources.html` | 来源平台列表 |
| `cps/courses/templates/category_source.html` | 来源/分类钻取页 |
| `cps/courses/templates/categories.html` | 分类管理页 |
| `cps/courses/templates/subscriptions.html` | 已订阅课程 |
| `cps/courses/templates/search.html` | 搜索结果页 |
| `cps/courses/static/courses.css` | 样式覆盖 |
| `cps/courses/static/courses.js` | 前端交互 |

### 1.3 修改的已有文件

| 文件 | 改动 |
|------|------|
| `cps/main.py` | 注册 courses blueprint |
| `cps/templates/layout.html` | 侧边栏新增「课程库」导航项 |
| `cps/render_template.py` | 可选：新增 sidebar 配置项 |

---

## 二、数据库设计

### 2.1 库文件

**位置：** `00-db/courses.db`（独立于 metadata.db）

**初始化方式：** `cps/courses/models.py` 中 `init_db()` 自动创建

### 2.2 courses（课程主表）

```sql
CREATE TABLE courses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,              -- 课程名（从目录名提取）
    source        TEXT DEFAULT '',            -- 来源平台，如 "01-得到"
    channel       TEXT DEFAULT '',            -- 频道，如 "01-专栏"（可选）
    lecturer      TEXT DEFAULT '',            -- 主讲人
    publish_date  TEXT DEFAULT '',            -- 课程上线时间，如 "2020-03-15"
    total_lessons INTEGER DEFAULT 0,          -- 总课程节数
    description   TEXT DEFAULT '',            -- 课程简介
    tags          TEXT DEFAULT '',            -- 标签，逗号分隔，如 "金融学,商业,投资"
    category_id   INTEGER DEFAULT NULL REFERENCES course_categories(id), -- 所属分类
    file_count    INTEGER DEFAULT 0,          -- 文件总数（含附件）
    total_size    INTEGER DEFAULT 0,          -- 总大小（字节）
    has_cover     INTEGER DEFAULT 0,          -- 是否有封面
    cover_path    TEXT DEFAULT '',            -- 封面图路径
    created_at    TEXT DEFAULT (datetime('now')),
    note          TEXT DEFAULT ''             -- 备注/待整理
);
CREATE INDEX idx_courses_source ON courses(source);
CREATE INDEX idx_courses_title ON courses(title);
CREATE INDEX idx_courses_lecturer ON courses(lecturer);
CREATE INDEX idx_courses_category ON courses(category_id);
```

### 2.3 course_files（课程文件表）

```sql
CREATE TABLE course_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,                -- 文件名
    extension   TEXT DEFAULT '',              -- 小写扩展名
    full_path   TEXT NOT NULL,                -- 完整原始路径（Windows格式）
    display_path TEXT DEFAULT '',             -- 课程内相对路径
    file_size   INTEGER DEFAULT 0,           -- 文件大小
    duration    TEXT DEFAULT '',              -- 播放时长，如 "15分15秒"
    module_name TEXT DEFAULT '',              -- 所属模块，如 "模块一：戒瘾与奖赏"
    is_primary  INTEGER DEFAULT 0,           -- 是否为主文件（封面/简介等）
    sort_order  INTEGER DEFAULT 0            -- 排序序号
);
CREATE INDEX idx_cf_course ON course_files(course_id);
CREATE INDEX idx_cf_ext ON course_files(extension);
```

### 2.4 course_categories（分类字典表）

```sql
CREATE TABLE course_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,                -- 分类名
    parent_id   INTEGER REFERENCES course_categories(id),  -- NULL=一级
    sort_order  INTEGER DEFAULT 0,           -- 排序序号
    course_count INTEGER DEFAULT 0           -- 物化课程数
);
CREATE INDEX idx_cc_parent ON course_categories(parent_id);

-- 预置 16 个一级分类
INSERT INTO course_categories (id, name, sort_order) VALUES
(1,'视频课',1),(2,'自我提升',2),(3,'金融学',3),(4,'商业',4),
(5,'心理学',5),(6,'人文社科',6),(7,'医学与健康',7),(8,'法律',8),
(9,'经济学',9),(10,'艺术',10),(11,'科技',11),(12,'职场',12),
(13,'家庭亲子',13),(14,'自然科学',14),(15,'管理学',15);
```

---

## 三、数据初始化（temp.txt → courses.db）

### 3.1 初始化流程

```
步骤 1：激活 Python 虚拟环境
步骤 2：创建分类字典表（预置 16 个分类）
步骤 3：逐行读取 temp.txt，筛选课程类目录
步骤 4：按「三级目录 = 课程」规则分组，提取各字段
步骤 5：批量写入 courses.db
步骤 6：FTS5 全文索引填充
步骤 7：物化统计字段更新
```

### 3.2 执行命令

```bash
# 1. 激活环境
cd /Users/zhouhanbao/Documents/01-CC/98-Content/g_20260714-calibre-web
source venv/bin/activate

# 2. 先预览统计（不下库，确认数据范围）
python -m cps.courses.import_data --input 01-init/temp.txt --dry-run

# 3. 正式导入（完整管道，约 1 分钟）
python -m cps.courses.import_data --input 01-init/temp.txt

# 4. 验证导入结果
python -c "
import sqlite3
conn = sqlite3.connect('00-db/courses.db')
cur = conn.execute('SELECT COUNT(*) FROM courses')
print(f'课程数: {cur.fetchone()[0]}')
cur = conn.execute('SELECT COUNT(*) FROM course_files')
print(f'文件数: {cur.fetchone()[0]}')
cur = conn.execute('SELECT COUNT(*) FROM course_categories')
print(f'分类数: {cur.fetchone()[0]}')
cur = conn.execute('SELECT source, COUNT(*) FROM courses GROUP BY source ORDER BY 2 DESC LIMIT 10')
for r in cur.fetchall():
    print(f'  {r[0]}: {r[1]}门课')
conn.close()
"

# 5. 增量更新（如果 temp.txt 更新了）
python -m cps.courses.import_data --input 01-init/temp.txt --resume
```

### 3.3 预期输出示例

```
📖 读取: 01-init/temp.txt (55MB)
总行数: 394,860

⏳ Phase 1: 筛选课程目录...
  纳入: 212,345 行（13个课程平台）
  排除: 182,515 行（电子书/游戏/杂项）

⏳ Phase 2: 课程分组...
  课程数: 4,872
  文件数: 212,345
  单文件课程: 312（标记待整理）

⏳ Phase 3: 写入 courses.db...
  ✓ 分类字典: 15 个一级 + 32 个二级（可选）
  ✓ 课程: 4,872 门
  ✓ 文件: 212,345 个
  ✓ FTS5 索引已填充

✅ 导入完成！总耗时 53s
```

### 3.4 各来源课程估算

| 来源 | 文件数 | 预估课程数 |
|------|--------|-----------|
| 01-得到 | 105,591 | ~1,800 |
| 02-喜马拉雅 | 57,671 | ~2,300 |
| 08-蜻蜓 | 20,523 | ~450 |
| 05_-少年得到 | 12,479 | ~300 |
| 03-混沌 | 7,842 | ~80 |
| 06_十点课堂 | 7,797 | ~200 |
| 12-网易 | 6,670 | ~150 |
| 04-樊登 | 3,766 | ~90 |
| 07_唯库 | 3,554 | ~100 |
| 11_三·节·课 | 3,897 | ~80 |
| 13_荔枝微课 | 1,581 | ~60 |
| 14_丁香妈妈 | 2,589 | ~50 |
| **总计** | **~245,000** | **~4,800** |

### 3.5 课程识别规则（代码逻辑）

```python
def extract_course_from_path(parts):
    """
    从路径分段中提取课程信息。
    parts = ['99-知识课程', '01-得到', '01-专栏', '香帅的北大金融学课', '第01讲.mp3']
             ↑ source      ↑ channel ↑ title            ↑ 文件
    """
    if len(parts) < 3:
        return None  # 深度不足

    source  = parts[0] if len(parts) >= 2 else ''   # 第一级
    channel = parts[1] if len(parts) >= 3 else ''   # 第二级
    title   = parts[2] if len(parts) >= 4 else ''   # 第三级 = 课程
    file_parts = parts[3:]                           # 课程以下所有 = 文件

    return {
        'source': source,
        'channel': channel,
        'title': title,
        'files': file_parts,
    }
```

### 3.6 异常处理规则

| 场景 | 处理方式 |
|------|---------|
| 路径不足 3 层 | 不导入，记录 `log` |
| 文件名含乱码 | 保留原字符串，编码 `utf-8 errors=replace` |
| 同一路径多个文件 | 归入同一课程 |
| 课程名含特殊字符 | 保留原始目录名 |
| 课程下无文件 | 不创建课程 |
| 课程下只有 1 个文件 | 创建课程，`note='待整理'` |
| 来源不在白名单 | 跳过（如「新建文件夹」「游戏」等） |
```

### 2.5 course_subscriptions（用户订阅表）

```sql
CREATE TABLE course_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,            -- FK→user.id (app.db)
    course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    created_at  TEXT DEFAULT (datetime('now')),
    progress    REAL DEFAULT 0,              -- 学习进度 0~1
    UNIQUE(user_id, course_id)
);
CREATE INDEX idx_cs_user ON course_subscriptions(user_id);
```

### 2.6 FTS5 全文搜索

```sql
CREATE VIRTUAL TABLE courses_fts USING fts5(
    title, lecturer, tags,
    content='courses',
    content_rowid='id',
    tokenize='unicode61'
);
```

---

## 三、导入脚本实现

### 3.1 命令行接口

```bash
# 标准导入（只导入课程类目录）
python -m cps.courses.import_data --input 01-init/temp.txt

# 预览统计（不写入 DB）
python -m cps.courses.import_data --dry-run

# 只导入指定来源
python -m cps.courses.import_data --source "01-得到"

# 只导入课程分类
python -m cps.courses.import_data --categories-only
```

### 3.2 导入管道（三段式）

```
Phase 1: 路径解析
  ├─ 逐行流式读取（55MB，不加载到内存）
  ├─ PureWindowsPath 解析
  ├─ 筛选课程类目录（约 20 个来源平台）
  ├─ 排除 metadata.db 已有的电子书目录
  └─ 排除游戏/软件/电影 等杂项

Phase 2: 课程分组（目录树 → 课程 + 文件）
  ├─ 从文件路径逐层向上，取第三层目录 = 课程名
  ├─ 课程目录下所有文件（含子目录）递归归入
  ├─ 提取字段：
  │   ├─ source   ← 第一级目录（平台名）
  │   ├─ channel  ← 第二级目录（频道名）
  │   ├─ title    ← 第三级目录（课程名）
  │   ├─ module_name ← 课程目录下的子目录名（模块/章节）
  │   ├─ duration ← 文件名中正则提取（\d+分\d+秒）
  │   └─ sort_order ← 文件名中正则提取序号前缀
  └─ 异常处理：
      ├─ 深度不足3层 → 取最深目录为课程
      ├─ 单文件课程 → 保留，标记 note='待整理'
      ├─ 编码异常 → 保留原字符串
      └─ 重复课程名（不同路径）→ 用全路径区分

Phase 3: 写入 courses.db
  ├─ 分类树去重缓存 → 先写入
  ├─ 课程 + 文件批量写入（每 500 课程 commit）
  ├─ 物化统计字段更新
  └─ FTS5 索引填充
```

### 3.3 性能指标

| 阶段 | 预估耗时 | 说明 |
|------|---------|------|
| Phase 1: 读取+筛选 | 5-10s | 逐行读 55MB |
| Phase 2: 分组 | 3-5s | 内存操作 |
| Phase 3: 写入 | 30-60s | 39万文件 + 批量 commit |
| **总计** | **~1 min** | |

---

## 四、路由设计

### 4.1 路由表

| 路由 | 视图 | 模板 | 说明 |
|------|------|------|------|
| `GET /courses/` | `index()` | `index.html` | 课程列表首页（分页 + 分类筛序） |
| `GET /courses/<id>/` | `detail()` | `detail.html` | 课程详情 + 文件列表 |
| `GET /courses/sources/` | `sources()` | `sources.html` | 来源平台列表 |
| `GET /courses/source/<name>/` | `source_detail()` | `category_source.html` | 特定来源的课程列表 |
| `GET /courses/categories/` | `categories()` | `categories.html` | 分类管理页（admin 权限） |
| `GET /courses/subscriptions/` | `subscriptions()` | `subscriptions.html` | 用户已订阅课程 |
| `POST /courses/subscribe/toggle` | `toggle_subscribe()` | JSON | AJAX 订阅/取消 |
| `GET /courses/search/` | `search()` | `search.html` | 课程+文件搜索 |

### 4.2 关键视图实现要点

**index()：**
- `?page=N` 分页，每页 24 条
- `?source=01-得到` 来源筛选
- `?category=3` 分类筛选（FK→course_categories.id）
- `?q=xxx` 搜索关键词（FTS5 查询）
- 使用 `Pagination` 类分页
- 返回 `entries`（课程列表）+ `categories`（分类树）+ `pagination`

**detail()：**
- 课程基本信息（title, lecturer, publish_date, total_lessons, description, tags）
- 课程文件列表按 `module_name` 分组
- 文件显示格式、时长、大小、下载按钮
- `total_lessons` 取 `course_files` 中 `extension IN ('mp3','mp4','m4a','wav')` 计数
- AJAX 订阅按钮

**search()：**
- 使用 FTS5 `MATCH` 查询
- 结果分「课程」和「文件」两个区段
- 格式筛序 `?ext=mp3`
- 来源筛序 `?source=01-得到`

### 4.3 数据库会话管理

```python
# cps/courses/__init__.py
from flask import g
from .models import init_db

def get_courses_db():
    if 'courses_db' not in g:
        g.courses_db = init_db()
    return g.courses_db

@courses.teardown_request
def teardown(exception):
    db = g.pop('courses_db', None)
    if db is not None:
        db.close()
```

---

## 五、页面模板

### 5.1 布局策略

所有模板 `extends "layout.html"`，复用 Calibre-Web 的导航栏和侧边栏。

```html
{% extends "layout.html" %}
{% block body %}
<div class="discover load-more">
  <h2>{{ title }}</h2>
  {# ... 页面内容 ... #}
</div>
{% endblock %}
```

### 5.2 课程卡片（复用 book card 样式）

```html
<div class="col-sm-3 col-lg-2 col-xs-6 book session">
  <div class="cover">
    <a href="{{ url_for('courses.detail', course_id=course.id) }}">
      <span class="img" title="{{ course.title }}">
        {% if course.has_cover %}
          <img src="{{ course.cover_path }}" alt="{{ course.title }}" />
        {% else %}
          <div style="background:linear-gradient(135deg,#e8f5e9,#b2dfdb);
                      width:180px;height:225px;display:flex;align-items:center;
                      justify-content:center;font-size:48px;border-radius:3px;">
            🎓
          </div>
        {% endif %}
      </span>
    </a>
    <span class="badge" style="position:absolute;top:8px;right:8px;
          background:rgba(0,0,0,.55);color:#fff;font-weight:400;font-size:11px;">
      {{ course.source }}
    </span>
    <span class="badge" style="position:absolute;bottom:8px;left:8px;
          background:var(--accent);color:#fff;font-weight:400;font-size:11px;">
      {{ course.total_lessons or course.file_count }}讲
    </span>
  </div>
  <div class="meta">
    <a href="{{ url_for('courses.detail', course_id=course.id) }}">
      <p class="title">{{ course.title|shortentitle(25) }}</p>
    </a>
    <p class="author">{{ course.lecturer or '' }}</p>
    {% if course.total_lessons %}
      <p class="series">共 {{ course.total_lessons }} 课时</p>
    {% endif %}
  </div>
</div>
```

### 5.3 分类平铺标签

```html
<div class="filterheader">
  {# 排序按钮 #}
  <a href="#" class="btn btn-link btn-sm" style="margin-left:auto;color:#45b29d"
     onclick="showPage('categories')">管理分类</a>
</div>

{# 一级分类平铺 #}
<div class="cat-tiles">
  <div class="cat-level1">
    <span class="l1-item {{ 'active' if not selected_category }}"
          onclick="filterByCategory(0)">全部 <span class="count">{{ total }}</span></span>
    {% for cat in categories if not cat.parent_id %}
      <span class="l1-item {{ 'active' if selected_category == cat.id }}"
            onclick="filterByCategory({{ cat.id }})">
        {{ cat.name }} <span class="count">{{ cat.course_count }}</span>
      </span>
    {% endfor %}
  </div>
  {# 二级分类 — JS 动态切换 #}
  {% for cat in categories if not cat.parent_id %}
    <div class="cat-level2 {{ 'show' if selected_category == cat.id }}" id="l2-{{ cat.id }}">
      <span class="l2-item active">全部</span>
      {% for sub in categories if sub.parent_id == cat.id %}
        <span class="l2-item">{{ sub.name }} <span class="count">{{ sub.course_count }}</span></span>
      {% endfor %}
    </div>
  {% endfor %}
</div>
```

### 5.4 课程详情 + 文件列表

```html
{% extends "layout.html" %}
{% block body %}
<div class="single">
  <div class="row">
    <div class="col-sm-3 col-xs-5">
      <div class="cover">{% if course.has_cover %}<img src="..."/>{% endif %}</div>
    </div>
    <div class="col-sm-9 col-xs-7 book-meta">
      <div class="btn-toolbar">
        <button class="btn btn-subscribe"><span class="glyphicon glyphicon-heart"></span> 订阅</button>
        <button class="btn btn-primary"><span class="glyphicon glyphicon-download-alt"></span> 下载全部</button>
      </div>
      <h2>{{ course.title }}</h2>
      <p><strong>主讲人：</strong>{{ course.lecturer }}</p>
      <p><strong>上线时间：</strong>{{ course.publish_date }}</p>
      <p><strong>总课时：</strong>{{ course.total_lessons }} 讲</p>
      <p><strong>标签：</strong>
        {% for tag in course.tags.split(',') %}
          <span class="label label-success">{{ tag }}</span>
        {% endfor %}
      </p>
      <p><strong>来源：</strong>{{ course.source }} / {{ course.channel }}</p>
      <hr>
      <p>{{ course.description }}</p>
    </div>
  </div>

  {# 文件列表 #}
  <table class="table table-hover">
    <thead><tr><th>#</th><th>文件名</th><th>格式</th><th>时长</th><th>大小</th><th></th></tr></thead>
    <tbody>
      {% for module, files in files|groupby('module_name') %}
        <tr class="module-separator"><td colspan="6">{{ module or '通用' }}</td></tr>
        {% for f in files %}
          <tr>
            <td>{{ f.sort_order if f.sort_order else '' }}</td>
            <td>{{ f.filename }}</td>
            <td><span class="label label-{{ 'info' if f.extension in ('mp3','m4a','wav') else 'warning' if f.extension in ('mp4','flv') else 'danger' }}">{{ f.extension }}</span></td>
            <td>{{ f.duration or '—' }}</td>
            <td>{{ f.file_size|filesizeformat }}</td>
            <td><a href="#" class="btn btn-xs btn-link"><span class="glyphicon glyphicon-download-alt"></span></a></td>
          </tr>
        {% endfor %}
      {% endfor %}
    </tbody>
  </table>
  {{ pagination|safe }}
</div>
{% endblock %}
```

---

## 六、侧边栏导航

### 6.1 在 layout.html 中新增导航项

在 `<ul id="scnd-nav">` 的 Browse 分区中，按 Calibre-Web 的 sidebar 配置模式新增：

```html
<li class="nav-head">课程库</li>
<li><a href="{{ url_for('courses.index') }}">
  <span class="glyphicon glyphicon-education"></span> 全部课程
</a></li>
<li><a href="{{ url_for('courses.sources') }}">
  <span class="glyphicon glyphicon-globe"></span> 来源平台
</a></li>
<li><a href="{{ url_for('courses.categories') }}">
  <span class="glyphicon glyphicon-tags"></span> 课程分类
</a></li>
<li><a href="{{ url_for('courses.subscriptions') }}">
  <span class="glyphicon glyphicon-heart"></span> 已订阅
</a></li>
```

也可在 `cps/render_template.py` 的 `get_sidebar_config()` 中按配置化方式加入（复用现有 sidebar 机制）。

### 6.2 注册 Blueprint

```python
# cps/main.py
from .courses import courses
app.register_blueprint(courses)
```

---

## 七、实施计划

### Phase 1：数据库 + 导入（估计 2-3 小时）

| 步骤 | 产出 |
|------|------|
| 1.1 创建 `cps/courses/` 包结构 | `__init__.py`, `models.py` |
| 1.2 定义 SQLAlchemy 模型 | 4 张表 + FTS5 |
| 1.3 实现导入脚本 | `import_data.py`（三段式管道） |
| 1.4 全量运行导入 | `courses.db` 生成 |
| 1.5 数据校验 | 课程数、文件数、分类层级 |

### Phase 2：路由 + API（估计 2-3 小时）

| 步骤 | 产出 |
|------|------|
| 2.1 Blueprint + 数据库会话管理 | `__init__.py` 完善 |
| 2.2 路由：课程列表 | `routes.index()` + 分页 |
| 2.3 路由：课程详情 | `routes.detail()` + 文件列表 |
| 2.4 路由：来源/分类 | `routes.sources()`, `routes.source_detail()` |
| 2.5 路由：搜索 | `routes.search()` FTS5 |
| 2.6 路由：订阅 | `routes.toggle_subscribe()` AJAX |
| 2.7 路由：分类管理 | `routes.categories()` admin CRUD |

### Phase 3：模板 + 前端（估计 3-4 小时）

| 步骤 | 产出 |
|------|------|
| 3.1 基础布局 | `courses_layout.html` |
| 3.2 课程列表页 | `index.html` 卡片网格 + 分类平铺 |
| 3.3 课程详情页 | `detail.html` 文件表格 |
| 3.4 来源/分类钻取 | `sources.html`, `category_source.html` |
| 3.5 分类管理页 | `categories.html` 树形管理 |
| 3.6 已订阅页 | `subscriptions.html` 进度显示 |
| 3.7 搜索页 | `search.html` 混合结果 |
| 3.8 CSS + JS | `courses.css`, `courses.js` |

### Phase 4：集成（估计 1 小时）

| 步骤 | 产出 |
|------|------|
| 4.1 注册 Blueprint | `cps/main.py` |
| 4.2 侧边栏导航 | `layout.html` / `render_template.py` |
| 4.3 Phoenix 启动测试 | 全流程走通 |
| 4.4 性能验证 | 搜索 < 200ms, 分页 < 50ms |

### 总预估工时：**8-11 小时**

---

## 八、可复用代码清单

| 现有代码 | 位置 | 复用方式 |
|----------|------|---------|
| `@user_login_required` | `cps/usermanagement.py` | 装饰器 |
| `Pagination` 类 | `cps/pagination.py` | 直接 import |
| `iter_pages()` | `cps/pagination.py` | 模板调用 |
| `url_for_other_page` | `cps/jinjia.py` | 模板 filter |
| `render_title_template()` | `cps/render_template.py` | 可选复用 |
| `layout.html` | `cps/templates/layout.html` | extends |
| `Bootstrap 3` | `cps/static/css/libs/bootstrap.min.css` | 无需重复打包 |
| `jQuery 3.6.3` | `cps/static/js/libs/jquery.min.js` | 直接引用 |
| `style.css` 配色变量 | `cps/static/css/style.css` | #45b29d 等 |
| `glyphicons` 图标 | `cps/static/fonts/` | 直接使用 |

---

## 九、验证清单

- [ ] `import_data.py` 全量导入，日志无报错
- [ ] `courses.db` 课程数、文件数、分类数符合预期
- [ ] `/courses/` 显示分类平铺 + 课程卡片网格
- [ ] 点击一级分类标签，二级分类显示/隐藏
- [ ] 分页正常，页码跳转正确
- [ ] 课程详情页文件按模块分组
- [ ] FTS5 搜索中文关键词返回正确结果
- [ ] AJAX 订阅/取消订阅，页面状态即时更新
- [ ] 跨 Calibre-Web 电子书页面导航不冲突
- [ ] 侧边栏「课程库」导航点亮态正常
- [ ] 移动端布局无错乱
