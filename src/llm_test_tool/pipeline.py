from __future__ import annotations

import ast
from datetime import datetime, timezone
import difflib
import hashlib
from pathlib import Path
import re
import uuid

from .config import LLMSettings, ProjectConfig
from .coverage_adapter import covered_units, reached, summarize, whitebox_context
from .llm import LLMClient, LLMFormatError
from .prompts import assessment_messages, generation_messages
from .repair import RepairEngine, source_context
from .report import render_report
from .runner import TestRunner, failed_nodes, passed_nodes, source_files
from .storage import ToolError, contained, copy_project, file_hash, read_json, read_python, write_json
from .validation import validate_assessment, validate_test


class Pipeline:
    def __init__(self, config: ProjectConfig, run_dir: Path, state: dict, client: LLMClient):
        self.config, self.run_dir, self.state, self.client = config, run_dir, state, client
        self.workspace = run_dir / "workspace"
        self.runner = TestRunner(config, run_dir, self.workspace)
        self.contracts = {item.id: item for item in config.contracts}

    def checkpoint(self):
        self.state["llm_usage"] = self.client.usage()
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        render_report(self.run_dir, self.state)

    def selections(self, mode: str | None = None, *, original=False) -> list[str]:
        generated = [item["path"] for item in self.state["tests"] if item["active"] and (mode is None or item["mode"] == mode)]
        return [*self.config.test_paths, *generated] if original else generated

    def measure(self, label: str) -> dict:
        results = {}
        for mode in ("baseline", "blackbox", "whitebox", "combined", "regression"):
            selections = self.config.test_paths if mode == "baseline" else self.selections(
                mode if mode in {"blackbox", "whitebox"} else None, original=mode == "regression")
            results[mode] = self.runner.run(selections, f"{label}-{mode}")
            self.assert_environment(results[mode])
        return results

    @staticmethod
    def assert_environment(result):
        if result["status"] != "ok":
            raise ToolError(f"执行结果不能用于验收：{result['status']}；{result.get('worker_error', '')[:1500]}")

    def feedback(self, mode: str) -> list[dict]:
        result = []
        for test in self.state["tests"]:
            if test["mode"] != mode:
                continue
            item = {"id": test["id"], "name": test["name"], "scenario": test["scenario"],
                    "requirement_ids": test["requirement_ids"], "outcome": test["outcome"],
                    "input": test["input_description"], "expected": test["expected_result"],
                    "observations": test.get("observations", [])}
            if mode == "whitebox":
                item["failures"] = [entry["failure"] for execution in test["executions"]
                                    for entry in execution["tests"].values() if entry["outcome"] in {"failed", "error"}][:4]
                item["collection_errors"] = [error for execution in test["executions"]
                                             for error in execution.get("collection_errors", [])][:2]
            item["execution_status"] = [execution["status"] for execution in test["executions"]]
            item["collection_exception_types"] = sorted({match for execution in test["executions"]
                for error in execution.get("collection_errors", [])
                for match in re.findall(r"\b[A-Za-z_][A-Za-z_0-9]*(?:Error|Exception)\b", error)})
            result.append(item)
        result.extend({"generation_error": item["reason"], "name": item.get("name", "")}
                      for item in self.state["errors"] if item.get("mode") == mode)
        return result[-60:]

    def generate(self, mode: str, round_number: int, coverage: dict) -> tuple[int, list[dict]]:
        existing = [{key: test[key] for key in ("name", "requirement_ids", "scenario", "input_description", "expected_result")}
                    for test in self.state["tests"] if test["active"] and test["mode"] == mode]
        missing_ids = {item.id for item in self.config.contracts if any(
            not any(test["scenario"] == scenario and item.id in test["requirement_ids"] for test in existing)
            for scenario in ("normal", "boundary", "exception"))}
        scope = [item for item in self.config.contracts if item.id in missing_ids] if missing_ids else self.config.contracts
        context = whitebox_context(coverage, self.workspace, {item.source_file for item in scope}) if mode == "whitebox" else None
        try:
            response = self.client.ask(generation_messages(mode, [item.blackbox_view() for item in scope],
                                       existing, self.feedback(mode), self.config.max_tests_per_round, context),
                                       f"generation/{mode}/round-{round_number}")
        except LLMFormatError as exc:
            return 0, [{"reason": str(exc)}]
        items = response.get("tests")
        if not isinstance(items, list):
            return 0, [{"reason": "生成响应的 tests 必须是列表"}]
        accepted = 0
        rejected = []
        for raw in items[:self.config.max_tests_per_round]:
            try:
                candidate = validate_test(raw, self.contracts)
                # Ignore function names when comparing candidates within one design mode.
                tree = ast.parse(candidate["code"])
                for node in tree.body:
                    if isinstance(node, ast.FunctionDef) and node.name == candidate["name"]:
                        node.name = "test_candidate"
                digest = hashlib.sha256(ast.dump(tree).encode()).hexdigest()
                if any(test["digest"] == digest and test["mode"] == mode and test["active"] for test in self.state["tests"]):
                    raise ToolError("同一模式的重复测试")
            except ToolError as exc:
                rejected.append({"reason": str(exc), "name": raw.get("name", "") if isinstance(raw, dict) else ""})
                continue
            number = sum(item["mode"] == mode for item in self.state["tests"]) + 1
            case_id = ("B" if mode == "blackbox" else "W") + f"{number:03d}"
            relative = f".llm_tests/{mode}/test_{case_id.lower()}.py"
            path = contained(self.workspace, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(candidate["code"] + "\n", encoding="utf-8")
            artifact = self.run_dir / "generated" / f"{case_id}.py"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(candidate["code"] + "\n", encoding="utf-8")
            test = {**candidate, "id": case_id, "mode": mode, "round": round_number, "path": relative,
                    "digest": digest, "active": True, "outcome": "pending", "executions": [], "observations": []}
            self.state["tests"].append(test)
            self.checkpoint()
            for repeat in range(self.config.repeat_tests):
                execution = self.runner.run([relative], f"{case_id}-repeat-{repeat}")
                test["executions"].append(execution)
                for entry in execution["tests"].values():
                    properties = entry.get("properties", {})
                    # No traceback or assertion source is passed to the blackbox generator.
                    test["observations"].append({"node": entry["nodeid"], "outcome": entry["outcome"],
                        "input": properties.get("input", candidate["input_description"]),
                        "expected": properties.get("expected", candidate["expected_result"]),
                        "actual": properties.get("actual", "观察到异常：" + entry["exception_type"]
                                                 if entry["exception_type"] else "未记录实际值"),
                        "property_history": entry.get("property_history", []),
                        "exception_type": entry["exception_type"]})
            executions = test["executions"]
            outcomes = [{node: item["outcome"] for node, item in result["tests"].items()} for result in executions]
            invalid = any(result["status"] != "ok" or not result["collected"] or
                          any(entry["outcome"] in {"skipped", "error"} for entry in result["tests"].values()) for result in executions)
            unstable = any(item != outcomes[0] for item in outcomes[1:])
            if invalid or unstable:
                test.update(active=False, outcome="environment_error" if invalid else "unstable")
                path.unlink()
            elif all(not failed_nodes(result) for result in executions):
                test["outcome"] = "passed"
                accepted += 1
            else:
                test["outcome"] = "failed"
                failures = [entry for result in executions for entry in result["tests"].values() if entry["outcome"] == "failed"]
                try:
                    proposal = self.client.ask(assessment_messages(candidate, failures,
                        [self.contracts[key].blackbox_view() for key in candidate["requirement_ids"]],
                        source_context(self.config, self.workspace, candidate["requirement_ids"])), f"assessment/{case_id}")
                except LLMFormatError as exc:
                    proposal = {"verdict": "uncertain", "explanation": str(exc)}
                try:
                    assessment = validate_assessment(proposal, candidate, self.contracts, self.config.min_defect_confidence)
                except ToolError as exc:
                    assessment = validate_assessment({"verdict": "uncertain", "explanation": str(exc)},
                                                     candidate, self.contracts, self.config.min_defect_confidence)
                test["assessment"] = assessment
                if assessment["verdict"] in {"test_error", "environment"}:
                    test.update(active=False, outcome=assessment["verdict"])
                    path.unlink()
                else:
                    status = "machine_confirmed" if assessment["verdict"] == "defect" and self.config.confirmation_policy == "llm" else "pending_review"
                    if assessment["verdict"] == "uncertain":
                        status = "uncertain"
                    self.state["defects"].append({"id": f"D{len(self.state['defects']) + 1:03d}", "test_id": case_id,
                        "status": status, "assessment": assessment, "reproduction": test["executions"], "repairs": []})
                    accepted += 1
            self.checkpoint()
        return accepted, rejected

    def audit_merged_failures(self, measured: dict, round_number: int) -> bool:
        """Assess reproducible failures that appear only when generated tests run together."""
        first = measured["combined"]
        candidates = [test for test in self.state["tests"] if test["active"] and test["outcome"] == "passed"
                      and any(node.split("::")[0] == test["path"] for node in failed_nodes(first))]
        if not candidates:
            return False
        calls_before = self.client.calls
        repeats = [first] + [self.runner.run(self.selections(), f"round-{round_number}-merge-audit-{i}")
                             for i in range(1, self.config.repeat_tests)]
        for result in repeats:
            self.assert_environment(result)
        changed = False
        for test in candidates:
            repro = {node for node in failed_nodes(first) if node.split("::")[0] == test["path"]}
            test["merged_executions"] = repeats
            if any(not repro <= failed_nodes(result) for result in repeats):
                test.update(active=False, outcome="unstable_in_merged_suite")
                contained(self.workspace, test["path"]).unlink()
                changed = True
                continue
            failures = [entry for result in repeats for node, entry in result["tests"].items() if node in repro]
            try:
                metadata = {key: test[key] for key in ("name", "code", "requirement_ids", "scenario",
                                                      "input_description", "expected_result", "reason")}
                proposal = self.client.ask(assessment_messages(metadata, failures,
                    [self.contracts[key].blackbox_view() for key in test["requirement_ids"]],
                    source_context(self.config, self.workspace, test["requirement_ids"])), f"assessment/merged/{test['id']}")
            except LLMFormatError as exc:
                proposal = {"verdict": "uncertain", "explanation": str(exc)}
            try:
                assessment = validate_assessment(proposal, test, self.contracts, self.config.min_defect_confidence)
            except ToolError as exc:
                assessment = validate_assessment({"verdict": "uncertain", "explanation": str(exc)}, test, self.contracts,
                                                 self.config.min_defect_confidence)
            test.update(outcome="failed", assessment=assessment)
            if assessment["verdict"] in {"test_error", "environment"}:
                test.update(active=False, outcome=assessment["verdict"])
                contained(self.workspace, test["path"]).unlink()
                changed = True
            else:
                status = "machine_confirmed" if assessment["verdict"] == "defect" and self.config.confirmation_policy == "llm" else "pending_review"
                if assessment["verdict"] == "uncertain":
                    status = "uncertain"
                self.state["defects"].append({"id": f"D{len(self.state['defects']) + 1:03d}", "test_id": test["id"],
                    "status": status, "assessment": assessment, "reproduction": repeats, "repairs": [], "discovery": "merged_suite"})
            self.checkpoint()
        self.state.setdefault("merge_audits", []).append({"round": round_number,
            "llm_calls": self.client.calls - calls_before, "test_ids": [test["id"] for test in candidates], "executions": repeats})
        return changed

    def scenario_coverage(self) -> dict:
        return {mode: {key: sorted({test["scenario"] for test in self.state["tests"] if test["active"]
                    and test["mode"] == mode and key in test["requirement_ids"] and test["outcome"] in {"passed", "failed"}})
                    for key in self.contracts} for mode in ("blackbox", "whitebox")}

    def generate_rounds(self):
        baseline = self.runner.run(self.config.test_paths, "initial-baseline")
        self.assert_environment(baseline)
        self.state["initial_baseline"] = baseline
        raw = baseline["coverage"]
        previous_units = covered_units(raw)
        previous_scenarios = set()
        stagnation = 0
        for round_number in range(1, self.config.max_rounds + 1):
            print(f"第 {round_number} 轮：生成黑盒与白盒单元测试", flush=True)
            for mode in ("blackbox", "whitebox"):
                calls_before = self.client.calls
                accepted, rejected = self.generate(mode, round_number, raw)
                suite = self.runner.run(self.selections(mode), f"round-{round_number}-{mode}")
                self.assert_environment(suite)
                self.state["rounds"].append({"round": round_number, "mode": mode, "accepted": accepted,
                    "runnable_tests": len(passed_nodes(suite) | {node for node, item in suite["tests"].items() if item["outcome"] == "failed"}),
                    "coverage": summarize(suite["coverage"]), "llm_calls": self.client.calls - calls_before,
                    "cumulative_llm_calls": self.client.calls, "suite": suite, "rejected_generation": rejected})
                self.state["errors"].extend({"stage": "generation", "mode": mode, "round": round_number, **item} for item in rejected)
                # Whitebox feedback sees what original and all generated tests already cover.
                all_tests = self.runner.run(self.selections(original=True), f"round-{round_number}-{mode}-regression")
                self.assert_environment(all_tests)
                raw = all_tests["coverage"]
                self.state["latest_regression"] = all_tests
                self.checkpoint()
            measured = self.measure(f"round-{round_number}")
            if self.audit_merged_failures(measured, round_number):
                measured = self.measure(f"round-{round_number}-after-audit")
            self.state["coverage_before_repair"] = measured
            self.state["scenario_coverage"] = self.scenario_coverage()
            units = covered_units(measured["combined"]["coverage"])
            scenarios = {(mode, key, scene) for mode, requirements in self.state["scenario_coverage"].items()
                         for key, scenes in requirements.items() for scene in scenes}
            improvement = bool(units - previous_units or scenarios - previous_scenarios)
            stagnation = 0 if improvement else stagnation + 1
            previous_units, previous_scenarios = units, scenarios
            complete_scenarios = all(len(scenes) == 3 for requirements in self.state["scenario_coverage"].values() for scenes in requirements.values())
            if complete_scenarios and reached(measured["combined"]["coverage"], self.config.target_line_coverage, self.config.target_branch_coverage):
                self.state["stop_reason"] = "两类测试的正常/边界/异常场景齐全，合并覆盖率达到目标"
                break
            if stagnation >= self.config.stagnation_rounds:
                self.state["stop_reason"] = "连续若干轮没有新增覆盖行/分支或需求场景"
                break
            self.checkpoint()
        else:
            self.state["stop_reason"] = "达到最大生成轮数"
        self.state["source_hashes_before_repair"] = {name: file_hash(contained(self.workspace, name)) for name in self.runner.files}
        self.checkpoint()

    def repair_confirmed(self):
        self.state["status"] = "repairing"
        self.state["repair_started"] = True
        engine = RepairEngine(self.config, self.runner, self.client)
        by_id = {test["id"]: test for test in self.state["tests"]}
        order = {key: index for index, key in enumerate(("boundary", "null_type", "exception_handling", "logic"))}
        for defect in sorted(self.state["defects"], key=lambda item: order[item["assessment"]["category"]]):
            if defect["status"] not in {"machine_confirmed", "human_confirmed"}:
                continue
            print(f"分类修复 {defect['id']}：{defect['assessment']['category']}", flush=True)
            record = engine.repair(defect, by_id[defect["test_id"]], self.selections(original=True))
            if record["status"] == "verified":
                defect["status"] = "fixed"
            elif record["status"] == "not_reproduced":
                previous = [item["id"] for item in self.state["defects"] if item["status"] == "fixed"]
                test = by_id[defect["test_id"]]
                reproduced = {node for execution in defect["reproduction"] for node in failed_nodes(execution)
                              if node.split("::")[0] == test["path"]}
                if previous and reproduced and all(reproduced <= passed_nodes(result) for result in record["before"]):
                    defect["status"] = "resolved_by_other_patch"
                    record.update(status="resolved_by_other_patch", related_verified_defects=previous,
                                  reason="初始记录中失败的复现测试，在先前已通过回归验证的补丁后连续通过")
            self.checkpoint()
        self.save_patch()

    def save_patch(self):
        # A cumulative, reviewable patch against the untouched project snapshot.
        diff = []
        for name in self.runner.files:
            before = read_python(contained(self.run_dir / "original_sources", name))
            after = read_python(contained(self.workspace, name))
            if before != after:
                diff.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="a/" + name, tofile="b/" + name))
        patch = self.run_dir / "patches" / "final.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text("".join(diff), encoding="utf-8")
        self.state["accepted_patch"] = "patches/final.patch"

    def execute(self, *, generation=True):
        if not generation:
            self.state["coverage_stale"] = True
        self.checkpoint()
        try:
            if generation:
                self.generate_rounds()
            if self.config.auto_repair or not generation:
                self.repair_confirmed()
            self.state["coverage_final"] = self.measure("final")
            self.state["coverage_stale"] = False
            self.state["source_hashes_final"] = {name: file_hash(contained(self.workspace, name)) for name in self.runner.files}
            self.state["status"] = "completed"
        except KeyboardInterrupt:
            self.state.update(status="interrupted", stop_reason="用户中断；记录和工作副本已保留")
            raise
        except Exception as exc:
            self.state.update(status="failed", stop_reason=self.client.redact(str(exc)))
            self.state["errors"].append({"stage": "pipeline", "reason": self.client.redact(str(exc))})
            if isinstance(exc, ToolError):
                raise
            raise ToolError(f"流程中止，已有记录保存在 {self.run_dir}：{self.client.redact(str(exc))}") from exc
        finally:
            if self.state.get("repair_started"):
                self.save_patch()
            self.checkpoint()


