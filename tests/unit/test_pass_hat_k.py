"""pass^k 可靠性指标单元测试。

覆盖 ``run_kongming_harness_eval`` 新增的 ``pass_hat_k`` 纯函数和
``aggregate_pass_hat_k`` 聚合函数。τ-bench 公式：pass^k = C(c,k)/C(n,k)，
按 task 平均。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "run_kongming_harness_eval.py"


def _load_runner_module():
    """加载 runtime eval 脚本模块，返回可直接调用的 module。"""

    spec = importlib.util.spec_from_file_location("run_kongming_harness_eval", RUNNER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_kongming_harness_eval"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# pass_hat_k 纯函数
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pass_hat_k_all_pass() -> None:
    """n 次全通过时，任意 k 的 pass^k 必须为 1.0。"""

    runner = _load_runner_module()
    assert runner.pass_hat_k(n=4, c=4, k=1) == 1.0
    assert runner.pass_hat_k(n=4, c=4, k=2) == 1.0
    assert runner.pass_hat_k(n=4, c=4, k=4) == 1.0


@pytest.mark.unit
def test_pass_hat_k_none_pass() -> None:
    """0 次通过时，任意 k 的 pass^k 必须为 0.0。"""

    runner = _load_runner_module()
    assert runner.pass_hat_k(n=4, c=0, k=1) == 0.0
    assert runner.pass_hat_k(n=4, c=0, k=4) == 0.0


@pytest.mark.unit
def test_pass_hat_k_partial() -> None:
    """n=4, c=2 时 pass^k 按组合数公式计算。"""

    runner = _load_runner_module()
    # k=1: C(2,1)/C(4,1) = 2/4 = 0.5
    assert runner.pass_hat_k(n=4, c=2, k=1) == pytest.approx(0.5)
    # k=2: C(2,2)/C(4,2) = 1/6
    assert runner.pass_hat_k(n=4, c=2, k=2) == pytest.approx(1 / 6)
    # k=3: C(2,3)/C(4,3) = 0/4 = 0
    assert runner.pass_hat_k(n=4, c=2, k=3) == 0.0
    # k=4: C(2,4)/C(4,4) = 0/1 = 0
    assert runner.pass_hat_k(n=4, c=2, k=4) == 0.0


@pytest.mark.unit
def test_pass_hat_k_single_run() -> None:
    """n=1 时 pass^k 退化为二元成功/失败。"""

    runner = _load_runner_module()
    assert runner.pass_hat_k(n=1, c=1, k=1) == 1.0
    assert runner.pass_hat_k(n=1, c=0, k=1) == 0.0


@pytest.mark.unit
def test_pass_hat_k_k_exceeds_n() -> None:
    """k > n 时应返回 0.0（无法从 n 次中取 k 次全通过）。"""

    runner = _load_runner_module()
    assert runner.pass_hat_k(n=3, c=3, k=5) == 0.0


# ---------------------------------------------------------------------------
# aggregate_pass_hat_k 聚合函数
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_aggregate_single_task() -> None:
    """单 task 聚合结果应与直接计算一致。"""

    runner = _load_runner_module()
    result = runner.aggregate_pass_hat_k(per_task=[(4, 3)], ks=[1, 2, 4])
    # C(3,1)/C(4,1) = 3/4 = 0.75
    assert result[1] == pytest.approx(0.75)
    # C(3,2)/C(4,2) = 3/6 = 0.5
    assert result[2] == pytest.approx(0.5)
    # C(3,4)/C(4,4) = 0/1 = 0
    assert result[4] == 0.0


@pytest.mark.unit
def test_aggregate_multi_task_average() -> None:
    """多 task 聚合是按 task 算术平均。"""

    runner = _load_runner_module()
    # task A: n=4, c=4 → pass^1=1.0
    # task B: n=4, c=2 → pass^1=0.5
    # 平均 pass^1 = (1.0 + 0.5) / 2 = 0.75
    result = runner.aggregate_pass_hat_k(per_task=[(4, 4), (4, 2)], ks=[1])
    assert result[1] == pytest.approx(0.75)


@pytest.mark.unit
def test_aggregate_empty() -> None:
    """空 task 列表应返回所有 k 为 0.0。"""

    runner = _load_runner_module()
    result = runner.aggregate_pass_hat_k(per_task=[], ks=[1, 2])
    assert result[1] == 0.0
    assert result[2] == 0.0
