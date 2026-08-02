# mypy: no-ignore-errors
"""mypy 负例：SessionEngine.run 不接受运行期审批 provider。"""

from core.contracts import ApprovalProvider
from runtime_assembly.session_engine import SessionEngine


async def invalid_override(runtime: SessionEngine, approval: ApprovalProvider) -> None:
    """构造旧调用姿势，输入为 runtime/provider，输出为预期 mypy 诊断。"""
    await runtime.run("unsafe", approval=approval)
