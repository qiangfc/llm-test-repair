"""Independently cross-check saved test, coverage and regression evidence. No API calls."""
from collections import Counter
import difflib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dotenv import dotenv_values
from llm_test_tool.coverage_adapter import covered_units, summarize
from llm_test_tool.runner import failed_nodes, passed_nodes
from llm_test_tool.storage import contained, file_hash, read_json, read_python, write_json


def audit(run_dir: Path) -> dict:
    state = read_json(run_dir / "state.json")
    checks = []
    def check(name, ok, detail=""):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
    check("pipeline_completed", state["status"] == "completed", state["status"])
    check("generated_tests_exist", bool(state["tests"]))
    check("report_matches_state", read_json(run_dir / "report.json") == state)
    final_regression = state.get("coverage_final", {}).get("regression", {})
    check("final_regression_all_passed", bool(final_regression.get("tests")) and
          all(item["outcome"] == "passed" for item in final_regression.get("tests", {}).values()))
    check("no_unresolved_candidates", not [item for item in state["defects"] if item["status"] in
          {"machine_confirmed", "human_confirmed", "pending_review", "uncertain"}])
    config = read_json(run_dir / "config.snapshot.json")
    check("all_requirement_scenarios", all(
        set(state.get("scenario_coverage", {}).get(mode, {}).get(contract["id"], [])) == {"normal", "boundary", "exception"}
        for mode in ("blackbox", "whitebox") for contract in config["contracts"]),
        str(state.get("scenario_coverage", {})))
    for phase in ("coverage_before_repair", "coverage_final"):
        groups = state.get(phase, {})
        check(phase + "/all_five_test_sets", set(groups) == {"baseline", "blackbox", "whitebox", "combined", "regression"})
        if not groups:
            continue
        black, white, combined = [groups[mode] for mode in ("blackbox", "whitebox", "combined")]
        check(phase + "/measured_union", covered_units(black["coverage"]) | covered_units(white["coverage"]) == covered_units(combined["coverage"]))
        check(phase + "/collected_union", set(black["collected"]) | set(white["collected"]) == set(combined["collected"]))
        for mode, result in groups.items():
            check(phase + "/" + mode + "/execution", result["status"] == "ok")
            check(phase + "/" + mode + "/coverage_exists", bool(result["coverage"]["files"]))
            directory = run_dir / result["artifact_path"]
            check(phase + "/" + mode + "/saved_result", read_json(directory / "result.json") == result)
    original = Path(state["provenance"]["source_project"])
    check("original_project_sources_unchanged", all(file_hash(contained(original, name)) == digest
          for name, digest in state["provenance"]["source_hashes"].items()))
    check("source_snapshots_match_fingerprints", all(file_hash(contained(run_dir / "original_sources", name)) == digest
          for name, digest in state["provenance"]["source_hashes"].items()))
    check("final_workspace_matches_fingerprints", bool(state.get("source_hashes_final")) and all(
        file_hash(contained(run_dir / "workspace", name)) == digest for name, digest in state.get("source_hashes_final", {}).items()))
    if state.get("accepted_patch"):
        diff = []
        for name in sorted(state["provenance"]["source_hashes"]):
            before = read_python(contained(run_dir / "original_sources", name))
            after = read_python(contained(run_dir / "workspace", name))
            diff.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="a/" + name, tofile="b/" + name))
        check("final_patch_matches_workspace", contained(run_dir, state["accepted_patch"]).read_text(encoding="utf-8") == "".join(diff))
    check("active_generated_tests_unmodified", all(
        contained(run_dir / "workspace", item["path"]).read_text(encoding="utf-8") == item["code"] + "\n"
        for item in state["tests"] if item["active"]))
    for relative in config["test_paths"]:
        base = contained(original, relative)
        paths = [base] if base.is_file() else list(base.rglob("*.py"))
        check("original_tests_unmodified/" + relative, all(
            file_hash(path) == file_hash(contained(run_dir / "workspace", path.relative_to(original).as_posix())) for path in paths))
    tests = {item["id"]: item for item in state["tests"]}
    for defect in state["defects"]:
        for number, repair in enumerate(defect["repairs"]):
            for attempt in repair["attempts"]:
                if attempt["status"] != "verified":
                    continue
                label = f"{defect['id']}/repair-{number}/attempt-{attempt['number']}"
                before = repair["before"]
                repro = {node for node in failed_nodes(before[0]) if node.split("::")[0] == tests[defect["test_id"]]["path"]}
                passes = set().union(*(passed_nodes(result) for result in before))
                collected = set().union(*(set(result["collected"]) for result in before))
                allowed_failures = set.intersection(*(failed_nodes(result) for result in before)) - repro
                check(label + "/failed_before", bool(repro) and all(repro <= failed_nodes(result) for result in before))
                check(label + "/passed_after", len(attempt["after"]) >= config["repeat_tests"] and
                      all(repro <= passed_nodes(result) and result["status"] == "ok" for result in attempt["after"]))
                check(label + "/regression", all(passes <= passed_nodes(result) and collected <= set(result["collected"])
                      and failed_nodes(result) <= allowed_failures for result in attempt["after"]))
    for path in sorted((run_dir / "llm_calls").glob("*.json")):
        call = read_json(path)
        if not call["purpose"].startswith("generation/"):
            continue
        message = call["messages"][1]["content"]
        payload = json.loads(message[message.index("{"):])
        if "/blackbox/" in call["purpose"]:
            check(path.name + "/blackbox_no_source", "source_and_missing_coverage" not in payload and
                  all("source_file" not in contract for contract in payload["contracts"]) and
                  all("failures" not in feedback and "collection_errors" not in feedback for feedback in payload["execution_feedback"]))
        else:
            check(path.name + "/whitebox_has_source", bool(payload.get("source_and_missing_coverage")))
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    key = env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY")
    if key:
        leaked = [str(path.relative_to(run_dir)) for path in run_dir.rglob("*")
                  if path.is_file() and path.suffix in {".json", ".html", ".md", ".log", ".py", ".patch"}
                  and key in path.read_text(encoding="utf-8", errors="replace")]
        check("api_key_not_in_artifacts", not leaked, str(leaked))
    summary = {mode: {**summarize(result["coverage"]), "outcomes": dict(Counter(item["outcome"] for item in result["tests"].values()))}
               for mode, result in state.get("coverage_final", {}).items()}
    result = {"run": str(run_dir), "passed": all(item["passed"] for item in checks), "checks": checks,
              "check_count": len(checks), "failed_checks": [item for item in checks if not item["passed"]],
              "final_coverage": summary, "defects": dict(Counter(item["status"] for item in state["defects"]))}
    write_json(run_dir / "audit.json", result)
    return result


if __name__ == "__main__":
    result = audit(Path(sys.argv[1]).resolve())
    print(f"Evidence checks: {result['check_count']}; passed: {result['passed']}")
    for item in result["failed_checks"]:
        print(item["check"], item["detail"])
    raise SystemExit(0 if result["passed"] else 1)
