from __future__ import annotations

from pathlib import Path
from ._vendor.coverup.segment import get_missing_coverage
from .storage import contained, read_python


def summarize(raw: dict) -> dict:
    lines = branches = covered_lines = covered_branches = 0
    for item in raw.get("files", {}).values():
        summary = item.get("summary", {})
        lines += summary.get("num_statements", 0)
        covered_lines += summary.get("covered_lines", 0)
        branches += summary.get("num_branches", 0)
        covered_branches += summary.get("covered_branches", 0)
    return {"line_total": lines, "line_covered": covered_lines,
            "line_percent": round(100 * covered_lines / lines, 2) if lines else None,
            "branch_total": branches, "branch_covered": covered_branches,
            "branch_percent": round(100 * covered_branches / branches, 2) if branches else None}


def reached(raw: dict, line_target: float, branch_target: float) -> bool:
    summary = summarize(raw)
    return (summary["line_percent"] is not None and summary["line_percent"] >= line_target
            and (summary["branch_total"] == 0 or summary["branch_percent"] >= branch_target))


def covered_units(raw: dict) -> set[tuple]:
    units = set()
    for filename, item in raw.get("files", {}).items():
        units.update((filename, "line", line) for line in item.get("executed_lines", []))
        units.update((filename, "branch", start, end) for start, end in item.get("executed_branches", []))
    return units


def whitebox_context(raw: dict, root: Path, allowed_files: set[str], *, max_chars: int = 24000) -> list[dict]:
    """Adapt coverage.py JSON to CoverUp's segment selection; no target import is executed."""
    compatible = {"files": {}}
    for filename, item in raw.get("files", {}).items():
        if filename not in allowed_files:
            continue
        compatible["files"][str(root / filename)] = {
            "missing_lines": item.get("missing_lines", []), "executed_lines": item.get("executed_lines", []),
            "missing_branches": [[max(0, start), max(0, end)] for start, end in item.get("missing_branches", [])],
        }
    segments = get_missing_coverage(compatible, line_limit=160)
    segments.sort(key=lambda segment: (-segment.missing_count(), str(segment.filename), segment.begin))
    result = []
    used = 0
    for segment in segments:
        excerpt = segment.get_excerpt()
        if used + len(excerpt) > max_chars:
            continue
        result.append({"file": Path(segment.filename).relative_to(root).as_posix(), "symbol": segment.name,
                       "begin": segment.begin, "end": segment.end,
                       "missing_lines": sorted(segment.missing_lines), "missing_branches": sorted(segment.missing_branches),
                       "source_excerpt": excerpt})
        used += len(excerpt)
    represented = {item["file"] for item in result}
    for filename in sorted(allowed_files - represented):
        text = read_python(contained(root, filename))
        remaining = max_chars - used
        if remaining <= 0:
            break
        excerpt = text[:remaining]
        result.append({"file": filename, "source_excerpt": excerpt, "truncated": len(excerpt) < len(text),
                       "missing_lines": raw.get("files", {}).get(filename, {}).get("missing_lines", []),
                       "missing_branches": raw.get("files", {}).get(filename, {}).get("missing_branches", [])})
        used += len(excerpt)
    return result
