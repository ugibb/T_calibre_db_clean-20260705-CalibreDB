# Calibre 查重删除插件 · 实现方案

> 版本 **v3** · 2026-09-23 · 状态：**已实现，待 Windows 验收**
>
> 关联项目：20260705-CalibreDB（Calibre 增量预处理流水线）
>
> **v2 → v3 变更**：插件已按本方案实现完毕（15 个模块 / **380 个测试通过** / `dist/BookDedup4Del.zip` 可安装）。
> v3 把**实现期间核实过的新事实**与**实现时偏离方案的决策**回填到本文档，并修正了 §7/§8/§9/§10 与附录 A 中已被实现推翻的描述。
> 另记录了首次装机踩到的 `InvalidPlugin`（zip 布局）及其修复与回归测试。
>
> **v1 → v2 变更**：v1 基于 1,647 本的样本库写成，结论有系统性偏差。v2 依据真实库 `03-input/metadata-20260923.db`（**64,787 本 / 1.9TB**）的实测数据重写，第 3 节与第 6 节为全新内容。
>
> 本方案的 API 与打包规则均取自 calibre 与 Find Duplicates 的**真实源码**，实测数据可用附录 B 脚本复现。
>
> 📖 **操作手册与开发说明见 [`06-src/calibre_plugin/BookDedup4Del/README.md`](../06-src/calibre_plugin/BookDedup4Del/README.md)** —— 本文档是设计依据，那份是使用/维护入口。

---

## 0. 一句话

做一个 Calibre 界面动作插件：按**用户自选的字段组合**找出重复书籍，按保留策略自动分配「保留 / 删除」标记，人工在**组级**批量复核后，一键把「删除」标记的书连文件带目录**移入回收站**。

---

## 0.5 实现现状（与本文档的差异）

方案总体成立，但有 **7 处实现时偏离**，理由记录在此，避免后来者以为代码写错了：

| # | 方案原文 | 实际实现 | 为什么改 |
|---|---|---|---|
| 1 | §5.1 用 calibre 标记（`marked:dup_del`）承载「保留/删除」 | **不用 calibre 标记**。保留/删除是插件内存态（`selection.py`），只在删除时落地 | 用户的第 5 条要的是「最后点击批量删除」，不是「打标记让用户自己删」。而且写 `marked:` 会污染用户既有标记，还得额外做「清除本插件标记」。内存态零副作用，代价是关窗不保留（可接受 —— 复核本来就是一次做完） |
| 2 | §7.1 走 `gui.iactions['Remove Books'].do_library_delete(ids)` | **直接调 `new_api.remove_books()`**，自己实现确认/进度/结果汇报 | `do_library_delete` 是**另一个插件**（Remove Books 动作）的方法，不是公开 API；它内部还有设备连接检测（`do_library_delete` 会先查已连接设备），插件的复核流程里插一层别人的对话框反而不可控。而且本机无法验证它的行为，调它等于把最关键的一步交给未验证代码 |
| 3 | §7.1 「固定 `permanent=False`，不提供永久删除」 | **提供 `delete_permanent` 开关**（默认关） | 回收站过期前磁盘空间不释放（§7.2），删 1.5TB 的用户有真实动机要绕过回收站。默认关闭 + 确认框明确提示「没有回头路」，风险可控 |
| 4 | §6.2 组级行 + 成员懒加载的树形视图 | **平铺表格 + 分页**（`model.py` / `dialog.py`） | 9,889 组用树形控件，即使懒加载，展开态管理也很绕。分页表格更简单，且天然与「每批最多删 500 本」的批次语义对齐 |
| 5 | §6.3「默认零勾选，必须显式应用标记」 | **默认即按策略预选**「第一条保留、其余删除」 | 与用户第 3 条直接冲突 —— 他要的就是「默认第一条保留，其他删除」。改由三层漏斗 + 批量上限 + 导出清单 + 手打确认来控风险，而不是靠「默认不选」 |
| 6 | §4.1「从 `db.field_metadata` 枚举，用户可勾选任意组合」 | **候选来自 `displayable_field_keys()`**（含自定义列），装配逻辑落在纯模块 `fields.arrange_choices()`；取数**按勾选**，另加一批**恒取**的策略字段（`fields.POLICY_FIELDS`） | 原实现的过滤条件 `is_custom_field(k) and not is_ignorable_field(k)` **永不同真**（calibre 里 `is_ignorable_field` 的定义就是「是自定义列」），自定义列列表恒为空；同时标准字段只列死了 10 个。这段逻辑原本在 `config.py`（Qt，本机跑不了）所以无人察觉 —— 挪进纯模块后有了单测 |
| 7 | §4.1 未提默认组合 | 默认从 `title+authors` 改为 **`title+authors+#book_init_path`**，并对已装用户做**一次性迁移**（`settings.py`） | 用户要求「包含自定义字段 BookInitPath」。实测新默认少删 6,865 本、多识别 1,059 组。迁移只搬「存量值恰好等于旧默认」的，用户自己挑过的组合一律不动 |

**方案中仍然成立、且已在代码里落实的**：三层漏斗（§3）、字段组合与多值聚合（§4）、保留策略判据顺序（§5.2）、头部统计（§6.1）、每组至少留一本（§6.3-1）、删除前强制导出 CSV（§6.3-4）、首轮 500 本上限（§6.3-5）、硬链接回收站的全部结论（§7.2）、分层架构（§8）。

**新增的、方案里没有的**：

- `settings.py` —— 设置契约单独成模块，见 §8
- `selection.py` —— 保留/删除状态机（含 `LastKeepError`）
- `report.py` —— 清单 CSV 导出
- `delete_flow.py` —— 删除流程独立成模块
- `display.py` —— 组标题截断（纯函数，本机可测）
- `fields.arrange_choices()` —— 「配置界面列哪些字段」的装配逻辑从 `config.py`（Qt）挪进纯模块 `fields.py`。**这是为了让它可被单测** —— 原来的实现在 Qt 模块里，那个恒假条件因此藏了很久（见偏离表第 6 条）
- `fields.POLICY_FIELDS` + `fields.record_fields()` —— 「哪些字段必须无条件取数」的单一真源。取数改成按勾选之后，引擎判据（元数据评分、占位作者）与 CSV/复核界面读的那批字段不在勾选界面里，漏取不报错只是静默降级（见备查清单 `README.md`「两个适配器要一直对齐」）
- 删除**前后双向对账** —— 动手前查一次实际存在性，报错后再查一次，把「其实已经删掉的」从失败集合里摘出来，不谎报
- 插件自带图标 `images/icon.png`（由 `tests/make_icon.py` 生成，无第三方依赖）

---

## 1. 查证结论（硬事实）

### 1.1 Find Duplicates 本身没有删除功能

拉取 kiwidude 的源码（Find Duplicates v1.10.10）后，全库 grep `remove_books` **零命中**。它只有两条弱路径：

| 路径 | 实际能力 | 默认状态 |
|---|---|---|
| `db.remove_format()` | 只在 Binary Compare 模式下删**同一本书内部的重复格式** | 关闭 |
| `db.set_marked_ids()` | 给书打标记，让用户**自己**按 Delete | 开启 |

所以本插件补的是 Find Duplicates 公认缺失的能力。

### 1.2 Calibre 的删除与回收站机制（已逐行核实）

| 事实 | 证据 |
|---|---|
| 回收站位于**书库内部**：`<library>/.caltrash` | `src/calibre/db/constants.py:12` |
| 删除 = 移入 `.caltrash/b/<book_id>/` | `backend.py:2443 move_book_to_trash` |
| **用硬链接实现，不复制文件** | `utils/copy_files.py` docstring：「Uses hardlinks, falling back to actual file copies only if hardlinking fails」 |
| 回收站保留 **14 天**后自动清除 | `constants.py:11` `DEFAULT_TRASH_EXPIRY_TIME_SECONDS = 14 * 86400` |
| 过期清理是**机会式**的，最多每小时跑一次 | `backend.py:2418` `if time.time() - self.last_expired_trash_at >= 3600` |
| 删除前会写入 `metadata.opf` 备份以便还原 | `cache.py:2725` `self.backend.write_backup(path, raw)` |

