"""Kongming Harness Eval 模块包。

# 本包将原 scripts/run_kongming_harness_eval.py 拆分为 10 个职责域模块。
# 仓库根 src/ 路径注入和 .env 加载在此一次性完成，下游模块直接 import kongming 内部包。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 仓库根 → src/ 加入 sys.path，使 `from core.xxx import ...` 可用
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

# 仓库根 .env 注入：load_config 只读 KONGMING_HOME/.env，而 eval 把 KONGMING_HOME
# 隔离到 task 子目录，必须主动把仓库根 .env 里的 *_API_KEY 注入到 os.environ。
try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:
    _load_dotenv = None  # type: ignore[assignment]
if _load_dotenv is not None:
    _REPO_DOTENV = _REPO_ROOT / ".env"
    if _REPO_DOTENV.exists():
        _load_dotenv(_REPO_DOTENV, override=False)

REPO_ROOT = _REPO_ROOT
