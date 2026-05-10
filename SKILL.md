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
| 迁移脚本 | `scripts/migrate.py`（通用版，运行时传进度文件路径） |
| 进度 | `utils/yuque/yuque-migration/progress/{book_id}_{旧库名}.json` |
| TOC 状态 | `utils/yuque/yuque-migration/toc/{book_id}_{旧库名}.json` |

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
  "skipped_binary": [{"doc_id": 777, "title": "二进制文件"}],
  "failed": 3,
  "failed_list": [{"id": 999, "title": "xx", "reason": "未知格式: pdf"}],
  "docs_with_attachments": [{"doc_id": 444, "title": "xx", "attachments": ["url1"]}],
  "lake_docs": [{"doc_id": 555, "new_id": 666, "title": "xx", "reason": "lake 格式无损搬运"}],
  "processed_doc_ids": [111, 222],
  "created_doc_mapping": {"444": 555, "555": 666},
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
- `created`：已成功创建到目标库的文档数（含拆分后的子文档）
- `skipped`：跳过的总数 = 去重 + 空文档 + 二进制
- `skipped_duplicates`：标题+内容匹配判定为重复的文档
- `skipped_empty`：空 body 文档
- `skipped_binary`：检测为二进制跳过的文档
- `failed` / `failed_list`：创建失败的文档及原因
- `docs_with_attachments`：含附件引用的文档（附件无法迁移，列清单）
- `lake_docs`：lake 格式文档，`new_id` 为在目标库创建后的 doc_id（也写入 `created_doc_mapping`）
- `processed_doc_ids`：已处理过的源库 doc_id（防止重复处理）
- `created_doc_mapping`：源 doc_id → 目标 doc_id 映射（含 lake 文档，用于 TOC 挂载和去重）
- `orphans`：已创建成功但 TOC 挂载失败的文档
- `rate_limit`：最后一次 API 调用后的限流状态
- `initial_count`：目标库初始文档数（步骤 2b 获取，切换目标库时更新）
- `local_created`：当前目标库累计创建的文档数（不含切换前的目标库），用于容量判断
- `target_history`：所有使用过的目标库记录（跨库迁移时追溯）
| API 文档 | `skills/yuque-ai/references/api_reference.md` |

> 基地址：`https://www.yuque.com/api/v2`，以下路径均为相对该基址。

## ⚠️ 必做清单（迁移完成前逐项检查）

- [ ] 步骤 1：获取旧库信息
- [ ] 步骤 2：验证目标库存在
- [ ] 步骤 2b：检查目标库容量
- [ ] 步骤 3a：分页获取文档列表
- [ ] 步骤 3b：逐篇清洗、去重、拆分、创建
- [ ] 步骤 3c：LLM 基于文档内容自动分类 + 批量建目录挂文档
- [ ] 步骤 3d：容量监控
- [ ] 步骤 4：汇报结果

## 流程

### 步骤 1：获取旧库信息

```
GET /users/{login}/repos  → 找到旧库 book_id
GET /repos/{book_id}      → 文档数量、namespace
```

> 若 `items_count` = 0（空源库），直接跳到步骤 4 汇报「源库为空，无需迁移」，不执行后续步骤。

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

⚠️ `items_count` 可能有缓存延迟。后续本地原子累加 `local_created` 兜底——如果本地累计显示超了但还没触发 429，说明 items_count 取少了，实际容量以本地累计为准提前给预警。不额外调 API 验证。

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
2. **标题截断**：语雀 API 标题长度上限 **200 字符**（官方文档写 255 是错误的，实测返回 `长度要求小于 200`）。创建文档前检查标题长度，超过 200 字符截断并加 `...`。否则 POST 返回 422。
3. 空文档跳过。先 `GET /repos/{book_id}/docs/{doc_id}?raw=1` 读取原文，取 `format` 字段：
   - **markdown** → 正常走清洗流程
   - **lake** → 不跳过，取 `body_lake` 字段（原生格式）作为文档内容，用 `format: "lake"` 创建到目标库。不做格式清洗、不去重（lake 格式无法比对文本）。标题保持不变，有附件链接原样保留。标记原因「lake 格式无损搬运」
   - **其他未知格式** → 记入 `failed`，标记原因「未知格式: {format}」
   - 空 body（去除空白后为空）→ 记入 `skipped_empty`，跳过
3. **二进制检测**：内容采样前 200 字符，若非 ASCII + 控制字符比例 > 25% 则判定为二进制文件 → 跳过（不记入 failed，直接跳过）
4. **格式清洗**：丢给 LLM 清理广告/免责条款/水评论/HTML残留，保留有技术价值的评论和内部链接。发现附件引用（图片/文件链接）标注到 `docs_with_attachments` 清单。

   **清洗优化**：以下类型内容**跳过 LLM 清洗**（无广告/水评需要清理，LLM 调用增加耗时和超时风险）：
   - SQL dump（`INSERT INTO`、`CREATE TABLE` 等开头）
   - JSON 数组/对象
   - 纯代码文件（源码文件如 `.js`、`.py` 等）
   - 小于 500 字符的短文档（引用/索引类，清洗无意义）
   
   跳过清洗的文档仍需做表格格式修复（移除包裹表格的代码块、移除表格行缩进）。

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

