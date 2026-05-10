---
name: yuque-migration
description: 将语雀知识库内容复制整理到另一个知识库。清洗格式、去重、拆大文档、批量挂目录、断点续传。当用户说「将《xx》内容整理到《xx》」时触发。
---

# 语雀知识库迁移

复制不搬。原库不动，目标库接收清洗后的内容。

## 核心原则

- 复制不搬：旧库**完全不动**
- 你驱动：你说「将《xxx》内容整理到《yyy》」我才动
- 不删原库：永远不自动删旧库

## 配置路径

| 文件 | 路径 |
|------|------|
| 配置 | `utils/yuque/yuque-ai/yuque-config.json` |
| 迁移脚本 | `scripts/migrate.py`（v4 迁移即分类版） |
| 进度 | `utils/yuque/yuque-migration/progress/{book_id}_{旧库名}.json` |

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
  "failed": 3,
  "failed_list": [{"id": 999, "title": "xx", "reason": "未知格式: pdf"}],
  "docs_with_attachments": [{"doc_id": 444, "title": "xx", "attachments": ["url1"]}],
  "lake_docs": [{"doc_id": 555, "new_id": 666, "title": "xx", "reason": "lake格式无损搬运"}],
  "processed_doc_ids": [111, 222],
  "created_doc_mapping": {"444": 555, "555": 666},
  "toc_map": {"Java": "uuid-xxx", "Python/异步": "uuid-yyy"},
  "orphans": [{"doc_id": 999, "title": "xx", "errors": ["TOC挂载失败"]}],
  "rate_limit": {"remaining": 1234, "last_checked": "2026-01-01T00:00:00Z"},
  "initial_count": 4500,
  "local_created": 300,
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
- `created`：已成功创建到目标库的文档数（含拆分子文档 + 多目录副本）
- `multi_category_copies`：多目录复制产生的文档数
- `skipped`：跳过的总数
- `toc_map`：已建目录缓存 `{分类名: uuid}`，避免重复 PUT TITLE
- `orphans`：已创建成功但 TOC 挂载失败的文档
- `local_created`：当前目标库累计创建的文档数，用于容量判断
- 其余字段同上

| API 文档 | `references/api_reference.md` |

> 基地址：`https://www.yuque.com/api/v2`，以下路径均为相对该基址。

## ⚠️ 必做清单（迁移完成前逐项检查）

- [ ] 步骤 1：获取旧库信息
- [ ] 步骤 2：验证目标库存在
- [ ] 步骤 2b：检查目标库容量
- [ ] 步骤 3a：分页获取文档列表
- [ ] 步骤 3b：逐篇清洗+分类、去重、拆分、创建、挂目录
- [ ] 步骤 3c：容量监控
- [ ] 步骤 4：汇报结果

## 流程

### 步骤 1：获取旧库信息

```
GET /users/{login}/repos  → 找到旧库 book_id
GET /repos/{book_id}      → 文档数量、namespace
```

> 若 `items_count` = 0（空源库），直接跳到步骤 4 汇报「源库为空，无需迁移」。

### 步骤 2：验证目标库存在

```
GET /users/{login}/repos  → 检查目标库，不存在则提示先创建
```

### 步骤 2b：检查目标库容量

```
GET /repos/{target_book_id} → 取 items_count
若 >= 4500 → 暂停，提示用户切换目标库
```

> 容量检查只调一次 API，迁移过程中**本地累加** `local_created`。详见步骤 3c。

### 步骤 3：逐篇复制（清洗+分类+拆分+创建+挂目录）

**3a. 分页获取文档列表**

```
GET /repos/{book_id}/docs?offset={N}&limit=100
```

**3b. 逐篇处理：清洗+分类合并**

对每篇文档：
1. `GET /repos/{book_id}/docs/{doc_id}?raw=1` 读取原文
2. **标题截断**：语雀 API 标题上限 200 字符，超长截断加 `...`
3. 取 `format` 字段：
   - **markdown** → LLM 清洗+分类（一次调用，全文输入）
   - **lake** → 取 `body_lake`，`format: "lake"` 创建到目标库。不做清洗分类，默认「未分类」
   - 空 body → 跳过记 `skipped_empty`
   - 未知格式 → 记入 `failed`
