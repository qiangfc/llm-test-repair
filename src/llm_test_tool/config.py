from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import math
import os
from urllib.parse import urlparse

from .storage import ToolError, contained, read_json


@dataclass
class Contract:
    id: str
    module: str
    symbol: str
    signature: str
    description: str
    source_file: str
    expected_behavior: list[str]
    normal_examples: list[str] = field(default_factory=list)
    boundary_cases: list[str] = field(default_factory=list)
    exception_cases: list[str] = field(default_factory=list)

    def blackbox_view(self) -> dict:
        # The source file mapping is controller-only, not sent to this generator.
        return {key: value for key, value in asdict(self).items() if key != "source_file"}

    def evidence_text(self) -> str:
        return "\n".join([self.description, *self.expected_behavior, *self.normal_examples,
                          *self.boundary_cases, *self.exception_cases])


@dataclass
class ProjectConfig:
    name: str
    project_path: Path
    source_paths: list[str]
    test_paths: list[str]
    python_paths: list[str]
    contracts: list[Contract]
    repository_url: str = ""
    revision: str = ""
    backend: str = "docker"
    docker_image: str = "llm-test-runner:0.1"
    local_python: str = ""
    pytest_plugins: list[str] = field(default_factory=list)
    max_rounds: int = 3
    max_tests_per_round: int = 6
    stagnation_rounds: int = 2
    target_line_coverage: float = 90.0
    target_branch_coverage: float = 80.0
    timeout_seconds: int = 120
    repeat_tests: int = 2
    auto_repair: bool = True
    confirmation_policy: str = "llm"
    min_defect_confidence: float = 0.9
    max_repair_attempts: int = 2
    max_changed_lines: int = 100

    @classmethod
    def load(cls, path: Path) -> ProjectConfig:
        data = read_json(path)
        project = Path(data.get("project_path", ""))
        if not str(data.get("project_path", "")).strip():
            raise ToolError("配置中缺少 project_path")
        if not project.is_absolute():
            project = path.resolve().parent / project
        data["project_path"] = project.resolve()
        contract_data = data.pop("contracts", None)
        if contract_data is None:
            spec_path = contained(path.resolve().parent, data.pop("contracts_file", "contracts.json"))
            contract_data = read_json(spec_path).get("contracts", [])
        else:
            data.pop("contracts_file", None)
        if not isinstance(contract_data, list) or not contract_data:
            raise ToolError("请提供至少一条 contracts 需求/接口约定，黑盒模式不会从源码推测预期行为")
        try:
            data["contracts"] = [Contract(**item) for item in contract_data]
            result = cls(**data)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"项目配置字段不合法: {exc}") from exc
        result.validate()
        return result

    def validate(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ToolError("name 必须是非空项目名称")
        if not self.project_path.is_dir():
            raise ToolError(f"目标项目目录不存在: {self.project_path}")
        if self.backend not in {"docker", "local"}:
            raise ToolError("backend 必须是 docker 或 local")
        if self.confirmation_policy not in {"manual", "llm"}:
            raise ToolError("confirmation_policy 必须是 manual 或 llm")
        if not isinstance(self.auto_repair, bool):
            raise ToolError("auto_repair 必须为 true 或 false")
        if not isinstance(self.pytest_plugins, list) or any(not isinstance(name, str) or not name.strip()
                                                          for name in self.pytest_plugins):
            raise ToolError("pytest_plugins 必须是插件模块名列表")
        for field_name in ("max_rounds", "max_tests_per_round", "stagnation_rounds", "timeout_seconds",
                           "repeat_tests", "max_repair_attempts", "max_changed_lines"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ToolError(f"{field_name} 必须是正整数")
        for field_name in ("target_line_coverage", "target_branch_coverage"):
            value = getattr(self, field_name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ToolError(f"{field_name} 必须在 0—100 之间")
        if (isinstance(self.min_defect_confidence, bool) or not isinstance(self.min_defect_confidence, (int, float))
                or not math.isfinite(self.min_defect_confidence) or not 0 <= self.min_defect_confidence <= 1):
            raise ToolError("min_defect_confidence 必须在 0—1 之间")
        for name in ("source_paths", "test_paths", "python_paths"):
            paths = getattr(self, name)
            if not isinstance(paths, list) or (name != "test_paths" and not paths):
                raise ToolError(f"{name} 必须是相对路径列表")
            for relative in paths:
                path = contained(self.project_path, relative)
                if not path.exists():
                    raise ToolError(f"配置路径不存在: {relative}")
            setattr(self, name, [Path(relative).as_posix() for relative in paths])
        ids = set()
        for contract in self.contracts:
            for key in ("id", "module", "symbol", "signature", "description", "source_file"):
                if not isinstance(getattr(contract, key), str) or not getattr(contract, key).strip():
                    raise ToolError(f"需求 {contract.id!r} 缺少 {key}")
            if contract.id in ids:
                raise ToolError(f"需求编号重复: {contract.id}")
            ids.add(contract.id)
            for key in ("expected_behavior", "normal_examples", "boundary_cases", "exception_cases"):
                items = getattr(contract, key)
                if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
                    raise ToolError(f"需求 {contract.id} 的 {key} 必须是非空字符串列表")
            if not contract.expected_behavior:
                raise ToolError(f"需求 {contract.id} 必须声明 expected_behavior")
            source = contained(self.project_path, contract.source_file)
            if source.suffix != ".py" or not source.is_file() or not self.is_source(contract.source_file):
                raise ToolError(f"需求 {contract.id} 的 source_file 必须属于 source_paths 内的 Python 文件")
            contract.source_file = Path(contract.source_file).as_posix()
            if any(source == (base := contained(self.project_path, value)) or source.is_relative_to(base) for value in self.test_paths):
                raise ToolError(f"需求 {contract.id} 的源文件不能同时属于 test_paths")

    def is_source(self, relative: str) -> bool:
        path = contained(self.project_path, relative)
        return any(path == (base := contained(self.project_path, source)) or path.is_relative_to(base)
                   for source in self.source_paths)

    def serializable(self) -> dict:
        value = asdict(self)
        value["project_path"] = str(self.project_path)
        return value


@dataclass
class LLMSettings:
    endpoint: str
    api_key: str
    model: str
    timeout: float
    temperature: float
    max_tokens: int
    max_calls: int
    max_total_tokens: int
    retries: int
    extra_body: dict

    @classmethod
    def load(cls, env_path: Path | None = None) -> LLMSettings:
        from dotenv import load_dotenv
        import json

        load_dotenv(env_path or Path.cwd() / ".env", override=False, encoding="utf-8-sig")
        def get(name: str, fallback: str = "", default: str = "") -> str:
            return os.getenv(name) or (os.getenv(fallback) if fallback else None) or default

        base = get("LLM_BASE_URL", "OPENAI_BASE_URL", os.getenv("OPENAI_API_BASE", "")).rstrip("/")
        endpoint = get("LLM_CHAT_ENDPOINT") or (base if base.endswith("/chat/completions")
                                                else base + "/chat/completions" if base else "")
        api_key = get("LLM_API_KEY", "OPENAI_API_KEY")
        model = get("LLM_MODEL", "OPENAI_MODEL")
        if not endpoint or not model:
            raise ToolError("请在 .env 中配置 LLM_BASE_URL（含服务所需的 /v1 等前缀）和 LLM_MODEL")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ToolError("LLM 接口必须是无用户名密码的 http/https URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ToolError("非本机 LLM 接口必须使用 HTTPS")
        if not api_key and get("LLM_ALLOW_NO_KEY", default="false").lower() != "true":
            raise ToolError("请配置 LLM_API_KEY；无认证本机服务可显式设置 LLM_ALLOW_NO_KEY=true")
        try:
            result = cls(endpoint, api_key, model, float(get("LLM_TIMEOUT", default="120")),
                         float(get("LLM_TEMPERATURE", default="0")), int(get("LLM_MAX_TOKENS", default="12000")),
                         int(get("LLM_MAX_CALLS", default="60")), int(get("LLM_MAX_TOTAL_TOKENS", default="250000")),
                         int(get("LLM_RETRIES", default="2")), json.loads(get("LLM_EXTRA_BODY", default="{}")))
        except (ValueError, TypeError) as exc:
            raise ToolError(f".env 中的 LLM 数值或 JSON 配置不合法: {exc}") from exc
        if (not isinstance(result.extra_body, dict) or result.timeout <= 0 or not math.isfinite(result.timeout)
                or not math.isfinite(result.temperature) or result.max_tokens <= 0 or result.max_calls <= 0
                or result.max_total_tokens <= 0 or result.retries < 0 or result.retries > 5):
            raise ToolError("LLM 限额/超时必须有效，LLM_RETRIES 为 0—5，LLM_EXTRA_BODY 为对象")
        if {"messages", "model", "stream", "max_tokens", "temperature"} & result.extra_body.keys():
            raise ToolError("LLM_EXTRA_BODY 不允许覆盖 messages/model/stream/max_tokens/temperature")
        return result
