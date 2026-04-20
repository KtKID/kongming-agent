"""配置加载层错误。

所有错误都继承 :class:`core.errors.ConfigError`，这样装配层 / CLI 只用
``except ConfigError`` 就能覆盖配置层全部失败分支，而不需要感知
config_loader 内部细分。
"""

from __future__ import annotations

from core.errors import ConfigError as CoreConfigError


class ConfigLoadError(CoreConfigError):
    """加载或解析配置失败。

    触发场景：
    - 指定的配置路径不存在或不可读
    - YAML 文件本身无法解析（语法错误 / 编码错误）
    - 环境变量引用的路径缺失
    """


class ConfigValidationError(CoreConfigError):
    """配置内容校验失败。

    触发场景：
    - 必填字段缺失（如远端模型未提供 ``api_key``）
    - 字段类型或取值不合法（pydantic 校验失败）
    - 字段之间的约束冲突（如 session.backend=sqlite 但 store_path 为空）
    """


__all__ = ["ConfigLoadError", "ConfigValidationError"]
