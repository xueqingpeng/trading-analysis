import asyncio
from pathlib import Path
from typing import Any, Iterable

import logfire

from src._common import DEFAULT_DB, PROJECT_ROOT, build_agent, model_id

SKILL_DIR = "/skills/report_generation/"
MCP_SCRIPT = (
    PROJECT_ROOT / "skills" / "report_generation" / "scripts" / "mcp" / "report_generation_mcp.py"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "results" / "report_generation"


def _mcp_servers(db_path: Path) -> dict:
    return {
        "report_generation_mcp": {
            "command": "python3",
            "args": [str(MCP_SCRIPT), f"--db-path={db_path}"],
            "transport": "stdio",
        }
    }


async def run_one(agent, item: dict[str, Any], *, model_slug: str, output_root: Path) -> None:
    prompt = (
        f"weekly report for {item['symbol']} for week ending {item['date']}. "
        f"Use --model {model_slug} and --output-root {output_root} "
        f"when calling upsert_report.py."
    )
    thread_id = f"{item['symbol']}-{item['date']}"
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
    concurrency: int = 3,
    model: str | None = None,
    db_path: Path = DEFAULT_DB,
    output_root: Path | str | None = None,
) -> None:
    items = list(inputs)
    out = Path(output_root) if output_root else DEFAULT_OUTPUT_ROOT
    out.mkdir(parents=True, exist_ok=True)
    slug = model_id(model)
    with logfire.span(
        "report_generation.run n={n} concurrency={c} model={m} out={o}",
        n=len(items),
        c=concurrency,
        m=model,
        o=str(out),
    ):
        agent = await build_agent(
            skill_dir=SKILL_DIR,
            mcp_servers=_mcp_servers(db_path),
            model=model,
        )
        sem = asyncio.Semaphore(concurrency)

        async def bounded(it: dict[str, Any]) -> None:
            async with sem:
                await run_one(agent, it, model_slug=slug, output_root=out)

        await asyncio.gather(*(bounded(i) for i in items))
