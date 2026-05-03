import asyncio
from pathlib import Path
from typing import Any, Iterable

import logfire

from src._common import DEFAULT_DB, PROJECT_ROOT, build_agent, model_id

SKILL_DIR = "/skills/report_evaluation/"
MCP_SCRIPT = (
    PROJECT_ROOT / "skills" / "report_evaluation" / "scripts" / "mcp" / "report_evaluation_mcp.py"
)
DEFAULT_REPORTS_ROOT = PROJECT_ROOT / "results" / "report_generation"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "results" / "report_evaluation"


def _mcp_servers(db_path: Path, reports_root: Path) -> dict:
    return {
        "report_evaluation_mcp": {
            "command": "python3",
            "args": [
                str(MCP_SCRIPT),
                f"--db-path={db_path}",
                f"--reports-root={reports_root}",
            ],
            "transport": "stdio",
        }
    }


async def run_one(agent, item: dict[str, Any], *, model_slug: str, output_root: Path) -> None:
    prompt = (
        f"evaluate report_generation run for {item['symbol']} model={item['target_model']}. "
        f"Use --model {model_slug} and --output-root {output_root} "
        f"when calling upsert_evaluation.py."
    )
    thread_id = f"{item['symbol']}-{item['target_model']}"
    with logfire.span("agent.invoke prompt={prompt!r}", prompt=prompt, thread_id=thread_id):
        try:
            await agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"configurable": {"thread_id": thread_id}},
            )
        except Exception:
            logfire.exception("agent.ainvoke failed")
            raise


async def run_pipeline(
    inputs: Iterable[dict[str, Any]],
    *,
    model: str | None = None,
    db_path: Path = DEFAULT_DB,
    reports_root: Path | str | None = None,
    output_root: Path | str | None = None,
) -> None:
    items = list(inputs)
    rr = Path(reports_root) if reports_root else DEFAULT_REPORTS_ROOT
    out = Path(output_root) if output_root else DEFAULT_OUTPUT_ROOT
    out.mkdir(parents=True, exist_ok=True)
    slug = model_id(model)
    with logfire.span(
        "report_evaluation.run n={n} model={m} out={o}",
        n=len(items),
        m=model,
        o=str(out),
    ):
        agent = await build_agent(
            skill_dir=SKILL_DIR,
            mcp_servers=_mcp_servers(db_path, rr),
            model=model,
        )
        for it in items:
            await run_one(agent, it, model_slug=slug, output_root=out)
