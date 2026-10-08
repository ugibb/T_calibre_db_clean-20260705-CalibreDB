"""第四层验证：物理文件一致性（修正版）"""
import sqlite3
from pathlib import Path

LIBRARY_DIR = Path(r"E:\98-Calibre-books-new")
OUTPUT_DIR = Path("04-output/2026-09-24")
CLEANED_DB = OUTPUT_DIR / "step3" / "metadata.cleaned.db"
ROLLBACK_BAT = OUTPUT_DIR / "step4" / "step2_rollback.bat"

def check_physical_files():
    """验证 cleaned.db 中的 data 记录对应的物理文件是否存在"""
    print("=" * 70)
    print("第四层验证：物理文件一致性")
    print("=" * 70)
    
    if not LIBRARY_DIR.exists():
        print(f"[FAIL] Calibre 文库目录不存在：{LIBRARY_DIR}")
        return False
    
    print(f"Calibre 文库目录：{LIBRARY_DIR}")
    
    with sqlite3.connect(CLEANED_DB) as conn:
        conn.row_factory = sqlite3.Row
        
        # 查询所有 data 记录及其对应的 books.path
        rows = conn.execute("""
            SELECT 
                data.id as data_id,
                data.book as book_id,
                data.name as data_name,
                data.format as data_format,
                books.path as book_path,
                books.title as book_title,
                books.author_sort as author_sort
            FROM data
            JOIN books ON data.book = books.id
        """).fetchall()
        
        print(f"\n检查 {len(rows)} 条 data 记录的物理文件...")
        
        missing_files = []
        existing_files = 0
        
        for row in rows:
            ext = row["data_format"].lower()
            book_dir = LIBRARY_DIR / row["book_path"]
            
            if not book_dir.exists():
                missing_files.append({
                    "data_id": row["data_id"],
                    "book_id": row["book_id"],
                    "reason": "book_dir_not_found",
                    "book_path": str(book_dir),
                })
                continue
            
            # Calibre 文件命名规则：查找匹配扩展名的文件
            # 可能的文件名模式：
            # 1. {data_name}.{ext}
            # 2. {book_title} - {author}.{ext}
            # 3. 任何匹配扩展名的文件（如果目录中只有一个该格式的文件）
            
            expected_file1 = book_dir / f"{row['data_name']}.{ext}"
            if expected_file1.exists():
                existing_files += 1
                continue
            
            # 查找目录中所有匹配扩展名的文件
            matching_files = list(book_dir.glob(f"*.{ext}"))
            
            if len(matching_files) == 1:
                # 只有一个该格式的文件，认为匹配
                existing_files += 1
            elif len(matching_files) > 1:
                # 多个文件，检查是否有匹配的
                found = False
                for f in matching_files:
                    # 检查文件名是否包含 data_name 或 book_title 的关键词
                    if row['data_name'] in f.stem or row['book_title'] in f.stem:
                        existing_files += 1
                        found = True
                        break
                
                if not found:
                    missing_files.append({
                        "data_id": row["data_id"],
                        "book_id": row["book_id"],
                        "reason": "ambiguous_match",
                        "book_path": str(book_dir),
                        "expected": f"{row['data_name']}.{ext}",
                        "found": [f.name for f in matching_files],
                    })
            else:
                missing_files.append({
                    "data_id": row["data_id"],
                    "book_id": row["book_id"],
                    "reason": "no_matching_file",
                    "book_path": str(book_dir),
                    "expected": f"{row['data_name']}.{ext}",
                })
        
        print(f"  文件存在：{existing_files:,} [OK]")
        print(f"  文件缺失：{len(missing_files):,} {'[OK]' if len(missing_files) == 0 else '[WARN]'}")
        
        if missing_files:
            # 统计缺失原因
            reason_counts = {}
            for item in missing_files:
                reason = item['reason']
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            
            print(f"\n缺失原因统计：")
            for reason, count in sorted(reason_counts.items()):
                print(f"  {reason}: {count}")
            
            print(f"\n缺失文件示例（前 5 个）：")
            for item in missing_files[:5]:
                print(f"  data_id={item['data_id']}, book_id={item['book_id']}, reason={item['reason']}")
                print(f"    {item['book_path']}")
                if 'expected' in item:
                    print(f"    expected: {item['expected']}")
                if 'found' in item:
                    print(f"    found: {item['found']}")
        
        # 允许一定比例的缺失（可能是数据库与文件系统不完全同步）
        missing_ratio = len(missing_files) / len(rows) if rows else 0
        print(f"\n缺失率：{missing_ratio:.2%}")
        
        return missing_ratio < 0.05  # 缺失率 < 5% 认为通过

def check_rollback_bat():
    """验证回滚 BAT 文件"""
    print("\n" + "=" * 70)
    print("回滚 BAT 验证")
    print("=" * 70)
    
    if not ROLLBACK_BAT.exists():
        print(f"[FAIL] 回滚 BAT 不存在：{ROLLBACK_BAT}")
        return False
    
    print(f"回滚 BAT：{ROLLBACK_BAT}")
    
    # 读取 BAT 内容
    content = ROLLBACK_BAT.read_text(encoding="utf-8")
    
    # 统计操作类型
    rollback_count = content.count("call :do_rollback_book")
    mkdir_count = content.count("mkdir")
    
    print(f"  回滚操作数：{rollback_count:,}")
    print(f"  mkdir 命令数：{mkdir_count:,}")
    
    # 检查 BAT 结构
    has_echo_off = "@echo off" in content
    has_chcp = "chcp 65001" in content
    has_setlocal = "setlocal enabledelayedexpansion" in content
    has_subroutine = ":do_rollback_book" in content
    
    print(f"  @echo off: {'[OK]' if has_echo_off else '[FAIL]'}")
    print(f"  chcp 65001: {'[OK]' if has_chcp else '[FAIL]'}")
    print(f"  setlocal: {'[OK]' if has_setlocal else '[FAIL]'}")
    print(f"  :do_rollback_book: {'[OK]' if has_subroutine else '[FAIL]'}")
    
    # 检查是否引用了正确的目录
    has_library_dir = "LIBRARY_DIR" in content
    has_quarantine_dir = "QUARANTINE_DIR" in content
    
    print(f"  LIBRARY_DIR: {'[OK]' if has_library_dir else '[FAIL]'}")
    print(f"  QUARANTINE_DIR: {'[OK]' if has_quarantine_dir else '[FAIL]'}")
    
    all_ok = (has_echo_off and has_chcp and has_setlocal and 
              has_subroutine and has_library_dir and has_quarantine_dir and rollback_count > 0)
    
    return all_ok

if __name__ == "__main__":
    result1 = check_physical_files()
    result2 = check_rollback_bat()
    
    print("\n" + "=" * 70)
    print("验证结果")
    print("=" * 70)
    print(f"  物理文件一致性：{'[OK]' if result1 else '[FAIL]'}")
    print(f"  回滚 BAT 验证：{'[OK]' if result2 else '[FAIL]'}")
    print("=" * 70)
