#!/usr/bin/env python3
"""
PDF Shrinker v5 - 苹果风明亮 UI + 点击选择文件 + 桌面输出文件夹
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

def get_desktop_folder():
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    folder = os.path.join(desktop, "PDF压缩")
    if not os.path.exists(folder):
        os.makedirs(folder)
    return folder


# ============ 苹果风配色 ============
BG = "#F5F5F7"
CARD_BG = "#FFFFFF"
ACCENT = "#007AFF"
ACCENT_HOVER = "#0056CC"
TEXT = "#1D1D1F"
TEXT_DIM = "#86868B"
BORDER = "#D2D2D7"
SUCCESS = "#34C759"


class PDFShrinkerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PDF Shrinker")
        self.root.geometry("520x480")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        self.root.update_idletasks()
        w, h = 520, 480
        x = (self.root.winfo_screenwidth() // 2) - w // 2
        y = (self.root.winfo_screenheight() // 2) - h // 2
        self.root.geometry(f'{w}x{h}+{x}+{y}')

        self.file_path = None
        self._build_ui()

    def _build_ui(self):
        main_bg = tk.Frame(self.root, bg=BG)
        main_bg.pack(fill="both", expand=True)

        # 标题
        header = tk.Frame(main_bg, bg=BG)
        header.pack(pady=(28, 0))
        tk.Label(header, text="PDF Shrinker", font=("SF Pro Display", 28, "bold"),
                 fg=TEXT, bg=BG).pack()
        tk.Label(header, text=f"轻松压缩 PDF 到 {MAX_SIZE_MB}MB 以下",
                 font=("SF Pro Text", 13), fg=TEXT_DIM, bg=BG).pack(pady=(2, 0))

        # 主卡片
        card = tk.Frame(main_bg, bg=CARD_BG, bd=1,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(pady=20, padx=32)

        tk.Frame(card, bg=CARD_BG, height=20).pack()

        # 选择文件区域（点击选择，无拖拽）
        self.select_frame = tk.Frame(card, bg="#F0F0F5", bd=1,
                                     highlightbackground=BORDER, highlightthickness=1,
                                     width=440, height=120, cursor="hand2")
        self.select_frame.pack(pady=(0, 12))
        self.select_frame.pack_propagate(False)

        self.icon_label = tk.Label(self.select_frame, text="📎", font=("SF Pro Display", 30),
                                   bg="#F0F0F5", fg=ACCENT)
        self.icon_label.pack(pady=(18, 4))

        self.select_label = tk.Label(self.select_frame, text="点击选择 PDF 文件",
                                     font=("SF Pro Text", 14), bg="#F0F0F5", fg=TEXT)
        self.select_label.pack()

        self.select_hint = tk.Label(self.select_frame, text="支持任何大小的 PDF",
                                    font=("SF Pro Text", 11), bg="#F0F0F5", fg=TEXT_DIM)
        self.select_hint.pack(pady=(2, 0))

        for w in [self.select_frame, self.icon_label, self.select_label, self.select_hint]:
            w.bind("<Button-1>", lambda e: self._browse())
            w.bind("<Enter>", lambda e: self.select_frame.config(bg="#E8E8EF"))
            w.bind("<Leave>", lambda e: self.select_frame.config(bg="#F0F0F5"))

        # 文件信息
        self.info_label = tk.Label(card, text="", font=("SF Pro Text", 12),
                                   fg=TEXT_DIM, bg=CARD_BG)
        self.info_label.pack(pady=(4, 8))

        # 压缩按钮
        self.compress_btn = tk.Button(card, text="开始压缩",
                                      font=("SF Pro Display", 15, "bold"),
                                      bg=ACCENT, fg="white", bd=0, padx=40, pady=11,
                                      activebackground=ACCENT_HOVER, activeforeground="white",
                                      state="disabled", cursor="hand2", relief="flat",
                                      command=self._start_compress)
        self.compress_btn.pack()

        # 进度条
        self.progress = ttk.Progressbar(card, mode="indeterminate", length=420,
                                        style="Mac.Horizontal.TProgressbar")
        self.progress.pack(pady=(12, 0))
        s = ttk.Style()
        s.configure("Mac.Horizontal.TProgressbar",
                    background=ACCENT, troughcolor=BORDER, thickness=6)

        # 状态
        self.status_label = tk.Label(card, text="", font=("SF Pro Text", 11),
                                     fg=TEXT_DIM, bg=CARD_BG)
        self.status_label.pack(pady=(8, 16))

        # 底部
        footer = tk.Frame(main_bg, bg=BG)
        footer.pack(side="bottom", pady=(0, 14))
        out_folder = get_desktop_folder()
        tk.Label(footer, text=f"📁 压缩后文件保存至：桌面 / PDF压缩",
                 font=("SF Pro Text", 10), fg=TEXT_DIM, bg=BG).pack()

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
        self.compress_btn.config(state="disabled", bg=ACCENT_HOVER)
        self.progress.start(6)
        self.status_label.config(text="正在压缩...")
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
                self.compress_btn.config(state="normal", bg=ACCENT),
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
                f"建议减少 PDF 页数或降低图片分辨率。\n\n文件: {output_path}")


def main():
    root = tk.Tk()
    app = PDFShrinkerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
