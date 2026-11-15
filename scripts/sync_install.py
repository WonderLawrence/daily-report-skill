#!/usr/bin/env python3
"""Check or replace an installed daily-report skill, keeping the old directory as a backup."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
MANIFEST = '.install-manifest.json'
ROOT_FILES = ('SKILL.md', 'README.md', '.gitignore')
DIRECTORIES = ('references', 'examples', 'scripts', 'tests')


def included(path: Path) -> bool:
    return '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo', '.pyd')


def package_files(source: Path) -> dict[str, Path]:
    paths = [source / name for name in ROOT_FILES if (source / name).is_file()]
    for name in DIRECTORIES:
        paths.extend(p for p in (source / name).rglob('*') if p.is_file() and included(p.relative_to(source)))
    for path in paths:
        if path.is_symlink() or source not in path.resolve().parents:
            raise ValueError(f'安装包不支持符号链接或外部文件：{path}')
    return {str(path.relative_to(source)): path for path in sorted(paths)}


def hashes(files: dict[str, Path]) -> dict[str, str]:
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}


def installed_hashes(destination: Path) -> dict[str, str]:
    files = {str(p.relative_to(destination)): p for p in destination.rglob('*')
             if p.is_file() and p.name != MANIFEST and included(p.relative_to(destination))}
    return hashes(files)


def validate_destination(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise ValueError('目标目录不能是符号链接')
    dest = destination.resolve()
    if dest == source or dest in source.parents or source in dest.parents:
        raise ValueError('安装目录不能与工作区重叠')
    if destination.exists() and (not destination.is_dir() or not (destination / 'SKILL.md').is_file()):
        raise ValueError('已存在的目标必须是含 SKILL.md 的 skill 目录')


def sync(source: Path, destination: Path, apply: bool = False) -> int:
    source = source.resolve()
    validate_destination(source, destination)
    files = package_files(source)
    expected = hashes(files)
    actual = installed_hashes(destination)
    missing = sorted(expected.keys() - actual.keys())
    changed = sorted(name for name in expected.keys() & actual.keys() if expected[name] != actual[name])
    extra = sorted(actual.keys() - expected.keys())
    print(json.dumps({'新增': missing, '更新': changed, '移至备份': extra}, ensure_ascii=False, indent=2))
    if not (missing or changed or extra):
        print(f'[ok] {len(files)} 个文件一致：{destination}')
        return 0
    if not apply:
        return 1

    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix='.daily-report-stage-', dir=destination.parent))
    backup = None
    try:
        for name, path in files.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        if installed_hashes(staged) != expected:
            raise ValueError('复制校验失败，未替换安装目录')
        (staged / MANIFEST).write_text(json.dumps({'source': str(source), 'sha256': expected},
                                                 ensure_ascii=False, indent=2) + '\n')
        if destination.exists():
            backup_root = destination.parent / '.daily-report-backups'
            backup_root.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            backup = backup_root / stamp
            destination.rename(backup)
        try:
            staged.rename(destination)
        except OSError:
            if backup is not None:
                backup.rename(destination)
            raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    if installed_hashes(destination) != expected:
        raise ValueError('安装后校验失败，请核对备份与安装目录')
    print(f'[ok] 已同步并校验 {len(files)} 个文件：{destination}')
    if backup:
        print(f'[backup] {backup}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='只核对内容，存在差异时退出码为 1')
    mode.add_argument('--apply', action='store_true', help='备份旧目录后安装完整新版并核验哈希')
    parser.add_argument('--dest', type=Path, default=Path.home() / '.agents/skills/daily-report')
    args = parser.parse_args()
    try:
        return sync(SOURCE, args.dest.expanduser(), args.apply)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
