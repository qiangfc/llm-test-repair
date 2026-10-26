from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys

from .config import LLMSettings, ProjectConfig
from .pipeline import new_run, repair_run, review_defect, supplement_run
from .report import render_report
from .storage import ToolError, contained, read_json, read_python, write_json


def draft_config(args):
    project = Path(args.project).resolve()
    if not project.is_dir():
        raise ToolError(f"目标项目目录不存在：{project}")
    destination = Path(args.output).resolve()
    contracts_path = destination.parent / "contracts.json"
    if destination.exists() or contracts_path.exists():
        raise ToolError("配置或 contracts.json 已存在，请选择新的输出目录，避免覆盖已有约定")
    sources, tests, python_paths = args.source or ["src"], args.tests or [], args.python_path or ["src"]
    contracts = []
    for relative in sources:
        source = contained(project, relative)
        if not source.exists():
            raise ToolError(f"源码路径不存在：{relative}")
        for path in ([source] if source.is_file() else sorted(source.rglob("*.py"))):
            if path.is_symlink() or path.suffix != ".py":
                continue
            tree = ast.parse(read_python(path))
            base = next((contained(project, value) for value in python_paths
                         if path.is_relative_to(contained(project, value))), project)
            module_parts = list(path.relative_to(base).with_suffix("").parts)
            if module_parts[-1] == "__init__":
                module_parts.pop()
            module = ".".join(module_parts)
            if not module:
                continue
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or node.name.startswith("_"):
                    continue
                signature = node.name + "(" + ast.unparse(node.args) + ")" if hasattr(node, "args") else node.name + "(...)"
                contracts.append({"id": f"REQ-{len(contracts) + 1:03d}", "module": module, "symbol": node.name,
                                  "signature": signature, "source_file": path.relative_to(project).as_posix(),
                                  "description": "请依据公开接口文档补充功能说明，不要从当前实现推测正确行为。",
                                  "expected_behavior": [], "normal_examples": [], "boundary_cases": [], "exception_cases": []})
                if len(contracts) >= 12:
                    break
            if len(contracts) >= 12:
                break
        if len(contracts) >= 12:
            break
    if not contracts:
        raise ToolError("没有找到可生成配置的公开函数/类；可参照 examples 手工填写 contracts.json")
    write_json(contracts_path, {"contracts": contracts})
    write_json(destination, {"name": project.name, "project_path": str(project), "source_paths": sources,
                             "test_paths": tests, "python_paths": python_paths, "contracts_file": "contracts.json",
                             "repository_url": "", "revision": "", "backend": "docker", "max_rounds": 3,
                             "max_tests_per_round": 6, "auto_repair": True, "confirmation_policy": "llm"})
    print(f"已生成配置：{destination}\n请先填写 {contracts_path} 中的功能与预期行为，再开始生成测试。")


def parser():
    root = argparse.ArgumentParser(description="基于 CoverUp 的黑盒/白盒单元测试与分类修复工具")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="生成测试、多轮反馈、分类修复并输出验收记录")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--env", type=Path, default=Path(".env"))
    run.add_argument("--output", type=Path, default=Path("runs"))
    run.add_argument("--backend", choices=["docker", "local"])
    run.add_argument("--no-repair", action="store_true")
    init = commands.add_parser("init", help="为其他 Python 项目生成需求配置骨架，不调用 LLM")
    init.add_argument("--project", required=True)
    init.add_argument("--source", action="append", help="源码文件或目录，相对目标项目；可多次指定")
    init.add_argument("--tests", action="append", help="原有测试文件或目录；可多次指定")
    init.add_argument("--python-path", action="append", help="模块搜索根目录；可多次指定")
    init.add_argument("--output", default="project.json")
    repair = commands.add_parser("repair", help="根据保存的记录继续修复已确认缺陷")
    repair.add_argument("--run", type=Path, required=True)
    repair.add_argument("--env", type=Path, default=Path(".env"))
    supplement = commands.add_parser("supplement", help="复核后补齐场景并再次执行回归，保留原验收记录")
    supplement.add_argument("--run", type=Path, required=True)
    supplement.add_argument("--env", type=Path, default=Path(".env"))
    supplement.add_argument("--rounds", type=int, default=1)
    supplement.add_argument("--contracts-file", type=Path)
    review = commands.add_parser("review", help="人工复核候选缺陷")
    review.add_argument("--run", type=Path, required=True)
    review.add_argument("--defect", required=True)
    review.add_argument("--decision", choices=["confirm", "test_error", "environment", "uncertain"], required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--category", choices=["boundary", "null_type", "exception_handling", "logic"], help="人工修正缺陷分类")
    review.add_argument("--reviewer", choices=["human", "assistant"], default="human", help="记录实际复核来源")
    report = commands.add_parser("report", help="从保存的记录重新生成 HTML/JSON 报告，不调用 LLM")
    report.add_argument("--run", type=Path, required=True)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            draft_config(args)
        elif args.command == "run":
            config = ProjectConfig.load(args.config)
            if args.backend:
                config.backend = args.backend
            if args.no_repair:
                config.auto_repair = False
            settings = LLMSettings.load(args.env)
            run_dir = new_run(config, settings, args.output)
            print(f"流程结束。验收报告：{run_dir / 'report.html'}")
        elif args.command == "repair":
            repair_run(args.run.resolve(), LLMSettings.load(args.env))
            print(f"修复流程结束。报告：{args.run.resolve() / 'report.html'}")
        elif args.command == "supplement":
            supplement_run(args.run.resolve(), LLMSettings.load(args.env), args.rounds, args.contracts_file)
            print(f"补充验收结束。报告：{args.run.resolve() / 'report.html'}")
        elif args.command == "review":
            review_defect(args.run.resolve(), args.defect, args.decision, args.reason, args.category, args.reviewer)
            print("复核决定及来源已记录；如需修复，请运行 repair。修改测试集后覆盖率需重新执行更新。")
        elif args.command == "report":
            render_report(args.run.resolve(), read_json(args.run / "state.json"))
            print(f"报告：{args.run.resolve() / 'report.html'}")
        return 0
    except KeyboardInterrupt:
        print("流程已中断，已产生的记录保留。", file=sys.stderr)
        return 130
    except (ToolError, OSError, SyntaxError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
