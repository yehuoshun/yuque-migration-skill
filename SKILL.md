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
| 进度 | `utils/yuque/yuque-migration/progress/{旧库名}.json` |

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
  "skipped": 15,
  "skipped_duplicates": [{"doc_id": 111, "title": "xx", "matched": 222}],
  "skipped_empty": [{"doc_id": 333, "title": "空文档标题"}],
  "failed": 3,
  "failed_list": [{"id": 999, "title": "xx", "reason": "未知格式: pdf"}],
  "docs_with_attachments": [{"doc_id": 444, "title": "xx", "attachments": ["url1"]}],
  "lake_docs": [{"doc_id": 555, "new_id": 666, "title": "xx", "reason": "lake 格式已转为 markdown"}],
  "processed_doc_ids": [111, 222],
  "created_doc_mapping": {"444": 555},
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
- `created`：已成功创建到目标库的文档数（含拆分后的子文档）
- `skipped`：跳过的总数 = 去重 + 空文档 + 二进制
- `failed` / `failed_list`：创建失败的文档及原因
- `skipped_duplicates`：标题+内容匹配判定为重复的文档
- `skipped_empty`：空 body 文档
- `docs_with_attachments`：含附件引用的文档（附件无法迁移，列清单）
- `processed_doc_ids`：已处理过的源库 doc_id（防止重复处理）
- `created_doc_mapping`：源 doc_id → 目标 doc_id 映射（用于 TOC 挂载时可追溯）
| API 文档 | `skills/yuque-ai/references/api_reference.md` |

> 基地址：`https://www.yuque.com/api/v2`，以下路径均为相对该基址。

## ⚠️ 必做清单（迁移完成前逐项检查）

- [ ] 步骤 1：获取旧库信息
- [ ] 步骤 2：验证目标库存在
- [ ] 步骤 2b：检查目标库容量
- [ ] 步骤 3a：分页获取文档列表
- [ ] 步骤 3b：逐篇清洗、去重、拆分、创建
- [x] **步骤 3c：批量建目录挂文档 ← migrate_batch.py 已实现**
- [ ] 步骤 3d：容量监控
- [ ] 步骤 4：汇报结果

> migrate_batch.py 已实现：迁移完成后自动按标题关键词分类（17 个分类 + 其他），构建 TOC 目录，批量挂载文档（50 篇/批），失败 3 次自动拆单篇重试。支持 `--skip-toc` 跳过和 `--toc-only` 单独执行。

## 流程

### 步骤 1：获取旧库信息

```
GET /users/{login}/repos  → 找到旧库 book_id
GET /repos/{book_id}      → 文档数量、namespace
```

### 步骤 2：验证目标库存在

```
GET /users/{login}/repos  → 检查目标库，不存在则提示先创建
```

### 步骤 2b：检查目标库容量

```
GET /repos/{target_book_id} → 取 items_count
若 >= 4500 → 暂停，提示用户「目标库已达切换阈值 4500 篇，请提供新目标库」
继续时更新 target_book_id 到进度文件
```

> 容量检查只调一次 API 取初始值，迁移过程中**本地累加**已创建文档数。
> 仅在续传时重新 API 查询（防止中断期间他人写入）。详见步骤 3d。

### 步骤 3：逐篇复制 + 批量挂目录

**3a. 分页获取文档列表**

```
GET /repos/{book_id}/docs?offset={N}&limit=100
```

**3b. 逐篇处理**

对每篇文档：
1. `GET /repos/{book_id}/docs/{doc_id}?raw=1` 读取原文
2. 空文档跳过。先 `GET /repos/{book_id}/docs/{doc_id}?raw=1` 读取原文，取 `format` 字段：
   - **markdown** → 正常走清洗流程
   - **lake** → 不跳过，取 `body_lake` 字段（原生格式）作为文档内容，用 `format: "lake"` 创建到目标库。不做格式清洗、不去重（lake 格式无法比对文本）。标题保持不变，有附件链接原样保留。标记原因「lake 格式无损搬运」
   - **其他未知格式** → 记入 `failed`，标记原因「未知格式: {format}」
   - 空 body（去除空白后为空）→ 记入 `skipped_empty`，跳过
