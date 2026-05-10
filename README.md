# 语雀知识库迁移 Skill

> 将语雀知识库内容复制整理到另一个知识库 —— 清洗+分类合并、去重、长文档截断、逐篇挂目录、断点续传。

**核心理念：复制不搬。原库完全不动。**

[![Release](https://img.shields.io/github/v/release/yehuoshun/yuque-migration-skill?label=release)](https://github.com/yehuoshun/yuque-migration-skill/releases)
[![License](https://img.shields.io/github/license/yehuoshun/yuque-migration-skill)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.8+-blue)](https://www.python.org/)

## 目录

- [功能特性](#功能特性)
- [前置条件](#前置条件)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [使用方式](#使用方式)
- [项目结构](#项目结构)
- [迁移流程](#迁移流程)
  - [主流程](#主流程)
  - [逐篇文档处理](#逐篇文档处理)
  - [限流与容错](#限流与容错)
  - [续传](#续传)
- [API 参考](#api-参考)
- [License](#license)

## 功能特性

| 功能 | 说明 |
|------|------|
| 🔄 **跨库复制** | 源库毫发无伤，目标库接收清洗后的内容 |
| 🧹 **清洗+分类合并** | 一次 LLM 调用完成格式清洗和内容分类 |
| ✂️ **长文档截断** | 超过 20000 字符由 LLM 判断自然截断点 |
| 📂 **逐篇挂目录** | 创建即分类即挂载，不攒到后置阶段 |
| 📋 **多分类复制** | 文档跨越多个分类时自动复制副本 |
| 🧠 **智能跳过** | 自动识别纯代码/附件/二进制文档，不浪费 LLM 调用 |
| 💾 **断点续传** | `toc_map` 保留已建目录，续传直接复用，中断不怕 |
| 🚦 **限流保护** | 429 → 检查 `X-RateLimit-Remaining`，=0 等整点恢复 |
| 🛡️ **OOM 防护** | 内存感知，K8s 环境下自动降速防杀 |
| ⚡ **并发预取** | 后台线程预取下一页，减少 API 等待 |

## 前置条件

**配置分两步检查，缺哪块补哪块：**

| 步骤 | 检查项 | 说明 |
|------|--------|------|
| 1 | **语雀 Token** | 需 `doc:read` `doc:write` `repo:read` `repo:write` 权限 |
| 2 | **LLM 配置** | 兼容 OpenAI Chat Completions API，需 `model` / `url` / `api_key` 三项齐全 |
| — | **目标知识库** | 需提前在语雀创建（步骤 1 一次 API 调用同时验证源库+目标库） |

## 快速开始

### 环境要求

- Python 3.8+
- 无外部依赖（纯标准库）

### 1. 创建配置文件

在 `utils/yuque/yuque-ai/yuque-config.json`：

```json
{
  "token": "你的语雀 API Token",
  "llm": {
    "model": "deepseek-chat",
    "url": "https://api.deepseek.com/v1/chat/completions",
    "api_key": "sk-你的APIKey"
  }
}
```

### 2. 运行迁移

由 AI Agent 驱动，用户说「将《xxx》内容整理到《yyy》」即可触发。

或直接使用脚本：

```bash
# 首次运行（需 AI Agent 先生成进度文件）
python scripts/migrate.py utils/yuque-migration/progress/12345_旧库名.json

# 中断续传（直接重新运行同一命令）
python scripts/migrate.py utils/yuque-migration/progress/12345_旧库名.json
```

## 配置说明

### 语雀 Token

在 [语雀开放平台](https://www.yuque.com/settings/tokens) 创建 Token，需勾选：

- `doc:read` — 读取文档
- `doc:write` — 创建/修改文档
- `repo:read` — 读取知识库
- `repo:write` — 修改知识库目录

### LLM 配置

| 字段 | 说明 | 示例 |
|------|------|------|
| `llm.model` | 模型名 | `deepseek-chat`、`gpt-4o-mini` |
| `llm.url` | API 端点（OpenAI 兼容格式） | `https://api.deepseek.com/v1/chat/completions` |
| `llm.api_key` | API Key | `sk-xxx` |

> 只要兼容 OpenAI Chat Completions API 的模型均可使用（DeepSeek / OpenAI / 通义千问 / 豆包 等）。

### 容量限制

- 语雀单知识库上限 **5000** 篇文档
- 迁移时若目标库 ≥ 4500 篇 → 自动暂停，提示切换目标库
- 支持多目标库接力迁移

## 使用方式

由 AI Agent 驱动。用户说「**将《xxx》内容整理到《yyy》**」即可触发。

AI Agent 会自动：
1. 检查配置 → 获取旧库信息 → 验证目标库 → 检查容量
2. 逐篇清洗+分类+去重+创建+挂目录
3. 汇报迁移结果

### 脚本

| 脚本 | 说明 |
|------|------|
| `scripts/migrate.py` | v4 迁移脚本，清洗+分类+创建+挂目录一气呵成 |

### 进度文件

位于 `utils/yuque-migration/progress/{book_id}_{旧库名}.json`，由 AI Agent 在步骤 1 阶段自动创建。

续传时自动从 `last_offset` 恢复，`toc_map` 保留已建目录复用。

### 日志

位于 `utils/yuque-migration/logs/`。

## 项目结构

```
yuque-migration-skill/
├── SKILL.md              # Skill 规范文档（AI Agent 执行指南）
├── README.md             # 本文件
├── scripts/
│   └── migrate.py        # v4 迁移脚本
├── references/
│   └── api_reference.md  # 语雀 API 参考
└── .github/
    └── workflows/
        └── dingtalk-notify.yml  # CI：钉钉通知 + 自动 Release
```

## 迁移流程

### 主流程

```mermaid
flowchart TD
    A["🚀 开始"] --> B["检查 yuque-config.json"]
    B --> B1{"token 存在?"}
    B1 -->|否| B1a["提示填写 Token"] --> B
    B1 -->|是| B2{"llm 齐全?"}
    B2 -->|缺失| B2a["提示补充 LLM"] --> B
    B2 -->|已配置| C["步骤1: GET /users/{login}/repos\n一次调用定位源库+验证目标库"]
    C --> C1{"目标库存在?"}
    C1 -->|否| C1a["提示先创建"] --> C
    C1 -->|是| C2["GET /repos/{book_id} 取文档数"]
    C2 --> C3{"items_count = 0?"}
    C3 -->|是| G["汇报: 源库为空"]
    C3 -->|否| D["步骤2b: 检查目标库容量"]
    D --> D1{">= 4500?"}
    D1 -->|是| D1a["⛔ 暂停切换目标库"] --> C
    D1 -->|否| E["步骤3: 逐篇复制"]
    E --> E1["3a. 分页获取"]
    E1 --> E2["3b. 清洗分类+去重+创建+挂目录"]
    E2 --> E3{"容量 >= 4500?"}
    E3 -->|是| E3a["暂停切换"] --> E1
    E3 -->|否| E4["3d. 保存进度"]
    E4 --> E5{"更多页?"}
    E5 -->|是| E1
    E5 -->|否| F["步骤4: 汇报结果"]
    F --> H(["✅ 结束"])
```

### 逐篇文档处理

```mermaid
flowchart TD
    S["取一篇文档"] --> A["GET .../docs/{doc_id}?raw=1"]
    A --> B{"format?"}
    B -->|markdown| M["二进制检测 → LLM清洗分类"]
    B -->|lake| L["无损搬运 → 未分类"]
    B -->|空body| SK["跳过"]
    B -->|未知| F["记入 failed"]
    M --> M1{"跳过LLM?"}
    M1 -->|纯代码/附件/<500字| MC["默认未分类"]
    M1 -->|否| M2["🧠 LLM一次调用: 清洗+分类+截断"]
    M2 --> D1
    MC --> D1["去重: 搜标题 → 200字→500字→全文 三级比对"]
    L --> D1
    D1 --> D2{"重复?"}
    D2 -->|完全相同| SKIP["跳过"]
    D2 -->|内容不同| D3["加(重复标题-N)"]
    D3 --> CR["创建到目标库"]
    D2 -->|新文档| CR
    CR --> TC["挂目录: 拆层级→toc_map缓存→主分类直挂/副分类复制挂"]
    TC --> DN(["✅ 完成"])
```

### 限流与容错

```mermaid
flowchart TD
    A["API 请求"] --> B{"状态码?"}
    B -->|200| C["✅ 正常"]
    B -->|429| D["读 X-RateLimit-Remaining"]
    D --> D1{"== 0?"}
    D1 -->|是| D2["等整点恢复"] --> A
    D1 -->|否| D3["等1s重试×3"]
    D3 -->|成功| C
    D3 -->|失败| F["记 failed"]
    B -->|404| E["按空文档处理"]
    B -->|5xx/超时| F1["等1s重试×3"]
    F1 -->|成功| C
    F1 -->|失败| F
    B -->|329| G["TITLE冲突 → 等整点 → 降级挂父层"]
```

### 续传

```mermaid
flowchart TD
    A["⚡ 中断后继续"] --> B["读进度文件"]
    B --> C{"存在?"}
    C -->|否| D["offset=0 重头来"]
    C -->|是| E["恢复: last_offset + toc_map + local_created + processed_doc_ids"]
    E --> F["从 offset 继续分页"]
    F --> G{"doc_id 已处理?"}
    G -->|是| H["跳过"] --> F
    G -->|否| I["正常处理"]
    I --> J["每篇保存进度"]
    J --> K{"更多?"}
    K -->|是| F
    K -->|否| L(["✅ 完成"])
```

## API 参考

语雀 OpenAPI 接口参考见 [references/api_reference.md](./references/api_reference.md)。

基地址：`https://www.yuque.com/api/v2`

## License

MIT © [yehuoshun](https://github.com/yehuoshun)
