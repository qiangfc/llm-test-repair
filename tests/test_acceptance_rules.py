from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_test_tool.config import Contract, ProjectConfig
from llm_test_tool.llm import LLMClient
from llm_test_tool.prompts import generation_messages
from llm_test_tool.pipeline import Pipeline
from llm_test_tool.repair import RepairEngine, prepare_patch
from llm_test_tool.report import render_report
from llm_test_tool.runner import TestRunner as Runner, passed_nodes
from llm_test_tool.storage import ToolError, copy_project, write_json


SOURCE = "def calculate(value):\n    return value + 1\n"
PROPOSAL = {"edits": [{"path": "src/calc.py", "old": "return value + 1", "new": "return value + 0"}]}


@pytest.mark.parametrize("proposal", [
    {"edits": [{"path": "../escape.py", "old": "return value + 1", "new": "return value"}]},
    {"edits": [{"path": "src/calc.py", "old": "missing", "new": "return value"}]},
    {"edits": [{"path": "src/calc.py", "old": "calculate(value)", "new": "calculate(value, other)"}]},
])
def test_patch_cannot_escape_scope_or_change_api(proposal):
    with pytest.raises(ToolError):
        prepare_patch(proposal, {"src/calc.py": SOURCE}, 20)


@pytest.mark.parametrize("regression", [True, False])
def test_patch_acceptance_requires_repro_and_regression_and_restores_bytes(tmp_path, regression):
    workspace = tmp_path / "workspace"
    source = workspace / "src/calc.py"
    source.parent.mkdir(parents=True)
    original = SOURCE.replace("\n", "\r\n").encode()
    source.write_bytes(original)
    test_path = ".llm_tests/blackbox/test_b001.py"
    generated = workspace / test_path
    generated.parent.mkdir(parents=True)
    generated.write_text("# protected reproduction\n", encoding="utf-8")
    contract = Contract("REQ-1", "calc", "calculate", "calculate(value)", "返回原值", "src/calc.py", ["返回原值"])
    config = ProjectConfig("fake", workspace, ["src"], [], ["src"], [contract],
                           repeat_tests=2, max_repair_attempts=1)
    repro = test_path + "::test_repro"
    existing = "tests/test_existing.py::test_ok"
    def run(selections, label):
        after = "return value + 0" in source.read_text()
        return {"status": "ok", "collected": [repro, existing], "tests": {
            repro: {"outcome": "passed" if after else "failed", "failure": "wrong result"},
            existing: {"outcome": "failed" if after and regression else "passed", "failure": "regression"}}}
    runner = SimpleNamespace(workspace=workspace, run_dir=tmp_path, run=run)
    client = SimpleNamespace(calls=0, ask=lambda *args: PROPOSAL)
    defect = {"id": "D001", "test_id": "B001", "status": "machine_confirmed", "assessment": {"category": "logic"}}
    test = {"path": test_path, "name": "test_repro", "code": "assert calculate(1) == 1",
            "requirement_ids": ["REQ-1"], "scenario": "normal", "input_description": "1", "expected_result": "1"}
    result = RepairEngine(config, runner, client).repair(defect, test, [test_path])
    assert result["status"] == ("rejected" if regression else "verified")
    if regression:
        assert source.read_bytes() == original
        assert "回归" in result["attempts"][0]["reason"]
    else:
        assert "return value + 0" in source.read_text()
    assert generated.read_text() == "# protected reproduction\n"
    assert defect["repairs"] == [result]


def test_blackbox_request_has_no_source_context():
    request = generation_messages("blackbox", [{"id": "REQ-1", "signature": "calculate(value)"}], [], [], 3,
                                  [{"source_excerpt": "SOURCE_SENTINEL"}])
    assert "SOURCE_SENTINEL" not in request[1]["content"]
    assert "source_and_missing_coverage" not in request[1]["content"]
    white = generation_messages("whitebox", [], [], [], 3, [{"source_excerpt": "SOURCE_SENTINEL"}])
    assert "SOURCE_SENTINEL" in white[1]["content"]


