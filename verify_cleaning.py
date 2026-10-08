"""Step3 清洗验证脚本"""
import sqlite3
import re
from pathlib import Path

OUTPUT_DIR = Path("04-output/2026-09-24")
STEP2_DIR = OUTPUT_DIR / "step2"
STEP3_DIR = OUTPUT_DIR / "step3"
STEP4_DIR = OUTPUT_DIR / "step4"

def get_db_path(step, pattern):
    """获取数据库路径"""
    if step == "step2":
        dbs = list(STEP2_DIR.glob(pattern))
        return dbs[0] if dbs else None
    return STEP3_DIR / pattern

def query(db_path, sql):
    """执行查询并返回结果"""
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()

def query_set(db_path, sql):
    """执行查询并返回集合"""
    return set(row[0] for row in query(db_path, sql))

def count(db_path, table):
    """统计表记录数"""
    return query(db_path, f"SELECT COUNT(*) FROM {table}")[0][0]

print("=" * 70)
print("Step3 清洗验证报告")
print("=" * 70)

# 获取数据库路径
encoded_db = get_db_path("step2", "metadata-*.encoded.db")
merged_db = get_db_path("step3", "metadata.merged.db")
cleaned_db = get_db_path("step3", "metadata.cleaned.db")
sql_path = STEP4_DIR / "step2_cleaning_instructions.sql"

print(f"\n输入文件：")
print(f"  encoded.db: {encoded_db.name if encoded_db else 'N/A'}")
print(f"  merged.db:  {merged_db.name if merged_db.exists() else 'N/A'}")
print(f"  cleaned.db: {cleaned_db.name if cleaned_db.exists() else 'N/A'}")
print(f"  SQL:        {sql_path.name if sql_path.exists() else 'N/A'}")

# ─────────────────────────────────────────────────────────────
# 第一层：cleaned.db 内部完整性
# ─────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("第一层：cleaned.db 内部完整性")
print("=" * 70)

# 孤儿 data
orphan_data = count(cleaned_db, "data WHERE book NOT IN (SELECT id FROM books)")
print(f"  孤儿 data：{orphan_data} {'[OK]' if orphan_data == 0 else '[FAIL]'}")

# 空 book
empty_books = count(cleaned_db, "books WHERE id NOT IN (SELECT DISTINCT book FROM data)")
print(f"  空 book：{empty_books} {'[OK]' if empty_books == 0 else '[FAIL]'}")

# 孤儿 link（检查所有 link 表）
link_tables = [
    "books_tags_link",
    "books_authors_link",
    "books_publishers_link",
    "books_ratings_link",
    "books_series_link",
    "books_languages_link",
    "books_custom_column_1_link",
    "books_custom_column_2_link",
]
orphan_link_total = 0
for link_table in link_tables:
    try:
        orphan_count = query(cleaned_db, f"SELECT COUNT(*) FROM {link_table} WHERE book NOT IN (SELECT id FROM books)")[0][0]
        orphan_link_total += orphan_count
        if orphan_count > 0:
            print(f"    {link_table}: {orphan_count} [FAIL]")
    except:
        pass  # 表不存在
print(f"  孤儿 link：{orphan_link_total} {'[OK]' if orphan_link_total == 0 else '[FAIL]'}（检查 {len(link_tables)} 张 link 表）")

# 重复 format
dup_formats = query(cleaned_db, """
    SELECT book, format, COUNT(*) as cnt
    FROM data
    GROUP BY book, format
    HAVING cnt > 1
""")
print(f"  重复 format：{len(dup_formats)} {'[OK]' if len(dup_formats) == 0 else '[FAIL]'}")

# ─────────────────────────────────────────────────────────────
# 第二层：cleaned.db 删除正确性
# ─────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("第二层：cleaned.db 删除正确性")
print("=" * 70)

merged_books = query_set(merged_db, "SELECT id FROM books")
cleaned_books = query_set(cleaned_db, "SELECT id FROM books")
merged_data = query_set(merged_db, "SELECT id FROM data")
cleaned_data = query_set(cleaned_db, "SELECT id FROM data")

