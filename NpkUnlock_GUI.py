# -*- coding: utf-8 -*-
import os
import sys
import hashlib
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import subprocess
import threading
import logging

import zstandard as zstd
from PyQt5 import QtCore, QtGui, QtWidgets

CHILD_ARG = "--run-main-child"

# ===================== 日志系统 =====================

logger_gui = logging.getLogger("gui")
logger_gui.setLevel(logging.INFO)

formatter_gui = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter_gui)
logger_gui.addHandler(stream_handler)

file_handler = None


def set_file_logging(enabled: bool, log_dir: str):
    global file_handler
    if enabled:
        if not log_dir:
            return False, "日志目录为空"
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "neonpk.log")
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(formatter_gui)
            logger_gui.addHandler(fh)
            file_handler = fh
            logger_gui.info(f"启用日志文件写入: {log_path}")
            return True, log_path
        except Exception as e:
            return False, str(e)
    else:
        if file_handler is not None:
            logger_gui.removeHandler(file_handler)
            try:
                file_handler.close()
            except Exception:
                pass
        return True, ""


def format_gui_log_line(logger_name: str, level: str, message: str) -> str:
    now = datetime.now()
    ms = int(now.microsecond / 1000)
    timestamp = now.strftime(f"%Y-%m-%d %H:%M:%S,{ms:03d}")
    return f"{timestamp} - {level} - {logger_name} - {message}"


# ===================== 文件类型检测 =====================

FILE_CATEGORY_MAP = {
    ".wem": "普通文件",   # 内部分类仍然叫“普通文件”，UI 显示映射为“音频文件”
    ".bnk": "普通文件",
    ".png": "图片文件",
    ".dds": "图片文件",
    ".ktx": "图片文件",
    ".tga": "图片文件",
    ".mesh": "模型文件",
    ".npk": "数据文件",
    ".zst": "压缩文件",
    "": "未知文件",
}

TGA_TAIL_MAGIC = b"TRUEVISION-XFILE.\x00"


def detect_file_extension(data: bytes) -> str:
    if not data:
        return ""
    mesh_magic = b"\x34\x80\xc8\xbb"
    if len(data) >= 4 and data[:4] == mesh_magic:
        return ".mesh"
    png_magic = b"\x89PNG"
    if len(data) >= 4 and data[:4] == png_magic:
        return ".png"
    ktx_magic = b"\xABKTX 11\xBB"
    if len(data) >= 8 and data[:8] == ktx_magic:
        return ".ktx"
    if len(data) >= 3 and data[:3] == b"DDS":
        return ".dds"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wem"
    if len(data) >= 4 and data[:4] == b"BKHD":
        return ".bnk"
    if len(data) >= 4 and data[:4] == b"AKPK":
        return ".npk"
    if len(data) >= 4 and data[:4] == b"\x28\xb5\x2f\xfd":
        return ".zst"
    if len(data) >= len(TGA_TAIL_MAGIC) and data[-len(TGA_TAIL_MAGIC):] == TGA_TAIL_MAGIC:
        return ".tga"
    return ""


def scan_zstd_frames(data: bytes):
    magic = b"\x28\xb5\x2f\xfd"
    positions = []
    pos = 0
    while True:
        pos = data.find(magic, pos)
        if pos == -1:
            break
        positions.append(pos)
        pos += len(magic)
    return positions


def extract_single_frame(
    data: bytes,
    frame_start: int,
    output_root: str,
    frame_idx: int,
    extracted_hashes: set,
    stop_flag,
    enable_md5: bool = True,
    enable_type_detect: bool = True,
):
    if stop_flag():
        return False, "任务已中断（未开始解压该帧）", None

    prefix = f"[帧 {frame_idx + 1:04d} @ 0x{frame_start:08X}] "
    try:
        dctx = zstd.ZstdDecompressor()
        if stop_flag():
            return False, f"{prefix}任务已中断（跳过解压）", None

        decompressed = dctx.decompress(data[frame_start:])

        if stop_flag():
            return False, f"{prefix}任务已中断（解压完成但未写入文件）", None

        if enable_md5:
            file_hash = hashlib.md5(decompressed).hexdigest()
            if file_hash in extracted_hashes:
                msg = f"{prefix}跳过重复帧 (哈希: {file_hash[:8]})"
                return False, msg, None
        else:
            file_hash = hashlib.md5(decompressed).hexdigest()

        if enable_type_detect:
            ext = detect_file_extension(decompressed)
        else:
            ext = ""

        category = FILE_CATEGORY_MAP.get(ext, "未知文件")
        category_folder = os.path.join(output_root, category)
        Path(category_folder).mkdir(parents=True, exist_ok=True)
        output_filename = f"extracted_frame_{frame_idx + 1}{ext}"
        output_path = os.path.join(category_folder, output_filename)

        if stop_flag():
            msg = f"{prefix}任务已中断（未写入文件）"
        else:
            with open(output_path, "wb") as f:
                f.write(decompressed)
            if enable_md5:
                extracted_hashes.add(file_hash)
            size = len(decompressed)
            size_kb = size / 1024
            msg = (
                f"{prefix}成功解压: {output_filename} -> {category} "
                f"(大小: {size_kb:.2f} KB, 哈希: {file_hash[:8]})"
            )
            info = {
                "name": output_filename,
                "ext": ext,
                "category": category,
                "size": size,
                "path": output_path,
            }
            return True, msg, info

        return False, msg, None
    except zstd.ZstdError as e:
        msg = f"{prefix}解压失败: {str(e)}"
        return False, msg, None
    except Exception as e:
        msg = f"{prefix}处理异常: {str(e)}"
        return False, msg, None


# ===================== FlowLayout =====================

class FlowLayout(QtWidgets.QLayout):
    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self.itemList = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item):
        self.itemList.append(item)

    def count(self):
        return len(self.itemList)

    def itemAt(self, index):
        if 0 <= index < len(self.itemList):
            return self.itemList[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self.itemList):
            return self.itemList.pop(index)
        return None

    def expandingDirections(self):
        return QtCore.Qt.Orientations(QtCore.Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.doLayout(QtCore.QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self.doLayout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self.itemList:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def doLayout(self, rect, testOnly):
        x = rect.x()
        y = rect.y()
        lineHeight = 0

        for item in self.itemList:
            wid = item.widget()
            if wid is None:
                continue
            spaceX = self.spacing()
            spaceY = self.spacing()
            nextX = x + wid.sizeHint().width() + spaceX

            if nextX - spaceX > rect.right() and lineHeight > 0:
                x = rect.x()
                y = y + lineHeight + spaceY
                nextX = x + wid.sizeHint().width() + spaceX
                lineHeight = 0

            if not testOnly:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), wid.sizeHint()))

            x = nextX
            lineHeight = max(lineHeight, wid.sizeHint().height())

        return y + lineHeight - rect.y()


# ===================== Worker =====================

