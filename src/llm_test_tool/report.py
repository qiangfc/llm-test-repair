from __future__ import annotations

from collections import Counter
from html import escape
import json
from pathlib import Path

from .coverage_adapter import summarize
from .storage import write_json


def statistics(state: dict) -> dict:
    result = {}
    for category in ("boundary", "null_type", "exception_handling", "logic"):
        defects = [item for item in state.get("defects", []) if item["assessment"]["category"] == category]
        attempts = [attempt for item in defects for repair in item.get("repairs", []) for attempt in repair["attempts"]]
        verified = sum(item["status"] == "verified" for item in attempts)
        result[category] = {"candidates": len(defects),
                            "confirmed": sum(item["status"] in {"machine_confirmed", "human_confirmed", "fixed", "resolved_by_other_patch"} for item in defects),
                            "attempts": len(attempts), "verified_attempts": verified,
                            "verification_pass_rate": round(100 * verified / len(attempts), 2) if attempts else None}
    return result


def render_report(run_dir: Path, state: dict) -> None:
    state["classification_statistics"] = statistics(state)
    final = state.get("coverage_final", {}).get("regression")
    if final and not state.get("coverage_stale"):
        for test in state.get("tests", []):
            records = [entry for node, entry in final["tests"].items() if node.split("::")[0] == test["path"]]
            test["final_outcomes"] = dict(Counter(item["outcome"] for item in records))
            test["final_execution_records"] = records
        state["acceptance_summary"] = {
            "all_requirement_scenarios": bool(state.get("scenario_coverage")) and all(
                set(scenes) == {"normal", "boundary", "exception"}
                for requirements in state.get("scenario_coverage", {}).values() for scenes in requirements.values()),
            "final_regression_outcomes": dict(Counter(item["outcome"] for item in final["tests"].values())),
            "unresolved_candidate_ids": [item["id"] for item in state["defects"] if item["status"] in
                                          {"machine_confirmed", "human_confirmed", "pending_review", "uncertain"}],
            "note": "completed 表示流程结束；场景标注、最终结果及未解决候选需分别核查。机器确认不等同于上游确认。"}
    write_json(run_dir / "state.json", state)
    write_json(run_dir / "report.json", state)
    def safe(value):
        return escape(str(value), quote=True)
    def table(headers, rows):
        return "<table><thead><tr>" + "".join(f"<th>{safe(h)}</th>" for h in headers) + "</tr></thead><tbody>" + "".join(
            "<tr>" + "".join(f"<td>{safe(cell)}</td>" for cell in row) + "</tr>" for row in rows) + "</tbody></table>"
    def pct(value):
        return "无可测数据" if value is None else f"{value}%"
    body = [f"<h1>{safe(state.get('name', 'LLM 辅助软件测试'))}</h1>",
            f"<p>状态：{safe(state.get('status'))} · 终止原因：{safe(state.get('stop_reason', ''))}</p>",
            "<p>黑盒：需求与接口驱动。白盒：源码与未覆盖行/分支驱动。合并覆盖率来自合并执行，未采用百分比平均。"
            "机器确认属于模型判断，仍可能需要人工复核；回归通过仅代表当前测试集的验证结果。"
            "覆盖率范围是配置指定的源码文件，分类数量按候选记录计数。</p>",
            f"<h2>项目来源</h2><pre>{safe(json.dumps(state.get('provenance', {}), ensure_ascii=False, indent=2))}</pre>",
            f"<h2>LLM 预算</h2><pre>{safe(json.dumps(state.get('llm_usage', {}), ensure_ascii=False, indent=2))}</pre>"]
    if state.get("coverage_stale"):
        body.append("<p><strong>测试集合已人工调整：以下覆盖率为旧结果，需重新执行更新。</strong></p>")
    body.append("<h2>需求场景覆盖（元数据标注）</h2><pre>" +
                safe(json.dumps(state.get("scenario_coverage", {}), ensure_ascii=False, indent=2)) + "</pre>")
    if state.get("acceptance_summary"):
        body.append("<h2>验收摘要</h2><pre>" + safe(json.dumps(state["acceptance_summary"], ensure_ascii=False, indent=2)) + "</pre>")
    labels = {"baseline": "原有测试", "blackbox": "黑盒生成测试", "whitebox": "白盒生成测试",
              "combined": "黑白盒合并", "regression": "原有测试＋黑白盒"}
    for phase in ("coverage_before_repair", "coverage_final"):
        if phase not in state:
            continue
        rows = []
        for mode, result in state[phase].items():
            summary = summarize(result.get("coverage", {}))
            outcomes = Counter(item["outcome"] for item in result.get("tests", {}).values())
            rows.append([labels.get(mode, mode), result.get("status"), len(result.get("collected", [])),
                         outcomes["passed"], outcomes["failed"] + outcomes["error"], outcomes["skipped"],
                         pct(summary["line_percent"]), f"{summary['line_covered']}/{summary['line_total']}",
                         pct(summary["branch_percent"]), f"{summary['branch_covered']}/{summary['branch_total']}"])
        body.extend([f"<h2>{'修复前覆盖率' if phase.endswith('repair') else '最终覆盖率'}</h2>",
                     table(["测试集", "执行状态", "收集数量", "通过", "失败/错误", "跳过", "行覆盖率", "覆盖行", "分支覆盖率", "覆盖分支"], rows)])
        for mode, result in state[phase].items():
            body.append(f"<details><summary>{safe(labels.get(mode, mode))}：未覆盖位置与执行记录</summary><pre>" +
                        safe(json.dumps(result, ensure_ascii=False, indent=2)) + "</pre></details>")
    body.extend(["<h2>多轮生成</h2>", table(["轮次", "方式", "新增有效模块", "可运行测试数", "行覆盖率", "分支覆盖率", "本轮 LLM 调用"],
        [[r["round"], r["mode"], r["accepted"], r["runnable_tests"], pct(r["coverage"]["line_percent"]),
          pct(r["coverage"]["branch_percent"]), r["llm_calls"]] for r in state.get("rounds", [])])])
    if state.get("merge_audits"):
        body.extend(["<h3>合并运行新增失败审查</h3>", table(["轮次", "审查测试", "额外 LLM 调用"],
            [[item["round"], ", ".join(item["test_ids"]), item["llm_calls"]] for item in state["merge_audits"]])])
    body.extend(["<h2>缺陷分类及修复统计</h2>", table(["分类", "候选", "已确认", "修复尝试", "验证通过", "尝试通过率"],
        [[key, item["candidates"], item["confirmed"], item["attempts"], item["verified_attempts"], pct(item["verification_pass_rate"])]
         for key, item in state["classification_statistics"].items()])])
    for defect in state.get("defects", []):
        body.append(f"<h3>{safe(defect['id'])} · {safe(defect['status'])}</h3><pre>" +
                    safe(json.dumps(defect, ensure_ascii=False, indent=2)) + "</pre>")
    body.append("<h2>测试用例与验收证据</h2>")
    for test in state.get("tests", []):
        metadata = {key: value for key, value in test.items() if key != "code"}
        body.append(f"<details><summary>{safe(test['id'])} · {safe(test['mode'])} · {safe(test['scenario'])} · {safe(test['name'])} · " +
                    f"生成时：{safe(test['outcome'])} · 最终：{safe(test.get('final_outcomes', {}))}</summary>" +
                    f"<pre>{safe(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre><pre>{safe(test['code'])}</pre></details>")
    body.append("<h2>错误及生成拒绝记录</h2><pre>" + safe(json.dumps(state.get("errors", []), ensure_ascii=False, indent=2)) + "</pre>")
    html = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" + \
           "<title>LLM 测试验收报告</title><style>body{max-width:1200px;margin:36px auto;padding:0 24px;font:16px/1.7 system-ui;color:#172b38;background:#f7fafb}" + \
           "table{width:100%;border-collapse:collapse;background:white}td,th{padding:10px;border:1px solid #d7e0e5;text-align:left}" + \
           "pre{background:#eef3f6;padding:16px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}summary{cursor:pointer;padding:12px}" + \
           "h2{margin-top:36px}details{border:1px solid #d7e0e5;margin:12px 0;background:white}</style><body>" + "".join(body) + "</body></html>"
    (run_dir / "report.html").write_text(html, encoding="utf-8")
    counts = Counter(item["status"] for item in state.get("defects", []))
    scope_note = ("示例项目含人工注入缺陷，不能作为真实开源缺陷发现验收。" if
                  state.get("provenance", {}).get("configured_revision", "").startswith("demo-") else
                  "真实开源目标的发现以本地接口约定为依据，候选与修复尚未获得上游维护者确认。")
    (run_dir / "report.md").write_text(
        f"# {state.get('name', '')}\n\n状态：{state.get('status')}\n\n终止原因：{state.get('stop_reason', '')}\n\n"
        f"生成测试模块：{len(state.get('tests', []))}；缺陷状态统计：{dict(counts)}。\n\n"
        "详细覆盖率、需求映射、输入/预期/实际、LLM 调用及修复回归证据见 report.html 和 report.json。\n\n"
        "机器确认不等同于人工审查。" + scope_note + "\n", encoding="utf-8")
