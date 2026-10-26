from __future__ import annotations

import ast
import difflib
import io
from pathlib import Path
import tokenize
import uuid

from .config import ProjectConfig
from .llm import LLMClient, LLMBudgetError
from .prompts import REPAIR_STRATEGIES, repair_messages
from .runner import TestRunner, failed_nodes, passed_nodes
from .storage import ToolError, contained, file_hash, read_python, write_json


def source_context(config: ProjectConfig, workspace: Path, ids: list[str]) -> dict[str, str]:
    files = sorted({item.source_file for item in config.contracts if item.id in ids})
    result = {name: read_python(contained(workspace, name)) for name in files}
    if sum(map(len, result.values())) > 100000:
        raise ToolError("相关源文件过大；请缩小测试对象，单次修复上下文最多 100000 字符")
    return result


def signatures(text: str) -> dict:
    tree = ast.parse(text)
    result = {}
    def visit(node, prefix=""):
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result[prefix + child.name] = (type(child).__name__, ast.dump(child.args),
                                              ast.dump(child.returns) if child.returns else "")
            elif isinstance(child, ast.ClassDef):
                result[prefix + child.name] = tuple(ast.dump(base) for base in child.bases)
                visit(child, prefix + child.name + ".")
    visit(tree)
    return result


def prepare_patch(value: dict, source: dict[str, str], max_lines: int) -> tuple[dict[str, str], str]:
    edits = value.get("edits")
    if not isinstance(edits, list) or not edits or len(edits) > 20:
        raise ToolError("补丁必须包含 1—20 个 edits")
    modified = dict(source)
    for edit in edits:
        if not isinstance(edit, dict):
            raise ToolError("edit 必须是对象")
        name, old, new = edit.get("path"), edit.get("old"), edit.get("new")
        if not isinstance(name, str) or name not in source:
            raise ToolError("补丁只能修改该缺陷对应的源文件")
        if not isinstance(old, str) or not old.strip() or not isinstance(new, str) or not new.strip():
            raise ToolError("补丁的 old/new 必须是非空源代码")
        if modified[name].count(old) != 1:
            raise ToolError(f"{name} 中 old 必须精确匹配且只出现一次")
        modified[name] = modified[name].replace(old, new, 1)
    diffs = []
    changed = 0
    final = {}
    for name, after in modified.items():
        before = source[name]
        if before == after:
            continue
        try:
            if signatures(before) != signatures(after):
                raise ToolError("补丁改变了现有函数/类接口，拒绝接受")
        except SyntaxError as exc:
            raise ToolError(f"补丁含语法错误: {exc}") from exc
        lines = list(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile="a/" + name, tofile="b/" + name))
        changed += sum(line.startswith(("+", "-")) and not line.startswith(("+++", "---")) for line in lines)
        diffs.extend(lines)
        final[name] = after
    if not final or changed > max_lines:
        raise ToolError(f"补丁为空或超过 {max_lines} 行修改限制")
    return final, "".join(diffs)


def suite_fingerprints(workspace: Path, config: ProjectConfig) -> dict:
    files = set()
    for name in [*config.test_paths, ".llm_tests"]:
        path = contained(workspace, name)
        if path.exists():
            files.update([path] if path.is_file() else (p for p in path.rglob("*") if p.is_file()))
    return {path.relative_to(workspace).as_posix(): file_hash(path) for path in sorted(files)}