class ExtractWorker(QtCore.QObject):
    log_signal = QtCore.pyqtSignal(str)
    progress_signal = QtCore.pyqtSignal(int, int)
    file_signal = QtCore.pyqtSignal(dict)
    finished_signal = QtCore.pyqtSignal(int)
    error_signal = QtCore.pyqtSignal(str)

    def __init__(self, input_file: str, output_root: str, fast_mode: bool, max_threads: int,
                 enable_md5: bool = True, enable_type_detect: bool = True):
        super().__init__()
        self.input_file = input_file
        self.output_root = output_root
        self.fast_mode = fast_mode
        self.max_threads = max_threads
        self.enable_md5 = enable_md5
        self.enable_type_detect = enable_type_detect
        self._stop = False

    @QtCore.pyqtSlot()
    def run(self):
        try:
            if not os.path.exists(self.input_file):
                msg = f"错误：文件不存在 -> {self.input_file}"
                logger_gui.error(msg)
                self.error_signal.emit(format_gui_log_line("gui", "ERROR", msg))
                return
            if not os.path.exists(self.output_root):
                os.makedirs(self.output_root, exist_ok=True)

            file_size = os.path.getsize(self.input_file)

            self.log_signal.emit(format_gui_log_line("gui", "INFO", "============================================================"))
            self.log_signal.emit(format_gui_log_line("gui", "INFO", "开始解包任务..."))
            self.log_signal.emit(format_gui_log_line("gui", "INFO", f"文件: {self.input_file}"))
            self.log_signal.emit(format_gui_log_line(
                "gui", "INFO",
                f"大小: {file_size} 字节 ({file_size / 1024 / 1024:.2f} MB)"
            ))
            self.log_signal.emit(format_gui_log_line("gui", "INFO", "开始解析 Zstd 容器结构..."))
            self.log_signal.emit(format_gui_log_line("gui", "INFO", "------------------------------------------------------------"))
            self.log_signal.emit(format_gui_log_line("gui", "INFO", "正在扫描 Zstd 帧位置..."))

            with open(self.input_file, "rb") as f:
                data = f.read()
            frame_positions = scan_zstd_frames(data)
            total_frames = len(frame_positions)

            if self._stop:
                self.log_signal.emit(format_gui_log_line("gui", "INFO", "解包已停止（扫描阶段后）。"))
                self.finished_signal.emit(0)
                return

            self.log_signal.emit(format_gui_log_line("gui", "INFO", f"总共找到 {total_frames} 个 Zstd 帧"))
            self.log_signal.emit(format_gui_log_line("gui", "INFO", "开始解压..."))
            self.log_signal.emit(format_gui_log_line("gui", "INFO", "------------------------------------------------------------"))

            if total_frames == 0:
                self.finished_signal.emit(0)
                return

            extracted_hashes = set()
            extracted_count = 0
            self.progress_signal.emit(0, total_frames)

            stop_flag = lambda: self._stop

            if self.fast_mode:
                self.log_signal.emit(format_gui_log_line(
                    "gui", "INFO", f"[快速模式] 使用多线程解压, 线程数={self.max_threads}"
                ))
                with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                    futures = []
                    for i, frame_start in enumerate(frame_positions):
                        if self._stop:
                            break
                        futures.append(
                            executor.submit(
                                extract_single_frame,
                                data,
                                frame_start,
                                self.output_root,
                                i,
                                extracted_hashes,
                                stop_flag,
                                self.enable_md5,
                                self.enable_type_detect,
                            )
                        )
                    for idx, future in enumerate(futures):
                        if self._stop:
                            break
                        ok, msg, info = future.result()
                        self.log_signal.emit(format_gui_log_line("gui.extract", "INFO", msg))
                        if ok and info is not None:
                            extracted_count += 1
                            self.file_signal.emit(info)
                        self.progress_signal.emit(idx + 1, total_frames)
            else:
                self.log_signal.emit(format_gui_log_line("gui", "INFO", "[正常模式] 串行解压"))
                for i, frame_start in enumerate(frame_positions):
                    if self._stop:
                        break
                    ok, msg, info = extract_single_frame(
                        data, frame_start, self.output_root, i,
                        extracted_hashes, stop_flag,
                        self.enable_md5, self.enable_type_detect
                    )
                    self.log_signal.emit(format_gui_log_line("gui.extract", "INFO", msg))
                    if ok and info is not None:
                        extracted_count += 1
                        self.file_signal.emit(info)
                    self.progress_signal.emit(i + 1, total_frames)

            if self._stop:
                self.log_signal.emit(format_gui_log_line("gui", "INFO", "解包已停止。"))
            else:
                self.log_signal.emit(format_gui_log_line("gui", "INFO", "------------------------------------------------------------"))
                self.log_signal.emit(format_gui_log_line(
                    "gui", "INFO", f"解压完成! 共提取 {extracted_count} 个不重复文件"
                ))
            self.finished_signal.emit(extracted_count)

        except Exception as e:
            msg = f"解压过程中发生异常: {str(e)}"
            logger_gui.error(msg)
            self.error_signal.emit(format_gui_log_line("gui", "ERROR", msg))

    def stop(self):
        self._stop = True


# ===================== 设置中心 =====================

