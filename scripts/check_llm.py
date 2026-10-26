"""Small authenticated connection check; never prints credentials."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from llm_test_tool.config import LLMSettings
from llm_test_tool.llm import LLMClient
from llm_test_tool.storage import ToolError, write_json


def main():
    directory = Path("runs") / ("connection-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    directory.mkdir(parents=True)
    settings = replace(LLMSettings.load(Path(".env")), max_tokens=128, retries=0)
    client = LLMClient(settings, directory)
    try:
        result = client.ask([{"role": "system", "content": 'Return JSON only: {"ok":true}.'},
                             {"role": "user", "content": "Check connection. Return the JSON object."}], "connection-check")
        if result.get("ok") is not True:
            raise ToolError("接口响应可达，但连接检查未返回预期 JSON")
        write_json(directory / "check.json", {"status": "passed", "endpoint": settings.endpoint, "model": settings.model,
                   "checked_at": datetime.now(timezone.utc).isoformat(), "usage": client.usage()})
        print(f"Connection passed: {settings.model}; evidence: {directory}")
        return 0
    except ToolError as exc:
        write_json(directory / "check.json", {"status": "failed", "reason": str(exc), "usage": client.usage()})
        print(f"Connection failed: {exc}; evidence: {directory}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
