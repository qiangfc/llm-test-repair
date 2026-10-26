from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import shutil
import tokenize


class ToolError(Exception):
    """A user-facing configuration, provider or execution error."""


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"无法读取 JSON 文件 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolError(f"{path} 的顶层必须是 JSON 对象")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def contained(root: Path, relative: str) -> Path:
    """Resolve an untrusted project-relative path; reject traversal and symlinks."""
    if not isinstance(relative, str) or not relative.strip():
        raise ToolError("必须使用非空的项目内相对路径")
    rel = Path(relative)
    if rel.is_absolute() or rel.drive or ".." in rel.parts:
        raise ToolError(f"必须使用项目内相对路径: {relative}")
    root = root.resolve()
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ToolError(f"不支持符号链接路径: {relative}")
    resolved = cursor.resolve()
    if not resolved.is_relative_to(root):
        raise ToolError(f"路径超出项目目录: {relative}")
    return resolved


def read_python(path: Path) -> str:
    with tokenize.open(path) as stream:
        return stream.read()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


IGNORE_NAMES = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
                ".cache", "runs", ".llm_tests", ".llm_runner", "dist", "build"}


def copy_project(source: Path, destination: Path, *, exclude: Path | None = None, include_generated: bool = False) -> None:
    """Copy into a fresh sandbox; never copy dotenv files or linked directories."""
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise ToolError(f"工作副本已存在，拒绝覆盖: {destination}")

    def ignore(directory: str, names: list[str]) -> set[str]:
        rejected = set()
        for name in names:
            path = Path(directory) / name
            ignored_names = IGNORE_NAMES - {".llm_tests"} if include_generated else IGNORE_NAMES
            if (name in ignored_names or name == ".env" or name.startswith(".env.")
                    or path.is_symlink() or name.startswith(".coverage")
                    or (exclude is not None and path.resolve() == exclude.resolve())):
                rejected.add(name)
        return rejected

    shutil.copytree(source, destination, ignore=ignore)
