---
name: yuque-migration
description: 将语雀知识库内容复制整理到另一个知识库。清洗格式、去重、截断长文档、批量挂目录、断点续传。当用户说「将《xx》整理到《xx》」或「将《xx》迁移到《xx》」时触发。
---

# 语雀知识库迁移

复制不搬。原库不动，目标库接收清洗后的内容。

## 核心原则

- 复制不搬：旧库**完全不动**
- 你驱动：你说「将《xxx》内容整理到《yyy》」或「将《xxx》内容迁移到《yyy》」我才动
- 《xxx》/《yyy》可以是知识库名、知识库 ID、或知识库 slug
- 若知识库名匹配到多个同名知识库，让用户选择
- 不删原库：永远不自动删旧库

## 前置配置

### 首次使用

用户说「将《xxx》内容整理到《yyy》」或「将《xxx》内容迁移到《yyy》」后，分两步检查 `yuque-config.json`：

**第一步：检查 `token`**

无 token → 提示用户填写语雀 OpenAPI Token（需 `doc:read` `doc:write` `repo:read` `repo:write` 权限）。

**第二步：检查 `llm` 字段**

无 `llm.model` / `llm.url` / `llm.api_key` 任意一项 → 提示用户补充 LLM 配置。

缺哪块补哪块，补全后再开始：

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

| 字段 | 说明 |
|------|------|
| `token` | 语雀 OpenAPI Token（需 `doc:read` `doc:write` `repo:read` `repo:write` 权限） |
| `llm.model` | LLM 模型名（兼容 OpenAI Chat Completions API 的模型均可） |
| `llm.url` | LLM API 端点（OpenAI 兼容格式） |
| `llm.api_key` | LLM API Key |

> 缺少 `llm` 配置时无法做清洗+分类，提示用户补充后再开始。

## 配置路径

| 文件 | 路径 |
|------|------|
| 配置 | `config/yuque-config.json`（skill 目录下的 config 文件夹） |
| 迁移脚本 | `scripts/migrate.py`（v5 去重+重拟标题+容量追踪版） |
| 进度 | `progress/{book_id}_{旧库名}.json`（skill 目录下的 progress 文件夹，可自定义） |

进度文件结构：
```json
{
  "source_book_id": 123,
  "source_name": "旧库名",
  "source_namespace": "user/repo",
  "target_book_id": 456,
  "target_name": "目标库名",
  "target_namespace": "user/repo",
  "last_offset": 150,
  "total_docs": 300,
  "created": 120,
  "multi_category_copies": 15,
  "skipped": 15,
  "skipped_duplicates": [{"doc_id": 111, "title": "xx", "matched": 222}],
  "skipped_empty": [{"doc_id": 333, "title": "空文档标题"}],
  "skipped_binary": [{"doc_id": 777, "title": "二进制文件"}],
  "skipped_meaningless": [{"doc_id": 999, "title": "xx", "reason": "无意义文档"}],
  "skipped_unsupported": [{"doc_id": 1000, "title": "xx", "format": "pdf", "reason": "不支持的文件格式"}],
  "failed": 3,
  "failed_list": [{"id": 999, "title": "xx", "reason": "未知格式: pdf"}],
  "docs_with_attachments": [{"doc_id": 444, "title": "xx", "attachments": ["url1"]}],
  "skipped_lake": [{"doc_id": 555, "title": "xx", "reason": "lake文档无法完美迁移，已跳过"}],
  "processed_doc_ids": [111, 222],
  "created_doc_mapping": {"444": 555, "555": 666},
  "toc_map": {"Java": "uuid-xxx", "Python/异步": "uuid-yyy"},
  "orphans": [{"doc_id": 999, "title": "xx", "errors": ["TOC挂载失败"]}],
  "rate_limit": {"remaining": 1234, "last_checked": "2026-01-01T00:00:00Z"},
  "initial_count": 50,
  "local_created": 300,
  "_created_title_cache": {
    "文档标题": {"doc_id": 123, "body_200": "...", "body_500": "..."}
  },
  "target_history": [
    {"book_id": 111, "book_name": "目标库A", "doc_count": 4500},
    {"book_id": 456, "book_name": "目标库B", "doc_count": 150}
  ]
}
```

