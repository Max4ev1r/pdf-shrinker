#!/usr/bin/env python3
"""
PDF Shrinker v7 - 苹果风 UI + 自定义输出路径 + Ghostscript 压缩
依赖: Python 3, Ghostscript
"""

import os, sys, shutil, io, subprocess, json, atexit
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
try:
    import winreg
except ImportError:
    winreg = None

# Windows 隐藏命令行窗口
CREATE_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0

MAX_SIZE_MB = 5
MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.pdfshrinkerrc')

# ============ 苹果风配色 ============
BG = "#F5F5F7"
CARD_BG = "#FFFFFF"
ACCENT = "#007AFF"
ACCENT_HOVER = "#0056CC"
TEXT = "#1D1D1F"
TEXT_DIM = "#86868B"
BORDER = "#D2D2D7"
SUCCESS = "#34C759"
WARN = "#FF9500"


def load_output_dir():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                d = json.load(f)
                path = d.get('output_dir', '')
                if path and os.path.isdir(path):
                    return path
    except Exception:
        pass
    # 默认桌面/PDF压缩
    return get_default_output_dir()


def save_output_dir(path):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({'output_dir': path}, f)
    except Exception:
        pass


def get_default_output_dir():
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    folder = os.path.join(desktop, "PDF压缩")
    if not os.path.exists(folder):
        try:
            os.makedirs(folder)
        except Exception:
            return desktop
    return folder


def find_gs():
    """在 Windows 上查找 Ghostscript"""
    # 1. PATH 中直接查找
    for cmd in ['gs', 'gswin64c', 'gswin32c']:
        try:
            r = subprocess.run([cmd, '--version'], capture_output=True, text=True, timeout=5,
                              creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0:
                return cmd
        except Exception:
            continue

    if winreg is None:
        return None

    # 2. Windows 注册表
    reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Ghostscript'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Artifex Software\Ghostscript'),
        (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Ghostscript'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Ghostscript'),
    ]
    for hkey, subkey in reg_paths:
        try:
            key = winreg.OpenKey(hkey, subkey)
            i = 0
            latest_ver = None
            latest_path = None
            while True:
                try:
                    name, value, _ = winreg.EnumKey(key, i)
                    if latest_ver is None or name > latest_ver:
                        latest_ver = name
                        latest_path = value
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(key)
            if latest_path:
                for exe in ['gswin64c.exe', 'gswin64.exe', 'gswin32c.exe']:
                    full = os.path.join(latest_path, 'bin', exe)
                    if os.path.exists(full):
                        return full
        except Exception:
            continue

    # 3. 常见安装目录
    for d in [
        r'C:\Program Files\gs\gs10.70\bin',
        r'C:\Program Files\Ghostscript\gs10.70\bin',
        r'C:\Program Files\gs\bin',
        r'C:\Program Files\Ghostscript\bin',
        r'C:\Program Files (x86)\gs\gs10.70\bin',
    ]:
        for exe in ['gswin64c.exe', 'gswin64.exe', 'gswin32c.exe']:
            full = os.path.join(d, exe)
            if os.path.exists(full):
                return full

    return None


class PDFShrinkerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PDF Shrinker")
        self.root.geometry("520x560")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        self.root.update_idletasks()
        w, h = 520, 560
        x = (self.root.winfo_screenwidth() // 2) - w // 2
        y = (self.root.winfo_screenheight() // 2) - h // 2
        self.root.geometry(f'{w}x{h}+{x}+{y}')

        self.file_path = None
        self.output_dir = load_output_dir()
        self.gs_path = find_gs()
        self._build_ui()

    def _build_ui(self):
        main_bg = tk.Frame(self.root, bg=BG)
        main_bg.pack(fill="both", expand=True)

        # 标题
        header = tk.Frame(main_bg, bg=BG)
        header.pack(pady=(28, 0))
        tk.Label(header, text="PDF Shrinker", font=("SF Pro Display", 28, "bold"),
                 fg=TEXT, bg=BG).pack()
        tk.Label(header, text=f"压缩 PDF 到 {MAX_SIZE_MB}MB 以下",
                 font=("SF Pro Text", 13), fg=TEXT_DIM, bg=BG).pack(pady=(2, 0))

        # GS 状态
        if self.gs_path:
            gs_label = tk.Label(header, text=f"✓ Ghostscript 已就绪",
                               font=("SF Pro Text", 10), fg=SUCCESS, bg=BG)
        else:
            gs_label = tk.Label(header, text="⚠️ Ghostscript 未找到，请先安装",
                               font=("SF Pro Text", 10), fg=WARN, bg=BG)
        gs_label.pack(pady=(4, 0))

        # 主卡片
        card = tk.Frame(main_bg, bg=CARD_BG, bd=1,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(pady=20, padx=32)

        tk.Frame(card, bg=CARD_BG, height=16).pack()

        # 选择文件区域
        self.select_frame = tk.Frame(card, bg="#F0F0F5", bd=1,
                                     highlightbackground=BORDER, highlightthickness=1,
                                     width=440, height=100, cursor="hand2")
        self.select_frame.pack(pady=(0, 10))
        self.select_frame.pack_propagate(False)

        self.icon_label = tk.Label(self.select_frame, text="📎", font=("SF Pro Display", 26),
                                   bg="#F0F0F5", fg=ACCENT)
        self.icon_label.pack(pady=(14, 2))

        self.select_label = tk.Label(self.select_frame, text="点击选择 PDF 文件",
                                      font=("SF Pro Text", 13), bg="#F0F0F5", fg=TEXT)
        self.select_label.pack()

        self.select_hint = tk.Label(self.select_frame, text="支持任何大小的 PDF",
                                    font=("SF Pro Text", 10), bg="#F0F0F5", fg=TEXT_DIM)
        self.select_hint.pack()

        for w in [self.select_frame, self.icon_label, self.select_label, self.select_hint]:
            w.bind("<Button-1>", lambda e: self._browse())
            w.bind("<Enter>", lambda e: self.select_frame.config(bg="#E8E8EF"))
            w.bind("<Leave>", lambda e: self.select_frame.config(bg="#F0F0F5"))

        # 文件信息
        self.info_label = tk.Label(card, text="", font=("SF Pro Text", 11),
                                   fg=TEXT_DIM, bg=CARD_BG)
        self.info_label.pack(pady=(0, 4))

        # 输出路径设置
        path_frame = tk.Frame(card, bg=CARD_BG)
        path_frame.pack(fill="x", padx=4, pady=(0, 8))

        tk.Label(path_frame, text="📁 保存到：", font=("SF Pro Text", 11),
                 fg=TEXT, bg=CARD_BG).pack(side="left")

        # 输出路径显示 + 切换按钮
        self.path_btn = tk.Button(path_frame, text=self._short_path(self.output_dir),
                                  font=("SF Pro Text", 10), fg=ACCENT, bg=CARD_BG,
                                  bd=0, cursor="hand2", relief="flat",
                                  command=self._choose_output_dir)
        self.path_btn.pack(side="left", fill="x", expand=True, anchor="w")

        tk.Label(card, text="", font=("SF Pro Text", 1), bg=CARD_BG, height=1).pack()

        # 压缩按钮
        self.compress_btn = tk.Button(card, text="开始压缩",
                                      font=("SF Pro Display", 15, "bold"),
                                      bg=ACCENT, fg="white", bd=0, padx=40, pady=10,
                                      activebackground=ACCENT_HOVER, activeforeground="white",
                                      state="disabled", cursor="hand2", relief="flat",
                                      command=self._start_compress)
        self.compress_btn.pack()

        # 进度条
        self.progress = ttk.Progressbar(card, mode="indeterminate", length=420,
                                        style="Mac.Horizontal.TProgressbar")
        self.progress.pack(pady=(10, 0))
        s = ttk.Style()
        s.configure("Mac.Horizontal.TProgressbar",
                    background=ACCENT, troughcolor=BORDER, thickness=5)

        # 状态
        self.status_label = tk.Label(card, text="", font=("SF Pro Text", 11),
                                     fg=TEXT_DIM, bg=CARD_BG)
        self.status_label.pack(pady=(8, 14))

    def _short_path(self, path):
        """缩短路径显示"""
        if len(path) <= 40:
            return path
        parts = path.split(os.sep)
        if len(parts) >= 3:
            return parts[0] + os.sep + '...' + os.sep + parts[-2] + os.sep + parts[-1]
        return path

    def _choose_output_dir(self):
        folder = filedialog.askdirectory(title="选择压缩后文件的保存位置",
                                          initialdir=self.output_dir)
        if folder:
            self.output_dir = folder
            save_output_dir(folder)
            self.path_btn.config(text=self._short_path(folder))

    def _browse(self):
        path = filedialog.askopenfilename(title="选择 PDF 文件",
                                          filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        if path:
            self._set_file(path)

    def _set_file(self, path):
        self.file_path = path
        size_mb = os.path.getsize(path) / 1024 / 1024
        name = os.path.basename(path)
        short_name = name if len(name) <= 35 else name[:32] + "..."
        self.info_label.config(text=f"📄 {short_name}  ({size_mb:.1f}MB)")
        self.select_label.config(text=short_name)
        self.select_hint.config(text=f"{size_mb:.1f}MB  ·  点击更换文件")
        self.icon_label.config(text="✓")
        self.compress_btn.config(state="normal", bg=ACCENT)
        self.status_label.config(text="")

    def _start_compress(self):
        if not self.file_path:
            return
        if not self.gs_path:
            messagebox.showwarning("提示", "Ghostscript 未找到\n请先安装 Ghostscript 后重试")
            return
        self.compress_btn.config(state="disabled", bg=ACCENT_HOVER)
        self.progress.start(6)
        self.status_label.config(text="正在压缩...")
        threading.Thread(target=self._compress, daemon=True).start()

    def _compress(self):
        input_path = self.file_path
        base = Path(input_path).stem
        output_path = os.path.join(self.output_dir, f"{base}_compressed.pdf")
        original_size = os.path.getsize(input_path)

        try:
            result_size = self._reduce_pdf(input_path, output_path, original_size)
            self.root.after(0, self._done, input_path, output_path, original_size, result_size)
        except Exception as e:
            self.root.after(0, lambda: [
                self.progress.stop(),
                self.compress_btn.config(state="normal", bg=ACCENT),
                self.status_label.config(text=""),
                messagebox.showerror("压缩失败", str(e))
            ])

    def _reduce_pdf(self, input_path, output_path, original_size):
        if not self.gs_path:
            raise RuntimeError("Ghostscript 未找到")

        # 确保输出目录存在
        out_dir = os.path.dirname(output_path)
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)

        # 从高到低尝试，直到 < 5MB
        for profile, label in [
            ("prepress", "高"),
            ("ebook", "中"),
            ("screen", "低"),
        ]:
            self.root.after(0, lambda l=label: self.status_label.config(text=f"压缩中... ({l}质量)"))

            rc = subprocess.run([
                self.gs_path,
                '-sDEVICE=pdfwrite',
                '-dCompatibilityLevel=1.4',
                f'-dPDFSETTINGS=/{profile}',
                '-dNOPAUSE',
                '-dQUIET',
                '-dBATCH',
                f'-sOutputFile={output_path}',
                input_path
            ], capture_output=True, text=True, timeout=300,
               creationflags=CREATE_NO_WINDOW)

            if rc.returncode == 0 and os.path.exists(output_path):
                result_size = os.path.getsize(output_path)
                if result_size <= MAX_SIZE_BYTES:
                    return result_size

        # 最后一搏：极限压缩
        self.root.after(0, lambda: self.status_label.config(text="深度压缩中..."))
        rc = subprocess.run([
            self.gs_path,
            '-sDEVICE=pdfwrite',
            '-dCompatibilityLevel=1.4',
            '-dColorImageResolution=72',
            '-dGrayImageResolution=72',
            '-dMonoImageResolution=72',
            '-dDownsampleMonoImages=true',
            '-dDownsampleGrayImages=true',
            '-dDownsampleColorImages=true',
            '-dNOPAUSE',
            '-dQUIET',
            '-dBATCH',
            f'-sOutputFile={output_path}',
            input_path
        ], capture_output=True, text=True, timeout=300,
           creationflags=CREATE_NO_WINDOW)

        if os.path.exists(output_path):
            return os.path.getsize(output_path)

        raise RuntimeError("压缩失败，请稍后重试")

    def _done(self, input_path, output_path, original_size, result_size):
        self.progress.stop()
        self.compress_btn.config(state="normal", bg=ACCENT)
        ratio = (1 - result_size / original_size) * 100 if original_size > 0 else 0

        if result_size <= MAX_SIZE_BYTES:
            self.status_label.config(text=f"✅ 完成！节省 {ratio:.0f}%")
            messagebox.showinfo("完成",
                f"✅ 压缩完成！\n\n原始: {original_size/1024/1024:.1f}MB\n"
                f"压缩后: {result_size/1024/1024:.1f}MB\n节省: {ratio:.0f}%\n\n"
                f"文件已保存到:\n{output_path}")
        else:
            self.status_label.config(text=f"⚠️ 仍有 {result_size/1024/1024:.1f}MB")
            messagebox.showwarning("部分完成",
                f"⚠️ 压缩后仍有 {result_size/1024/1024:.1f}MB，超过 {MAX_SIZE_MB}MB。\n\n"
                f"建议减少 PDF 页数。\n\n文件: {output_path}")


def main():
    root = tk.Tk()
    app = PDFShrinkerApp(root)
    root.mainloop()


if __name__ == '__main__':
    # PyInstaller 打包后清理临时目录，避免退出时弹窗报错
    if hasattr(sys, '_MEIPASS'):
        tmpdir = sys._MEIPASS
        def cleanup():
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
        atexit.register(cleanup)
    main()