3. **二进制检测**：内容采样前 200 字符，若非 ASCII + 控制字符比例 > 25% 则判定为二进制文件 → 跳过（不记入 failed，直接跳过）
4. **格式清洗**：丢给 LLM 清理广告/免责条款/水评论/HTML残留，保留有技术价值的评论和内部链接。发现附件引用（图片/文件链接）标注到 `docs_with_attachments` 清单。

   **清洗后表格自检**（创建文档前逐篇检查）：
   
   | 检查项 | 异常表现 | 修复方式 |
   |--------|----------|----------|
   | 表格被代码块包裹 | 表格在 \`\`\` 内部 | 删除包裹的 \`\`\` 标记 |
   | 表格行被缩进 | 行首有 4 空格或 Tab | 移除表格行前导空白 |
   | 表格前缺空行 | 表格紧跟前一行文字 | 在表格前插入一个空行 |
   | 分隔行列数不对 | `\|---\|---\|---\|` vs 表头列数 | 补齐或删除多余的列分隔 |
   | 单元格含未转义竖线 | `\|` 出现在单元格内容中 | 转义为 `\\\|` |
   
   检查命令（Python 一行）：
   ```python
   import re
   # 检测被代码块包裹的表格
   bad = re.findall(r'```\s*\n(\|[^\n]+\|[\s\S]*?)\n```', body)
   if bad: print(f'⚠️ {len(bad)} 个表格被代码块包裹，需修复')
   # 检测缩进的表格行
   indented = re.findall(r'^(    |\t)\|', body, re.MULTILINE)
   if indented: print(f'⚠️ {len(indented)} 行表格被缩进，需修复')
   ```

清洗 prompt 模板：
```
你是语雀文档格式清洗助手。

输入：一篇从语雀导出的 Markdown 文档（可能含广告、免责条款、水评论、HTML 残留、空链接等噪音）。

要求：
1. 删除：广告横幅、纯表情/灌水评论、HTML 注释、废弃的 HTML 标签
2. 保留：正文全部技术内容、转载来源标记（"本文来自"/"原文链接"等）、有实质讨论的评论（标注"评论："）、文档内部超链接、代码块、表格、Mermaid 图表
3. 修复：断裂的 Markdown 格式、中文全角标点混用、空链接 `[]()`
4. 不改动：标题层级、代码块内容、表格数据

⚠️ 表格铁律（违反会导致表格在语雀中变成代码块）：
- 绝对不要用代码块（```）包裹表格
- 绝对不要缩进表格行（4空格缩进=代码块）
- 表格前必须保留一个空行，表格后也必须保留一个空行
- 表格分隔行必须用 `| --- | --- |` 格式，列数必须与表头一致
- 表格单元格内的竖线 `|` 必须转义为 `\|`
- 表格中不要使用 HTML 标签（`<br>` 等）
- 保持原始表格的列对齐不变

输出清洗后的完整 Markdown，不做摘要。
```
5. **去重**：先用 `GET /search?q={标题}&type=doc&scope={目标库namespace}` 搜标题 → 有匹配则 GET 该文档正文，逐级比对（200 字 → 500 字 → 全文），任意一级不同即视为不同文档；完全相同跳过；标题相同内容不同加 `(重复标题-N)`，N 为目标库已有同名最大编号+1
6. **大文档**：>50000 字（约 200KB，不统计代码块内字数）→ 按内容结构拆分，标题加 `(1/N)` 后缀<br>拆分优先级：`##` 标题 → `###` 标题 → 段落边界（空行）<br>铁律：不断在段内、不跨代码块/表格切、代码块跟随最近的标题整块带走、每份 ≤50000 字
7. `POST /repos/{book_id}/docs` 创建文档，收集 `doc_id`

API 超时（>30s）→ 下载到本地处理，处理完清理临时文件。

**3c. 批量挂目录（每 50 个 doc_id 一批）**

新建目录结构（目标库内）：

1. 建一个根 TITLE 节点，标题用旧库名，获取 `root_uuid`
2. 将清洗后的文档标题+正文摘要丢给 LLM，按主题自动分类，输出子目录结构（TITLE 节点名 + 各节点归属文档列表）
3. 按分类结果逐级建子节点，挂文档