class SettingsDialog(QtWidgets.QDialog):
    settings_applied = QtCore.pyqtSignal(dict)

    def __init__(self, parent=None, current_settings=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(800, 500)
        self.current_settings = current_settings or {}
        self.init_ui()
        self.load_from_settings(self.current_settings)

    def init_ui(self):
        main_layout = QtWidgets.QHBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        self.list_categories = QtWidgets.QListWidget()
        self.list_categories.setViewMode(QtWidgets.QListView.ListMode)
        self.list_categories.setIconSize(QtCore.QSize(24, 24))
        self.list_categories.setSpacing(4)
        self.list_categories.setFixedWidth(180)
        self.list_categories.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)

        def make_icon(std_icon):
            icon = self.style().standardIcon(std_icon)
            pix = icon.pixmap(24, 24).scaled(
                24, 24, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
            )
            return QtGui.QIcon(pix)

        def add_category(text, std_icon):
            item = QtWidgets.QListWidgetItem(make_icon(std_icon), text)
            self.list_categories.addItem(item)

        add_category("外观", QtWidgets.QStyle.SP_DesktopIcon)
        add_category("解包", QtWidgets.QStyle.SP_DirOpenIcon)
        add_category("日志", QtWidgets.QStyle.SP_FileDialogDetailedView)
        add_category("记忆", QtWidgets.QStyle.SP_DialogSaveButton)
        add_category("高级", QtWidgets.QStyle.SP_MessageBoxWarning)

        self.stack_pages = QtWidgets.QStackedWidget()

        self.page_appearance = self.create_appearance_page()
        self.page_extract = self.create_extract_page()
        self.page_logging = self.create_logging_page()
        self.page_persistence = self.create_persistence_page()
        self.page_advanced = self.create_advanced_page()

        self.stack_pages.addWidget(self.page_appearance)
        self.stack_pages.addWidget(self.page_extract)
        self.stack_pages.addWidget(self.page_logging)
        self.stack_pages.addWidget(self.page_persistence)
        self.stack_pages.addWidget(self.page_advanced)

        main_layout.addWidget(self.list_categories)
        main_layout.addWidget(self.stack_pages, 1)

        btn_box = QtWidgets.QDialogButtonBox()
        btn_box.setStandardButtons(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel | QtWidgets.QDialogButtonBox.Apply
        )
        btn_box.accepted.connect(self.on_ok)
        btn_box.rejected.connect(self.reject)
        btn_box.button(QtWidgets.QDialogButtonBox.Apply).clicked.connect(self.on_apply)

        dlg_layout = QtWidgets.QVBoxLayout()
        dlg_layout.addLayout(main_layout)
        dlg_layout.addWidget(btn_box)
        self.setLayout(dlg_layout)

        self.list_categories.currentRowChanged.connect(self.stack_pages.setCurrentIndex)
        self.list_categories.setCurrentRow(0)

    def create_card(self, title: str):
        group = QtWidgets.QGroupBox(title)
        group.setStyleSheet("""
            QGroupBox {
                border: 1px solid #3C3C3C;
                border-radius: 8px;
                margin-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 8px;
            }
        """)
        layout = QtWidgets.QFormLayout(group)
        layout.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        layout.setFormAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        layout.setHorizontalSpacing(15)
        layout.setVerticalSpacing(10)
        return group, layout

    def create_appearance_page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        card_font, font_layout = self.create_card("字体")
        self.combo_font = QtWidgets.QFontComboBox()
        self.spin_font_size = QtWidgets.QSpinBox()
        self.spin_font_size.setRange(8, 32)
        self.spin_font_size.setValue(10)
        font_layout.addRow("默认字体:", self.combo_font)
        font_layout.addRow("字号:", self.spin_font_size)

        card_theme, theme_layout = self.create_card("主题与颜色")
        self.combo_theme = QtWidgets.QComboBox()
        self.combo_theme.addItems(["深色", "浅色"])
        theme_layout.addRow("主题:", self.combo_theme)

        layout.addWidget(card_font)
        layout.addWidget(card_theme)
        layout.addStretch()
        return page

    def create_extract_page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        card_threads, threads_layout = self.create_card("线程与模式")
        self.spin_default_threads = QtWidgets.QSpinBox()
        self.spin_default_threads.setRange(1, 64)
        self.spin_default_threads.setValue(8)
        self.chk_default_fast = QtWidgets.QCheckBox("默认启用快速模式 (多线程)")
        threads_layout.addRow("默认线程数:", self.spin_default_threads)
        threads_layout.addRow("", self.chk_default_fast)

        card_output, output_layout = self.create_card("输出目录")
        self.edit_default_output = QtWidgets.QLineEdit()
        self.btn_browse_default_output = QtWidgets.QPushButton("浏览...")
        hl = QtWidgets.QHBoxLayout()
        hl.addWidget(self.edit_default_output)
        hl.addWidget(self.btn_browse_default_output)
        output_layout.addRow("默认输出目录:", hl)

        layout.addWidget(card_threads)
        layout.addWidget(card_output)
        layout.addStretch()

        self.btn_browse_default_output.clicked.connect(self.choose_default_output_dir)
        return page

    def create_logging_page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        card_log, log_layout = self.create_card("日志配置")
        self.combo_log_level = QtWidgets.QComboBox()
        self.combo_log_level.addItems(["INFO", "DEBUG"])
        self.chk_log_to_file = QtWidgets.QCheckBox("启用日志文件写入")
        self.edit_log_dir = QtWidgets.QLineEdit()
        self.btn_browse_log_dir = QtWidgets.QPushButton("浏览...")
        hl = QtWidgets.QHBoxLayout()
        hl.addWidget(self.edit_log_dir)
        hl.addWidget(self.btn_browse_log_dir)
        self.chk_show_program_log_in_gui = QtWidgets.QCheckBox("在界面中显示程序日志")
        self.chk_show_extract_log_in_gui = QtWidgets.QCheckBox("在界面中显示提取日志")
        self.chk_show_program_log_in_gui.setChecked(True)
        self.chk_show_extract_log_in_gui.setChecked(True)

        log_layout.addRow("日志级别:", self.combo_log_level)
        log_layout.addRow("", self.chk_log_to_file)
        log_layout.addRow("日志目录:", hl)
        log_layout.addRow("", self.chk_show_program_log_in_gui)
        log_layout.addRow("", self.chk_show_extract_log_in_gui)

        layout.addWidget(card_log)
        layout.addStretch()

        self.btn_browse_log_dir.clicked.connect(self.choose_log_dir)
        return page

    def create_persistence_page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        card_mem, mem_layout = self.create_card("记忆设置")
        self.chk_remember_last_input = QtWidgets.QCheckBox("记住上次输入文件")
        self.chk_remember_last_output = QtWidgets.QCheckBox("记住上次输出目录")
        self.chk_remember_window = QtWidgets.QCheckBox("记住窗口大小与位置")
        self.chk_remember_theme_font = QtWidgets.QCheckBox("记住主题和字体")
        self.chk_remember_last_input.setChecked(True)
        self.chk_remember_last_output.setChecked(True)
        self.chk_remember_window.setChecked(True)
        self.chk_remember_theme_font.setChecked(True)

        mem_layout.addRow("", self.chk_remember_last_input)
        mem_layout.addRow("", self.chk_remember_last_output)
        mem_layout.addRow("", self.chk_remember_window)
        mem_layout.addRow("", self.chk_remember_theme_font)

        layout.addWidget(card_mem)
        layout.addStretch()
        return page

    def create_advanced_page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        card_adv, adv_layout = self.create_card("高级选项")
        self.chk_enable_md5 = QtWidgets.QCheckBox("启用 MD5 去重")
        self.chk_enable_type_detect = QtWidgets.QCheckBox("启用文件类型自动识别")
        self.chk_enable_crash_log = QtWidgets.QCheckBox("启用崩溃日志（占位）")
        self.chk_enable_md5.setChecked(True)
        self.chk_enable_type_detect.setChecked(True)

        adv_layout.addRow("", self.chk_enable_md5)
        adv_layout.addRow("", self.chk_enable_type_detect)
        adv_layout.addRow("", self.chk_enable_crash_log)

        layout.addWidget(card_adv)
        layout.addStretch()
        return page

    def choose_default_output_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择默认输出目录", "")
        if path:
            self.edit_default_output.setText(path)

    def choose_log_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择日志目录", "")
        if path:
            self.edit_log_dir.setText(path)

    def collect_settings(self) -> dict:
        s = {}
        s["font_family"] = self.combo_font.currentFont().family()
        s["font_size"] = self.spin_font_size.value()
        s["theme"] = "dark" if self.combo_theme.currentIndex() == 0 else "light"
        s["default_threads"] = self.spin_default_threads.value()
        s["default_fast"] = self.chk_default_fast.isChecked()
        s["default_output_dir"] = self.edit_default_output.text().strip()
        s["log_level"] = self.combo_log_level.currentText()
        s["log_to_file"] = self.chk_log_to_file.isChecked()
        s["log_dir"] = self.edit_log_dir.text().strip()
        s["show_program_log_in_gui"] = self.chk_show_program_log_in_gui.isChecked()
        s["show_extract_log_in_gui"] = self.chk_show_extract_log_in_gui.isChecked()
        s["remember_last_input"] = self.chk_remember_last_input.isChecked()
        s["remember_last_output"] = self.chk_remember_last_output.isChecked()
        s["remember_window"] = self.chk_remember_window.isChecked()
        s["remember_theme_font"] = self.chk_remember_theme_font.isChecked()
        s["enable_md5"] = self.chk_enable_md5.isChecked()
        s["enable_type_detect"] = self.chk_enable_type_detect.isChecked()
        s["enable_crash_log"] = self.chk_enable_crash_log.isChecked()
        return s

    def load_from_settings(self, s: dict):
        font_family = s.get("font_family", "微软雅黑")
        font_size = s.get("font_size", 10)
        theme = s.get("theme", "dark")
        index_font = self.combo_font.findText(font_family, QtCore.Qt.MatchExactly)
        if index_font >= 0:
            self.combo_font.setCurrentIndex(index_font)
        self.spin_font_size.setValue(font_size)
        self.combo_theme.setCurrentIndex(0 if theme == "dark" else 1)
        self.spin_default_threads.setValue(s.get("default_threads", 8))
        self.chk_default_fast.setChecked(s.get("default_fast", True))
        self.edit_default_output.setText(s.get("default_output_dir", ""))

        log_level = s.get("log_level", "INFO")
        idx_level = self.combo_log_level.findText(log_level)
        if idx_level >= 0:
            self.combo_log_level.setCurrentIndex(idx_level)
        self.chk_log_to_file.setChecked(s.get("log_to_file", False))
        self.edit_log_dir.setText(s.get("log_dir", ""))

        self.chk_show_program_log_in_gui.setChecked(s.get("show_program_log_in_gui", True))
        self.chk_show_extract_log_in_gui.setChecked(s.get("show_extract_log_in_gui", True))

        self.chk_remember_last_input.setChecked(s.get("remember_last_input", True))
        self.chk_remember_last_output.setChecked(s.get("remember_last_output", True))
        self.chk_remember_window.setChecked(s.get("remember_window", True))
        self.chk_remember_theme_font.setChecked(s.get("remember_theme_font", True))

        self.chk_enable_md5.setChecked(s.get("enable_md5", True))
        self.chk_enable_type_detect.setChecked(s.get("enable_type_detect", True))
        self.chk_enable_crash_log.setChecked(s.get("enable_crash_log", False))

    def on_apply(self):
        s = self.collect_settings()
        self.settings_applied.emit(s)

    def on_ok(self):
        s = self.collect_settings()
        self.settings_applied.emit(s)
        self.accept()


