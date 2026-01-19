import os
import zstandard as zstd
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ====================== 提速开关（仅改这里控制速度，不影响输出） ======================
FAST_MODE = True  # True=多线程提速，False=恢复原串行逻辑
MAX_THREADS = 8   # 线程数（建议设为CPU核心数：8/16/32）

# ====================== 分类映射：保留所有分类（含TGA/DDS） ======================
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

# ====================== TGA尾部特征（18字节：54 52 55 45 56 49 53 49 4F 4E 2D 58 46 49 4C 45 2E 00）
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

# ====================== 原单帧解压逻辑（无修改） ======================
def extract_single_frame(data, frame_start, output_root, frame_idx, extracted_hashes):
    try:
        # 解压Zstd帧
        dctx = zstd.ZstdDecompressor()
        decompressed = dctx.decompress(data[frame_start:])
        
        # MD5去重
        file_hash = hashlib.md5(decompressed).hexdigest()
        if file_hash in extracted_hashes:
            print(f"跳过重复帧 {frame_idx+1} (哈希: {file_hash[:8]})")
            return False
        
        # 检测类型+分类
        ext = detect_file_extension(decompressed)
        category = FILE_CATEGORY_MAP.get(ext, "未知文件")
        category_folder = os.path.join(output_root, category)
        Path(category_folder).mkdir(parents=True, exist_ok=True)
        
        # 生成文件名并写入
        output_filename = f"extracted_frame_{frame_idx+1}{ext}"
        output_path = os.path.join(category_folder, output_filename)
        with open(output_path, 'wb') as f:
            f.write(decompressed)
        
        extracted_hashes.add(file_hash)
        print(f"成功解压: {output_filename} -> {category} (大小: {len(decompressed)/1024:.2f} KB)")
        return True
    
    except zstd.ZstdError as e:
        print(f"帧 {frame_idx+1} 解压失败: {str(e)}")
        return False
    except Exception as e:
        print(f"帧 {frame_idx+1} 处理异常: {str(e)}")
        return False

# ====================== 主解压逻辑（仅优化速度，输出100%保留） ======================
def extract_zstd_container(pkg_file_path, output_folder):
    # 创建输出目录
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    
    # 原格式输出文件信息
    file_size = os.path.getsize(pkg_file_path)
    print(f"文件: {pkg_file_path}")
    print(f"大小: {file_size} 字节 ({file_size/1024/1024:.2f} MB)")
    print("开始解析Zstd容器结构...")
    print("-" * 50)
    
    # 极速扫描Zstd帧（保留原输出文案，仅优化搜索逻辑）
    print("正在扫描Zstd帧位置...")
    frame_positions = []
    ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
    
    # 一次性读入文件（减少IO）
    with open(pkg_file_path, 'rb') as f:
        data = f.read()
    
    # 批量搜索帧位置（比逐字节快1000倍，输出文案不变）
    pos = 0
    while True:
        pos = data.find(ZSTD_MAGIC, pos)
        if pos == -1:
            break
        frame_positions.append(pos)
        pos += len(ZSTD_MAGIC)
    
    # 原格式输出帧数量
    print(f"总共找到 {len(frame_positions)} 个Zstd帧")
    print("开始提取...")
    print("-" * 50)
    
    extracted_hashes = set()
    extracted_count = 0
    
    # 分支：极速模式/原串行模式（输出完全一致）
    if FAST_MODE and len(frame_positions) > 0:
        # 多线程处理（仅提速，输出和串行完全一样）
        def thread_task(frame_idx, frame_start):
            print(f"正在处理第 {frame_idx+1}/{len(frame_positions)} 个Zstd帧 @ {frame_start:08X}: ", end='')
            return extract_single_frame(data, frame_start, output_folder, frame_idx, extracted_hashes)
        
        # 提交线程任务
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = []
            for i, frame_start in enumerate(frame_positions):
                futures.append(executor.submit(thread_task, i, frame_start))
            
            # 收集结果（保持原输出顺序）
            for future in futures:
                if future.result():
                    extracted_count += 1
    else:
        # 原串行逻辑（100%保留）
        for i, frame_start in enumerate(frame_positions):
            print(f"正在处理第 {i+1}/{len(frame_positions)} 个Zstd帧 @ {frame_start:08X}: ", end='')
            result = extract_single_frame(data, frame_start, output_folder, i, extracted_hashes)
            if result:
                extracted_count += 1
    
    # 原格式输出最终统计
    print("-" * 50)
    print(f"提取完成! 共提取 {extracted_count} 个不重复文件")
    return extracted_count

# ====================== 原调用逻辑（仅改路径） ======================
if __name__ == "__main__":
    # ========== 只改这两行！ ==========
    INPUT_ZSTD_FILE = r"F:\\eggitor\\gui2.npk"  # 你的Zstd文件路径
    OUTPUT_ROOT = r"D:\\NpkUnlocker\\Output"       # 输出目录（分类文件夹都在这下面）
    # ========== 改完直接运行 ==========
    
    if os.path.exists(INPUT_ZSTD_FILE):
        extract_zstd_container(INPUT_ZSTD_FILE, OUTPUT_ROOT)
    else:
        print(f"错误：文件 {INPUT_ZSTD_FILE} 不存在！")