建节点：
```
PUT /repos/{book_id}/toc { action: "appendNode", action_mode: "child", type: "TITLE", title: "旧库名" }
→ 获取 uuid（记为 root_uuid）
PUT /repos/{book_id}/toc { action_mode: "child", type: "TITLE", title: "子目录名", target_uuid: "{root_uuid}" }
```

批量挂文档：
```
PUT /repos/{book_id}/toc { action: "appendNode", action_mode: "child", type: "DOC",
           target_uuid: "{子目录uuid}", doc_ids: [123,456,789,...] }
```

TOC 失败 → 等 1s 重试，最多 3 次。3 次均失败 → 拆成单篇逐条重试。单条 3 次还挂 → 记入进度文件 `orphans`（文档已创建但未挂目录，含错误原因，最终汇报列出）。

**3d. 容量监控（本地累加，不重复调 API）**

步骤 2b 已取 `initial_count`，迁移过程中本地累计：

```
current_count = initial_count + 本地已成功创建到当前目标库的文档数
若 current_count >= 4500 → 暂停迁移，提示用户：
  「⚠️ 目标库《xxx》已达切换阈值 4500 篇，已迁移 N 篇。请提供新的目标库名称，或确认原目标库有空间后说『继续整理』」
```

> 只在切换新目标库或续传时才重新调 API 取 `initial_count`（防止中断期间他人写入了文档）。

暂停时：
1. 保存进度文件（含当前 target_book_id、last_offset、当前库已创建文档数）
2. 等待用户提供新目标库

用户提供新目标库后：
1. API 验证新目标库存在，取 `items_count` 作为新 `initial_count`
2. 更新进度文件中的 `target_book_id` 和 `initial_count`，重置本地已创建计数为 0
3. 在进度文件中追加历史目标库记录（用于最终汇报）：
   ```json
   "target_history": [
     {"book_id": 456, "book_name": "目标库A", "doc_count": 4500},
     {"book_id": 789, "book_name": "目标库B", "doc_count": 0}
   ]
   ```
4. 后续创建文档使用新 target_book_id
5. TOC 目录结构在新目标库中**重新创建**（不复用旧目标库的 TOC）

**3e. 每批次保存进度**：每处理完一批（100 篇）保存一次进度文件，包含 `last_offset`、`created`、`skipped`、`failed` 及各类清单。API 请求过程中若触发限流或错误也即时保存。

### 步骤 4：汇报结果

```
📦 《旧库名》(N篇) → 目标库/
   ├─ 复制: C 篇（成功创建，含拆分后的子文档）
   ├─ 跳过: D 篇（去重）E 篇（空文档）F 篇（二进制文件）
   ├─ 大文档: W 篇（已拆分）
   ├─ 含附件文档: V 篇（清单，附件无法迁移，请手动处理）
   ├─ Lake 无损搬运: L 篇（lake 格式原样搬运，原生表格/样式完整保留）
   ├─ 失败: U 篇（清单，列出原因）
   ├─ 孤儿文档: P 篇（已创建但未挂目录，需手动处理）
   ├─ 目标库用量:
   │    目标库A: 4500/5000（已切换）
   │    目标库B: 350/5000（当前）
   └─ 原库: 未动
```

> 若跨多个目标库，列出每个目标库的文档分布。

## 限流处理

```
触发 429 → 检查 X-RateLimit-Remaining:
  =0 (5000/h) → 保存进度，通知整点后说「继续整理《旧库名》」
  >0 (100/s)  → 等 1s 重试，最多 3 次，还失败暂停
```

## 错误处理

所有 API 错误（网络超时、5xx、4xx 非 404）→ 等 1s 重试，最多 3 次 → 失败记入进度文件 `failed`（含错误信息）。404 → 直接跳过记空文档。

## 续传

中断后说「继续整理《旧库名》」→ 从 `last_offset` 续传，先检查目标库已有文档避免重复。

## 并发与内存

- 同时处理的文档不超过 5 篇
- 下载后累计正文超过 5MB → 暂停新请求，等当前批处理完释放再继续
- 单篇 >10 万字 → 串行处理，不与其他文档并行

## 不需要做的事

- 不建分类库（目前不需要）
- 不删原库
- 不搬附件（API 不支持，列清单）
- 源库整体迁入单个目标库，不分散到多个目标库
- 不构建索引