class RepairEngine:
    def __init__(self, config: ProjectConfig, runner: TestRunner, client: LLMClient):
        self.config, self.runner, self.client = config, runner, client

    def repair(self, defect: dict, test: dict, selections: list[str]) -> dict:
        record = {"defect_id": defect["id"], "category": defect["assessment"]["category"],
                  "strategy": REPAIR_STRATEGIES[defect["assessment"]["category"]], "attempts": [], "status": "rejected"}
        defect.setdefault("repairs", []).append(record)
        before = []
        record["before"] = before
        for i in range(self.config.repeat_tests):
            before.append(self.runner.run(selections, f"{defect['id']}-before-{i}"))
        if any(item["status"] != "ok" for item in before):
            record.update(status="environment_error", reason="修复前执行环境异常")
            return record
        repro_nodes = {node for node in failed_nodes(before[0]) if node.split("::")[0] == test["path"]}
        if not repro_nodes or any(not repro_nodes <= failed_nodes(item) for item in before):
            record.update(status="not_reproduced", reason="当前工作副本中复现测试未连续失败；未提交新补丁")
            return record
        required_passes = set().union(*(passed_nodes(item) for item in before))
        required_collected = set().union(*(set(item["collected"]) for item in before))
        allowed_failures = set.intersection(*(failed_nodes(item) for item in before)) - repro_nodes
        allowed_skips = set.intersection(*({node for node, value in item["tests"].items() if value["outcome"] == "skipped"}
                                          for item in before))
        workspace = self.runner.workspace
        protection = suite_fingerprints(workspace, self.config)
        source = source_context(self.config, workspace, test["requirement_ids"])
        contracts = [item.blackbox_view() for item in self.config.contracts if item.id in test["requirement_ids"]]
        feedback = []
        for attempt_number in range(1, self.config.max_repair_attempts + 1):
            artifact = self.runner.run_dir / "patches" / defect["id"] / f"attempt-{attempt_number}-{uuid.uuid4().hex[:8]}"
            artifact.mkdir(parents=True, exist_ok=False)
            attempt = {"number": attempt_number, "artifact_path": str(artifact.relative_to(self.runner.run_dir)), "status": "rejected"}
            record["attempts"].append(attempt)
            originals = {}
            accepted = False
            try:
                details = {key: defect[key] for key in ("id", "test_id", "assessment", "status")}
                details["reproduction_test"] = {key: test[key] for key in ("name", "code", "requirement_ids", "scenario",
                                                                          "input_description", "expected_result")}
                details["observed_failures"] = [{"node": node, "failure": value["failure"][:12000]}
                                                for result in before[:1] for node, value in result["tests"].items() if node in repro_nodes]
                proposal = self.client.ask(repair_messages(details, source, contracts, feedback, self.config.max_changed_lines),
                                           f"repair/{defect['id']}/{attempt_number}")
                write_json(artifact / "proposal.json", proposal)
                modified, diff = prepare_patch(proposal, source, self.config.max_changed_lines)
                (artifact / "candidate.patch").write_text(diff, encoding="utf-8")
                attempt["diff"] = diff
                for name, text in modified.items():
                    path = contained(workspace, name)
                    originals[name] = path.read_bytes()
                    encoding, _ = tokenize.detect_encoding(io.BytesIO(originals[name]).readline)
                    path.write_bytes(text.encode(encoding))
                after = []
                attempt["after"] = after
                for i in range(self.config.repeat_tests):
                    after.append(self.runner.run(selections, f"{defect['id']}-after-{attempt_number}-{i}"))
                reasons = []
                for result in after:
                    if result["status"] != "ok":
                        reasons.append("修复后执行环境或文件完整性异常")
                    if not repro_nodes <= passed_nodes(result):
                        reasons.append("复现测试修复后未全部通过")
                    if not required_passes <= passed_nodes(result):
                        reasons.append("原先通过的测试发生回归")
                    if not required_collected <= set(result["collected"]):
                        reasons.append("修复后测试收集数量减少")
                    if not failed_nodes(result) <= allowed_failures:
                        reasons.append("出现新的失败测试")
                    skips = {node for node, value in result["tests"].items() if value["outcome"] == "skipped"}
                    if not skips <= allowed_skips:
                        reasons.append("修复后出现新的跳过测试")
                if suite_fingerprints(workspace, self.config) != protection:
                    reasons.append("测试文件被修改")
                if reasons:
                    raise ToolError("；".join(sorted(set(reasons))))
                accepted = True
                attempt.update(status="verified", reason="修复前失败、修复后通过，原先通过的测试继续通过")
                record.update(status="verified", accepted_attempt=attempt_number)
            except LLMBudgetError as exc:
                attempt.update(status="budget_exhausted", reason=str(exc))
                record["status"] = "budget_exhausted"
                raise
            except ToolError as exc:
                attempt["reason"] = str(exc)
                feedback.append({"attempt": attempt_number, "reason": str(exc),
                                 "failures": [{"node": node, "failure": value["failure"]}
                                              for result in attempt.get("after", []) for node, value in result["tests"].items()
                                              if value["outcome"] in {"failed", "error"}][:12]})
            finally:
                if not accepted:
                    for name, raw in originals.items():
                        contained(workspace, name).write_bytes(raw)
                write_json(artifact / "validation.json", attempt)
            if accepted:
                break
        return record