def new_run(config: ProjectConfig, settings: LLMSettings, output: Path) -> Path:
    run_dir = output.resolve() / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    if output.resolve() == config.project_path.resolve() or config.project_path.resolve().is_relative_to(output.resolve()):
        raise ToolError("输出目录不能是目标项目本身或其上级目录")
    run_dir.mkdir(parents=True, exist_ok=False)
    copy_project(config.project_path, run_dir / "workspace", exclude=output.resolve())
    files = source_files(config, run_dir / "workspace")
    hashes = {}
    for name in files:
        path = contained(run_dir / "original_sources", name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contained(run_dir / "workspace", name).read_bytes())
        hashes[name] = file_hash(path)
    write_json(run_dir / "config.snapshot.json", config.serializable())
    state = {"schema_version": 1, "name": config.name, "status": "generating", "stop_reason": "",
             "created_at": datetime.now(timezone.utc).isoformat(), "tests": [], "defects": [], "rounds": [], "errors": [],
             "provenance": {"repository_url": config.repository_url, "configured_revision": config.revision,
                            "source_project": str(config.project_path), "source_hashes": hashes,
                            "coverup_version": "v0.6.3", "coverup_commit": "c4ad2f13c74c1154761038cf924ae5ece27dffdc",
                            "backend": config.backend, "confirmation_policy": config.confirmation_policy}}
    print(f"本次记录目录：{run_dir}", flush=True)
    Pipeline(config, run_dir, state, LLMClient(settings, run_dir)).execute()
    return run_dir