字段说明：
- `source_book_id` / `target_book_id`：源库和目标库的数字 ID
- `last_offset`：当前分页偏移量（续传起点）
- `total_docs`：源库文档总数
- `created`：已成功创建到目标库的文档数（含多目录副本）
- `multi_category_copies`：多目录复制产生的文档数
- `skipped`：跳过的总数
- `toc_map`：已建目录缓存 `{分类名: uuid}`，避免重复 PUT TITLE
- `orphans`：已创建成功但 TOC 挂载失败的文档
- `initial_count`：目标库迁移前的初始文档数
- `local_created`：当前目标库累计创建的文档数
- `_created_title_cache`：已创建文档的标题缓存，用于去重快速匹配
- 其余字段同上

| API 文档 | `references/api_reference.md` |

> 基地址：`https://www.yuque.com/api/v2`，以下路径均为相对该基址。

## ⚠️ 必做清单（迁移完成前逐项检查）

- [ ] 步骤 1：获取旧库信息 + 验证目标库存在（一次 API 调用同时完成）
- [ ] 步骤 2：检查目标库容量
- [ ] 步骤 3a：分页获取文档列表
- [ ] 步骤 3b：逐篇清洗+分类、去重、截断、创建、挂目录
- [ ] 步骤 3c：容量监控
- [ ] 步骤 4：汇报结果

## 流程

### 步骤 1：获取旧库信息 + 验证目标库

```
GET /users/{login}/repos → 一次调用返回所有知识库
  ├─ 找到旧库 → book_id
  └─ 检查目标库 → 存在? (不存在则提示先创建)
GET /repos/{book_id} → 文档数量、namespace
```

> 若 `items_count` = 0（空源库），直接跳到步骤 4 汇报「源库为空，无需迁移」。

### 步骤 2：检查目标库容量

```
GET /repos/{target_book_id} → 取 items_count
若 >= 4500 → 暂停，提示用户切换目标库
```

> 容量检查只调一次 API，迁移过程中**本地累加** `local_created`。详见步骤 3c。

### 步骤 3：逐篇复制（清洗+分类+截断+创建+挂目录）

**3a. 分页获取文档列表**

```
GET /repos/{book_id}/docs?offset={N}&limit=100
```

**3b. 逐篇处理：去重→清洗+分类→重拟标题**

对每篇文档：
1. `GET /repos/{book_id}/docs/{doc_id}?raw=1` 读取原文
2. **标题截断**：语雀 API 标题上限 200 字符，超长截断加 `...`
3. 取 `format` 字段：
   - **markdown** → 继续去重+LLM 清洗+分类
   - **lake** → 跳过，记入 `skipped_lake`。lake 文档无法完美迁移，结构复杂、格式不兼容
   - 空 body → 跳过记 `skipped_empty`
   - 未知格式 → 记入 `failed`
4. **二进制检测**：非 ASCII + 控制字符 > 30% → 跳过
5. **去重**（先于 LLM，省 token）：
   - 先查本地缓存 `_created_title_cache`（已创建文档的标题→内容指纹映射）
   - 缓存未命中则搜索目标库同标题文档
   - 逐级比内容（200字→500字→全文），标准化空白后比较
   - 完全相同 → 跳过记 `skipped_duplicates`，不浪费 LLM 调用
   - 无同标题 → 继续步骤 6
   - 标题同内容不同 → 标记「需重拟标题」，继续步骤 6
6. **LLM 清洗+分类+重拟标题（合并一次调用）**：

   **长文档截断**：单次喂入上限 **20000 字符**。超过则截取前 20000 字符送入 LLM，LLM 在可见文本内找最后一个完整段落/章节边界作为输出终点。

   **跳过 LLM（默认「未分类」）**：
   - 纯代码文档（源码文件内容，非教程/技术文章）
   - 附件文档（内容为文件链接/下载地址列表，无实质正文）
   - < 500 字符短文档

   LLM 一次调用完成：
   - 格式清洗 + 内容分类（多选，宁可少分不误分）
   - 长文档在可见文本内找最后一个完整边界截断（不会在句子中间或代码块内部断）
   - 若标记「需重拟标题」→ 生成有区分度的新标题
   - 最终标题格式：`旧标题（新标题）`
   - 输出：正文 + `<!-- CATEGORIES: ["分类1", "分类2"] -->` + `<!-- NEW_TITLE: "新标题" -->`（如有）