removed_books = merged_books - cleaned_books
added_books = cleaned_books - merged_books
removed_data = merged_data - cleaned_data
added_data = cleaned_data - merged_data

print(f"  merged → cleaned：books {len(merged_books):,} → {len(cleaned_books):,}（删除 {len(removed_books):,}）{'[OK]' if len(added_books) == 0 else '[FAIL]'}")
print(f"  merged → cleaned：data {len(merged_data):,} → {len(cleaned_data):,}（删除 {len(removed_data):,}）{'[OK]' if len(added_data) == 0 else '[FAIL]'}")
print(f"  意外新增 books：{len(added_books)} {'[OK]' if len(added_books) == 0 else '[FAIL]'}")
print(f"  意外新增 data：{len(added_data)} {'[OK]' if len(added_data) == 0 else '[FAIL]'}")
print(f"  cleaned_books is subset of merged_books: {'[OK]' if cleaned_books.issubset(merged_books) else '[FAIL]'}")
print(f"  cleaned_data is subset of merged_data: {'[OK]' if cleaned_data.issubset(merged_data) else '[FAIL]'}")

# ─────────────────────────────────────────────────────────────
# 第三层：CSV ↔ SQL ↔ DB 一致性
# ─────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("第三层：CSV <-> SQL <-> DB 一致性")
print("=" * 70)

# 从 SQL 提取 DELETE 的 data_id 和 book_id
DELETE_DATA_RE = re.compile(r"^\s*DELETE\s+FROM\s+data\s+WHERE\s+id\s*=\s*(\d+)\s*;\s*$", re.IGNORECASE)
DELETE_BOOK_RE = re.compile(r"^\s*DELETE\s+FROM\s+books\s+WHERE\s+id\s*=\s*(\d+)\s+AND\s+NOT\s+EXISTS\b", re.IGNORECASE)

sql_text = sql_path.read_text(encoding="utf-8")
sql_data_ids = set()
sql_book_ids = set()
for line in sql_text.splitlines():
    data_match = DELETE_DATA_RE.match(line)
    if data_match:
        sql_data_ids.add(int(data_match.group(1)))
        continue
    book_match = DELETE_BOOK_RE.match(line)
    if book_match:
        sql_book_ids.add(int(book_match.group(1)))

# 验证 SQL 已执行
remaining_data = query(cleaned_db, f"SELECT id FROM data WHERE id IN ({','.join(map(str, sql_data_ids))})") if sql_data_ids else []
remaining_books = query(cleaned_db, f"SELECT id FROM books WHERE id IN ({','.join(map(str, sql_book_ids))})") if sql_book_ids else []

print(f"  SQL DELETE data 数：{len(sql_data_ids):,}")
print(f"  SQL DELETE books 数：{len(sql_book_ids):,}")
print(f"  SQL DELETE data 已执行：{len(sql_data_ids) - len(remaining_data):,} / {len(sql_data_ids):,} {'[OK]' if len(remaining_data) == 0 else '[FAIL]'}")
print(f"  SQL DELETE books 已执行：{len(sql_book_ids) - len(remaining_books):,} / {len(sql_book_ids):,} {'[OK]' if len(remaining_books) == 0 else '[FAIL]'}")

# 验证删除数量一致
actual_data_deleted = len(merged_data) - len(cleaned_data)
actual_books_deleted = len(merged_books) - len(cleaned_books)
print(f"  实际删除 data：{actual_data_deleted:,}（SQL 预期 {len(sql_data_ids):,}）{'[OK]' if actual_data_deleted == len(sql_data_ids) else '[WARN]'}")
print(f"  实际删除 books：{actual_books_deleted:,}（SQL 预期 {len(sql_book_ids):,}）{'[OK]' if actual_books_deleted == len(sql_book_ids) else '[WARN]'}")

# ─────────────────────────────────────────────────────────────
# 第四层：物理文件一致性（跳过）
# ─────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("第四层：物理文件一致性")
print("=" * 70)
print("  [跳过 — 需在 Windows 上跑 BAT 后验证]")

print("\n" + "=" * 70)
print("验证完成")
print("=" * 70)
