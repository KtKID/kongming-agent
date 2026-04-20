# config/

kongming-agent 的统一配置入口示例。

## 配置文件

- `default.yaml`：默认配置示例，覆盖第一版必须显式支持的全部配置项。
- `local-model.yaml`：本地 OpenAI-compatible 模型基线配置，作为
  `make cli` / e2e 的默认入口。

## 加载顺序

第一版采用「显式指定优先」策略：

1. CLI 参数 `--config <path>` 显式指定的配置文件（最高优先级）。
2. 环境变量 `KONGMING_CONFIG` 指定的配置文件路径。
3. 项目根目录下的 `config/local-model.yaml`（开发态默认）。
4. 项目根目录下的 `config/default.yaml`（兜底示例）。

注意：第一版**不做**多层 YAML 合并 / overlay，避免配置语义早期就过度复杂化。
覆写要么改文件，要么换文件，要么靠 CLI 参数针对单字段覆盖（如有实现）。

## 远端模型 vs 本地模型语义差异

统一配置结构相同，只在语义层面区分：

| 字段 | 远端模型 | 本地模型 |
| --- | --- | --- |
| `model.base_url` | 必填，例如 `https://api.openai.com/v1` | 必填，例如 `http://127.0.0.1:1234` |
| `model.api_key`  | 必填 | 允许为空字符串 `""` |
| `model.provider` | `openai_compatible` 或后续扩展 | 通常 `openai_compatible` |

第一版**不在代码层判别"本地"或"远端"**，所有差异都通过配置语义表达。
provider 实现侧只关心 `base_url + api_key + name + timeout` 这四件事是否齐全。

## 用环境变量覆盖配置文件路径

```bash
export KONGMING_CONFIG=/absolute/path/to/your.yaml
make cli
```

如果同时设置了 CLI `--config` 与环境变量，以 CLI 参数为准。

## 配置项速查

完整字段定义、默认值与约束见 [`src/config_loader/models.py`](../src/config_loader/models.py)，Pydantic 模型即 schema 的权威来源。
