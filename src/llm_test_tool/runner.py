from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import uuid

from .config import ProjectConfig
from .storage import ToolError, contained, copy_project, file_hash, read_json, write_json


def source_files(config: ProjectConfig, root: Path) -> list[str]:
    files = set()
    test_roots = [contained(root, path) for path in config.test_paths]
    for relative in config.source_paths:
        source = contained(root, relative)
        candidates = [source] if source.is_file() else source.rglob("*.py")
        for path in candidates:
            if path.suffix != ".py" or path.is_symlink():
                continue
            if any(part in {".git", ".venv", "__pycache__", ".llm_tests", ".llm_runner", ".cache"}
                   for part in path.relative_to(root).parts):
                continue
            if any(path == base or path.is_relative_to(base) for base in test_roots):
                continue
            files.add(path.relative_to(root).as_posix())
    if not files:
        raise ToolError("source_paths 中没有可测量的 Python 源文件")
    return sorted(files)


def clean_environment() -> dict[str, str]:
    # Do not pass model/cloud credentials, dotenv variables or pytest addopts to target code.
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
               "LANG", "LC_ALL", "TZ", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONIOENCODING="utf-8")
    return result


class TestRunner:
    def __init__(self, config: ProjectConfig, run_dir: Path, workspace: Path):
        self.config = config
        self.run_dir = run_dir
        self.workspace = workspace
        self.files = source_files(config, workspace)

    def run(self, selections: list[str], label: str) -> dict:
        job_id = uuid.uuid4().hex[:12]
        job = self.run_dir / "executions" / f"{label}-{job_id}"
        job.mkdir(parents=True, exist_ok=False)
        worker = Path(__file__).with_name("_worker.py")
        roots = sorted({(Path(path).parent.as_posix() if contained(self.workspace, path).is_file() else path)
                        for path in self.config.source_paths})
        request = {"project_root": "/workspace", "python_paths": self.config.python_paths,
                   "coverage_roots": roots, "source_files": self.files, "selections": selections,
                   "pytest_plugins": self.config.pytest_plugins}
        local_copy = None
        protected = {}
        if self.config.backend == "docker":
            name = "llm-test-" + job_id
            write_json(job / "request.json", request)
            command = ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128",
                       "--memory", "768m", "--cpus", "1", "--tmpfs", "/tmp:rw,nosuid,size=256m",
                       "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                       "--mount", f"type=bind,source={self.workspace.resolve()},target=/workspace,readonly",
                       "--mount", f"type=bind,source={job.resolve()},target=/artifacts",
                       "--mount", f"type=bind,source={worker.resolve()},target=/worker.py,readonly",
                       "--workdir", "/workspace", self.config.docker_image,
                       "python", "/worker.py", "--request", "/artifacts/request.json", "--output", "/artifacts/result.json"]
        else:
            # Each invocation has a fresh disposable copy, so a test cannot silently change the
            # controller's accepted source or test files. Local mode is still NOT a security sandbox.
            local_copy = job / "project"
            copy_project(self.workspace, local_copy, include_generated=True)
            request["project_root"] = str(local_copy.resolve())
            write_json(job / "request.json", request)
            protected = {p.relative_to(local_copy).as_posix(): file_hash(p) for p in local_copy.rglob("*.py")}
            command = [self.config.local_python or sys.executable, str(worker), "--request", str(job / "request.json"),
                       "--output", str(job / "result.json")]
            name = ""
        write_json(job / "command.json", {"backend": self.config.backend, "command": command})
        try:
            completed = subprocess.run(command, cwd=self.workspace, env=clean_environment(),
                                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       timeout=self.config.timeout_seconds, **self._process_options())
            (job / "stdout.log").write_text(completed.stdout, encoding="utf-8")
            (job / "stderr.log").write_text(completed.stderr, encoding="utf-8")
        except subprocess.TimeoutExpired:
            if name:
                try:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20,
                                   env=clean_environment(), **self._process_options())
                except (OSError, subprocess.TimeoutExpired):
                    pass
            result = self._error("timeout", "测试执行超时", job)
            write_json(job / "result.json", result)
            return result
        except OSError as exc:
            raise ToolError(f"无法启动 {self.config.backend} 测试执行器: {exc}") from exc
        if completed.returncode or not (job / "result.json").is_file():
            raise ToolError(f"测试执行器失败，详情见 {job / 'stderr.log'}；请检查运行镜像/解释器及目标项目依赖")
        result = read_json(job / "result.json")
        result["artifact_path"] = str(job.relative_to(self.run_dir))
        result["status"] = "ok" if result["exit_code"] in {0, 1, 5} and not result["worker_error"] else "environment_error"
        if local_copy:
            after = {p.relative_to(local_copy).as_posix(): file_hash(p) for p in local_copy.rglob("*.py")}
            if protected != after:
                result["status"] = "integrity_error"
                result["worker_error"] = "测试执行期间修改了源文件或测试文件，此次结果不用于验收"
        write_json(job / "result.json", result)
        return result

    @staticmethod
    def _process_options() -> dict:
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    @staticmethod
    def _error(status: str, message: str, job: Path) -> dict:
        return {"status": status, "exit_code": -1, "worker_error": message, "tests": {}, "collected": [],
                "collection_errors": [], "coverage": {"files": {}}, "artifact_path": str(job)}


def passed_nodes(result: dict) -> set[str]:
    return {key for key, item in result.get("tests", {}).items() if item["outcome"] == "passed"}


def failed_nodes(result: dict) -> set[str]:
    return {key for key, item in result.get("tests", {}).items() if item["outcome"] in {"failed", "error"}}