def repair_run(run_dir: Path, settings: LLMSettings):
    state = read_json(run_dir / "state.json")
    data = read_json(run_dir / "config.snapshot.json")
    data["project_path"] = str((run_dir / "workspace").resolve())
    # Preserve the original snapshot file; validate the saved workspace through a separate resume config.
    write_json(run_dir / "config.resume.json", data)
    config = ProjectConfig.load(run_dir / "config.resume.json")
    Pipeline(config, run_dir, state, LLMClient(settings, run_dir)).execute(generation=False)


def supplement_run(run_dir: Path, settings: LLMSettings, rounds: int, contracts_file: Path | None = None):
    """Generate replacements after review; preserve the original run and patch evidence."""
    if rounds < 1 or rounds > 10:
        raise ToolError("补充轮数必须为 1—10")
    state = read_json(run_dir / "state.json")
    if state["status"] not in {"completed", "failed", "interrupted"}:
        raise ToolError("请等当前执行结束后再补充测试")
    data = read_json(run_dir / "config.snapshot.json")
    data["project_path"] = str((run_dir / "workspace").resolve())
    if contracts_file:
        contracts = read_json(contracts_file)["contracts"]
        if {item["id"] for item in contracts} != {item["id"] for item in data["contracts"]}:
            raise ToolError("补充约定需保留原需求编号；新增测试对象请新建 run")
        data["contracts"] = contracts
    supplement_dir = run_dir / "supplements" / uuid.uuid4().hex[:8]
    write_json(supplement_dir / "config.json", data)
    config = ProjectConfig.load(supplement_dir / "config.json")
    client = LLMClient(settings, run_dir)
    pipeline = Pipeline(config, run_dir, state, client)
    stage = {"artifact_path": str(supplement_dir.relative_to(run_dir)), "maximum_rounds": rounds,
             "source_hashes": {name: file_hash(contained(pipeline.workspace, name)) for name in pipeline.runner.files},
             "reason": "复核排除错误用例后补齐需求场景；本阶段使用当前工作副本，不混入最初修复前的覆盖率。"}
    state.setdefault("supplements", []).append(stage)
    state.update(status="supplementing", coverage_stale=True)
    pipeline.checkpoint()
    try:
        offset = max((item["round"] for item in state["rounds"]), default=0)
        current = pipeline.runner.run(pipeline.selections(original=True), "supplement-baseline")
        pipeline.assert_environment(current)
        raw = current["coverage"]
        for index in range(rounds):
            number = offset + index + 1
            for mode in ("blackbox", "whitebox"):
                if all(len(scenes) == 3 for scenes in pipeline.scenario_coverage()[mode].values()):
                    continue
                before_calls = client.calls
                accepted, rejected = pipeline.generate(mode, number, raw)
                suite = pipeline.runner.run(pipeline.selections(mode), f"supplement-{number}-{mode}")
                pipeline.assert_environment(suite)
                state["rounds"].append({"round": number, "phase": "supplement", "mode": mode, "accepted": accepted,
                    "runnable_tests": len(passed_nodes(suite) | failed_nodes(suite)), "coverage": summarize(suite["coverage"]),
                    "llm_calls": client.calls - before_calls, "cumulative_llm_calls": client.calls,
                    "suite": suite, "rejected_generation": rejected})
                state["errors"].extend({"stage": "generation", "mode": mode, "round": number, **item} for item in rejected)
                current = pipeline.runner.run(pipeline.selections(original=True), f"supplement-{number}-{mode}-all")
                pipeline.assert_environment(current)
                raw = current["coverage"]
                pipeline.checkpoint()
            state["scenario_coverage"] = pipeline.scenario_coverage()
            if all(len(scenes) == 3 for requirements in state["scenario_coverage"].values() for scenes in requirements.values()):
                break
        stage["status"] = "generated"
        # The same strict repair and final measurement path also validates previously accepted patches
        # against the expanded suite; original per-attempt evidence remains intact.
        pipeline.execute(generation=False)
        stage["status"] = "completed"
    except BaseException as exc:
        stage.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", reason=client.redact(str(exc)))
        state["status"] = stage["status"]
        raise
    finally:
        pipeline.checkpoint()