# ===================== 主窗口 =====================

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NpkUnlock_GUI")
        self.resize(1100, 700)
        self.setAcceptDrops(True)

        self.all_files = []
        self.worker_thread = None
        self.worker = None

        self.settings = QtCore.QSettings("XuanQian", "NeoNpkExtractor")
        self.app_settings = self.load_settings()

        self.create_actions()
        self.create_menus()
        self.create_central_widgets()

        self.apply_theme(self.app_settings.get("theme", "dark"))
        self.apply_font(self.app_settings.get("font_family", "微软雅黑"),
                        self.app_settings.get("font_size", 10))
        self.update_widget_colors_for_theme()
        self.refresh_table_item_colors()
        self.load_window_state()

        if self.app_settings.get("show_program_log_in_gui", True):
            self.append_log(format_gui_log_line("gui", "INFO", "Starting NpkUnlock_GUI in GUI mode..."))

        self.apply_default_params()

        if self.app_settings.get("log_to_file", False):
            ok, msg = set_file_logging(True, self.app_settings.get("log_dir", ""))
            if not ok:
                QtWidgets.QMessageBox.warning(self, "日志", f"启用日志文件失败：{msg}")

    # ---- 设置持久化 ----

    def load_settings(self) -> dict:
        s = {}
        v = self.settings.value
        s["font_family"] = v("font_family", "微软雅黑")
        s["font_size"] = int(v("font_size", 10))
        s["theme"] = v("theme", "dark")
        s["default_threads"] = int(v("default_threads", 8))
        s["default_fast"] = v("default_fast", "true") == "true"
        s["default_output_dir"] = v("default_output_dir", "")
        s["log_level"] = v("log_level", "INFO")
        s["log_to_file"] = v("log_to_file", "false") == "true"
        s["log_dir"] = v("log_dir", "")
        s["show_program_log_in_gui"] = v("show_program_log_in_gui", "true") == "true"
        s["show_extract_log_in_gui"] = v("show_extract_log_in_gui", "true") == "true"
        s["remember_last_input"] = v("remember_last_input", "true") == "true"
        s["remember_last_output"] = v("remember_last_output", "true") == "true"
        s["remember_window"] = v("remember_window", "true") == "true"
        s["remember_theme_font"] = v("remember_theme_font", "true") == "true"
        s["enable_md5"] = v("enable_md5", "true") == "true"
        s["enable_type_detect"] = v("enable_type_detect", "true") == "true"
        s["enable_crash_log"] = v("enable_crash_log", "false") == "true"
        s["last_input"] = v("last_input", "")
        s["last_output"] = v("last_output", "")
        return s

    def save_settings(self):
        s = self.app_settings
        w = self.settings.setValue
        w("font_family", s.get("font_family", "微软雅黑"))
        w("font_size", s.get("font_size", 10))
        w("theme", s.get("theme", "dark"))
        w("default_threads", s.get("default_threads", 8))
        w("default_fast", "true" if s.get("default_fast", True) else "false")
        w("default_output_dir", s.get("default_output_dir", ""))
        w("log_level", s.get("log_level", "INFO"))
        w("log_to_file", "true" if s.get("log_to_file", False) else "false")
        w("log_dir", s.get("log_dir", ""))
        w("show_program_log_in_gui", "true" if s.get("show_program_log_in_gui", True) else "false")
        w("show_extract_log_in_gui", "true" if s.get("show_extract_log_in_gui", True) else "false")
        w("remember_last_input", "true" if s.get("remember_last_input", True) else "false")
        w("remember_last_output", "true" if s.get("remember_last_output", True) else "false")
        w("remember_window", "true" if s.get("remember_window", True) else "false")
        w("remember_theme_font", "true" if s.get("remember_theme_font", True) else "false")
        w("enable_md5", "true" if s.get("enable_md5", True) else "false")
        w("enable_type_detect", "true" if s.get("enable_type_detect", True) else "false")
        w("enable_crash_log", "true" if s.get("enable_crash_log", False) else "false")
        w("last_input", s.get("last_input", ""))
        w("last_output", s.get("last_output", ""))

    def load_window_state(self):
        if self.app_settings.get("remember_window", True):
            geo = self.settings.value("window_geometry")
            state = self.settings.value("window_state")
            if geo is not None:
                self.restoreGeometry(geo)
            if state is not None:
                self.restoreState(state)

    def closeEvent(self, event: QtGui.QCloseEvent):
        if self.worker is not None:
            self.worker.stop()
        if self.worker_thread is not None and self.worker_thread.isRunning():
            self.worker_thread.quit()
            self.worker_thread.wait(5000)

        if self.app_settings.get("remember_window", True):
            self.settings.setValue("window_geometry", self.saveGeometry())
            self.settings.setValue("window_state", self.saveState())
        if self.app_settings.get("remember_last_input", True):
            self.app_settings["last_input"] = self.edit_input.text().strip()
        if self.app_settings.get("remember_last_output", True):
            self.app_settings["last_output"] = self.edit_output.text().strip()
        self.save_settings()
        super().closeEvent(event)

    # ---- 主题 & 字体 ----

    def apply_theme(self, theme: str):
        if theme == "dark":
            qss = """
            QMainWindow {
                background-color: #252526;
            }
            QWidget {
                background-color: #252526;
                color: #CCCCCC;
                font-family: "Microsoft YaHei";
                font-size: 10pt;
            }
            QGroupBox {
                border: 1px solid #3C3C3C;
                border-radius: 6px;
                margin-top: 8px;
                color: #CCCCCC;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 4px;
                color: #DDDDDD;
            }
            QMenuBar {
                background-color: #2D2D30;
                color: #CCCCCC;
            }
            QMenuBar::item:selected {
                background-color: #3E3E40;
                color: #FFFFFF;
            }
            QMenu {
                background-color: #2D2D30;
                color: #CCCCCC;
            }
            QMenu::item:selected {
                background-color: #3E3E40;
                color: #FFFFFF;
            }
            QLineEdit, QComboBox, QSpinBox {
                background-color: #1E1E1E;
                border: 1px solid #3C3C3C;
                padding: 2px;
                selection-background-color: #264F78;
                color: #CCCCCC;
            }
            QComboBox QAbstractItemView {
                background-color: #252526;
                selection-background-color: #094771;
                color: #CCCCCC;
            }
            QCheckBox {
                spacing: 4px;
                color: #CCCCCC;
            }
            QPushButton {
                background-color: #0E639C;
                border: 1px solid #0E639C;
                padding: 4px 8px;
                border-radius: 4px;
                color: #FFFFFF;
            }
            QPushButton:hover {
                background-color: #1177BB;
            }
            QPushButton:disabled {
                background-color: #3C3C3C;
                border-color: #3C3C3C;
                color: #777777;
            }
            QPlainTextEdit, QTextEdit {
                background-color: #1E1E1E;
                border: 1px solid #3C3C3C;
                color: #CCCCCC;
            }
            QTableWidget {
                background-color: #1E1E1E;
                gridline-color: #3C3C3C;
                color: #DDDDDD;
            }
            QHeaderView::section {
                background-color: #333337;
                color: #CCCCCC;
                padding: 4px;
                border: 1px solid #3C3C3C;
            }
            QScrollBar:vertical, QScrollBar:horizontal {
                background-color: #252526;
            }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
                background-color: #3E3E42;
                min-height: 20px;
            }
            QProgressBar {
                background-color: #1E1E1E;
                border: 1px solid #3C3C3C;
                border-radius: 2px;
                text-align: center;
                color: #CCCCCC;
            }
            QProgressBar::chunk {
                background-color: #0E639C;
            }
            QListWidget {
                background-color: #252526;
                border: 1px solid #3C3C3C;
                color: #CCCCCC;
            }
            QListWidget::item:selected {
                background-color: #094771;
            }
            """
        else:
            qss = """
            QMainWindow {
                background-color: #F3F3F3;
            }
            QWidget {
                background-color: #F3F3F3;
                color: #000000;
                font-family: "Microsoft YaHei";
                font-size: 10pt;
            }
            QGroupBox {
                border: 1px solid #C0C0C0;
                border-radius: 6px;
                margin-top: 8px;
                color: #000000;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 4px;
                color: #333333;
            }
            QMenuBar {
                background-color: #E6E6E6;
                color: #000000;
            }
            QMenuBar::item:selected {
                background-color: #D0D0D0;
                color: #000000;
            }
            QMenu {
                background-color: #FFFFFF;
                color: #000000;
            }
            QMenu::item:selected {
                background-color: #D0D0D0;
                color: #000000;
            }
            QLineEdit, QComboBox, QSpinBox {
                background-color: #FFFFFF;
                border: 1px solid #C0C0C0;
                padding: 2px;
                selection-background-color: #0078D7;
                color: #000000;
            }
            QComboBox QAbstractItemView {
                background-color: #FFFFFF;
                selection-background-color: #0078D7;
                color: #000000;
            }
            QCheckBox {
                spacing: 4px;
                color: #000000;
            }
            QPushButton {
                background-color: #0078D7;
                border: 1px solid #0078D7;
                padding: 4px 8px;
                border-radius: 4px;
                color: #FFFFFF;
            }
            QPushButton:hover {
                background-color: #1A86E2;
            }
            QPushButton:disabled {
                background-color: #CCCCCC;
                border-color: #CCCCCC;
                color: #777777;
            }
            QPlainTextEdit, QTextEdit {
                background-color: #FFFFFF;
                border: 1px solid #C0C0C0;
                color: #000000;
            }
            QTableWidget {
                background-color: #FFFFFF;
                gridline-color: #C0C0C0;
                color: #000000;
            }
            QHeaderView::section {
                background-color: #E0E0E0;
                color: #000000;
                padding: 4px;
                border: 1px solid #C0C0C0;
            }
            QScrollBar:vertical, QScrollBar:horizontal {
                background-color: #F3F3F3;
            }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
                background-color: #C0C0C0;
                min-height: 20px;
            }
            QProgressBar {
                background-color: #FFFFFF;
                border: 1px solid #C0C0C0;
                border-radius: 2px;
                text-align: center;
                color: #000000;
            }
            QProgressBar::chunk {
                background-color: #0078D7;
            }
            QListWidget {
                background-color: #FFFFFF;
                border: 1px solid #C0C0C0;
                color: #000000;
            }
            QListWidget::item:selected {
                background-color: #0078D7;
                color: #FFFFFF;
            }
            """
        self.setStyleSheet(qss)
        self.app_settings["theme"] = theme

    def apply_font(self, family: str, size: int):
        font = QtGui.QFont(family)
        font.setPointSize(size)
        QtWidgets.QApplication.instance().setFont(font)
        self.app_settings["font_family"] = family
        self.app_settings["font_size"] = size

    def update_widget_colors_for_theme(self):
        dark = self.app_settings.get("theme", "dark") == "dark"
        text_color = QtGui.QColor("#DDDDDD" if dark else "#000000")

        for w in [getattr(self, "edit_search", None),
                  getattr(self, "edit_input", None),
                  getattr(self, "edit_output", None)]:
            if isinstance(w, QtWidgets.QLineEdit):
                pal = w.palette()
                pal.setColor(QtGui.QPalette.Text, text_color)
                w.setPalette(pal)

        if isinstance(getattr(self, "text_log", None), QtWidgets.QPlainTextEdit):
            pal = self.text_log.palette()
            pal.setColor(QtGui.QPalette.Text, text_color)
            self.text_log.setPalette(pal)

        if isinstance(getattr(self, "table_files", None), QtWidgets.QTableWidget):
            pal = self.table_files.palette()
            pal.setColor(QtGui.QPalette.Text, text_color)
            self.table_files.setPalette(pal)

    def refresh_table_item_colors(self):
        if not hasattr(self, "table_files") or self.table_files is None:
            return
        dark = self.app_settings.get("theme", "dark") == "dark"
        fg = QtGui.QColor("#DDDDDD" if dark else "#000000")
        rows = self.table_files.rowCount()
        cols = self.table_files.columnCount()
        for r in range(rows):
            for c in range(cols):
                item = self.table_files.item(r, c)
                if item is not None:
                    item.setForeground(QtGui.QBrush(fg))

    # ---- Actions & Menus ----

    def create_actions(self):
        self.act_open_file = QtWidgets.QAction("打开文件...", self)
        self.act_open_file.triggered.connect(self.browse_input_file)

        self.act_clear_list = QtWidgets.QAction("清空文件列表", self)
        self.act_clear_list.triggered.connect(self.clear_file_list)

        self.act_exit = QtWidgets.QAction("退出", self)
        self.act_exit.triggered.connect(self.close)

        self.act_about = QtWidgets.QAction("关于", self)
        self.act_about.triggered.connect(self.show_about)

        self.act_settings = QtWidgets.QAction("设置", self)
        self.act_settings.triggered.connect(self.open_settings_dialog)

    def create_menus(self):
        menubar = self.menuBar()
        menu_file = menubar.addMenu("文件")
        menu_file.addAction(self.act_open_file)
        menu_file.addAction(self.act_clear_list)
        menu_file.addSeparator()
        menu_file.addAction(self.act_exit)

        menubar.addAction(self.act_settings)

        menubar.addMenu("工具")  # 预留，后续加功能

        menu_about = menubar.addMenu("关于")
        menu_about.addAction(self.act_about)

    # ---- 中央布局 ----

    def create_central_widgets(self):
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        main_layout = QtWidgets.QHBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        left_panel = QtWidgets.QWidget()
        left_panel.setFixedWidth(360)
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setSpacing(8)

        group_config = QtWidgets.QGroupBox("当前配置")
        cfg_layout = QtWidgets.QHBoxLayout(group_config)
        self.combo_config = QtWidgets.QComboBox()
        self.combo_config.addItem("通用")
        cfg_layout.addWidget(self.combo_config)

        group_filter = QtWidgets.QGroupBox("过滤器")
        filter_layout = QtWidgets.QVBoxLayout(group_filter)

        self.edit_search = QtWidgets.QLineEdit()
        self.edit_search.setPlaceholderText("按文件名搜索...")
        self.edit_search.textChanged.connect(self.apply_filters)

        tag_layout = FlowLayout(spacing=6)

        def make_tag_button(text):
            btn = QtWidgets.QPushButton(text)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setMinimumWidth(70)
            btn.setStyleSheet("""
            QPushButton {
                background-color: #2D2D30;
                color: #CCCCCC;
                border: 1px solid #3C3C3C;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QPushButton:checked {
                background-color: #0E639C;
                color: #FFFFFF;
            }
            """)
            return btn

        self.btn_filter_audio = make_tag_button("音频文件")
        self.btn_filter_image = make_tag_button("图片文件")
        self.btn_filter_mesh = make_tag_button("模型文件")
        self.btn_filter_data = make_tag_button("数据文件")
        self.btn_filter_zst = make_tag_button("压缩文件")
        self.btn_filter_unknown = make_tag_button("未知文件")

        for btn in [
            self.btn_filter_audio,
            self.btn_filter_image,
            self.btn_filter_mesh,
            self.btn_filter_data,
            self.btn_filter_zst,
            self.btn_filter_unknown,
        ]:
            btn.clicked.connect(self.apply_filters)
            tag_layout.addWidget(btn)

        filter_layout.addWidget(self.edit_search)
        filter_layout.addLayout(tag_layout)

        group_input = QtWidgets.QGroupBox("输入 / 输出")
        io_layout = QtWidgets.QFormLayout(group_input)
        self.edit_input = QtWidgets.QLineEdit()
        self.edit_input.setPlaceholderText("拖拽文件到窗口或点击浏览选择...")
        btn_browse_in = QtWidgets.QPushButton("浏览...")
        btn_browse_in.clicked.connect(self.browse_input_file)
        in_layout = QtWidgets.QHBoxLayout()
        in_layout.addWidget(self.edit_input)
        in_layout.addWidget(btn_browse_in)
        self.edit_output = QtWidgets.QLineEdit()
        self.edit_output.setPlaceholderText("默认: 输入文件同级目录/Output")
        btn_browse_out = QtWidgets.QPushButton("浏览...")
        btn_browse_out.clicked.connect(self.browse_output_folder)
        out_layout = QtWidgets.QHBoxLayout()
        out_layout.addWidget(self.edit_output)
        out_layout.addWidget(btn_browse_out)
        io_layout.addRow("输入文件:", in_layout)
        io_layout.addRow("输出目录:", out_layout)

        group_options = QtWidgets.QGroupBox("解包选项")
        opt_layout = QtWidgets.QGridLayout(group_options)
        self.chk_fast_mode = QtWidgets.QCheckBox("快速模式 (多线程)")
        self.spin_threads = QtWidgets.QSpinBox()
        self.spin_threads.setRange(1, 64)
        opt_layout.addWidget(self.chk_fast_mode, 0, 0, 1, 2)
        opt_layout.addWidget(QtWidgets.QLabel("线程数:"), 1, 0)
        opt_layout.addWidget(self.spin_threads, 1, 1)

        group_run = QtWidgets.QGroupBox("执行")
        run_layout = QtWidgets.QHBoxLayout(group_run)
        self.btn_start = QtWidgets.QPushButton("开始解包")
        self.btn_start.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaPlay))
        self.btn_start.clicked.connect(self.start_extract)
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_BrowserStop))
        self.btn_stop.clicked.connect(self.stop_extract)
        self.btn_stop.setEnabled(False)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        run_layout.addWidget(self.btn_start)
        run_layout.addWidget(self.btn_stop)
        run_layout.addWidget(self.progress_bar)

        left_layout.addWidget(group_config)
        left_layout.addWidget(group_filter)
        left_layout.addWidget(group_input)
        left_layout.addWidget(group_options)
        left_layout.addWidget(group_run)
        left_layout.addStretch()

        right_panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        group_files = QtWidgets.QGroupBox("已提取文件")
        files_layout = QtWidgets.QVBoxLayout(group_files)
        self.table_files = QtWidgets.QTableWidget()
        self.table_files.setColumnCount(5)
        self.table_files.setHorizontalHeaderLabels(
            ["文件名", "扩展名", "类别", "大小", "路径"]
        )
        self.table_files.horizontalHeader().setStretchLastSection(True)
        self.table_files.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table_files.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table_files.setAlternatingRowColors(False)
        self.table_files.doubleClicked.connect(self.open_file_location)
        self.table_files.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table_files.customContextMenuRequested.connect(self.show_file_context_menu)
        files_layout.addWidget(self.table_files)

        group_log = QtWidgets.QGroupBox("日志")
        log_layout = QtWidgets.QVBoxLayout(group_log)
        self.text_log = QtWidgets.QPlainTextEdit()
        self.text_log.setReadOnly(True)
        font = QtGui.QFont(self.app_settings.get("font_family", "Microsoft YaHei"))
        font.setPointSize(self.app_settings.get("font_size", 10))
        self.text_log.setFont(font)
        log_layout.addWidget(self.text_log)

        splitter.addWidget(group_files)
        splitter.addWidget(group_log)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        right_layout.addWidget(splitter)

        main_layout.addWidget(left_panel, 0)
        main_layout.addWidget(right_panel, 1)

        if self.app_settings.get("remember_last_input", True):
            last_input = self.app_settings.get("last_input", "")
            if last_input:
                self.edit_input.setText(last_input)
        if self.app_settings.get("remember_last_output", True):
            last_output = self.app_settings.get("last_output", "")
            if last_output:
                self.edit_output.setText(last_output)

    def apply_default_params(self):
        self.spin_threads.setValue(self.app_settings.get("default_threads", 8))
        self.chk_fast_mode.setChecked(self.app_settings.get("default_fast", True))

    # ---- 日志 & 文件列表 ----

    def append_log(self, text: str):
        self.text_log.appendPlainText(text)
        sb = self.text_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear_file_list(self):
        self.all_files.clear()
        self.table_files.setRowCount(0)

    def format_size(self, size: int) -> str:
        if size < 1024 * 1024:
            return f"{size / 1024:.2f} KB"
        else:
            return f"{size / (1024 * 1024):.2f} MB"

    def add_file_to_list(self, info: dict):
        self.all_files.append(info)
        self.apply_filters()

    def apply_filters(self):
        search_text = self.edit_search.text().strip().lower()

        enabled_categories = set()
        if self.btn_filter_audio.isChecked():
            enabled_categories.add("普通文件")  # 音频按钮 → 普通文件
        if self.btn_filter_image.isChecked():
            enabled_categories.add("图片文件")
        if self.btn_filter_mesh.isChecked():
            enabled_categories.add("模型文件")
        if self.btn_filter_data.isChecked():
            enabled_categories.add("数据文件")
        if self.btn_filter_zst.isChecked():
            enabled_categories.add("压缩文件")
        if self.btn_filter_unknown.isChecked():
            enabled_categories.add("未知文件")

        self.table_files.setRowCount(0)
        dark = self.app_settings.get("theme", "dark") == "dark"
        fg_color = "#DDDDDD" if dark else "#000000"

        for info in self.all_files:
            name = info.get("name", "")
            category = info.get("category", "未知文件")
            if category not in enabled_categories:
                continue
            if search_text and search_text not in name.lower():
                continue
            row = self.table_files.rowCount()
            self.table_files.insertRow(row)

            def _item(text):
                it = QtWidgets.QTableWidgetItem(str(text))
                it.setForeground(QtGui.QBrush(QtGui.QColor(fg_color)))
                return it

            size_val = info.get("size", 0)
            size_text = self.format_size(size_val)

            category_display = "音频文件" if category == "普通文件" else category

            self.table_files.setItem(row, 0, _item(name))
            self.table_files.setItem(row, 1, _item(info.get("ext", "")))
            self.table_files.setItem(row, 2, _item(category_display))
            self.table_files.setItem(row, 3, _item(size_text))
            self.table_files.setItem(row, 4, _item(info.get("path", "")))

        self.table_files.resizeColumnsToContents()

    def get_selected_file_paths(self):
        rows = sorted(set(idx.row() for idx in self.table_files.selectedIndexes()))
        paths = []
        for r in rows:
            item = self.table_files.item(r, 4)
            if item:
                p = item.text()
                if p:
                    paths.append(p)
        return rows, paths

    def show_file_context_menu(self, pos: QtCore.QPoint):
        rows, paths = self.get_selected_file_paths()
        if not paths:
            return
        menu = QtWidgets.QMenu(self)

        act_open_file = QtWidgets.QAction("打开文件", self)
        act_open_dir = QtWidgets.QAction("打开所在目录", self)
        act_extract_to = QtWidgets.QAction("提取文件到...", self)
        act_copy_path = QtWidgets.QAction("复制路径", self)

        if len(paths) > 1:
            act_open_file.setEnabled(False)

        act_open_file.triggered.connect(lambda: self.context_open_file(paths[0]) if paths else None)
        act_open_dir.triggered.connect(lambda: self.context_open_dir(paths))
        act_extract_to.triggered.connect(lambda: self.context_extract_files(paths))
        act_copy_path.triggered.connect(lambda: self.context_copy_paths(paths))

        menu.addAction(act_open_file)
        menu.addAction(act_open_dir)
        menu.addSeparator()
        menu.addAction(act_extract_to)
        menu.addSeparator()
        menu.addAction(act_copy_path)

        menu.exec_(self.table_files.viewport().mapToGlobal(pos))

    def context_open_file(self, path: str):
        if not os.path.exists(path):
            QtWidgets.QMessageBox.warning(self, "提示", f"文件不存在:\n{path}")
            return
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')

    def context_open_dir(self, paths):
        opened = set()
        for p in paths:
            if not os.path.exists(p):
                continue
            folder = os.path.dirname(p)
            if folder in opened:
                continue
            opened.add(folder)
            if sys.platform.startswith("win"):
                norm = os.path.normpath(p)
                os.system(f'explorer /select,"{norm}"')
            elif sys.platform == "darwin":
                os.system(f'open -R "{p}"')
            else:
                os.system(f'xdg-open "{folder}"')

    def context_extract_files(self, paths):
        target_dir = QtWidgets.QFileDialog.getExistingDirectory(self, "选择提取目标目录", "")
        if not target_dir:
            return
        for p in paths:
            if not os.path.exists(p):
                continue
            try:
                shutil.copy2(p, target_dir)
            except Exception as e:
                logger_gui.error(f"复制文件失败: {p} -> {target_dir}, 错误: {e}")
        QtWidgets.QMessageBox.information(self, "完成", f"已提取 {len(paths)} 个文件到:\n{target_dir}")

    def context_copy_paths(self, paths):
        cb = QtWidgets.QApplication.clipboard()
        cb.setText("\n".join(paths))

    def open_file_location(self):
        row = self.table_files.currentRow()
        if row < 0:
            return
        path_item = self.table_files.item(row, 4)
        if not path_item:
            return
        path = path_item.text()
        if not os.path.exists(path):
            QtWidgets.QMessageBox.warning(self, "提示", f"文件不存在:\n{path}")
            return
        if sys.platform.startswith("win"):
            norm = os.path.normpath(path)
            os.system(f'explorer /select,"{norm}"')
        elif sys.platform == "darwin":
            os.system(f'open -R "{path}"')
        else:
            folder = os.path.dirname(path)
            os.system(f'xdg-open "{folder}"')

    # ---- 解包流程 ----

    def browse_input_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择 NPK / Zstd 文件",
            "",
            "所有文件 (*);;NPK / Zstd 文件 (*.npk *.zst *.bin)",
        )
        if path:
            self.set_input_file(path)

    def set_input_file(self, path: str):
        self.edit_input.setText(path)
        if not self.edit_output.text().strip():
            base_dir = os.path.dirname(path)
            default_output = os.path.join(base_dir, "Output")
            self.edit_output.setText(default_output)

    def browse_output_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择输出目录", ""
        )
        if path:
            self.edit_output.setText(path)

    def start_extract(self):
        input_file = self.edit_input.text().strip()
        output_root = self.edit_output.text().strip()
        fast_mode = self.chk_fast_mode.isChecked()
        max_threads = self.spin_threads.value()
        if not input_file:
            QtWidgets.QMessageBox.warning(self, "提示", "请选择输入文件")
            return
        if not os.path.isfile(input_file):
            QtWidgets.QMessageBox.critical(self, "错误", f"输入文件不存在:\n{input_file}")
            return
        if not output_root:
            base_dir = os.path.dirname(input_file)
            output_root = os.path.join(base_dir, "Output")
            self.edit_output.setText(output_root)

        if self.app_settings.get("remember_last_input", True):
            self.app_settings["last_input"] = input_file
        if self.app_settings.get("remember_last_output", True):
            self.app_settings["last_output"] = output_root

        self.progress_bar.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

        enable_md5 = self.app_settings.get("enable_md5", True)
        enable_type_detect = self.app_settings.get("enable_type_detect", True)

        self.worker_thread = QtCore.QThread()
        self.worker = ExtractWorker(
            input_file, output_root, fast_mode, max_threads,
            enable_md5=enable_md5, enable_type_detect=enable_type_detect
        )
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.log_signal.connect(self.on_extract_log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.file_signal.connect(self.add_file_to_list)
        self.worker.finished_signal.connect(self.extract_finished)
        self.worker.error_signal.connect(self.extract_error)
        self.worker.finished_signal.connect(self.worker_thread.quit)
        self.worker.error_signal.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.on_thread_finished)
        self.worker_thread.start()

    def stop_extract(self):
        if self.worker is not None:
            self.worker.stop()
            self.append_log(format_gui_log_line("gui", "INFO", "正在请求停止解包..."))
        self.btn_stop.setEnabled(False)

    @QtCore.pyqtSlot()
    def on_thread_finished(self):
        if self.worker_thread is not None:
            self.worker_thread.wait()
        self.worker = None
        self.worker_thread = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    @QtCore.pyqtSlot(str)
    def on_extract_log(self, msg: str):
        if self.app_settings.get("show_extract_log_in_gui", True):
            self.append_log(msg)

    @QtCore.pyqtSlot(int, int)
    def update_progress(self, current: int, total: int):
        if total <= 0:
            self.progress_bar.setValue(0)
            return
        value = int(current * 100 / total)
        self.progress_bar.setValue(value)

    @QtCore.pyqtSlot(int)
    def extract_finished(self, count: int):
        msg = f"任务完成，共提取 {count} 个不重复文件。"
        if self.app_settings.get("show_program_log_in_gui", True):
            self.append_log(format_gui_log_line("gui", "INFO", msg))
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        QtWidgets.QMessageBox.information(
            self, "完成", f"解包完成！\n共提取 {count} 个不重复文件。"
        )

    @QtCore.pyqtSlot(str)
    def extract_error(self, msg: str):
        if self.app_settings.get("show_program_log_in_gui", True):
            self.append_log(msg)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        QtWidgets.QMessageBox.critical(self, "错误", msg)

    # ---- 其他 ----

    def show_about(self):
        QtWidgets.QMessageBox.information(
            self,
            "关于",
            "NpkUnlock_GUI\n\n"
            "Zstd 多帧解包 / 自动分类 / MD5 去重 / 多线程支持\n",
        )

    def open_settings_dialog(self):
        dlg = SettingsDialog(self, self.app_settings)
        dlg.settings_applied.connect(self.apply_settings)
        dlg.exec_()

    @QtCore.pyqtSlot(dict)
    def apply_settings(self, s: dict):
        self.app_settings.update(s)
        self.apply_font(s.get("font_family", "微软雅黑"), s.get("font_size", 10))
        self.apply_theme(s.get("theme", "dark"))
        self.update_widget_colors_for_theme()
        self.refresh_table_item_colors()

        level = s.get("log_level", "INFO")
        logger_gui.setLevel(logging.DEBUG if level == "DEBUG" else logging.INFO)

        log_to_file = s.get("log_to_file", False)
        log_dir = s.get("log_dir", "")
        ok, msg = set_file_logging(log_to_file, log_dir)
        if log_to_file and not ok:
            QtWidgets.QMessageBox.warning(self, "日志", f"启用日志文件失败：{msg}")
        self.save_settings()

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QtGui.QDropEvent):
        urls = event.mimeData().urls()
        if not urls:
            return
        local_path = urls[0].toLocalFile()
        if os.path.isfile(local_path):
            self.set_input_file(local_path)


