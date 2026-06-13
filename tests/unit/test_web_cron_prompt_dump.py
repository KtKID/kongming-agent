"""真实环境下 dump cron 子 runtime 的 system prompt（含 # skills listing）。

复现 ``web/run.py:_ensure_shared_assets`` 内拼装 ``_instructions_cache[0]`` 的逻辑，
以便人工验证 sitian-scan 等 skill 是否出现在 `# skills` 区块。

跑法：
    uv run pytest tests/unit/test_web_cron_prompt_dump.py -s
"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.config.paths import get_kongming_home
from prompting.instructions.instruction_loader import assemble_instructions
from prompting.skills.skill_loader import format_skill_listing, load_skill_specs


@pytest.mark.asyncio
async def test_dump_cron_instructions_real(capsys) -> None:
    home = get_kongming_home()
    sitian_root = home / "sitian"

    rendered, origins = await assemble_instructions(
        kongming_home=home,
        sitian_root=sitian_root,
    )

    skill_specs_list = await load_skill_specs(
        home,
        workspace=Path.cwd(),
        event_sinks=[],
    )
    listing = format_skill_listing(skill_specs_list)
    if listing:
        rendered = rendered + f"\n\n# skills\n{listing}"
        origins = [*origins, "skills"]

    with capsys.disabled():
        print("\n" + "=" * 80)
        print("CRON SYSTEM PROMPT (real assembly, what cron LLM will see):")
        print("=" * 80)
        print(rendered)
        print("=" * 80)
        print(f"total length: {len(rendered)} chars")
        print(f"origins: {origins}")
        print(f"skill count: {len(skill_specs_list)}")

    if not listing:
        pytest.skip("当前环境没有发现 skills，跳过真实 skills listing 断言")
    assert "# skills" in rendered, "rendered prompt missing '# skills' section"
    skill_names = [s.name for s in skill_specs_list]
    if "sitian-scan" in skill_names:
        assert "sitian-scan" in rendered, "sitian-scan present in specs but missing in rendered"
