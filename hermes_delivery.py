#!/usr/bin/env python3
"""Local, offline delivery evidence. Never updates code, installs, or restarts.

capture preserves explicitly selected source files (including dirty content) in
content-addressed blobs; verify fails closed on missing/changed files. Secrets,
data and runtime directories are not discovered or copied. Callers must supply
all owning source roots and separately retain deployment configuration backups.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

SOURCE_SUFFIXES = {'.py', '.sh', '.js', '.ts', '.tsx', '.json', '.yaml', '.yml', '.toml', '.md', '.txt', '.lock'}
EXCLUDED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'reports', 'logs', 'data', 'models', '.cache', 'dist', 'build'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_files(root):
    """Bounded source inventory; never traverse directory symlinks."""
    root = Path(root).resolve(strict=True)
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith('.'))
        for name in sorted(files):
            path = Path(parent) / name
            if name.startswith('.') or path.suffix not in SOURCE_SUFFIXES:
                continue
            if path.is_symlink():
                raise ValueError(f'Explicitly inventory symlink target: {path}')
            yield path


def inventory(root):
    root = Path(root).resolve(strict=True)
    files = {str(p.relative_to(root)): digest(p.read_bytes()) for p in source_files(root)}
    if not files:
        raise ValueError(f'Empty source root: {root}')
    git = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    status = subprocess.run(['git', '-C', str(root), 'status', '--porcelain=v1', '--untracked-files=all'], capture_output=True, text=True)
    return {'root': str(root), 'head': git.stdout.strip() if git.returncode == 0 else None,
            'status': status.stdout.splitlines() if status.returncode == 0 else None, 'files': files}


def capture(roots, output):
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    blobs = output / 'blobs'
    blobs.mkdir(mode=0o700)
    records = []
    for root in roots:
        record = inventory(root)
        for name, expected in record['files'].items():
            data = (Path(record['root']) / name).read_bytes()
            if digest(data) != expected:
                raise ValueError(f'Source changed during capture: {name}')
            target = blobs / expected
            if not target.exists():
                target.write_bytes(data)
                target.chmod(0o600)
        if inventory(root)['files'] != record['files']:
            raise ValueError(f'Source changed during capture: {root}')
        records.append(record)
    manifest = {'schema': 1, 'scope': 'explicit source roots only; not a full runtime backup', 'sources': records}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'manifest.json').chmod(0o600)
    return manifest


def verify(bundle):
    bundle = Path(bundle)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if manifest.get('schema') != 1 or not manifest.get('sources'):
        raise ValueError('Invalid or empty manifest')
    errors = []
    for record in manifest['sources']:
        actual = inventory(record['root'])['files']
        for name in sorted(set(actual) | set(record['files'])):
            expected = record['files'].get(name)
            if actual.get(name) != expected:
                errors.append(f"source drift: {record['root']}/{name}")
            if expected:
                blob = bundle / 'blobs' / expected
                if not blob.is_file() or digest(blob.read_bytes()) != expected:
                    errors.append(f'backup unavailable or corrupt: {name}')
    return errors


def runtime_probe(python, worktree):
    code = """
import importlib.metadata as md
import json, sqlite3, sys
import tools.lazy_deps as lazy
import fastembed
print(json.dumps({
    'interpreter': sys.executable,
    'python': list(sys.version_info[:3]),
    'sqlite': sqlite3.sqlite_version,
    'lazy_deps_source': lazy.__file__,
    'memory_vault_spec': list(lazy.LAZY_DEPS.get('memory.vault', ())),
    'fastembed_version': md.version('fastembed'),
    'packages': sorted((d.metadata['Name'].lower(), d.version) for d in md.distributions()),
}))
"""
    result = subprocess.run([str(python), '-B', '-c', code], cwd=worktree,
                            capture_output=True, text=True, timeout=120, check=True)
    report = json.loads(result.stdout)
    if Path(report['interpreter']).absolute() != Path(python).absolute():
        raise ValueError('Interpreter mismatch')
    if Path(report['lazy_deps_source']).resolve() != Path(worktree).resolve() / 'tools/lazy_deps.py':
        raise ValueError('Lazy dependency source mismatch')
    if report['memory_vault_spec'] != ['fastembed>=0.8.0,<1']:
        raise ValueError('Accepted local memory.vault contract was lost or changed')
    return report


def compare_runtime(before, after, require_fixed_sqlite=False):
    errors = []
    for key in ('python', 'packages', 'lazy_deps_source', 'memory_vault_spec'):
        if before[key] != after[key]:
            errors.append(f'Unapproved runtime change: {key}')
    version = tuple(int(n) for n in after['sqlite'].split('.'))
    fixed = version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51, 0) or (3, 44, 6) <= version < (3, 45, 0)
    if require_fixed_sqlite and not fixed:
        errors.append('SQLite WAL-reset fix missing')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    cap = sub.add_parser('capture')
    cap.add_argument('--root', action='append', required=True)
    cap.add_argument('--output', required=True)
    check = sub.add_parser('verify')
    check.add_argument('bundle')
    probe = sub.add_parser('probe')
    probe.add_argument('--python', required=True)
    probe.add_argument('--worktree', required=True)
    accept = sub.add_parser('accept-runtime')
    accept.add_argument('--bundle', required=True)
    accept.add_argument('--before', required=True)
    accept.add_argument('--python', required=True)
    accept.add_argument('--worktree', required=True)
    accept.add_argument('--require-fixed-sqlite', action='store_true')
    args = parser.parse_args()
    if args.command == 'accept-runtime':
        errors = verify(args.bundle)
        after = runtime_probe(args.python, args.worktree)
        errors.extend(compare_runtime(json.loads(Path(args.before).read_text()), after, args.require_fixed_sqlite))
        print(json.dumps({'runtime_acceptance': 'FAIL' if errors else 'PASS',
                          'scope': 'source preservation, dependency parity and runtime imports; focused tests and live health are separate mandatory gates',
                          'errors': errors, 'runtime': after}, indent=2))
        return bool(errors)
    if args.command == 'probe':
        print(json.dumps(runtime_probe(args.python, args.worktree), indent=2))
        return 0
    if args.command == 'capture':
        result = capture(args.root, args.output)
        print(json.dumps({'captured_roots': len(result['sources']), 'output': args.output}))
        return 0
    errors = verify(args.bundle)
    print(json.dumps({'source_preservation': 'FAIL' if errors else 'PASS', 'errors': errors}, indent=2))
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