判断示例：

示例1 — 水评论（删除）：
  输入："感谢分享，学习了！！👍👍"
  处理：删除（纯表情+客套话，无技术内容）

示例2 — 有技术价值的评论（保留）：
  输入："这里用 Promise.all 更好，因为三个请求互不依赖，可以并行。另外 catch 里别忘了 revoke ObjectURL。"
  处理：保留，标注为「评论：这里用 Promise.all 更好...」

示例3 — 广告（删除）：
  输入："扫码加入微信群，领取免费课程→ [广告图]"
  处理：删除（引流广告）

输出清洗后的完整 Markdown，不做摘要。
```
5. **去重**：先用 `GET /search?q={标题}&type=doc&scope={目标库namespace}` 搜标题 → 有匹配则 GET 该文档正文，逐级比对（200 字 → 500 字 → 全文），任意一级不同即视为不同文档；完全相同跳过；标题相同内容不同加 `(重复标题-N)`，N 为目标库已有同名最大编号+1
6. **大文档**：>50000 字（约 200KB，不统计代码块内字数）→ 按内容结构拆分，标题加 `(1/N)` 后缀<br>拆分优先级：`##` 标题 → `###` 标题 → 段落边界（空行）<br>铁律：不断在段内、不跨代码块/表格切、代码块跟随最近的标题整块带走、每份 ≤50000 字

   ⚠️ **代码块安全拆分**：用 `re.split(r'\n(?=## )', body)` 会在代码块内的 `##` 处错误切分。必须**逐行解析**，跟踪 ``` 进出状态，仅当不在代码块内且遇到标题时才切分：
   
   ```python
   in_code = False
   sections = []
   current = ""
   for line in body.split('\n'):
       if line.strip().startswith('```'):
           in_code = not in_code
       if not in_code and (line.startswith('## ') or line.startswith('### ')):
           if current:
               sections.append(current)
           current = line + '\n'
       else:
           current += line + '\n'
   if current.strip():
       sections.append(current)
   ```
7. `POST /repos/{book_id}/docs` 创建文档，收集 `doc_id`

API 超时（>30s）→ 下载到本地处理，处理完清理临时文件。

**3c. LLM 自动分类 + 批量建目录挂文档**

迁移完成后（所有文档已创建），基于文档内容自动建目录结构：

1. 将所有已创建文档的**标题 + 正文前 500 字摘要**丢给 LLM，按内容主题自动聚类，输出目录结构（不预设分类数量，由 LLM 根据实际内容决定）
2. 先建根 TITLE 节点（标题用旧库名），获取 `root_uuid`
3. 按分类结果逐级建子 TITLE 节点，挂文档

分类 prompt 模板：
```
你是文档分类助手。输入是一批文档的标题和内容摘要。请根据文档的实际内容主题进行聚类，输出一个树形目录结构。

要求：
- 不预设分类数量，根据内容自然聚类
- 每个分类的标题简洁（2-6 个字）
- 每个文档只归属一个分类
- 如果某个分类下文档 >50 篇，考虑拆分子分类
- 归类不了的文档放入「其他」

输出格式（JSON）：
[
  {"category": "分类名", "doc_ids": [111, 222]},
  {"category": "分类名/子分类", "doc_ids": [333]}
]

输入文档：
{doc_id: 标题 | 摘要}
...
```

建节点（失败重试 3 次，等 1s）：
```
PUT /repos/{book_id}/toc { action: "appendNode", action_mode: "child", type: "TITLE", title: "旧库名" }
→ 获取 uuid（记为 root_uuid）
PUT /repos/{book_id}/toc { action_mode: "child", type: "TITLE", title: "子目录名", target_uuid: "{root_uuid}" }
```

> TITLE 节点创建 3 次均失败 → 降级：该分类下的文档直接挂到 root_uuid 下（跳过子目录层级），并在 orphans 中标注原因「子目录创建失败，已降级挂载」。

批量挂文档（每 50 个 doc_id 一批）：
```
PUT /repos/{book_id}/toc { action: "appendNode", action_mode: "child", type: "DOC",
           target_uuid: "{子目录uuid}", doc_ids: [123,456,789,...] }
```

TOC 挂载失败 → 等 1s 重试，最多 3 次。3 次均失败 → 拆成单篇逐条重试。单条 3 次还挂 → 记入进度文件 `orphans`（文档已创建但未挂目录，含错误原因，最终汇报列出）。

**TOC 状态文件**：建完目录后，将目录结构（节点标题 + uuid + 归属文档列表）写入 `utils/yuque/yuque-migration/toc/{book_id}_{旧库名}.json`：
```json
{
  "root_uuid": "xxx",
  "nodes": [
    {"title": "分类A", "uuid": "yyy", "parent_uuid": "xxx", "doc_ids": [111, 222]},
    {"title": "分类B", "uuid": "zzz", "parent_uuid": "xxx", "doc_ids": [333]}
  ],
  "degraded": [{"category": "分类C", "reason": "子目录创建失败，文档直接挂 root"}]
}
```
后续需要追加文档到已有目录时，直接读取此文件获取正确的 uuid，无需重建整个 TOC。

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

**3e. 每篇保存进度**：每处理完一篇文档立即更新进度文件，包含 `last_offset`、`created`、`skipped`、`failed` 及各类清单。API 请求过程中若触发限流或错误也即时保存。每次保存前先读取当前进度文件（防止并发写覆盖），合并后写入。

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
触发 429 → 保存进度，暂停迁移，等整点恢复后自动续传
触发 429 → 无论 X-RateLimit-Remaining 是多少，一律暂停
恢复条件：当前时间分钟数 = 0（整点），重试上次失败的请求
```

