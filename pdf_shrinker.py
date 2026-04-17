#!/usr/bin/env python3
"""
PDF Shrinker - 拖拽式 PDF 压缩工具
依赖: Python 3, Ghostscript
安装 Ghostscript: brew install ghostscript (macOS) / https://ghostscript.com (Windows)
"""

import os
import sys
import subprocess
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import threading
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger(__name__)

MAX_SIZE_MB = 5
MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024

# 查找 gs 路径
def find_gs():
    for cmd in ['gs', 'gswin64c', 'gswin32c']:
        try:
            result = subprocess.run([cmd, '--version'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return cmd
        except Exception:
            continue
    return None


class PDFShrinkerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PDF Shrinker")
        self.root.geometry("500x400")
        self.root.resizable(False, False)
        self.file_path = None
        self.gs_path = find_gs()

        # 居中
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (w // 2)
        y = (self.root.winfo_screenheight() // 2) - (h // 2)
        self.root.geometry(f'+{x}+{y}')

        self._build_ui()

    def _build_ui(self):
        # 标题
        title = tk.Label(self.root, text="PDF Shrinker", font=("Helvetica", 22, "bold"))
        title.pack(pady=(20, 2))

        sub = tk.Label(self.root, text=f"PDF 压缩到 {MAX_SIZE_MB}MB 以下",
                       font=("Helvetica", 10), foreground="#888")
        sub.pack()

        # 警告
        if not self.gs_path:
            warn = tk.Label(self.root, text="⚠️ 未检测到 Ghostscript",
                            font=("Helvetica", 11), foreground="#e74c3c")
            warn.pack(pady=(8, 0))
            inst = tk.Label(self.root,
                           text="macOS: brew install ghostscript\nWindows: 从 https://ghostscript.com 下载安装",
                           font=("Helvetica", 9), foreground="#666")
            inst.pack()
        else:
            ok = tk.Label(self.root, text=f"✅ Ghostscript {self.gs_path} 已就绪",
                           font=("Helvetica", 10), foreground="#27ae60")
            ok.pack(pady=(8, 0))

        # 拖拽区域
        self.drop_frame = tk.Frame(self.root, bd=2, relief="groove", bg="white",
                                   width=400, height=120)
        self.drop_frame.pack(pady=16)
        self.drop_frame.pack_propagate(False)

        self.drop_label = tk.Label(self.drop_frame, text="📄 点击选择 PDF 文件\n或拖拽文件到这里",
                                    font=("Helvetica", 13), bg="white", fg="#aaa",
                                    justify="center")
        self.drop_label.place(relx=0.5, rely=0.5, anchor="center")

        for w in [self.drop_frame, self.drop_label]:
            w.bind("<Button-1>", self._browse)
            w.bind("<Enter>", lambda e: self.drop_frame.config(bg="#f5f5f5", bd=2))
            w.bind("<Leave>", lambda e: self.drop_frame.config(bg="white", bd=2))

        # 文件信息
        self.info_label = tk.Label(self.root, text="", font=("Helvetica", 10), foreground="#555")
        self.info_label.pack()

        # 压缩按钮
        self.compress_btn = ttk.Button(self.root, text="🗜️ 开始压缩",
                                        command=self._start_compress, state="disabled")
        self.compress_btn.pack(pady=(8, 0))

        # 进度条
        self.progress = ttk.Progressbar(self.root, mode="indeterminate", length=350)
        self.progress.pack(pady=8)

        # 状态栏
        self.status_label = tk.Label(self.root, text="", font=("Helvetica", 9), foreground="#888")
        self.status_label.pack(side="bottom", fill="x", pady=(0, 8))

        self._setup_dnd()

    def _setup_dnd(self):
        """简单的拖拽绑定（Windows/macOS 通用点击选择）"""
        try:
            self.root.tk.call('tk', 'busy', 'hold', self.drop_frame)
        except:
            pass

    def _browse(self, e=None):
        path = filedialog.askopenfilename(
            title="选择 PDF 文件", filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")]
        )
        if path:
            self._set_file(path)

    def _set_file(self, path):
        self.file_path = path
        size_mb = os.path.getsize(path) / 1024 / 1024
        name = os.path.basename(path)
        self.info_label.config(text=f"📄 {name}  ({size_mb:.1f}MB)")
        self.drop_label.config(text=f"📄 {name[:40]}", fg="#333")
        self.compress_btn.config(state="normal")
        self.status_label.config(text="")

    def _start_compress(self):
        if not self.file_path:
            return
        if not self.gs_path:
            messagebox.showerror("错误", "请先安装 Ghostscript")
            return

        self.compress_btn.config(state="disabled")
        self.progress.start(8)
        self.status_label.config(text="压缩中，请稍候...")
        threading.Thread(target=self._compress, daemon=True).start()

    def _compress(self):
        input_path = self.file_path
        base = Path(input_path).stem
        output_path = str(Path(input_path).parent / f"{base}_compressed.pdf")
        original_size = os.path.getsize(input_path)

        try:
            # 已经 < 5MB，直接复制
            if original_size < MAX_SIZE_BYTES:
                shutil.copy(input_path, output_path)
                result_size = original_size
            else:
                result_size = original_size
                tried = []

                # 三档质量逐步尝试
                for name, dpi in [
                    ("screen (低)", 72),
                    ("ebook (中)", 150),
                    ("prepress (高)", 300),
                ]:
                    if result_size <= MAX_SIZE_BYTES:
                        break
                    tried.append(name)
                    self.root.after(0, lambda n=name: self.status_label.config(
                        text=f"尝试 {n}..."))

                    result = subprocess.run([
                        self.gs_path,
                        '-sDEVICE=pdfwrite',
                        '-dCompatibilityLevel=1.4',
                        '-dPDFSETTINGS=/' + name.split()[0],
                        '-dNOPAUSE',
                        '-dQUIET',
                        '-dBATCH',
                        f'-r{dpi}',
                        f'-sOutputFile={output_path}',
                        input_path
                    ], capture_output=True, text=True, timeout=180)

                    if os.path.exists(output_path):
                        result_size = os.path.getsize(output_path)
                        if result_size <= MAX_SIZE_BYTES:
                            break

                # 最后一搏：把所有图片降到 72dpi
                if result_size > MAX_SIZE_BYTES:
                    try:
                        subprocess.run([
                            self.gs_path,
                            '-sDEVICE=pdfwrite',
                            '-dCompatibilityLevel=1.4',
                            '-dColorImageResolution=72',
                            '-dGrayImageResolution=72',
                            '-dMonoImageResolution=72',
                            '-dNOPAUSE',
                            '-dQUIET',
                            '-dBATCH',
                            f'-sOutputFile={output_path}',
                            input_path
                        ], capture_output=True, text=True, timeout=180)
                        if os.path.exists(output_path):
                            result_size = os.path.getsize(output_path)
                    except Exception:
                        pass

            self.root.after(0, self._done, input_path, output_path, original_size, result_size)

        except Exception as e:
            self.root.after(0, lambda: [
                self.progress.stop(),
                self.compress_btn.config(state="normal"),
                self.status_label.config(text=""),
                messagebox.showerror("压缩失败", str(e))
            ])

    def _done(self, input_path, output_path, original_size, result_size):
        self.progress.stop()
        self.compress_btn.config(state="normal")
        ratio = (1 - result_size / original_size) * 100 if original_size > 0 else 0

        if result_size <= MAX_SIZE_BYTES:
            self.status_label.config(text=f"✅ 完成！节省 {ratio:.0f}%")
            msg = (f"✅ 压缩完成！\n\n"
                   f"原始: {original_size/1024/1024:.1f}MB\n"
                   f"压缩后: {result_size/1024/1024:.1f}MB\n"
                   f"节省: {ratio:.0f}%\n\n"
                   f"文件已保存到:\n{output_path}")
            messagebox.showinfo("完成", msg)
        else:
            self.status_label.config(text=f"⚠️ 仍有 {result_size/1024/1024:.1f}MB")
            msg = (f"⚠️ 压缩后仍有 {result_size/1024/1024:.1f}MB，超过 {MAX_SIZE_MB}MB。\n\n"
                   f"建议减少 PDF 页数或降低图片分辨率。\n\n文件: {output_path}")
            messagebox.showwarning("部分完成", msg)


def main():
    if sys.version_info[0] < 3:
        print("需要 Python 3")
        sys.exit(1)

    root = tk.Tk()
    app = PDFShrinkerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
