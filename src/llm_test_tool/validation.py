from __future__ import annotations

import ast
import re
from .storage import ToolError


SCENARIOS = {"normal", "boundary", "exception"}
CATEGORIES = {"boundary", "null_type", "exception_handling", "logic"}
VERDICTS = {"defect", "test_error", "environment", "uncertain"}


def validate_test(item: dict, known_contracts: dict) -> dict:
    if not isinstance(item, dict):
        raise ToolError("测试用例必须是 JSON 对象")
    fields = {"name", "requirement_ids", "scenario", "input_description", "expected_result", "reason", "code"}
    if not fields <= item.keys():
        raise ToolError("测试用例缺少必要的元数据字段")
    if not isinstance(item["name"], str) or not re.fullmatch(r"test_[A-Za-z0-9_]+", item["name"]):
        raise ToolError("测试函数名必须是 test_ 开头的合法标识符")
    ids = item["requirement_ids"]
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) or key not in known_contracts for key in ids):
        raise ToolError("测试必须对应已存在的需求编号")
    if not isinstance(item["scenario"], str) or item["scenario"] not in SCENARIOS:
        raise ToolError("scenario 必须是 normal、boundary 或 exception")
    for key in ("input_description", "expected_result", "reason", "code"):
        if not isinstance(item[key], str) or not item[key].strip():
            raise ToolError(f"测试用例的 {key} 必须是非空字符串")
    if len(item["code"]) > 24000:
        raise ToolError("单个测试源码超过长度限制")
    try:
        tree = ast.parse(item["code"])
    except SyntaxError as exc:
        raise ToolError(f"生成测试语法不合法: {exc}") from exc
    for statement in tree.body:
        if not isinstance(statement, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)):
                raise ToolError("测试模块顶层只允许导入、同步函数定义和文档字符串")
    tests = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
    if len(tests) != 1 or tests[0].name != item["name"]:
        raise ToolError("每个用例必须包含且仅包含一个与 name 相同的测试函数")
    if "record_property" not in {arg.arg for arg in tests[0].args.args}:
        raise ToolError("测试函数须使用 record_property fixture 记录输入、预期与实际结果")
    recorded = {node.args[0].value for node in ast.walk(tests[0])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "record_property"
                and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)}
    if not {"input", "expected", "actual"} <= recorded:
        raise ToolError("测试函数必须分别调用 record_property 记录 input、expected、actual")
    assertions = [node for node in ast.walk(tests[0]) if isinstance(node, ast.Assert)]
    raises = [node for node in ast.walk(tests[0]) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "raises"]
    if not assertions and not raises:
        raise ToolError("用例必须有 assert 或 pytest.raises")
    for node in assertions:
        if not any(isinstance(part, (ast.Name, ast.Call, ast.Attribute)) for part in ast.walk(node.test)):
            raise ToolError("不允许使用常量断言制造有效测试")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"skip", "skipif", "xfail", "importorskip"}:
            raise ToolError("生成测试不允许 skip/xfail/importorskip")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "compile", "__import__"}:
            raise ToolError("生成测试不允许动态执行代码")
    imports = []
    target_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
            for alias in node.names:
                if any(alias.name == known_contracts[key].module or known_contracts[key].module.startswith(alias.name + ".") for key in ids):
                    target_aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
            if any(node.module == known_contracts[key].module or known_contracts[key].module.startswith(node.module + ".") for key in ids):
                target_aliases.update(alias.asname or alias.name for alias in node.names)
    if not any(module == contract.module or contract.module.startswith(module + ".")
               for key in ids for contract in [known_contracts[key]] for module in imports):
        raise ToolError("测试必须显式导入其需求对应的被测模块")
    def root_name(node):
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None
    if not any(isinstance(node, ast.Call) and root_name(node.func) in target_aliases for node in ast.walk(tests[0])):
        raise ToolError("测试函数必须调用实际导入的被测对象，不能只导入后使用伪造结果")
    return {key: item[key] for key in fields}


def validate_assessment(value: dict, test: dict, contracts: dict, min_confidence: float) -> dict:
    verdict = value.get("verdict", "uncertain")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise ToolError("缺陷评估 verdict 不合法")
    category = value.get("category", "logic")
    if not isinstance(category, str) or category not in CATEGORIES:
        raise ToolError("缺陷类型不合法")
    confidence = value.get("confidence", 0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ToolError("confidence 必须在 0—1 之间")
    requirement_id = value.get("requirement_id", "")
    quote = value.get("evidence_quote", "")
    evidence_valid = (isinstance(requirement_id, str) and isinstance(quote, str) and len(quote.strip()) >= 4 and requirement_id in test["requirement_ids"]
                      and quote in contracts[requirement_id].evidence_text())
    original_verdict = verdict
    if verdict == "defect" and (not evidence_valid or confidence < min_confidence):
        verdict = "uncertain"
    return {"verdict": verdict, "proposed_verdict": original_verdict, "category": category,
            "confidence": confidence, "requirement_id": requirement_id, "evidence_quote": quote,
            "evidence_valid": bool(evidence_valid), "explanation": str(value.get("explanation", ""))[:8000]}