> 语雀 API 限流严格，不冒险重试。触发一次就等整点，避免账号被长时间封禁。

## 错误处理

所有 API 错误（网络超时、5xx、4xx 非 404 非 429）→ 等 1s 重试，最多 3 次 → 失败记入进度文件 `failed`（含错误信息）。404 → 直接跳过按空文档处理。429 → 走限流处理流程（暂停等整点恢复）。

TITLE 节点创建特殊处理：3 次失败 → 降级（该分类文档直接挂父节点），不阻塞整体流程。

## 续传

中断后说「继续整理《旧库名》」→ 从 `last_offset` 续传，先检查目标库已有文档避免重复。

## 并发与内存

- **单迁移任务**：同时只允许一个迁移任务运行（多个任务会触发语雀 API 高并发限制）。**禁止多进程/多脚本并行操作同一个进度文件**。
- **进度文件原子写入**：每次保存进度必须用原子方式（写临时文件 → `os.replace` 覆盖），防止写入中途崩溃导致文件损坏。每次保存前重新读取进度文件再合并写入，防止并发覆盖。

### 内存感知自适应并发（K8s OOM 防杀）

⚠️ **死规则**：绝对不能在 K8s 环境下触发 OOM。Pod 被杀 = 宕机 = 迁移中断。

不设固定并发数。并发数由以下因素**实时动态决定**：

1. **预检文档大小**（拉取列表时附带 `body_lake`/`body` 预览，不完整拉取）→ 估算单篇内存占用
2. **分级处理**：
   - 小文档（< 500KB 正文）：可并发
   - 中文档（500KB ~ 2MB 正文）：并发数降半
   - 大文档（> 2MB 正文）：串行处理，不与其他文档并行
   - 超大文档（> 10 万字 / > 5MB 正文）：串行，且处理完立即 `del` 释放引用
3. **实时内存监控**：每批处理前检查当前进程 RSS，超过 Pod limit 的 **60%** → 暂停新请求，等当前批处理完、垃圾回收后再继续
4. **安全水位**：
   - Pod 512MB → 脚本可用上限 300MB
   - Pod 1GB → 脚本可用上限 600MB
   - 未知 Pod limit → 默认上限 256MB（保守策略）

**并发初始化流程**：
```
1. 获取 Pod/容器内存 limit（/sys/fs/cgroup/memory/memory.limit_in_bytes）
2. 采样第一批文档的 body 大小 → 估算单篇平均内存
3. 初始并发数 = min(安全水位 / 平均单篇内存, 10)
4. 运行中持续监控 RSS，动态下调
```

**实现要点**：
- 用 `multiprocessing` 或 `concurrent.futures.ProcessPoolExecutor` 替代 `ThreadPoolExecutor`，每个 worker 独立进程空间，处理完即释放（避免 Python GC 延迟）
- 每批提交前跑 `gc.collect()`
- 语雀 API QPS 是 100/s，**5-10 并发完全碰不到限流线，不需要主动降速**。仅响应 429 时暂停等整点

## ⚠️ 免责声明

本工具按「原样」提供，使用即视为同意以下条款：

- **源库安全**：本工具**只复制不搬移**，不会删除或修改源知识库任何内容。如源库出现问题，与本工具无关。
- **目标库风险**：迁移过程可能产生文档拆分、标题去重更名、格式变化等情况。目标库已有的文档不会被覆盖，但新建文档可能与已有内容产生标题冲突（自动加后缀处理）。
- **附件不支持**：语雀 API 不支持附件迁移，原文档中的图片/文件引用在目标库中会失效。请自行处理附件。
- **限流与中断**：语雀 API 有严格限流（5000 次/小时），迁移大库可能耗时数小时并多次暂停等整点恢复。中断后可从断点续传，不会重复创建。
- **容量限制**：API 每个知识库上限 5000 篇文档。超出需切换新目标库。
- **不保证完美**：格式清洗（去广告/去水评论等）依赖 LLM，小概率漏清或误删。建议迁移后抽检关键文档。
- **责任限制**：作者对使用本工具导致的任何文档错乱、数据丢失、知识库混乱、API 配额消耗等问题不承担责任。请先在测试库验证，确认无误后再迁移正式库。

## 不需要做的事

- 不建分类库（目前不需要）
- 不删原库
- 不搬附件（API 不支持，列清单）
- 源库整体迁入单个目标库，不分散到多个目标库
- 不构建索引
- 不同时运行多个迁移任务
