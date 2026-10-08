"""检查缺失文件的详细原因"""
import sqlite3
from pathlib import Path

LIBRARY_DIR = Path(r"E:\98-Calibre-books-new")
CLEANED_DB = Path("04-output/2026-09-24/step3/metadata.cleaned.db")

with sqlite3.connect(CLEANED_DB) as conn:
    conn.row_factory = sqlite3.Row
    
    # 查询所有 data 记录
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
    
    missing_files = []
    
    for row in rows:
        ext = row["data_format"].lower()
        file_path = LIBRARY_DIR / row["book_path"] / f"{row['data_name']}.{ext}"
        
        if not file_path.exists():
            missing_files.append({
                "data_id": row["data_id"],
                "book_id": row["book_id"],
                "book_path": row["book_path"],
                "data_name": row["data_name"],
                "format": row["data_format"],
                "file_path": file_path,
            })
    
    print(f"Total missing files: {len(missing_files)}")
    print("\n" + "=" * 70)
    
    # 分析缺失原因
    for i, item in enumerate(missing_files[:20]):
        print(f"\n[{i+1}] data_id={item['data_id']}, book_id={item['book_id']}")
        print(f"  book_path: {item['book_path']}")
        print(f"  data_name: {item['data_name']}")
        print(f"  format: {item['format']}")
        print(f"  file_path: {item['file_path']}")
        
        # 检查目录是否存在
        book_dir = LIBRARY_DIR / item['book_path']
        print(f"  book_dir exists: {book_dir.exists()}")
        
        if book_dir.exists():
            # 列出目录中的文件
            files_in_dir = list(book_dir.iterdir())
            print(f"  files in book_dir: {len(files_in_dir)}")
            for f in files_in_dir[:5]:
                print(f"    - {f.name}")
