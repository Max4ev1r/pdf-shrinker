#!/usr/bin/env python3
"""
PDF Shrinker v3 - 现代 UI + 桌面输出文件夹
纯 Python，pip install pypdf pillow 即可
"""

import os, sys, shutil, io
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    print("缺少 pypdf，请运行: pip install pypdf")
    input("按回车退出...")
    sys.exit(1)

MAX_SIZE_MB = 5
MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024

# 跨平台桌面路径
def get_desktop_folder():
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    folder = os.path.join(desktop, "PDF压缩")
    if not os.path.exists(folder):
        os.makedirs(folder)
    return folder


# ============ 现代配色 ============
BG = "#1a1a2e"       # 深蓝黑背景
CARD_BG = "#16213e"   # 卡片背景
ACCENT = "#e94560"     # 玫红强调
ACCENT2 = "#0f3460"   # 深蓝
TEXT = "#eaeaea"       # 主文字
TEXT_DIM = "#8892b0"  # 次文字
BTN_BG = "#e94560"    # 按钮背景
BTN_HOVER = "#ff6b6b" # 按钮悬停


class ModernApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PDF Shrinker")
        self.root.geometry("520x480")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        # 居中
        self.root.update_idletasks()
        w, h = 520, 480
        x = (self.root.winfo_screenwidth() // 2) - w // 2
        y = (self.root.winfo_screenheight() // 2) - h // 2
        self.root.geometry(f'{w}x{h}+{x}+{y}')

        self.file_path = None
        self._build_ui()

    def _build_ui(self):
        # 标题
        tk.Label(self.root, text="PDF Shrinker", font=("SF Pro Display", 26, "bold"),
                 fg=TEXT, bg=BG).pack(pady=(24, 4))
        tk.Label(self.root, text=f"压缩到 {MAX_SIZE_MB}MB 以下 · 纯 Python",
                 font=("SF Pro Text", 11), fg=TEXT_DIM, bg=BG).pack()

        # 主卡片
        card = tk.Frame(self.root, bg=CARD_BG, bd=0)
        card.pack(pady=20, padx=24)

        # 拖拽区域
        self.drop_frame = tk.Frame(card, bg=ACCENT2, bd=0, width=460, height=140, cursor="hand2")
        self.drop_frame.pack(pady=(16, 8))
        self.drop_frame.pack_propagate(False)

        self.drop_icon = tk.Label(self.drop_frame, text="📄", font=("SF Pro Display", 32),
                                  bg=ACCENT2, fg=TEXT)
        self.drop_icon.pack(pady=(20, 4))

        self.drop_label = tk.Label(self.drop_frame, text="点击选择 PDF 文件",
                                    font=("SF Pro Text", 13), bg=ACCENT2, fg=TEXT)
        self.drop_label.pack()

        self.drop_hint = tk.Label(self.drop_frame, text="或拖拽文件到这里",
                                   font=("SF Pro Text", 10), bg=ACCENT2, fg=TEXT_DIM)
        self.drop_hint.pack(pady=(2, 0))

        for w in [self.drop_frame, self.drop_icon, self.drop_label, self.drop_hint]:
            w.bind("<Button-1>", self._browse)
            if hasattr(w, 'bind'):
                try:
                    w.bind("<Enter>", lambda e: self.drop_frame.config(bg="#1a3a6e"))
                    w.bind("<Leave>", lambda e: self.drop_frame.config(bg=ACCENT2))
                except: pass

        self.drop_frame.bind("<Enter>", lambda e: self.drop_frame.config(bg="#1a3a6e"))
        self.drop_frame.bind("<Leave>", lambda e: self.drop_frame.config(bg=ACCENT2))
        self.drop_frame.bind("<Button-1>", self._browse)

        # 文件信息
        self.info_label = tk.Label(card, text="", font=("SF Pro Text", 11),
                                    fg=TEXT, bg=CARD_BG)
        self.info_label.pack(pady=(4, 8))

        # 压缩按钮
        self.compress_btn = tk.Button(card, text="🗜️ 开始压缩",
                                       font=("SF Pro Display", 14, "bold"),
                                       bg=BTN_BG, fg="white", bd=0, padx=32, pady=10,
                                       activebackground=BTN_HOVER, activeforeground="white",
                                       state="disabled", cursor="hand2",
                                       command=self._start_compress)
        self.compress_btn.pack(pady=(0, 8))

        # 进度条（自定义样式）
        self.progress = ttk.Progressbar(card, mode="indeterminate", length=420,
                                         style="Modern.Horizontal.TProgressbar")
        self.progress.pack(pady=(0, 8))
        self.style = ttk.Style()
        self.style.configure("Modern.Horizontal.TProgressbar",
                              background=ACCENT, troughcolor=ACCENT2, thickness=6)

        # 状态
        self.status_label = tk.Label(card, text="", font=("SF Pro Text", 10),
                                      fg=TEXT_DIM, bg=CARD_BG)
        self.status_label.pack(pady=(0, 8))

        # 输出路径提示
        out_folder = get_desktop_folder()
        tk.Label(self.root, text=f"📁 输出位置：桌面 / PDF压缩",
                 font=("SF Pro Text", 9), fg=TEXT_DIM, bg=BG).pack(side="bottom", pady=(0, 10))

    def _browse(self, e=None):
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
        self.drop_label.config(text=short_name)
        self.drop_hint.config(text=f"{size_mb:.1f}MB")
        self.compress_btn.config(state="normal")
        self.status_label.config(text="")

    def _start_compress(self):
        if not self.file_path:
            return
        self.compress_btn.config(state="disabled", bg=ACCENT2)
        self.progress.start(6)
        self.status_label.config(text="压缩中，请稍候...")
        threading.Thread(target=self._compress, daemon=True).start()

    def _compress(self):
        input_path = self.file_path
        base = Path(input_path).stem
        output_dir = get_desktop_folder()
        output_path = os.path.join(output_dir, f"{base}_compressed.pdf")
        original_size = os.path.getsize(input_path)

        try:
            if original_size < MAX_SIZE_BYTES:
                shutil.copy(input_path, output_path)
                result_size = original_size
            else:
                result_size = self._reduce_pdf(input_path, output_path, original_size)

            self.root.after(0, self._done, input_path, output_path, original_size, result_size)
        except Exception as e:
            self.root.after(0, lambda: [
                self.progress.stop(),
                self.compress_btn.config(state="normal", bg=BTN_BG),
                self.status_label.config(text=""),
                messagebox.showerror("压缩失败", str(e))
            ])

    def _reduce_pdf(self, input_path, output_path, original_size):
        reader = PdfReader(input_path)
        total_pages = len(reader.pages)
        best_size = original_size
        best_buf = None

        for quality in [85, 70, 55, 40, 30, 20, 15, 10]:
            if best_size <= MAX_SIZE_BYTES:
                break

            self.root.after(0, lambda q=quality: self.status_label.config(text=f"压缩中... 质量 {q}%"))

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)

            buf = io.BytesIO()
            writer.write(buf)
            result_size = len(buf.getvalue())

            if result_size < best_size:
                best_size = result_size
                best_buf = buf.getvalue()
                if result_size <= MAX_SIZE_BYTES:
                    break

        if best_size > MAX_SIZE_BYTES:
            for keep_ratio in [0.8, 0.6, 0.5, 0.3, 0.2]:
                keep_count = max(1, int(total_pages * keep_ratio))
                writer = PdfWriter()
                for i in range(keep_count):
                    writer.add_page(reader.pages[i])
                buf = io.BytesIO()
                writer.write(buf)
                result_size = len(buf.getvalue())
                if result_size < best_size:
                    best_size = result_size
                    best_buf = buf.getvalue()
                    if result_size <= MAX_SIZE_BYTES:
                        break

        if best_buf:
            with open(output_path, 'wb') as f:
                f.write(best_buf)

        return best_size

    def _done(self, input_path, output_path, original_size, result_size):
        self.progress.stop()
        self.compress_btn.config(state="normal", bg=BTN_BG)
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
                f"建议减少 PDF 页数或降低图片分辨率。\n\n文件: {output_path}")


def main():
    root = tk.Tk()
    app = ModernApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