4. **二进制检测**：非 ASCII + 控制字符 > 30% → 跳过
5. **LLM 清洗+分类（合并一次调用）**：LLM 阅读全文，输出清洗后正文 + 末尾分类标记 `<!-- CATEGORIES: ["分类1", "分类2"] -->`。分类按内容多选，宁可少分类不误分类。

   跳过 LLM（默认「未分类」）：SQL dump / JSON 数组 / 代码文件 / <500 字符短文档。

6. **去重**：搜标题 → 逐级比内容（200字→500字→全文），完全不同则创建；完全相同跳过；标题同内容不同加 `(重复标题-N)`。
7. **大文档拆分**：>50000 字按 `##` 标题拆分，代码块安全切分。每 part 独立 POST。
8. **立即挂目录**（不攒到后置阶段）：
   - 从分类列表按 `/` 拆层级建 TITLE 节点（`toc_map` 缓存 uuid）
   - 主分类挂原始 doc_ids，额外分类**复制文档**后挂副本（语雀不支持一文档多目录）
   - 329 → 等整点重试；失败 → 降级挂父层级 + 记 orphans

**3c. 容量监控**

```
current_count = initial_count + local_created
若 >= 4500 → 暂停，提示用户提供新目标库
```

> 切换目标库或续传时重新 API 取 `initial_count`。

**3d. 每篇保存进度**：每处理完一篇立即保存进度文件。429 或错误时也即时保存。

### 步骤 4：汇报结果

```
📦 《旧库名》(N篇) → 目标库/
   ├─ 复制: C 篇（含拆分子文档 + D 篇多目录副本）
   ├─ 跳过: S1 篇（去重）S2 篇（空文档）S3 篇（二进制）
   ├─ 大文档: W 篇（已拆分）
   ├─ Lake 无损搬运: L 篇
   ├─ 失败: U 篇（清单）
   ├─ 孤儿文档: P 篇（已创建但挂载失败）
   ├─ 目录: K 个
   ├─ 目标库用量:
   │    目标库A: 4500/5000（已切换）
   │    目标库B: 350/5000（当前）
   └─ 原库: 未动
```

## 限流处理

```
收到 429 → 读取 X-RateLimit-Remaining
  remaining == "0" → 暂停等整点恢复
  remaining > "0"  → 等 1s 重试（最多 3 次）
恢复条件：当前分钟数 = 0（整点）
```

## 错误处理

- API 错误（超时/5xx/4xx非404非429）→ 等 1s 重试，最多 3 次 → 记入 `failed`
- 404 → 跳过按空文档处理
- 429 → 走限流流程
- TITLE 节点创建失败 → 降级挂父层级，不阻塞

## 续传

中断后说「继续整理《旧库名》」→ 从 `last_offset` 续传。`toc_map` 保留已建目录结构，新文档直接复用已有 uuid。

## 并发与内存

- **单任务**：不同时跑多个迁移任务
- **原子写入**：`os.replace(tmp, real)`
- **内存感知**：读取 cgroup limit，RSS > 85% 安全水位 → 降为串行，> 60% → 并发降半
- 语雀 API QPS 100/s，5-10 并发碰不到限流线，仅响应 429 暂停

## ⚠️ 免责声明

本工具按「原样」提供：
- 源库只复制不搬移，不会删除源库任何内容
- 迁移可能导致文档拆分、标题去重更名、格式变化；附件不支持迁移
- 语雀 API 限流严格，大库迁移耗时较长
- 格式清洗依赖 LLM，小概率漏清或误删，建议先测试
- 作者对文档错乱、数据丢失等不承担责任

## 不需要做的事

- 不删原库
- 不搬附件
- 不构建索引
- 不同时运行多个迁移任务
