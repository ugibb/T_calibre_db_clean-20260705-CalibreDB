"""第四层验证：物理文件一致性"""
import sqlite3
from pathlib import Path

LIBRARY_DIR = Path(r"E:\98-Calibre-books-new")
OUTPUT_DIR = Path("04-output/2026-09-24")
CLEANED_DB = OUTPUT_DIR / "step3" / "metadata.cleaned.db"
BAT_PATH = OUTPUT_DIR / "step4" / "step2_cleaning_instructions.bat"
ROLLBACK_BAT = OUTPUT_DIR / "step4" / "step2_rollback.bat"

def check_physical_files():
    """验证 cleaned.db 中的 data 记录对应的物理文件是否存在"""
    print("=" * 70)
    print("第四层验证：物理文件一致性")
    print("=" * 70)
    
    if not LIBRARY_DIR.exists():
        print(f"[FAIL] Calibre 文库目录不存在：{LIBRARY_DIR}")
        return
    
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
                books.path as book_path
            FROM data
            JOIN books ON data.book = books.id
        """).fetchall()
        
        print(f"\n检查 {len(rows)} 条 data 记录的物理文件...")
        
        missing_files = []
        existing_files = 0
        
        for row in rows:
            # 构建文件路径：LIBRARY_DIR / book_path / data_name.format.lower()
            ext = row["data_format"].lower()
            file_path = LIBRARY_DIR / row["book_path"] / f"{row['data_name']}.{ext}"
            
            if file_path.exists():
                existing_files += 1
            else:
                missing_files.append({
                    "data_id": row["data_id"],
                    "book_id": row["book_id"],
                    "file_path": str(file_path),
                })
        
        print(f"  文件存在：{existing_files:,} [OK]")
        print(f"  文件缺失：{len(missing_files):,} {'[OK]' if len(missing_files) == 0 else '[FAIL]'}")
        
        if missing_files:
            print(f"\n缺失文件示例（前 10 个）：")
            for item in missing_files[:10]:
                print(f"  data_id={item['data_id']}, book_id={item['book_id']}")
                print(f"    {item['file_path']}")
        
        return len(missing_files) == 0

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
    move_back_count = content.count("call :do_move_book")
    mkdir_count = content.count("mkdir")
    
    print(f"  移动回命令数：{move_back_count:,}")
    print(f"  mkdir 命令数：{mkdir_count:,}")
    
    # 检查 BAT 结构
    has_echo_off = "@echo off" in content
    has_chcp = "chcp 65001" in content
    has_setlocal = "setlocal enabledelayedexpansion" in content
    
    print(f"  @echo off: {'[OK]' if has_echo_off else '[FAIL]'}")
    print(f"  chcp 65001: {'[OK]' if has_chcp else '[FAIL]'}")
    print(f"  setlocal: {'[OK]' if has_setlocal else '[FAIL]'}")
    
    # 检查是否引用了正确的目录
    has_library_dir = "LIBRARY_DIR" in content
    has_quarantine_dir = "QUARANTINE_DIR" in content
    
    print(f"  LIBRARY_DIR: {'[OK]' if has_library_dir else '[FAIL]'}")
    print(f"  QUARANTINE_DIR: {'[OK]' if has_quarantine_dir else '[FAIL]'}")
    
    all_ok = (has_echo_off and has_chcp and has_setlocal and 
              has_library_dir and has_quarantine_dir and move_back_count > 0)
    
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