**两条关键推论**：

1. **磁盘不会爆** —— 硬链接不占额外空间。但**空间也不会立即释放**：删掉的 1.5TB 仍以硬链接形式占着盘，直到 14 天后过期或手动清空 `.caltrash`。这是「可恢复」的代价，必须知情。
2. **恢复窗口是 14 天**，且过期清理是每小时机会式触发的，不是精确到点。

### 1.3 本机无法运行验证

证据：`/Applications` 无 `calibre.app`；`~/Calibre 书库` 与 `~/library1.db` 均不存在；`calibre-debug` 不在 PATH；深度 6 全盘搜索无命中。仅残留 `~/Library/Preferences/calibre/` 配置（`language=zh_CN`，最后写入 **2026-03-27**）。

同时 [step1_2_plan.py](../06-src/knowledge_assets/preprocess/step1_2_plan.py) 产出 `.bat`，[README.md:184](../06-src/knowledge_assets/README.md#L184) 注明「留给人工在本地跑」—— 真正运行 Calibre 的机器是 **Windows**。

**影响**：交付的是**未经运行验证的代码**。应对见第 9 节。

### 1.4 【重要】真实库实测数据

对 `03-input/metadata-20260923.db` 跑「书名 + 作者」精确分组的结果：

```
全库          : 64,787 本   1.9 TB
分组耗时      : 0.79 秒                    ← 性能不是瓶颈
重复组        :  9,889 组
涉及书籍      : 60,287 本   (93% 的库)
待删候选      : 50,398 本   1.5 TB        (78% 的体积)
保留后        : 14,389 本   436 GB
```

> **以上是 `title` + `authors` 口径。** 插件现默认组合多了自定义列 `#book_init_path`
> （电子书原始路径），同口径复算：
>
> | 字段组合 | 重复组 | 涉及书数 | 待删 |
> |---|---|---|---|
> | `title` + `authors`（旧默认） | 9,889 | 60,287 | 50,398 |
> | **`title` + `authors` + `#book_init_path`（现默认）** | **10,948** | **54,481** | **43,533** |
> | `#book_init_path` 单独 | 10,835 | 54,657 | 43,822 |
> | `title` + `authors` + `uuid` | **0** ⚠️ | 0 | 0 |
>
> 新默认**少删 6,865 本**：代价是 5,806 本「同名同作者但来源路径不同」不再判为重复。
> 最后一行说明为什么 `uuid` 这类每本唯一的字段必须在界面上警告（见 §4.1）。
> 复现方式：`06-src/calibre_plugin/BookDedup4Del/tests/report_baseline.py`。

**重复呈严格倍数特征**，这是本库最重要的特征：

| 组大小 | 组数 | 涉及书籍 |
|---|---|---|
| 恰好 2 本 | 3,146 | 6,292 |
| 恰好 4 本 | 560 | 2,240 |
| 恰好 6 本 | 799 | 4,794 |
| **恰好 8 本** | **2,950** | **23,600** |
| 恰好 16 本 | 216 | 3,456 |
| 恰好 24 本 | 27 | 648 |

8 的倍数反复出现 —— 说明**同一批资源被反复导入了 8 次**，而不是零散的手工重复。抽样验证确认这些是真重复（同名、同作者、同格式集合）。

**结论：你的库确实有 78% 是重复的，这不是误报。** 这也解释了为什么现有流水线在 `title+ext+bytes` 口径下同样报出「重复文件 17847 组 / 待删除 48829 个」。

---

## 2. 需求确认（你的 5 条）逐条回应

| # | 你的要求 | 可行性 | 说明 |
|---|---|---|---|
| 1 | 书库约十万本 | ✅ | 实测 64,787 本。设计按 10 万本留余量 |
| 2 | 允许自定义单个/多个字段组合查重 | ✅ | 见第 4 节。候选来自 `field_metadata.displayable_field_keys()`，**含自定义列** |
| 3 | 默认第一条标记「保留」，其余标记「删除」 | ✅ | 见第 5 节。建议「第一条」由保留策略排序后产生，而非 DB 顺序 |
| 4 | 头部显示总数 / 重复数 / 待删数 / 保留数 | ✅ | 见第 6.1 节 |
| 5 | 人工逐条确认后批量删除到回收站 | ⚠️ **需重新设计** | 见第 3 节。5 万条逐条确认不可行 |

---

## 3. ⚠️ 第 5 条必须重新设计：5 万条无法逐条确认

### 3.1 算术

> 本节与 §3.2 的分层数字都按 **`title` + `authors` 口径**（9,889 组 / 待删 50,398）计算 ——
> 那是立项时实测的基线。当前默认组合多了 `#book_init_path`（10,948 组 / 待删 43,533，见 §1.4），
> 数量级与结论完全一样，**漏斗设计不因此改变**；产品实现里分层的实际数字以插件跑出来的为准。

| 复核粒度 | 条数 | 每条 3 秒 | 结论 |
|---|---|---|---|
| 记录级（逐条书） | 50,398 | **42 小时** | ❌ 不可行 |
| 组级（逐个重复组） | 9,889 | 8.2 小时 | ⚠️ 勉强，一次性做完不现实 |
| 组级 + 分层 | **1,522** | **1.3 小时** | ✅ 可行 |

### 3.2 替代方案：三层复核漏斗

不是降低严谨性，而是把人工注意力**只投到真正需要判断的地方**。

```
L1 自动层（不占人工注意力）
   垃圾标题组 234 组 + 占位作者组 1,296 组
   → 标题是 index / content / 副本 / 未知 作者，保留哪个都无意义
   → 按策略自动标记，只在最终确认页汇总展示

L2 批量层（组级复核，主力）
   干净组 8,402 组 → 待删 45,855 本
   → 默认全部接受策略建议，界面按「组」展示（8,402 行）
   → 人工只做「扫读 + 推翻个别组」，不逐本勾选

L3 人工层（真正逐组细看）
   >8 本/组 的 1,522 组 → 待删 17,619 本
   → 这些组的删除后果最大，逐组展开确认
```

分层依据（实测）：

| 分层 | 组数 | 待删 | 建议动作 |
|---|---|---|---|
| 垃圾标题组 | 234 | 1,480 | 自动 |
| 占位作者组 | 1,296 | 3,397 | 自动 |
| 干净 · 2–3 本/组 | 2,753 | 3,121 | 批量信任 |
| 干净 · 4–8 本/组 | 4,127 | 25,115 | 批量信任 + 抽检 |
| 干净 · **>8 本/组** | **1,522** | **17,619** | **逐组人工** |

> 注：垃圾标题组与占位作者组有 43 组重叠；已去重。

### 3.3 UI 承载

8,402 组 / 54,257 成员**不能**用 `QTreeWidget` 一次性装载（会卡死）。设计约束：

- 默认只渲染**组级行**（8,402 行），成员行**懒加载**
- 成员请求 `>200` 的组标记为「虚拟组」，展开时提示「该组 2,950 项，建议直接按策略处理」
- 用 `QTreeView` + 自定义 `QAbstractItemModel`，或分页（每页 500 组）
- 筛选/搜索在**模型层**做，不做 widget 级过滤

---

## 4. 字段组合查重

### 4.1 可选字段

候选来自**书库自身的字段元数据** `field_metadata.displayable_field_keys()`
（`field_metadata.py:628-636`），**不是**写死的一张表：

| 类别 | 来源 | 说明 |
|---|---|---|
| 标准字段 | `displayable_field_keys()` 里非 `#` 开头的 | 书名 · 作者 · 系列 · 出版社 · 标签 · 语言 · 格式 · 大小 · 标识符 · 评分 · 入库时间 · 发布日期 · 备注…… |
| **自定义列** | 同上，`#` 开头 | 本机书库是 `#book_init_path`（显示名 `BookInitPath`） |
| 虚拟字段 | **排除** | `marked` · `ondevice` · `cover` · `au_map` · `series_sort` · `in_tag_browser` 及 series index |

> `marked` 是本插件自己用来标记重复书的字段 —— 拿它分组会把所有被标记过的书归成一个巨大的「重复组」。

**排序与标注**由纯函数 `fields.arrange_choices()` 负责（常用字段 → 其它标准字段 → 自定义列，
后两组按标签排序），自定义列的标签附带 `#lookup_name`（`BookInitPath（#book_init_path）`）——
用户在 calibre 搜索框里要用的是后者，光有显示名对不上。

两类字段会被标注警告：

| 类型 | 一览 | 后果 |
|---|---|---|
| **每本唯一** | `uuid` · `id` · `path` · `last_modified` · `timestamp` · `sort` · `author_sort` | 每组至多剩一本，**等于查不出重复** |
| **计算列**（composite） | 自定义列里 `datatype == 'composite'` 的 | 不落库，每次取数都要重新求值，十万本会明显变慢 |

组合语义：勾选多个字段 = **全部字段都相等**才算重复组（AND）。

**默认组合 = `title` + `authors` + `#book_init_path`**（实测少删 6,865 本、多识别 1,059 组，见 §1.4）。
`#book_init_path` 是本机书库特有的列名 —— 换一个没有这一列的库，取数会失败并**如实报到界面上**
（`CalibreLibrary.last_read_failures`），**不静默退回**成「只按书名+作者查」。

**取数按勾选**（`book_records(fields=...)`），但另有一批**恒取**字段
`fields.POLICY_FIELDS = (title, authors, publisher, series, tags, languages, identifiers)`：
它们不参与分组键，却是引擎判据（元数据完整度评分、占位作者判定）与 CSV/复核界面要读的。
写成单一真源、由两个适配器共用 —— 漏掉的失效方式是「组数、待删数一个都不变，只是建议保留哪本悄悄换人」，
统计基线照样全绿。`tests/test_plugin_metadata.py::TestRecordFieldCoverage` 用 AST 扫源码盯住它。

### 4.2 多值字段必须按书聚合

实测：`authors` 平均 **1.47 个/本**，`tags` 更多。**直接 SQL JOIN 会扇出**——一本两作者的书出现两行，会「和自己重复」。

正确做法：先按 `book_id` 聚合为集合，再参与分组：

```python
authors_of[book_id] = ['张三', '李四']          # 不是两次 JOIN 结果
key_part = '\x1f'.join(sorted(normalize(a) for a in authors_of[book_id]))
```

排序保证 `张三&李四` 与 `李四&张三` 命中同一组。

### 4.3 空值保护

实测有 **1,296 组的作者是「未知」或空**。若不加保护，「所有无作者的书」会被归为一组。

规则：**勾选的字段全部为空时不参与分组**（`if not any(key_parts): skip`）。实测跳过 2 本，影响可忽略，但规则必须写死。

### 4.4 规范化规则

精确匹配的召回率完全取决于规范化。实测样本中已确认需要处理的差异：全角/半角（`；` vs `;`、`！` vs `!`）、破折号（`—` vs `-`）、包裹符号（`《》【】`）。

```
normalize(s):
  1. unicodedata.normalize('NFKC', s)   # 全角→半角，一次覆盖字母/数字/标点
  2. lower()
  3. 剥离首尾包裹符号 《》【】[]（）()""''
  4. 统一破折号 — – － → -
  5. 删除所有空白字符
```

**明确不做**（风险 > 收益，列为配置项默认关闭）：剥离副标题（会把《XX：上册》《XX：下册》误判为重复）、剥离作者后缀（著/译/编）、繁简转换（需引入 opencc）。

---

## 5. 标记模型

### 5.1 三态

| 状态 | 标记值（内部） | UI 显示 | 来源 |
|---|---|---|---|
| 保留 | `dup_keep` | 🔒 保留 | 保留策略自动分配，人工可改 |
| 删除 | `dup_del` | 🗑 待删除 | 同组其余成员，人工可改 |
| 未决 | — | ⚪ 未处理 | 人工推翻后未重选的中间态 |

内部标记值用 **ASCII**（`dup_keep` / `dup_del`），UI 显示中文。原因：calibre 搜索语法是 `marked:<value>`，中文值虽可用但易受输入法与空格影响，ASCII 更稳。

标记后可在 Calibre 搜索框用 `marked:dup_del` 复核 —— 这是删除前的最后一道自检。

> ⚠️ **本节已被实现推翻**：插件**不写 calibre 标记**，保留/删除是内存态（`selection.py`），
> 只在删除时落地。理由见 §0.5 第 1 条。上表保留为设计推演记录，不再描述实际行为。

### 5.2 保留策略

用户第 3 条要求「默认第一条保留，其余删除」。**「第一条」不应是 DB 顺序**（无意义），而应是策略排序后的第一条：

| 优先级 | 判据 | 方向 | 理由 |
|---|---|---|---|
| 1 | 元数据完整度（封面 + 出版社 + 系列 + 标签 + 语言 + 标识符 的数量） | 高者保留 | 信息多的更值得留 |
| 2 | 格式数（`len(formats)`） | 多者保留 | EPUB+MOBI+PDF 比单 MOBI 好 |
| 3 | 格式优先级 EPUB > AZW3 > MOBI > PDF > 其他 | 高者保留 | 通用性 |
| 4 | `timestamp` 入库时间 | **早者保留** | 先入库的是正本 |
| 5 | `book_id` | 小者保留 | 兜底，保证确定性 |

策略可选、可关闭（关闭后全部标「未决」，完全人工）。

> 判据 1 读的这批字段**不在查重字段的勾选界面里** —— 所以它们必须在取数层无条件带上
> （`fields.POLICY_FIELDS = title + authors + publisher + series + tags + languages + identifiers`）。
> 漏掉的失效方式见 §4.1 末段：组数、待删数一个都不变，只是「建议保留哪本」悄悄换人。
>
> 与现有流水线的差异：流水线 [step1_1_scan.py:536](../06-src/knowledge_assets/preprocess/step1_1_scan.py#L536) 的 `_duplicate_keep_key` 首选判据 `file_exists` 恒为 False（[README.md:336](../06-src/knowledge_assets/README.md#L336) 自述），实际退化成「路径最短者留」。插件跑在 Calibre 里该判据可用，但本期不用它。**两边保留结论会不同**，本期独立、不对账。

---

## 6. 界面

### 6.1 头部统计（你的第 4 条）

```
┌─ 书库：Calibre 书库 · 64,787 本 · 1.9 TB ────────────────────────┐
│ 重复组 9,889 │ 待删除 50,398 │ 保留 14,389 │ 未决 0             │
│ 分层：自动 1,487 组 · 批量 6,880 组 · 人工 1,522 组              │
└──────────────────────────────────────────────────────────────────┘
```

> 草图数字为 `title` + `authors` 口径。现默认组合（多一个 `#book_init_path`）下的实测基线是
> **重复组 10,948 ／ 待删 43,533**，验收时以这个为准（见 §9.2 第 5 步）。

### 6.2 主体

```
┌─ 复核（显示 人工层 1,522 组 / 全部 9,889 组）────────────────────┐
│ 字段: [书名☑][作者☑][系列☐][出版社☐][格式☐][+自定义]  [重新查重] │
│ 筛选: (o)人工层 ( )全部 ( )垃圾组 ( )已改动   [搜索____]         │
├──────────────────────────────────────────────────────────────────┤
│ 🔒 组1  额尔古纳河右岸 · 迟子建            32 本 → 删 31          │
│    🔒 #690   额尔古纳河右岸    EPUB,MOBI,PDF  2019-03-02  保留★  │
│    🗑 #3736  额尔古纳河右岸    MOBI           2020-07-11         │
│    … 还有 30 本（懒加载）                                        │
│ 🗑 组2  index · 未知                       81 本 → 自动全删      │
├──────────────────────────────────────────────────────────────────┤
│ 已选 45,855 本待删除 · 约 1.4 TB（移入 .caltrash，14 天内可恢复）│
│      [导出复核 CSV]  [应用标记]  [批量删除…]  [关闭]              │
└──────────────────────────────────────────────────────────────────┘
```

### 6.3 安全约束（代码强制，不靠自觉）

1. **每组至少留一本** —— 某组被全删时该行标红，「批量删除」拒绝执行
2. **默认零勾选** —— 打开时无任何待删项，必须显式「应用标记」
3. **删除按钮需二次确认** —— 弹窗写明「删除 N 本 / 保留 M 本 / 移入回收站，14 天内可恢复」
4. **删除前强制导出 CSV** —— 快照不可跳过，落盘留档
5. **首轮限制批量** —— 单次删除上限可配（默认 500 本），超出提示分批

---

## 7. 批量删除

### 7.1 执行路径（已实现，与 v2 方案不同）

**不用 `do_library_delete`**（理由见 §0.5 第 2 条）。实际路径：

```python
# delete_flow.py: _delete_chunk()
delete_books(db, chunk, permanent=permanent)   # → new_api.remove_books(ids, permanent=...)

# 整块失败就退化成逐本删 —— 免得一本坏书拖垮一整块
for book_id in chunk:
    try:    delete_books(db, (book_id,), permanent=permanent)
    except: failed.append(book_id)
```

自己实现的完整流程（`DeleteFlow.run()`）：

```
导出清单 CSV ──失败/取消──▶ 中止（没有清单就不删）
      │成功
      ▼
  二次确认 ──< 200 本：确认框（默认按钮 & Esc 都是「取消」）
      │      ──≥ 200 本：要求手打「删除」二字
      ▼
  对账：动手前查一次实际存在性，已消失的单独归类
      ▼
  分批删除（CHUNK_SIZE=200，QProgressDialog，可取消）
      ▼
  再对账：报错的逐本复查，把「其实已删掉」的从失败集合摘出来
      ▼
  结果汇报：实删 / 动手前已不存在 / 仍失败  三类分开列，并给「打开回收站…」
```

**关键设计点**：

- 确认框的 **`setDefaultButton` 与 `setEscapeButton` 都指向「取消」** —— 默认按钮决定误按回车的代价
- 删除**前后各对账一次**（`existing_ids()` → `db.all_field_for("title", ids)`）。对账本身出错时返回「全部存在」，即**宁可多报一次失败，也不谎报成功**
- `delete_permanent` 默认 `False`；开启时确认框文案改为「永久删除 / 这一步没有回头路」

### 7.2 磁盘与恢复（务必知情）

- 删除为**硬链接移动**，不额外占用磁盘
- 但 **1.5 TB 不会立即释放**，仍以硬链接形式占盘
- 想真正腾出空间：calibre 的回收站界面手动清空，或等 14 天自动过期
- 想恢复：**书库的「移除书籍」菜单 → 「恢复最近删除」**，或插件删除结果框里的
  「打开回收站…」按钮（它直接开 `calibre.gui2.trash.TrashView`）
- ⚠️ **没有 `marked:dup_del` 这条恢复路径** —— 本实现不使用 calibre 标记，见 §0.5 第 1 条

### 7.3 删除后的一致性

删除后插件持有的 `book_id` 全部失效。已实现（`Selection.after_deleting()`）：

- 丢弃已删的 `book_id`
- **幸存者少于 2 本的组直接消解**（不再是「重复组」）
- 刷新头部统计与分页游标
- 剩余组的保留/删除状态**不动**

---

## 8. 架构与文件结构（已实现）

采用 **Repository 模式**解耦：本机没有 Calibre，**只有零 calibre 依赖的模块才能被真正测试**。

```
action.py / dialog.py / config.py     ← UI 与事件，无判定逻辑
        ↓
delete_flow.py                        ← 删除编排（导出/确认/分批/对账）
        ↓
selection.py  engine.py  settings.py  ← 状态机 / 分组分层 / 设置契约  【零依赖】
        ↓
library.py                            ← LibraryAdapter 抽象接口 + BookRecord
        ↓
calibre_library.py                    ← 唯一 import calibre 的实现
```

**这条边界由测试用 AST 强制**（`tests/test_plugin_metadata.py`）：`PURE_MODULES` 里的模块不允许出现模块级 `calibre` / `qt` / `PyQt` import；纯模块也不得 import Qt 模块。函数体内的惰性 import 不算 —— 那是故意用来把「真实读写」推迟到 calibre 运行时的（如 `settings.settings_store()`）。

实际文件（`06-src/calibre_plugin/BookDedup4Del/`）：

```
├── plugin-import-name-BookDedup4Del.txt  # ⚠️ 必须存在，见 10.1（空文件）
├── images/icon.png                       # 由 tests/make_icon.py 生成，无第三方依赖
├── dist/BookDedup4Del.zip                # 由 tests/pack.py 产出
├── conftest.py                           # 本机测试引导（calibre 最小 stub）
├── README.md                             # 操作手册
├── tests/                                # 见下
└── BookDedup4Del/                        # ← 源码包，与插件同名
    ├── __init__.py         48 行   InterfaceActionBase 包装 + 元数据 + 配置入口
    ├── action.py           94 行   入口、菜单、窗口单例、自带图标
    ├── dialog.py          662 行   复核主窗口（最大）
    ├── delete_flow.py     301 行   导出 → 确认 → 分批删 → 对账   ← 唯一动用户文件处
    ├── calibre_library.py 294 行   LibraryAdapter 的 calibre 实现（按需取数 + 失败上报）
    ├── model.py           276 行   复核列表的 Qt 模型
    ├── config.py          266 行   首选项界面
    ├── engine.py          245 行   分组 / 分层 / 保留策略        【纯】
    ├── selection.py       220 行   保留/删除状态机              【纯】
    ├── fields.py          212 行   字段定义 · 恒取字段 · 选项装配  【纯】
    ├── settings.py        202 行   设置契约（含默认组合迁移）      【纯】
    ├── report.py          132 行   清单 CSV 导出（含「分组键」列） 【纯】
    ├── action.py           94 行   入口、菜单、窗口单例、自带图标
    ├── normalize.py        69 行   字段值规范化                 【纯】
    ├── library.py          63 行   LibraryAdapter 接口 + BookRecord 【纯】
    ├── __init__.py         48 行   InterfaceActionBase 包装 + 元数据 + 配置入口
    └── display.py          31 行   组标题截断                   【纯】
```

> ⚠️ **源码包名 `BookDedup4Del/` 与标记文件名、`actual_plugin` 三者必须一致**，
> 且整个工程按功能收在 `BookDedup4Del/` 一个目录下（源码包、tests、images、
> marker、dist、README、conftest、.venv 全在里面），便于整体搬迁。
> 目录层级已经调整过一次 —— 见 §9.4 的教训。
>
> 说明：`dialog.py` 662 行超出「单文件 < 400 行」的目标。它内部已按 `_build_*` / `_visible_*` / `_apply_*` 分组，继续拆会把「用户看到什么 = 删除范围是什么」这个核心不变式拆散到多个文件里，反而更难保证。**如果后续它继续长，优先拆出「工具栏与分页」「结果汇报与回收站」两块。**

```text
06-src/calibre_plugin/BookDedup4Del/
├── conftest.py               # 路径与 stub 装配
├── tests/
│   ├── test_normalize.py         # 本机可跑
│   ├── test_engine.py            # 本机可跑：内存构造 BookRecord（rec() 工厂），不碰 DB
│   ├── test_engine_realdb.py     # 本机可跑：直接读真实库，两组字段基线都断言组数/待删数
│   ├── test_selection.py         # 本机可跑：状态机 + LastKeepError
│   ├── test_settings.py          # 本机可跑：契约、边界值、越界回退
│   ├── test_fields.py            # 本机可跑：字段常量 + arrange_choices 装配 + POLICY_FIELDS
│   ├── test_report.py            # 本机可跑：CSV 导出（含「分组键」列）
│   ├── test_display.py           # 本机可跑：组标题截断
│   ├── test_plugin_metadata.py   # 本机可跑：AST 检查打包约定、纯/Qt 分层、字段读取覆盖
│   ├── test_packaging.py         # 本机可跑：用 calibre 自己的算法验证 zip 布局
│   ├── test_sqlite_library.py    # 本机可跑：库路径解析（§9.4）+ 两适配器取数范围一致 + 自定义列对账
│   ├── make_icon.py              # 图标生成器（zlib + struct，无 Pillow）
│   ├── pack.py                   # 校验 + 打包，产出 dist/BookDedup4Del.zip
│   ├── calibre_loader.py         # 逐行抄自 zipplugin._locate_code / find_spec
│   ├── report_baseline.py        # 基线数字复算
│   └── sqlite_library.py         # 直读 metadata.db 的 LibraryAdapter 实现（测试用）
```

---

## 9. 验证策略

### 9.1 本机能真正跑的部分（实测结果）

```bash
cd 06-src/calibre_plugin/BookDedup4Del
.venv/bin/python -m pytest -q      # → 380 passed
.venv/bin/python tests/pack.py     # → dist/BookDedup4Del.zip（50.7 KB / 17 个条目）
```

| 检查 | 命令 | 覆盖 | 状态 |
|---|---|---|---|
| 全部单测 | `.venv/bin/python -m pytest -q` | 见下表 | ✅ **380 passed / 0 skipped** |
| 语法 | `tests/pack.py` 内的 `py_compile` | **15 个模块全部**，含本机跑不到的 Qt 模块 | ✅ |
| 真实库回归 | `tests/test_engine_realdb.py` | 直接对 64,787 本实跑，**两组字段基线**都断言组数/待删数 | ✅ |
| 打包校验 | `tests/pack.py` | 7 条约定（见 10.3） | ✅ |
| zip 布局 | `tests/test_packaging.py` | 用 calibre 自己的 `_locate_code` 算法正反两向验 | ✅ |

本机能跑起来的模块与它们各自的测试：

| 模块 | 测试文件 | 覆盖要点 |
|---|---|---|
| `normalize.py` | `test_normalize.py` | 全角半角、书名号、多作者乱序、GROUP_SEP 不变量 |
| `engine.py` | `test_engine.py` + `test_engine_realdb.py` | 分组、分层、保留策略、空值、多值聚合；**真实库全量回归** |
| `selection.py` | `test_selection.py` | 状态机、`LastKeepError`、分页批次、删后组消解 |
| `settings.py` | `test_settings.py` | 默认值最保守、边界钳制、空列表回落、`None` 处理 |
| `fields.py` | `test_fields.py` | 常量一致性、排除列表、`arrange_choices()` 装配（**含自定义列的输入必须产出非空列表**）、`POLICY_FIELDS` 单一真源、`record_fields()` |
| `report.py` | `test_report.py` | CSV 列、转义、编码、**「分组键」列**（任意字段组合都能带出判重依据） |
| `display.py` | `test_display.py` | 组标题截断、`PART_LIMIT`、省略号 |
| 打包约定 + 分层 | `test_plugin_metadata.py` | AST 检查：模块级零 calibre/Qt、`minimum_calibre_version ≥ 6.0`、`actual_plugin` 路径、标记文件一致性、**`record.raw/first` 读的字段必须在 `POLICY_FIELDS` 里**、layout 必须带 parent 构造 |
| zip 布局 | `test_packaging.py` | 平铺、`__init__.py` 在根、标记文件名字合法且与包名一致 |
| 库路径解析 | `test_sqlite_library.py` | 向上查找（不数层数）、多库取最新、env 覆盖、**两个适配器取数范围逐项一致**、自定义列逐本对账 |

**纯模块共 8 个、约 1,100 行核心逻辑在本机有真实测试覆盖**，含一次真实库全量回归（两组基线）。这是分层设计的直接收益。

> 已在此环节抓到的真实 bug（说明这套测试不是摆设）：
> - `as_str_list(None)` 会产出字符串 `"None"` —— 若 JSON 里标记列表含 `null`，会凭空多出一个能匹配「None」标题的关键词。已修。
> - `pack.py` 计算 zip 内路径时漏了包名前缀，导致校验永远查不到 `action.py`（静默失效）。已修。
> - `pack.py` 把校验写反了：把「模块平铺在 zip 根」当成命名空间污染来拦，而它恰恰是 calibre 的硬要求。**装机即报 InvalidPlugin**（详见 §10.2）。已修。
> - `default_db_path()` 靠「向上数 4 层」定位 `03-input/`，插件工程下沉一层后就找不到库 —— 22 个真实库回归**全部静默 skip**，测试照样全绿。已改为向上查找（详见 §9.4）。
> - `_build_toolbar()` / `_build_pager()` 返回 `QHBoxLayout`，却被 `_build()` 里的 `addWidget()` 收下 —— 装机后开窗口即抛 `QBoxLayout.addWidget(): argument 1 has unexpected type 'QHBoxLayout'`。它们是全工程**仅有的两个不带 parent 构造的 layout**（另 5 处都写了 parent）。已修，并加 `TestQtLayoutHygiene` 静态守卫：一条查「layout 必须带 parent 构造」，一条查「`_build_*` 不得返回 layout 变量」。**守卫已用「把代码改回错误写法」验证过会精确变红**。
> - `config.py` 里筛自定义列的 `is_custom_field(key) and not is_ignorable_field(key)` —— **恒假条件**。calibre 的 `is_ignorable_field` 定义就是「是自定义列」（`field_metadata.py`：`return self.is_custom_field(key) or key.startswith('@')`），两个条件永不同真，`_custom_field_names()` 无条件返回 `[]`。不报错、不打日志，只是 `BookInitPath` 从未出现在首选项里。已把装配逻辑挪进纯模块 `fields.arrange_choices()`，并加了一条「含自定义列的输入必须产出非空列表」的测试直接锁死根因
> - 取数改成「按用户勾选取」之后，`engine.metadata_score()` 要读的 publisher/series/tags/languages/identifiers、`_is_placeholder_author()` 要读的 authors **不在勾选界面里** —— 用户不勾就缺席，评分静默退化成「只看有没有封面」，占位作者判定失去输入。**组数、待删数一个都不变**，所有统计基线照样全绿，只有「建议保留哪本」和分层悄悄换人。已引入 `fields.POLICY_FIELDS` 作为单一真源（两个适配器共用 `fields.record_fields()`），并加 `TestRecordFieldCoverage` 用 AST 扫全部消费方的字段读取
>
> 这几条的共同点：**失效时不报错** —— 要么本机测不到、只在装机时报错，要么连装机都不报错，只是功能静默缺席。最后两条尤其说明问题：dialog.py 六百多行 Qt 代码在本机**一次都没真正执行过**，而「配置界面列哪些字段」这类逻辑原本也藏在 Qt 模块里无人过问，所以凡能在源码层面拦住的，都要用静态检查拦住；凡是能挪进纯模块的，都要挪进去。

### 9.2 Windows 验收清单

> ⚠️ 前 6 步全部通过之前，**不要碰真实库**。

1. `首选项 → 插件 → 从文件加载插件` 选 `dist/BookDedup4Del.zip` → **重启 Calibre**
2. 插件列表出现 `BookDedup4Del` 且**无报错**（包名解析失败会在此暴露）
3. 工具栏出现「查重删除」按钮，**图标是两本叠放的书**（不是内置的复制图标 → 说明
   `_apply_own_icon` 生效了；是内置图标也不影响功能，只是没贴上新图标）
4. 点按钮，窗口打开；点「开始扫描」，**64k 本应在数秒内扫完**
5. 头部统计与 §1.4 的基线吻合：**重复组 10,948 ／ 待删 43,533**
   （默认组合 `title + authors + #book_init_path`；旧的 `title + authors` 是 9,889 ／ 50,398。
   保留策略不同会有微差，差一个数量级才是问题。
   若你的库没有 `#book_init_path` 这一列，扫描后会明确列出「这几个字段没读到」，
   组数随之退化成 9,889 —— 那是预期行为，不是 bug）
6. 切换字段组合（如加 `series`），组数应变化
7. **首选项能打开**：字段列表里应能看到全部标准字段 **以及自定义列 `BookInitPath（#book_init_path）`**；
   勾掉所有字段点确定会被拦下（对话框不关）
8. **在测试库或备份 `metadata.db` 上操作**：切到「人工层」，确认列表里第一行标「保留」、
   其余标「删除」；点某一行能切换状态
9. **验证「至少留一本」**：把某组全部改成删除，界面应阻止 / 提示
10. **首轮只删 ≤10 本**：
    - 弹导出对话框 → 存一份 CSV，打开看内容对不对；**末列「分组键」应能看出这几本书
      凭什么被判为重复**（默认组合下应同时看到书名、作者、原始路径三段）
    - 确认框的**默认按钮是「取消」**，直接回车应取消
    - 确认删除后，回收站（`<书库>/.caltrash`）出现条目，文件仍在
11. 确认**磁盘空间未立即释放**（符合 §7.2 预期）
12. 点「打开回收站…」，**还原一本**，验证可恢复
13. 试一次「导出失败」路径：把 CSV 存到只读位置 → **删除应被中止**
14. 重开插件重新扫描，已删的书不再出现

### 9.3 诚实声明

本机无 Calibre，9.1 之外的一切（GUI 渲染、calibre API 调用、插件加载、真实删除）**只能靠源码级核对保证**。所有 API 均在附录 A 标注源码出处。若第 1、2 或 4 步失败，把 `calibre-debug -g` 的控制台报错贴回来即可定位。

### 9.4 教训：两类不报错的失效

**第一类：把目录结构写进代码。** 两处失效都源自「凭目录位置找东西」，且**都不报错**：

| 位置 | 原写法 | 失效方式 | 现写法 |
|---|---|---|---|
| `pack.py` 的 zip 内路径 | 按源码目录名拼包名前缀 | 平铺要求被当成污染拦下 → 装机报 `InvalidPlugin` | `package_files()` 显式做「源码目录 → 平铺」映射，是**唯一**转换点 |
| `sqlite_library.default_db_path()` | 向上数 4 层找 `03-input/` | 多一层就找不到库 → 22 个回归用例静默 skip | 向上逐级搜 `03-input/metadata-*.db`，不数层数 |

一条通用规则：**「找不到」必须能被察觉**。凡是「找不到就跳过/降级」的分支，都要配一个会主动失败的测试，否则它只会安静地把保障变成摆设（见 §12 的「静默失效」风险项）。

#### 9.4.1 第二类：恒假条件

同一个失效家族的另一个形态是**写了一个永远不会成立的条件** —— 代码执行了、没报错、
也没走「找不到就降级」的分支，只是那个 `if` 永远是 False：

| 位置 | 原写法 | 为什么恒假 | 现写法 |
|---|---|---|---|
| `config.py` 筛自定义列 | `is_custom_field(key) and not is_ignorable_field(key)` | calibre 的 `is_ignorable_field` 定义就是 `is_custom_field(key) or key.startswith('@')` —— 两个条件**互为否定** | 删掉这段，改用 `field_metadata.displayable_field_keys()`，装配逻辑挪进纯模块 `fields.arrange_choices()` |

这类条件**没有测试能自然发现**：它不抛异常、不返回错误值，只是让某段代码从不执行。
唯一可靠的拦法是「把它的**结果**写成断言」—— 比如「给定含自定义列的输入，产出的列表必须非空」，
而不是去断言那个条件本身。判据是：**凡是从 calibre/外部 API 拿来做过滤的谓词，都要先确认它到底判断的是什么**
（读源码或 `print` 一次），别按名字猜语义 —— `is_ignorable_field` 听起来像「无意义的字段」，
实际是「用户自己加的列」。

---

## 10. 打包

### 10.1 `plugin-import-name-*.txt` 是必须的（会直接导致插件报废）

读 `src/calibre/customize/zipplugin.py:348-362`：

```python
plugin_name = None
for name in names:
    name, ext = posixpath.splitext(name)
    if name.startswith('plugin-import-name-') and ext == '.txt':
        plugin_name = name.rpartition('-')[-1]

if plugin_name is None:
    c = 0
    while True:
        c += 1
        plugin_name = f'dummy{c}'      # ← 缺失则退化成 dummy1
        if plugin_name not in self.loaded_plugins:
            break
```

zip 里若没有**空文件** `plugin-import-name-BookDedup4Del.txt`，包名退化为 `dummy1`，`__init__.py` 里写死的 `actual_plugin = 'calibre_plugins.BookDedup4Del.action:BookDedupAction'` 解析不到 —— 插件装上了、按钮也在，**点了没反应**。

### 10.2 zip 结构：模块必须**平铺**在根目录

```text
BookDedup4Del.zip
├── plugin-import-name-BookDedup4Del.txt   # 空文件，必须在根
├── __init__.py                            # 必须在根
├── action.py · config.py · library.py · calibre_library.py
├── normalize.py · engine.py · model.py · dialog.py · …
└── images/icon.png                        # 唯一带目录的条目（load_resources 约定）
```

**为什么不能套一层 `BookDedup4Del/`**：`zipplugin.py: _locate_code()` 里

```python
candidates = [posixpath.dirname(x) for x in pynames if x.endswith('/__init__.py')]
candidates.sort(key=lambda x: x.count('/'))
valid_packages = set()
for candidate in candidates:
    parts = candidate.split('/')
    parent = '.'.join(parts[:-1])
    if parent and parent not in valid_packages:
        continue                       # ← 子目录的父包不在集合里，整段丢掉
    valid_packages.add('.'.join(parts))

for candidate in pynames:
    package = '.'.join(parts[:-1])
    if package and package not in valid_packages:
        continue                       # ← 子目录里的 .py 在这里被跳过
```

只有 `dirname` 为空的（顶层）包能进 `valid_packages`，所以子目录里的模块**全部被跳过**，
`names` 里凑不出 `__init__` 键，直接抛：

```
InvalidPlugin: ... does not contain a top-level __init__.py file
```

> ⚠️ **这条真踩过**：v2 之后的第一版 `pack.py` 把模块包成了 `book_dedup/__init__.py`，
> 装机即报上述错误。修复点集中在 `pack.py: package_files()` —— 它返回
> `{zip 名: 磁盘路径}`，是**唯一**做「源码目录 → 平铺」转换的地方。
> 并用 `tests/calibre_loader.py`（逐行抄自 `_locate_code` / `find_spec`）
> 在 `tests/test_packaging.py` 里回归，本机即可发现，不必等装机。
>
> 注意当时还叠了一个错：**打包器的校验规则写反了** —— 它把「模块平铺在根」
> 当成命名空间污染拦下来，而平铺恰恰是 calibre 的硬要求。所以即使
> §10.3 事前就写对了，代码还是照错的方向执行。文档写得对不等于代码做得对，
> 这也是后来补 `test_packaging.py` 的原因：**用 calibre 自己的算法验，而不是用我的理解验**。

### 10.3 打包器的校验清单

`tests/pack.py` 用标准库 `zipfile` 生成，并在打包前**强制校验**（任一不过即中止）：

1. 根目录有 `__init__.py`
2. 没有任何 `.py` 落在子目录里
3. 标记文件存在且唯一，名字合法（`[a-zA-Z][_0-9a-zA-Z]*`），且与 `actual_plugin` 的包名一致
4. `actual_plugin` 指向的模块在 zip 里真的存在
5. 关键模块齐全（`action.py` / `dialog.py` / `config.py` / `selection.py` / `delete_flow.py`）
6. `minimum_calibre_version ≥ (6, 0, 0)`
7. 每个 `.py` 过一遍 `py_compile`

---

## 11. 交付物

| 文件 | 说明 |
|---|---|
| `06-src/calibre_plugin/BookDedup4Del/` | **插件工程根**（源码包、tests、images、marker、dist、README、conftest、.venv 全在此目录下） |
| `06-src/calibre_plugin/BookDedup4Del/BookDedup4Del/` | 插件源码（**15 个模块**，3,115 行；其中纯模块 8 个、1,174 行） |
| `06-src/calibre_plugin/BookDedup4Del/tests/` | 16 个测试/工具文件（**380 个用例**，含真实库全量回归 —— 两组字段基线） |
| `06-src/calibre_plugin/BookDedup4Del/images/icon.png` | 插件图标（由 `tests/make_icon.py` 生成） |
| `06-src/calibre_plugin/BookDedup4Del/dist/BookDedup4Del.zip` | 可在 Windows 加载的插件包（50.7 KB / 17 个条目） |
| `06-src/calibre_plugin/BookDedup4Del/README.md` | 操作手册 + 开发说明 + 已验证事实速查 |

---

## 12. 风险与明确不做的事

| 风险 | 等级 | 缓解 |
|---|---|---|
| 本机无法运行验证，首次加载报错 | **高** | 严格照源码核对；附录 A 标出处；`test_packaging.py` 用 calibre 自己的算法跑通布局；交付验收清单（9.2） |
| `plugin-import-name` 缺失致插件报废 | 中 | 打包脚本强制校验（10.3） |
| **模块套在子目录里致 InvalidPlugin** | 中 | 已踩过一次并修复；`package_files()` 是唯一转换点；`test_packaging.py` 正向+反向回归（10.2） |
| **误删 5 万本** | **高** | 三层漏斗 + 组级复核 + **默认一组只删到剩 1 本** + 首轮限量 500 + **强制导出 CSV（导出失败即中止删除）** + 手打「删除」阈值 + 确认框默认「取消」 + 删前删后双向对账 + 14 天回收站 |
| 磁盘空间不释放被误解为「删除失败」 | 中 | §7.2 已说明；确认框与结果框都写明「空间不会立刻变多」 |
| 10,948 组渲染卡死 | 中 | 分页表格（每页 200 组），模型层按层级 + 搜索过滤 |
| 中文规范化不足漏检 | 中 | 规范化单测 + 真实库回归断言组数 |
| 删除中途报错被谎报成失败 | 中 | `_reconcile()` 删后再查一次实际存在性；对账本身出错时宁可多报失败也不谎报成功 |
| **静默失效**：保障逻辑因目录/环境变化而「跳过」而非报错 | **高** | 已踩两次（§9.4）；规则：凡「找不到就降级」的分支都要配一个会主动失败的测试；`pytest -q -rs` 必须看到 `0 skipped` |
| **恒假条件**：过滤谓词按名字猜语义，实际永不同真 | **高** | 已踩一次（§9.4.1：自定义列列表恒为空）；规则：凡拿外部 API 的谓词做过滤，先读源码确认它判断的是什么；并且**断言结果而非条件**（「含自定义列的输入必须产出非空列表」） |
| **取数漏字段致判据静默降级** | **高** | 字段改成按勾选取后，引擎判据与 CSV/UI 读的字段不在勾选界面里；已引入 `fields.POLICY_FIELDS` 单一真源（两适配器共用），并由 `TestRecordFieldCoverage` AST 扫源码盯住。这类失效**组数、待删数一个都不变**，统计基线全绿 —— 只能靠静态检查发现 |

**明确不做（本期）**：二进制哈希比对 · 书库间比对 · 豁免名单 · 与流水线 CSV 互认 · 模糊匹配/soundex（对中文无效） · 用 calibre 标记（`marked:`）承载保留/删除状态（见 §0.5 第 1 条）。

**已从「不做」移出**：~~永久删除~~ —— 已实现为 `delete_permanent` 开关，默认关闭（见 §0.5 第 3 条）。

---

## 附录 A：已验证 API 速查

| API | 签名 / 用法 | 出处 |
|---|---|---|
| 插件基类 | `calibre.customize.InterfaceActionBase` | `src/calibre/customize/__init__.py` |
| GUI 动作基类 | `calibre.gui2.actions.InterfaceAction` | `src/calibre/gui2/actions/__init__.py` |
| Qt 导入 | `from qt.core import QDialog, QTreeView, ...` | calibre 5.0+ |
| 确认框 | `calibre.gui2.dialogs.confirm_delete.confirm(msg, name, parent)` | Find Duplicates `action.py:23` |
| 保存文件 | `choose_save_file(window, name, title, filters=[], all_files=True, initial_path=None, initial_filename=None)` | `src/calibre/gui2/__init__.py:1212` |
| 错误框 | `calibre.gui2.error_dialog(parent, title, msg, det_msg=…)` | `src/calibre/gui2/__init__.py` |
| 字段枚举 | `db.field_metadata` | `src/calibre/db/legacy.py:346` |
| **可展示字段从哪来** | `field_metadata.displayable_field_keys()`：滤掉 `kind != 'field'`、`datatype is None`、`marked`/`ondevice`/`cover`/`au_map`/`series_sort`/`in_tag_browser` 与 series index，**保留自定义列** —— 这是插件字段候选的正确来源 | `src/calibre/db/field_metadata.py:628-636` |
| **`is_ignorable_field` 的真实语义** | `return self.is_custom_field(key) or key.startswith('@')` —— **「是自定义列」本身就等于「可忽略」**。所以 `is_custom_field(k) and not is_ignorable_field(k)` 永不同真（见 §9.4.1） | `src/calibre/db/field_metadata.py` |
| 自定义列 | `db.custom_column_label_map`；另：显示名在 `metadata[key]['name']`（用户起的 `BookInitPath`），而 `key_to_label('#book_init_path')` 返回的是 `book_init_path`（无 `#`、无大写），给用户看对不上 | `src/calibre/db/legacy.py:380` |
| 取字段值 | `new_api.field_for(name, book_id, default_value=None)` `@read_api` | `src/calibre/db/cache.py:934` |
| 批量取字段值 | `new_api.all_field_for(name, ids)` → `{id: value}`；**接受 `#xxx` 自定义列 key**，用于对账实际存在性与批量取数 | `src/calibre/db/cache.py:988` |
| **批量取字段对缺失字段抛异常** | `all_field_for` 第 991 行 `self.fields[field]` **没有 try/except，直接 `KeyError`** —— 不像单本版 `field_for` 会兜底返回 `default_value`。**所以取数必须逐字段 try/except 并把失败上报**（`CalibreLibrary.last_read_failures`），否则勾了不存在的列等于白勾 | `src/calibre/db/cache.py:988-991` vs `:959` |
| `all_field_for` 返回类型 | 单值 text → `str`/`None`；多值 text → `tuple`；`identifiers` → `dict`；datetime 类 → **带时区的 `datetime`**（不是 epoch）；composite → 渲染后的 `str` | `src/calibre/db/cache.py` |
| **自定义列存在哪** | 值表 `custom_column_<id>`（`normalized=1` 时）或映射表的 `value` 列（否则），映射表统一是 `books_custom_column_<id>_link`；`custom_columns` 表有 `label`/`name`/`datatype`/`is_multiple`/`normalized` | 本机真实库实测（附录 B） |
| 哪些字段每本唯一 | calibre **只保证 `uuid` 与 `id` 唯一**；`path`/`timestamp`/`last_modified`/`sort`/`author_sort` 实际每本不同，但**不是契约保证**。这 7 个都按「每本唯一」警告 | `src/calibre/db/field_metadata.py` |
| 全部 book_id | `new_api.all_book_ids()` | `src/calibre/db/cache.py:1086` |
| 打标记 | `db.set_marked_ids({id: 'dup_del'})` | `src/calibre/db/view.py:447` |
| **删除（本插件实际使用）** | `new_api.remove_books(ids, permanent=False)` | `src/calibre/db/cache.py:2711` |
| ~~`do_library_delete`~~ | ⚠️ 是 Remove Books **动作**的方法，不是公开 API，且带设备检测。**本插件不调用** | `src/calibre/gui2/actions/delete.py:481` |
| 回收站路径 | `<library>/.caltrash`，`b/` 整本、`f/` 单格式 | `zipplugin.py: TRASH_DIR_NAME` |
| 回收站 GUI | `calibre.gui2.trash.TrashView(db, parent=None)` —— 内部做 `db.new_api`，**要传 `current_db`**；有 `books_restored` 信号 | `src/calibre/gui2/trash.py` |
| 回收站 API | `clear_trash_bin()` / `list_trash_entries()` / `restore_book()` / `copy_book_from_trash()` / `move_book_from_trash()` / `delete_trash_entry()` / `expire_old_trash()` | `src/calibre/db/cache.py` |
| 回收站期限 | `DEFAULT_TRASH_EXPIRY_TIME_SECONDS = 14*86400` | `src/calibre/db/constants.py:11` |
| 文件哈希 | `new_api.format_hash(book_id, fmt)` → SHA-256 | `src/calibre/db/cache.py:1231` |
| 读资源配置 | `config_widget()` / `save_settings(widget)` / `validate()`；**`validate_before_accept = True` 才能让校验失败时对话框不关** | `src/calibre/customize/__init__.py:124,145` |
| `do_user_config` 返回 | `config_dialog.result()`：`Accepted`(1) 真、`Rejected`(0) 假 | `src/calibre/customize/__init__.py` |
| 设置读写 | `JSONConfig(path)`；`__setitem__` 每次赋值都 `commit()`，批量写用 `with conf:` | `src/calibre/utils/config.py` |
| 读插件资源 | `self.load_resources(['images/icon.png'])` → `{路径: bytes}`；`plugin_path is None` 时抛 `ValueError` | `src/calibre/gui2/actions/__init__.py:372` |
| 图标解析 | `create_action` 用 `QIcon.ic(icon)` —— `action_spec[1]` 是**主题名**，不是 zip 路径 | `src/calibre/gui2/actions/__init__.py` |
| 包名解析 | `plugin-import-name-<name>.txt`，缺失则退化 `dummy1` | `src/calibre/customize/zipplugin.py:348-362` |
| **zip 布局** | 模块必须平铺在根目录；`_locate_code()` 只收录顶层包 | `src/calibre/customize/zipplugin.py:398` |

**参考实现**（GPL v3）：Find Duplicates v1.10.10 — `github.com/kiwidude68/calibre_plugins` → `find_duplicates/`。

---

## 附录 B：实测数据复现脚本

> 这段是**独立的最小复现**，用来验证 §1.4 里 `title` + `authors` 那组口径
> （预期 `组=9889 涉及=60287 待删=50398`）。它没走插件的 `normalize`，也没读自定义列。
> 复现插件**两组基线**（含 `#book_init_path`）请用
> `06-src/calibre_plugin/BookDedup4Del/tests/report_baseline.py` —— 那才是与产品同源的算法。

```python
# python3 reproduce.py 03-input/metadata-20260923.db
import sqlite3, sys, unicodedata, re
from collections import defaultdict

def nt(s):
    s = unicodedata.normalize('NFKC', s or '').lower()
    s = re.sub(r'^[《【\[（("\']+|[》】\]）)"\']+$', '', s)
    return re.sub(r'\s+', '', s)

c = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
auth = defaultdict(list)
for b, a in c.execute('select bal.book, a.name from books_authors_link bal'
                      ' join authors a on a.id=bal.author'):
    auth[b].append(a)
size = dict(c.execute('select book, sum(uncompressed_size) from data group by book'))

g = defaultdict(list)
for bid, title in c.execute('select id, title from books'):
    g[(nt(title), '\x1f'.join(sorted(nt(x) for x in auth.get(bid, []))))].append(bid)

dups = {k: v for k, v in g.items() if len(v) > 1}
cand = [b for v in dups.values() for b in sorted(v)[1:]]      # 简化保留策略：id 最小者留

print(f'组={len(dups)}  涉及={sum(map(len, dups.values()))}  待删={len(cand)}')
print(f'待删体积={sum(size.get(b, 0) for b in cand) / 1024**4:.2f} TB')
```

预期输出：`组=9889 涉及=60287 待删=50398`，待删体积 `1.4 TB` 量级（保留策略不同会有微差）。

---

## 13. 状态与下一步

### 已完成

- [x] 方案定稿（v2 → 本 v3 回填实现事实）
- [x] 15 个模块实现完毕，3,115 行（纯模块 8 个 / 1,174 行）
- [x] 380 个测试通过 / 0 skipped（含真实库全量回归的**两组字段基线** + 打包布局回归）
- [x] `dist/BookDedup4Del.zip` 可安装（50.7 KB / 17 个条目）
- [x] 打包布局用 calibre 自己的加载算法在本机验证通过
- [x] 插件改名 `book_dedup` → `BookDedup4Del`，工程整体收进 `BookDedup4Del/`
- [x] 查重字段扩为「全部标准字段 + 自定义列」（§0.5 偏离表第 6、7 条）：
      修掉恒假条件、字段候选改由 `displayable_field_keys()` 提供、装配逻辑挪进纯模块、
      取数改为按勾选 + `fields.POLICY_FIELDS` 恒取、默认组合加入 `#book_init_path` 并对存量设置做一次性迁移

### 待办

1. **Windows 装包验收** —— 按 §9.2 的 14 步清单走，前 6 步通过前不要碰真实库
2. **首轮建议**：先切「人工层」逐组看，确认保留策略符合直觉；再切「自动层」批量过
3. 若首次加载仍报错：`calibre-debug -g` 的控制台输出贴回来即可定位
4. 验收通过后可考虑：二进制哈希比对（真重复确认）、与流水线 CSV 互认

### 原始 4 个待确认问题的答复（已闭环）

| # | 问题 | 结论 |
|---|---|---|
| 1 | 接受「三层漏斗 + 组级复核」替代「逐条确认」？ | ✅ 已接受并实现（§3） |
| 2 | 「首轮限量 500」合适吗？ | ✅ 已接受（`batch_limit`，首选项可调） |
| 3 | 保留策略判据顺序符合直觉？ | ✅ 已实现（§5.2），验收时可在「人工层」实地检验 |
| 4 | 删除范围是**整本书**（连目录带所有格式）？ | ✅ 确认，实现即如此 |