def review_defect(run_dir: Path, defect_id: str, decision: str, reason: str, category: str | None = None,
                  reviewer: str = "human"):
    if len(reason.strip()) < 4:
        raise ToolError("人工审查须提供具体理由")
    state = read_json(run_dir / "state.json")
    defect = next((item for item in state["defects"] if item["id"] == defect_id), None)
    if not defect:
        raise ToolError(f"找不到缺陷 {defect_id}")
    if defect["status"] in {"fixed", "resolved_by_other_patch"}:
        raise ToolError("该缺陷已有验证通过的补丁，请在报告中审查补丁记录")
    test = next(item for item in state["tests"] if item["id"] == defect["test_id"])
    defect["review"] = {"decision": decision, "reason": reason, "reviewer": reviewer,
                       "at": datetime.now(timezone.utc).isoformat()}
    if category:
        defect["review"]["previous_category"] = defect["assessment"]["category"]
        defect["assessment"]["category"] = category
        defect["review"]["category"] = category
    defect["status"] = {"confirm": "human_confirmed" if reviewer == "human" else "machine_confirmed",
                        "test_error": "test_error", "environment": "environment", "uncertain": "uncertain"}[decision]
    if decision in {"test_error", "environment"}:
        test.update(active=False, outcome=decision)
        contained(run_dir / "workspace", test["path"]).unlink(missing_ok=True)
    elif decision == "confirm" and not test["active"]:
        raise ToolError("已排除的测试不能直接确认，请先重新生成有效复现用例")
    state["coverage_stale"] = True
    state["scenario_coverage"] = {mode: {contract["id"]: sorted({test["scenario"] for test in state["tests"]
        if test["active"] and test["mode"] == mode and contract["id"] in test["requirement_ids"]
        and test["outcome"] in {"passed", "failed"}}) for contract in read_json(run_dir / "config.snapshot.json")["contracts"]}
        for mode in ("blackbox", "whitebox")}
    render_report(run_dir, state)
