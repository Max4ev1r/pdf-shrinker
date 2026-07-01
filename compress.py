#!/usr/bin/env python3
"""
Hermes 压缩工具 - 本地文件/文件夹压缩
用法:
    compress <文件或文件夹路径>           # 自动选择最优格式
    compress <文件或文件夹路径> -f zip    # 强制 zip
    compress <文件或文件夹路径> -f tar.gz # 强制 tar.gz
    compress <文件或文件夹路径> -f gz     # 单独 gzip（仅文件）
"""

import os
import sys
import subprocess
import shutil
import argparse
from pathlib import Path

SUPPORTED_FORMATS = ['zip', 'tar.gz', 'gz']


def get_best_format(path: str) -> str:
    """根据文件类型自动选择最优格式"""
    if os.path.isdir(path):
        return 'tar.gz'
    ext = Path(path).suffix.lower()
    if ext in ['.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar']:
        return 'tar.gz'
    return 'tar.gz'


def compress_zip(src: str, output: str) -> str:
    """压缩为 zip"""
    if os.path.isdir(src):
        base = os.path.basename(src.rstrip('/'))
        parent = os.path.dirname(src) or '.'
        original_cwd = os.getcwd()
        os.chdir(parent)
        try:
            result = subprocess.run(
                ['zip', '-r', '-q', output, base],
                capture_output=True, text=True
            )
        finally:
            os.chdir(original_cwd)
    else:
        result = subprocess.run(
            ['zip', '-q', output, src],
            capture_output=True, text=True
        )
    if result.returncode != 0:
        raise RuntimeError(f"zip 失败: {result.stderr}")
    return output


def compress_tar_gz(src: str, output: str) -> str:
    """压缩为 tar.gz"""
    result = subprocess.run(
        ['tar', '-czf', output, '-C', os.path.dirname(src) or '.', os.path.basename(src)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"tar.gz 失败: {result.stderr}")
    return output


def compress_gz(src: str, output: str) -> str:
    """单独 gzip 压缩（仅文件）"""
    if os.path.isdir(src):
        raise ValueError("gzip 格式不支持压缩文件夹")
    result = subprocess.run(
        ['gzip', '-k', '-f', src],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"gzip 失败: {result.stderr}")
    return output


def get_size(path: str) -> str:
    """返回可读文件大小"""
    size = os.path.getsize(path)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def main():
    parser = argparse.ArgumentParser(description='Hermes 压缩工具')
    parser.add_argument('path', help='要压缩的文件或文件夹')
    parser.add_argument('-f', '--format', choices=SUPPORTED_FORMATS,
                        help='指定压缩格式（默认自动选择）')
    parser.add_argument('-o', '--output', help='输出路径（默认自动命名）')
    args = parser.parse_args()

    src = args.path
    if not os.path.exists(src):
        print(f"❌ 路径不存在: {src}", file=sys.stderr)
        sys.exit(1)

    fmt = args.format or get_best_format(src)
    original_size = get_size(src)

    # 生成输出文件名
    if args.output:
        output = args.output
    else:
        base = os.path.basename(src.rstrip('/'))
        if fmt == 'zip':
            output = f"{base}.zip"
        elif fmt == 'tar.gz':
            output = f"{base}.tar.gz"
        elif fmt == 'gz':
            output = f"{src}.gz"

    # 避免覆盖原文件
    if output == src or output == f"{src}.gz":
        print("❌ 输出路径不能与源文件相同", file=sys.stderr)
        sys.exit(1)

    print(f"📦 压缩中: {src} ({original_size}) → {fmt}")
    print(f"   目标: {output}")

    try:
        if fmt == 'zip':
            compress_zip(src, output)
        elif fmt == 'tar.gz':
            compress_tar_gz(src, output)
        elif fmt == 'gz':
            compress_gz(src, output)

        compressed_size = get_size(output)
        ratio = (1 - os.path.getsize(output) / os.path.getsize(src)) * 100
        print(f"✅ 完成: {compressed_size} (节省 {ratio:.1f}%)")
        print(f"📁 {os.path.abspath(output)}")
    except Exception as e:
        print(f"❌ 压缩失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