def test_resume_client_preserves_budget_and_log_numbers(tmp_path):
    write_json(tmp_path / "llm_calls/0003.json", {"call": 3, "tokens_charged": 17})
    client = LLMClient(SimpleNamespace(model="fake", max_calls=60, max_total_tokens=1000), tmp_path)
    assert client.calls == 3
    assert client.total_tokens == 17


def test_local_worker_executes_demo_and_records_actual_coverage(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = ProjectConfig.load(root / "examples/demo-project.json")
    config.backend = "local"
    workspace = tmp_path / "workspace"
    copy_project(config.project_path, workspace)
    result = Runner(config, tmp_path, workspace).run(config.test_paths, "baseline")
    assert result["status"] == "ok"
    assert len(passed_nodes(result)) == 2
    coverage = result["coverage"]["files"]["src/demo_calc/__init__.py"]
    assert coverage["summary"]["num_statements"] > 0
    assert coverage["summary"]["num_branches"] > 0
    assert coverage["missing_branches"]


def test_worker_keeps_all_loop_observations(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = ProjectConfig.load(root / "examples/demo-project.json")
    config.backend = "local"
    workspace = tmp_path / "workspace"
    copy_project(config.project_path, workspace)
    path = workspace / "tests/test_history.py"
    path.write_text("from demo_calc import discount_total\n"
        "def test_history(record_property):\n"
        "    for rate in (0, 0.2):\n"
        "        actual = discount_total(100, rate)\n"
        "        record_property('input', rate)\n"
        "        record_property('expected', 100 * (1 - rate))\n"
        "        record_property('actual', actual)\n"
        "        assert actual == 100 * (1 - rate)\n", encoding="utf-8")
    result = Runner(config, tmp_path, workspace).run(["tests/test_history.py"], "history")
    assert result["status"] == "ok"
    entry = next(iter(result["tests"].values()))
    assert entry["outcome"] == "passed"
    assert [item["value"] for item in entry["property_history"] if item["name"] == "input"] == ["0", "0.2"]
    assert len(entry["property_history"]) == 6


def test_report_distinguishes_initial_failure_from_final_pass(tmp_path):
    path = ".llm_tests/blackbox/test_b001.py"
    result = {"tests": {path + "::test_one": {"outcome": "passed"}}, "collected": [path + "::test_one"],
              "coverage": {"files": {}}, "status": "ok"}
    state = {"status": "completed", "tests": [{"id": "B001", "path": path, "name": "test_one", "mode": "blackbox",
             "scenario": "boundary", "outcome": "failed", "code": "# <script>"}], "defects": [],
             "coverage_final": {"regression": result}, "scenario_coverage": {"blackbox": {"REQ-1": ["boundary"]}}}
    render_report(tmp_path, state)
    assert state["tests"][0]["outcome"] == "failed"
    assert state["tests"][0]["final_outcomes"] == {"passed": 1}
    assert state["acceptance_summary"]["all_requirement_scenarios"] is False
    text = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_generation_prioritizes_requirement_with_missing_scene(tmp_path):
    import json
    root = Path(__file__).resolve().parents[1]
    config = ProjectConfig.load(root / "examples/demo-project.json")
    copy_project(config.project_path, tmp_path / "workspace")
    tests = []
    for contract in config.contracts:
        scenes = ("normal", "boundary", "exception") if contract.id == "DEMO-DISCOUNT" else ("boundary", "exception")
        for scene in scenes:
            tests.append({"active": True, "name": "test_" + scene, "mode": "blackbox", "scenario": scene,
                          "requirement_ids": [contract.id], "input_description": "input", "expected_result": "expected",
                          "id": str(len(tests)), "outcome": "passed", "executions": [], "observations": []})
    captured = []
    def ask(messages, purpose):
        captured.append(messages)
        return {"tests": []}
    state = {"tests": tests, "errors": []}
    pipeline = Pipeline(config, tmp_path, state, SimpleNamespace(ask=ask))
    assert pipeline.generate("blackbox", 3, {}) == (0, [])
    message = captured[0][1]["content"]
    payload = json.loads(message[message.index("{"):])
    assert [item["id"] for item in payload["contracts"]] == ["DEMO-MEAN"]
    assert payload["missing_requirement_scenarios"] == {"DEMO-MEAN": ["normal"]}
