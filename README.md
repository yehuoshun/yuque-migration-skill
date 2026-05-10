# 语雀知识库迁移 Skill

> 将语雀知识库内容复制整理到另一个知识库 —— 清洗+分类合并、去重、长文档截断、逐篇挂目录、断点续传。

**核心理念：复制不搬。原库完全不动。**

**作者：[yehuoshun](https://github.com/yehuoshun)**

## 功能特性

| 功能 | 说明 |
|------|------|
| 🔄 **跨库复制** | 源库毫发无伤 |
| 🧹 **清洗+分类合并** | 一次 LLM 调用完成格式清洗和内容分类 |
| ✂️ **长文档截断** | 超过 20000 字符由 LLM 判断自然截断点 |
| 📂 **逐篇挂目录** | 创建即分类即挂载，不攒到后置阶段 |
| 📋 **多分类复制** | 文档跨越多个分类时自动复制副本 |
| 🧠 **智能跳过** | 自动识别纯代码/附件/二进制文档，不浪费 LLM 调用 |
| 💾 **断点续传** | `toc_map` 保留已建目录，续传直接复用 |
| 🚦 **限流保护** | 429 → 检查 X-RateLimit-Remaining，=0 等整点 |
| 🛡️ **OOM 防护** | 内存感知，K8s 环境下自动降速防杀 |

## 前置条件

**配置分两步检查，缺哪块补哪块：**

1. **语雀 Token**：需 `doc:read` `doc:write` `repo:read` `repo:write` 权限（第一步检查）
2. **LLM 配置**：兼容 OpenAI Chat Completions API 的模型，需 `model` / `url` / `api_key` 三项齐全（第二步检查）
3. **目标知识库**：需提前创建

### 配置文件

`utils/yuque/yuque-ai/yuque-config.json`：

```json
{
  "token": "语雀 API Token",
  "llm": {
    "model": "deepseek-chat",
    "url": "https://api.deepseek.com/v1/chat/completions",
    "api_key": "sk-xxx"
  }
}
```

## 使用方式

由 AI Agent 驱动。用户说「将《xxx》内容整理到《yyy》」即可触发。

### 脚本

| 脚本 | 说明 |
|------|------|
| `scripts/migrate.py` | v4 迁移脚本，清洗+分类+创建+挂目录一气呵成 |

```bash
# 首次运行
python migrate.py utils/yuque-migration/progress/12345_旧库名.json

# 续传（中断后直接重新运行）
python migrate.py utils/yuque-migration/progress/12345_旧库名.json
```

### 进度文件

位于 `utils/yuque-migration/progress/{book_id}_{旧库名}.json`，由 AI Agent 在步骤 1-2 阶段自动创建。

### 日志

位于 `utils/yuque-migration/logs/`。

## License

MIT
