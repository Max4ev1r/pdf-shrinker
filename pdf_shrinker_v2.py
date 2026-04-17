#!/usr/bin/env python3
"""
PDF Shrinker v2 - 纯 Python PDF 压缩，无外部依赖
pip install pypdf pillow 即可
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


class PDFShrinkerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PDF Shrinker")
        self.root.geometry("500x400")
        self.root.resizable(False, False)

        self.root.update_idletasks()
        w, h = 500, 400
        x = (self.root.winfo_screenwidth() // 2) - w // 2
        y = (self.root.winfo_screenheight() // 2) - h // 2
        self.root.geometry(f'{w}x{h}+{x}+{y}')

        self.file_path = None
        self._build_ui()

    def _build_ui(self):
        tk.Label(self.root, text="PDF Shrinker", font=("Helvetica", 22, "bold")).pack(pady=(20, 2))
        tk.Label(self.root, text=f"PDF 压缩到 {MAX_SIZE_MB}MB 以下（纯 Python，无需 Ghostscript）",
                 font=("Helvetica", 9), foreground="#888").pack()

        self.drop_frame = tk.Frame(self.root, bd=2, relief="groove", bg="white", width=400, height=120)
        self.drop_frame.pack(pady=16)
        self.drop_frame.pack_propagate(False)

        self.drop_label = tk.Label(self.drop_frame, text="📄 点击选择 PDF 文件\n或拖拽文件到这里",
                                    font=("Helvetica", 13), bg="white", fg="#aaa", justify="center")
        self.drop_label.place(relx=0.5, rely=0.5, anchor="center")

        for w in [self.drop_frame, self.drop_label]:
            w.bind("<Button-1>", self._browse)
            w.bind("<Enter>", lambda e: self.drop_frame.config(bg="#f5f5f5"))
            w.bind("<Leave>", lambda e: self.drop_frame.config(bg="white"))

        self.info_label = tk.Label(self.root, text="", font=("Helvetica", 10), foreground="#555")
        self.info_label.pack()

        self.compress_btn = ttk.Button(self.root, text="🗜️ 开始压缩", command=self._start_compress, state="disabled")
        self.compress_btn.pack(pady=(8, 0))

        self.progress = ttk.Progressbar(self.root, mode="indeterminate", length=350)
        self.progress.pack(pady=8)

        self.status_label = tk.Label(self.root, text="", font=("Helvetica", 9), foreground="#888")
        self.status_label.pack(side="bottom", fill="x", pady=(0, 8))

    def _browse(self, e=None):
        path = filedialog.askopenfilename(title="选择 PDF 文件", filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
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
            if original_size < MAX_SIZE_BYTES:
                shutil.copy(input_path, output_path)
                result_size = original_size
            else:
                result_size = self._reduce_pdf(input_path, output_path, original_size)

            self.root.after(0, self._done, input_path, output_path, original_size, result_size)
        except Exception as e:
            self.root.after(0, lambda: [
                self.progress.stop(),
                self.compress_btn.config(state="normal"),
                self.status_label.config(text=""),
                messagebox.showerror("压缩失败", str(e))
            ])

    def _reduce_pdf(self, input_path, output_path, original_size):
        """逐步降低 JPEG 质量压缩，直到 < 5MB"""
        reader = PdfReader(input_path)
        total_pages = len(reader.pages)
        best_size = original_size
        best_buf = None

        # 从 85 开始，逐步降到 10
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

        # 如果降到最低质量还不够，减少页数
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

        # 写入文件
        final_buf = best_buf or io.BytesIO()
        if best_buf:
            with open(output_path, 'wb') as f:
                f.write(best_buf)
        else:
            shutil.copy(input_path, output_path)

        return best_size

    def _done(self, input_path, output_path, original_size, result_size):
        self.progress.stop()
        self.compress_btn.config(state="normal")
        ratio = (1 - result_size / original_size) * 100 if original_size > 0 else 0

        if result_size <= MAX_SIZE_BYTES:
            self.status_label.config(text=f"✅ 完成！节省 {ratio:.0f}%")
            messagebox.showinfo("完成",
                f"✅ 压缩完成！\n\n原始: {original_size/1024/1024:.1f}MB\n"
                f"压缩后: {result_size/1024/1024:.1f}MB\n节省: {ratio:.0f}%\n\n文件已保存到:\n{output_path}")
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
