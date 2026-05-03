import asyncio
from pathlib import Path
from typing import Any, Iterable

import logfire

from src._common import PROJECT_ROOT, build_agent, model_id

SKILL_DIR = "/skills/auditing/"
MCP_SCRIPT = PROJECT_ROOT / "skills" / "auditing" / "scripts" / "mcp" / "auditing_mcp.py"
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "auditing"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "results" / "auditing"


def _mcp_servers(data_root: Path) -> dict:
    return {
        "auditing_mcp": {
            "command": "python3",
            "args": [str(MCP_SCRIPT), f"--data-root={data_root}"],
            "transport": "stdio",
        }
    }


async def run_one(agent, item: dict[str, Any], *, model_slug: str, output_root: Path) -> None:
    prompt = (
        f"audit {item['concept_id']} for {item['period']} "
        f"in the {item['filing_name']} filing released by {item['ticker']} "
        f"on {item['issue_time']}. "
        f"Use --model {model_slug} and --output-root {output_root} "
        f"when calling write_audit.py."
    )
    thread_id = "-".join(
        str(item[k])
        for k in ("ticker", "filing_name", "issue_time", "concept_id", "period")
    )
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
    data_root: Path | str | None = None,
    output_root: Path | str | None = None,
) -> None:
    items = list(inputs)
    dr = Path(data_root) if data_root else DEFAULT_DATA_ROOT
    out = Path(output_root) if output_root else DEFAULT_OUTPUT_ROOT
    out.mkdir(parents=True, exist_ok=True)
    slug = model_id(model)
    with logfire.span(
        "auditing.run n={n} model={m} out={o}",
        n=len(items),
        m=model,
        o=str(out),
    ):
        agent = await build_agent(
            skill_dir=SKILL_DIR,
            mcp_servers=_mcp_servers(dr),
            model=model,
        )
        for it in items:
            await run_one(agent, it, model_slug=slug, output_root=out)
