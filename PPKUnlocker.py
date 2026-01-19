import os
import sys
import zstandard as zstd
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil

# ====================== 核心配置（可直接修改默认值） ======================
# 硬件适配（i5-7200U + 8GB内存）
MAX_THREADS = 4  # CPU线程数（2核4线程）
MAX_BLOCK_SIZE = 20 * 1024 * 1024  # 单Zstd块最大20MB
CHUNK_SIZE = 1024 * 1024  # 分块读取大小（减少内存占用）

# 导出路径配置（可修改默认输出目录）
DEFAULT_OUTPUT_DIR = None  # None表示默认输出到PPK目录下的Output文件夹
# 示例：固定输出到D盘指定目录 → DEFAULT_OUTPUT_DIR = r"D:\\PPKUnlocker\\Output

# ====================== 你的自定义分类/检测逻辑（完整保留） ======================
FILE_CATEGORY_MAP = {
    ".wem": "音频文件",  # RIFF+WAVE判定WEM
    ".bnk": "音频文件",  # 保留BNK
    ".png": "图片纹理",
    ".dds": "图片纹理",  # 仅DDS头就判定
    ".ktx": "图片纹理",
    ".tga": "图片纹理",  # TGA尾部特征
    ".mesh": "模型文件",
    ".npk": "数据包文件",
    ".zst": "压缩文件",
    "": "未知文件"
}

# TGA尾部特征（18字节：54 52 55 45 56 49 53 49 4F 4E 2D 58 46 49 4C 45 2E 00）
TGA_TAIL_MAGIC = b'TRUEVISION-XFILE.\x00'  # 对应十六进制特征

# ====================== 最终版文件检测逻辑 ======================
def detect_file_extension(data):
    if not data:
        return ""
    
    # MESH模型文件（34 80 C8 BB）
    MESH_MAGIC = b'\x34\x80\xc8\xbb'
    if len(data) >= 4 and data[:4] == MESH_MAGIC:
        return ".mesh"
    
    # PNG图片（89 50 4E 47）
    PNG_MAGIC = b'\x89PNG'
    if len(data) >= 4 and data[:4] == PNG_MAGIC:
        return ".png"
    
    # KTX纹理文件（AB 4B 54 58 20 31 31 BB）
    KTX_MAGIC = b'\xABKTX 11\xBB'
    if len(data) >= 8 and data[:8] == KTX_MAGIC:
        return ".ktx"
    
    # 修改DDS判定：仅前3字节是DDS就判定为DDS文件（移除UVERNVTT/DXT1校验）
    if len(data) >= 3 and data[:3] == b'DDS':
        return ".dds"
    
    # WEM判定：RIFF + WAVE（匹配你提供的文件头）
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        return ".wem"
    
    # BNK音库（BKHD）
    if len(data) >= 4 and data[:4] == b'BKHD':
        return ".bnk"
    
    # NPK包（AKPK）
    if len(data) >= 4 and data[:4] == b'AKPK':
        return ".npk"
    
    # Zstd压缩文件（28 B5 2F FD）
    if len(data) >= 4 and data[:4] == b'\x28\xb5\x2f\xfd':
        return ".zst"
    
    # TGA文件检测（基于尾部18字节特征）
    if len(data) >= len(TGA_TAIL_MAGIC):
        if data[-len(TGA_TAIL_MAGIC):] == TGA_TAIL_MAGIC:
            return ".tga"
    
    # 未知类型
    return ""

# ====================== 全局去重集合（线程安全） ======================
DUPLICATE_MD5 = set()

# ====================== 单文件处理函数（供多线程调用） ======================
def process_ppk_file(file_path, output_root):
    """处理单个PPK文件，提取Zstd块并解压分类"""
    file_name = Path(file_path).name
    processed_blocks = 0
    extracted_blocks = 0
    
    try:
        # 分块读取文件（减少内存占用）
        with open(file_path, "rb") as f:
            file_data = f.read()
        
        # 扫描所有Zstd魔数位置
        ZSTD_MAGIC = b"\x28\xB5\x2F\xFD"
        offset = 0
        block_idx = 0
        
        while offset < len(file_data):
            # 找Zstd魔数
            magic_pos = file_data.find(ZSTD_MAGIC, offset)
            if magic_pos == -1:
                break
            
            # 确定块结束位置
            next_magic_pos = file_data.find(ZSTD_MAGIC, magic_pos + 4)
            block_end = min(
                next_magic_pos if next_magic_pos != -1 else len(file_data),
                magic_pos + MAX_BLOCK_SIZE
            )
            
            # 提取Zstd块
            zstd_data = file_data[magic_pos:block_end]
            processed_blocks += 1
            
            # 过滤过小的块
            if len(zstd_data) < 1024:
                offset = block_end
                continue
            
            # 全局去重（线程安全）
            block_md5 = hashlib.md5(zstd_data).hexdigest()
            if block_md5 in DUPLICATE_MD5:
                offset = block_end
                continue
            DUPLICATE_MD5.add(block_md5)
            
            # 解压Zstd块
            try:
                dctx = zstd.ZstdDecompressor()
                decompressed = dctx.decompress(zstd_data)
            except Exception as e:
                offset = block_end
                continue
            
            # 检测文件类型
            file_ext = detect_file_extension(decompressed)
            category = FILE_CATEGORY_MAP.get(file_ext, "未知文件")
            
            # 创建分类目录
            category_dir = output_root / category
            category_dir.mkdir(exist_ok=True, parents=True)
            
            # 生成保存文件名
            save_name = f"{file_name}_block{block_idx}{file_ext}"
            save_path = category_dir / save_name
            
            # 保存文件
            with open(save_path, "wb") as f:
                f.write(decompressed)
            
            extracted_blocks += 1
            block_idx += 1
            offset = block_end
        
        return {
            "file": file_name,
            "processed": processed_blocks,
            "extracted": extracted_blocks,
            "status": "success"
        }
    
    except Exception as e:
        return {
            "file": file_name,
            "error": str(e)[:100],
            "status": "failed"
        }

