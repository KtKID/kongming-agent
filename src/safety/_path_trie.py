"""safety 内部 PathTrie 实现（M3 BoundaryResolver 专用）。

dict-of-dicts 结构，O(K) 前缀查询（K = 路径深度）。

**约束**：

- 文件名以前导下划线表示**模块私有**：``safety/__init__.py`` 不导出
- 不引第三方依赖（stdlib only）
- 不感知具体路径语义（不做 ``expanduser`` / ``resolve``）；调用方负责传规整化
  后的 :class:`pathlib.Path`

**语义**：

- :meth:`PathTrie.add` 把路径切成 segment 装入 trie，叶子节点用 ``_LEAF`` 哨兵标记
- :meth:`PathTrie.contains_or_prefix` 沿请求路径走 trie，若中途命中任何叶子
  即返回 True（"trie 中存在某条目是该路径的前缀"）；若沿请求路径走完所有段
  仍未遇到叶子，返回 False
- 不做"请求是 trie 条目的前缀"反向匹配：BoundaryResolver 期望"请求路径必须
  是某条信任/拦截规则覆盖范围内的子节点"，故只支持前者
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 叶子哨兵键。使用空字符串可避免与任何合法 path segment 冲突
# （path segment 不可能是空字符串，stdlib pathlib 会自动剥离）。
_LEAF = ""


class PathTrie:
    """基于 dict-of-dicts 的路径 trie。

    用于 ``ResolvedBoundary`` 的 5 个 trie：trusted_write / trusted_read /
    approval_read / approval_write / blocked。
    """

    __slots__ = ("_root", "_size")

    def __init__(self) -> None:
        self._root: dict[str, Any] = {}
        self._size: int = 0

    def add(self, path: Path | str) -> None:
        """把一条路径装入 trie。

        Args:
            path: 任意路径。``Path`` 会被取 ``parts``；``str`` 会按 ``/`` 切分。
        """
        segments = self._segments(path)
        if not segments:
            return
        node = self._root
        for seg in segments:
            node = node.setdefault(seg, {})
        if _LEAF not in node:
            node[_LEAF] = True
            self._size += 1

    def contains_or_prefix(self, path: Path | str) -> bool:
        """判断是否有 trie 条目覆盖 ``path``（trie 条目是 path 的前缀）。

        语义示例：trie 装入 ``/a/b/`` 后，``contains_or_prefix("/a/b")`` /
        ``"/a/b/c"`` 都返回 True；``"/a"`` 返回 False。
        """
        segments = self._segments(path)
        node = self._root
        # 根本身就是叶子（即装入的是 "/" 或空 trie 已 add 顶层）→ 任何路径命中
        if _LEAF in node:
            return True
        for seg in segments:
            if seg not in node:
                return False
            node = node[seg]
            if _LEAF in node:
                return True
        return False

    @staticmethod
    def _segments(path: Path | str) -> tuple[str, ...]:
        """提取路径段。

        - ``Path`` → 直接用 ``parts``
        - ``str`` → 用 ``Path`` 包装；空串返回空 tuple
        """
        if isinstance(path, Path):
            return path.parts
        if not path:
            return ()
        return Path(path).parts

    def __bool__(self) -> bool:
        return self._size > 0

    def __len__(self) -> int:
        return self._size

    def __repr__(self) -> str:
        return f"PathTrie(size={self._size})"


__all__ = ["PathTrie"]