7. **立即挂目录**（不攒到后置阶段）：
   - 从分类列表按 `/` 拆层级建 TITLE 节点（`toc_map` 缓存 uuid）
   - 主分类挂原始 doc_ids，额外分类**复制文档**后挂副本（语雀不支持一文档多目录）
   - 429 → 等整点重试
   - 非429失败 → 等1s重试×3 → 仍失败则降级挂父层级 + 记 orphans
   - **TOC 树本地缓存**：首次拉取完整目录树缓存到 `_toc_cache`，后续挂载时直接查本地树判断节点是否存在、快速定位 uuid，避免每次挂载都调 `GET /toc`。新创建的 TITLE 节点同步写入缓存

**3c. 容量监控**

首次启动时 API 获取目标库初始文档数 `initial_count`，写入进度文件。
迁移过程中累加 `local_created`：

```
current_count = initial_count + local_created
若 >= 4500 → 暂停，提示用户提供新目标库
```

> `initial_count` 只在首次获取一次，续传时从进度文件读取。

**3d. 每篇保存进度**：每处理完一篇立即保存进度文件。429 或错误时也即时保存。

### 步骤 4：汇报结果 + 生成报告

迁移完成后，脚本自动在进度文件同级生成 Markdown 汇总报告（`进度文件.report.md`），包含：

**终端输出**（摘要）：

```
📦 《旧库名》(N篇) → 目标库/
   ├─ 复制: C 篇（含 D 篇多目录副本）
   ├─ 跳过: S 篇（去重 / 空文档 / Lake / 二进制 / 无意义 / 不支持格式）
   ├─ 失败: F 篇（清单）
   ├─ 孤儿文档: P 篇（已创建但挂载失败）
   ├─ 目录: K 个
   ├─ 目标库用量: current/5000
   └─ 📄 汇总报告: progress/xxx.report.md
```

**Markdown 报告**（`{进度文件}.report.md`）包含：

- 概览表（源/目标库、总数、成功、跳过分类、失败、容量）
- 跳过明细（按类型分组，列出每篇文档标题和原因，附源链接可直接跳转）
- 失败清单
- 孤儿文档清单
- 目录结构列表

## 限流处理

```
收到 429 → 读取 X-RateLimit-Remaining
  remaining == "0" → 暂停等整点恢复
  remaining > "0"  → 等 1s 重试（最多 3 次）
恢复条件：当前分钟数 = 0（整点）
```

## 错误处理

- API 错误（超时/其他响应码 非404非429）→ 等 1s 重试，最多 3 次 → 记入 `failed`
- 404 → 跳过按空文档处理
- 429 → 走限流流程
- TITLE 节点创建失败 → 429等整点/非429等1s重试×3 → 仍失败降级挂父层级，不阻塞

## 续传

中断后说「继续整理《旧库名》」→ 从 `last_offset` 续传。`toc_map` 保留已建目录结构，新文档直接复用已有 uuid。

## 并发与内存

- **单任务**：不同时跑多个迁移任务
- **原子写入**：`os.replace(tmp, real)`
- **内存感知**：读取 cgroup limit，RSS > 85% 安全水位 → 降速并强制 GC，> 60% → 降半并发
- **OOM 防护优先**：K8s 环境下优先保证进程不被 OOM Kill，必要时自动降速
- 语雀 API QPS 100/s，正常速率下不设硬并发上限，仅响应 429 暂停

## ⚠️ 免责声明

本工具按「原样」提供：
- 源库只复制不搬移，不会删除源库任何内容
- 迁移可能导致长文档截断、标题去重更名、格式变化；附件不支持迁移
- 语雀 API 限流严格，大库迁移耗时较长
- 格式清洗依赖 LLM，小概率漏清或误删，建议先测试
- 作者对文档错乱、数据丢失等不承担责任

## 不需要做的事

- 不删原库
- 不搬附件
- 不构建索引
- 不同时运行多个迁移任务
