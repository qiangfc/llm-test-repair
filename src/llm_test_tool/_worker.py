"""Standalone pytest/coverage worker. Runs inside the configured execution backend."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import traceback


class ResultsPlugin:
    def __init__(self):
        self.collected: list[str] = []
        self.tests: dict[str, dict] = {}
        self.collection_errors: list[str] = []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(str(report.longrepr))

    def pytest_runtest_logreport(self, report):
        entry = self.tests.setdefault(report.nodeid, {"nodeid": report.nodeid, "outcome": "passed",
                                                      "duration": 0.0, "failure": "", "exception_type": "",
                                                      "properties": {}, "stdout": "", "stderr": ""})
        entry["duration"] += report.duration
        entry["stdout"] = report.capstdout[-12000:]
        entry["stderr"] = report.capstderr[-12000:]
        for key, value in report.user_properties:
            entry["properties"][str(key)] = str(value)[:12000]
        # Pytest repeats the accumulated user_properties on later phase reports. Replace the
        # history each time to preserve loop/parameter observations without triplicating them.
        entry["property_history"] = [{"name": str(key), "value": str(value)[:12000]}
                                     for key, value in report.user_properties]
        if report.failed:
            entry["outcome"] = "failed" if report.when == "call" else "error"
            entry["failure"] = str(report.longrepr)[-24000:]
            crash = getattr(report.longrepr, "reprcrash", None)
            message = getattr(crash, "message", "")
            match = re.search(r"\b([A-Za-z_][A-Za-z_0-9]*(?:Error|Exception))\b", message)
            entry["exception_type"] = match.group(1) if match else "AssertionError" if "assert" in message else "Unknown"
        elif report.skipped and entry["outcome"] == "passed":
            entry["outcome"] = "skipped"
            entry["failure"] = str(report.longrepr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(request["project_root"]).resolve()
    os.chdir(root)
    sys.dont_write_bytecode = True
    for path in reversed(request["python_paths"]):
        sys.path.insert(0, str(root / path))
    # No target-defined addopts or external plugin autoload; requested plugins are explicit.
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ.pop("PYTEST_ADDOPTS", None)
    import coverage
    import pytest

    result = {"exit_code": 3, "collected": [], "tests": {}, "collection_errors": [],
              "coverage": {}, "worker_error": ""}
    plugin = ResultsPlugin()
    cov = coverage.Coverage(branch=True, source=[str(root / path) for path in request["coverage_roots"]],
                            config_file=False, data_file=str(output.parent / ".coverage"))
    cov.start()
    try:
        selections = request["selections"]
        if selections:
            result["exit_code"] = int(pytest.main([
                "-q", "--tb=short", "--strict-markers", "-p", "no:cacheprovider", "-o", "addopts=",
                "-o", "filterwarnings=default", "--rootdir", str(root),
                *[arg for name in request.get("pytest_plugins", []) for arg in ("-p", name)],
                *selections,
            ], plugins=[plugin]))
        else:
            result["exit_code"] = 5
    except BaseException:
        result["worker_error"] = traceback.format_exc()
    finally:
        cov.stop()
        cov.save()
        try:
            coverage_path = output.parent / "coverage.raw.json"
            cov.json_report(outfile=str(coverage_path))
            raw = json.loads(coverage_path.read_text(encoding="utf-8"))
            allowed = set(request["source_files"])
            files = {}
            for name, value in raw.get("files", {}).items():
                path = Path(name)
                path = path.resolve() if path.is_absolute() else (root / path).resolve()
                if path.is_relative_to(root):
                    relative = path.relative_to(root).as_posix()
                    if relative in allowed:
                        files[relative] = value
            result["coverage"] = {"files": files, "meta": raw.get("meta", {})}
        except coverage.exceptions.NoDataError:
            result["coverage"] = {"files": {}, "meta": {}}
        except BaseException:
            result["worker_error"] += "\n覆盖率报告失败:\n" + traceback.format_exc()
        result.update(collected=plugin.collected, tests=plugin.tests, collection_errors=plugin.collection_errors)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