# ===================== 崩溃报告窗口 =====================

class CrashWindow(QtWidgets.QDialog):
    def __init__(self, log_tail: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NpkUnlock_GUI 崩溃报告")
        self.resize(900, 520)
        self.init_ui(log_tail)

    def init_ui(self, log_tail: str):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QtWidgets.QLabel("主程序已崩溃")
        title_font = QtGui.QFont("Microsoft YaHei", 12, QtGui.QFont.Bold)
        title.setFont(title_font)

        subtitle = QtWidgets.QLabel("以下为最近的错误日志片段，你可以复制后进行分析或汇报。")
        subtitle.setWordWrap(True)

        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        font = QtGui.QFont("Consolas")
        font.setPointSize(10)
        self.text.setFont(font)
        self.text.setPlainText(log_tail)

        self.setStyleSheet("""
        QDialog {
            background-color: #252526;
            color: #CCCCCC;
        }
        QLabel {
            color: #CCCCCC;
        }
        QPlainTextEdit {
            background-color: #1E1E1E;
            border: 1px solid #3C3C3C;
            color: #CCCCCC;
        }
        QPushButton {
            background-color: #0E639C;
            border: 1px solid #0E639C;
            padding: 4px 12px;
            border-radius: 4px;
            color: #FFFFFF;
        }
        QPushButton:hover {
           背景颜色: #1177BB;
        }
        QPushButton:disabled {
            background-color: #3C3C3C;
            border-color: #3C3C3C;
            color: #777777;
        }
        """)

        layout.addWidget(self.text, 1)

        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.addStretch()
        self.btn_copy = QtWidgets.QPushButton("复制到剪贴板")
        self.btn_close = QtWidgets.QPushButton("关闭")

        btn_layout.addWidget(self.btn_copy)
        btn_layout.addWidget(self.btn_close)

        layout.addLayout(btn_layout)

        self.btn_copy.clicked.connect(self.copy_to_clipboard)
        self.btn_close.clicked.connect(self.accept)

    def copy_to_clipboard(self):
        cb = QtWidgets.QApplication.clipboard()
        cb.setText(self.text.toPlainText())


# ===================== 子进程入口 =====================

def run_main_child():
    def exception_hook_child(exctype, value, tb):
        logger_gui.error("未捕获异常", exc_info=(exctype, value, tb))
        sys.__excepthook__(exctype, value, tb)

    sys.excepthook = exception_hook_child

    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling)
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


# ===================== 父进程入口（监控 + 崩溃报告） =====================

def run_launcher_parent():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    python_exe = sys.executable
    cmd = [python_exe, os.path.abspath(__file__), CHILD_ARG]

    proc = subprocess.Popen(
        cmd,
        cwd=base_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
    )

    lines = []

    def reader():
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                print(line)
                lines.append(line)
                if len(lines) > 800:
                    lines[:] = lines[-800:]
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    proc.wait()
    t.join()

    rc = proc.returncode
    if rc == 0:
        return

    tail_count = 200
    tail_lines = lines[-tail_count:]
    log_tail = "\n".join(tail_lines)

    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling)
    app = QtWidgets.QApplication([])
    dlg = CrashWindow(log_tail)
    dlg.exec_()


# ===================== 启动入口 =====================

if __name__ == "__main__":
    if CHILD_ARG in sys.argv:
        run_main_child()
    else:
        run_launcher_parent()