# ====================== 主函数（支持自定义输出路径） ======================
def main():
    # 显示使用帮助
    def print_help():
        print("="*60)
        print("PPK文件解析工具 - 支持自定义输出目录")
        print("="*60)
        print("用法1（使用默认输出路径）：")
        print("  python 脚本.py <PPK文件所在目录>")
        print("  示例：python ppk_extract.py D:/ppk_files")
        print("  输出路径：PPK目录/Output")
        print("\n用法2（自定义输出路径）：")
        print("  python 脚本.py <PPK文件所在目录> <自定义输出目录>")
        print("  示例：python ppk_extract.py D:/ppk_files E:/ppk_output")
        print("="*60)
    
    # 检查命令行参数
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print_help()
        sys.exit(1)
    
    # 获取PPK目录
    ppk_dir = Path(sys.argv[1])
    if not ppk_dir.exists() or not ppk_dir.is_dir():
        print(f"❌ 错误：目录 {ppk_dir} 不存在或不是有效目录")
        sys.exit(1)
    
    # 确定输出目录
    if len(sys.argv) == 3:
        # 命令行指定自定义输出目录
        output_root = Path(sys.argv[2])
    elif DEFAULT_OUTPUT_DIR is not None:
        # 使用脚本内配置的默认输出目录
        output_root = Path(DEFAULT_OUTPUT_DIR)
    else:
        # 默认输出到PPK目录下的Output文件夹
        output_root = ppk_dir / "Output"
    
    # 创建输出目录（自动创建多级目录）
    output_root.mkdir(parents=True, exist_ok=True)
    print(f"📂 输出目录已确定：{output_root.absolute()}")
    
    # 收集所有PPK文件（任意8位字母数字文件名，无后缀）
    ppk_files = []
    for file in ppk_dir.iterdir():
        if file.is_file() and len(file.name) == 8 and file.name.isalnum():
            ppk_files.append(file)
    
    if not ppk_files:
        print(f"⚠️ 在目录 {ppk_dir} 中未找到任何PPK文件（8位字母数字文件名）")
        sys.exit(0)
    
    # 多线程处理
    print(f"🚀 找到 {len(ppk_files)} 个PPK文件，使用 {MAX_THREADS} 线程处理...")
    results = []
    
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        # 提交任务
        future_to_file = {
            executor.submit(process_ppk_file, str(file), output_root): file 
            for file in ppk_files
        }
        
        # 处理结果
        for future in as_completed(future_to_file):
            file = future_to_file[future]
            try:
                result = future.result()
                results.append(result)
                if result["status"] == "success":
                    print(f"✅ {result['file']} - 处理块数：{result['processed']} - 提取块数：{result['extracted']}")
                else:
                    print(f"❌ {result['file']} - 错误：{result['error']}")
            except Exception as e:
                print(f"❌ {file.name} - 任务异常：{str(e)[:100]}")
    
    # 统计结果
    total_processed = 0
    total_extracted = 0
    failed_files = 0
    
    for res in results:
        if res["status"] == "success":
            total_processed += res["processed"]
            total_extracted += res["extracted"]
        else:
            failed_files += 1
    
    # 打印最终统计
    print("\n" + "="*60)
    print("📊 处理完成统计：")
    print(f"   📁 总PPK文件数：{len(ppk_files)}")
    print(f"   ❌ 处理失败文件数：{failed_files}")
    print(f"   🔍 总扫描Zstd块数：{total_processed}")
    print(f"   ✅ 去重后提取块数：{total_extracted}")
    print(f"   📂 最终输出目录：{output_root.absolute()}")
    print("="*60)

if __name__ == "__main__":
    # 安装依赖（自动检测）
    try:
        import zstandard
    except ImportError:
        print("📦 正在安装依赖包 zstandard...")
        os.system("pip install zstandard -q")
        import zstandard
    
    main()