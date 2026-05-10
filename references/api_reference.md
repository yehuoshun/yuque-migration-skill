# 语雀 OpenAPI 参考

> 来源：语雀官方 OpenAPI 文档
> 基地址：`https://www.yuque.com/api/v2`

> 💡 **API 调用方式**：使用 Python 标准库（urllib.request、json、concurrent.futures）调用语雀 API。简单请求也可用 curl + exec。禁止 pip install。
>
> ⚠️ **必须设置 timeout**：`urlopen(req, timeout=30)`，避免网络异常时请求无限挂起。超时后按错误处理规范重试。
>
> 📦 **通用封装参考**：[api_helper.py](api_helper.py) 提供了请求封装、429 重试、速率检查、并行批量等常用模式，可直接命令行调用或复制函数到执行脚本中。
>
> ```bash
> # 命令行直接调用
> python3 api_helper.py <config.json> get /user
> python3 api_helper.py <config.json> post /repos/123/docs '{"title":"test"}'
> ```
>
> **Python 示例**：
> ```python
> import urllib.request, json
> req = urllib.request.Request("https://www.yuque.com/api/v2/...", headers={"X-Auth-Token": token})
> data = json.loads(urllib.request.urlopen(req, timeout=30).read())
> ```
>
> **curl 示例**（仅简单请求）：
> ```bash
> curl -s -H "X-Auth-Token: $TOKEN" "https://www.yuque.com/api/v2/..."
> ```

## 认证

所有 API 请求需要携带 Token：

```http
X-Auth-Token: {token}
```

Token 从配置文件读取（配置文件路径记录在 MEMORY.md 的「语雀」章节）。

## 用户 API

### 获取当前用户

```http
GET /api/v2/user
```

**返回字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 用户 ID |
| `login` | string | 用户名 |
| `name` | string | 昵称 |
| `avatar_url` | string | 头像 URL |
| `description` | string | 简介 |

## 知识库 API

### 获取知识库列表

```http
GET /api/v2/users/{login}/repos
```

**返回字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 知识库 ID |
| `name` | string | 名称 |
| `slug` | string | slug |
| `description` | string | 描述 |
| `public` | int | 0=私有 / 1=公开 / 2=团队内公开 |
| `items_count` | int | 文档数量 |

### 获取知识库详情

```http
GET /api/v2/repos/{id_or_namespace}
```

> `{id_or_namespace}` 支持 book_id（数字）或 namespace（如 `group/book_slug`）。

### 创建知识库

```http
POST /api/v2/users/{login}/repos
Content-Type: application/json

{
  "name": "名称",
  "slug": "repo-slug",
  "description": "描述",
  "public": 0
}
```

**参数**：

| 参数 | 说明 | 默认 |
|------|------|------|
| `name` | 名称（必填） | - |
| `slug` | 路径（必填） | - |
| `description` | 简介 | - |
| `public` | 0=私有 / 1=公开 / 2=团队内公开 | 0 |

⚠️ **slug 必填**：语雀不再自动生成 slug。生成规则：`{拼音缩写}-{时间戳}`，如 `javamst-1714473600`，避免重复。

**slug 格式约束**：仅支持 `[a-z0-9._-]`，大写自动转小写，禁止空格。

### 更新知识库

```http
PUT /api/v2/repos/{id_or_namespace}
Content-Type: application/json

{
  "name": "新名称",
  "description": "新描述",
  "public": 1,
  "toc": "- [文档名](slug)\n  - [子文档](child-slug)"
}
```

**参数说明**：

| 参数 | 说明 |
|------|------|
| `name` | 名称 |
| `slug` | 路径 |
| `description` | 简介 |
| `public` | 0=私有 / 1=公开 / 2=团队内公开 |
| `toc` | 目录（Markdown 格式，全量替换）。格式：`[标题](文档slug)`，缩进空格表示层级 |

> 📌 `toc` 用于批量更新目录树，比逐个调目录 API 更高效。

### 删除知识库

> ⚠️ **硬删除**：不可逆，删除知识库会删除其下所有文档。确认提示应明确警告「不可恢复」。

```http
DELETE /api/v2/repos/{id_or_namespace}
```

**确认提示**：「即将删除知识库《XXX》，包含 N 篇文档。此操作不可恢复，确认删除吗？」

## 文档 API

### 获取文档列表

```http
GET /api/v2/repos/{book_id}/docs?offset={offset}&limit={limit}
```

**参数**：

| 参数 | 说明 | 默认 |
|------|------|------|
| `offset` | 偏移量（分页） | 0 |
| `limit` | 每页条数（≤100） | 100 |
| `optional_properties` | 额外字段，逗号分隔。支持：`hits`（阅读数）、`tags`（标签）、`latest_version_id`（最新已发版本 ID） | "" |

> ⚠️ `limit` 超过 100 时 `optional_properties` 会失效。分页获取全部文档时建议用 `offset` 递增，或直接用 TOC API 获取完整目录树。

**返回字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 文档 ID |
| `title` | string | 标题 |
| `slug` | string | 文档 slug |
| `created_at` | string | 创建时间 |
| `updated_at` | string | 更新时间 |
| `word_count` | int | 字数 |

### 获取文档详情

```http
GET /api/v2/repos/{book_id}/docs/{doc_id}?raw=1
```

**参数**：

| 参数 | 说明 |
|------|------|
| `raw` | `1` = 返回 markdown 原文。不传则返回语雀格式的 HTML |

**支持格式**：
- `markdown`：标准 Markdown
- `lake`：语雀原生 JSON 格式，`body` / `body_lake` 返回 Lake JSON。支持 Mermaid 图表等增强语法

**返回字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 文档 ID |
| `title` | string | 标题 |
| `body` | string | 正文（markdown 或 HTML，取决于 `raw` 参数） |
| `body_html` | string | HTML 内容 |
| `body_lake` | string | Lake 格式内容 |
| `created_at` | string | 创建时间 |
| `updated_at` | string | 更新时间 |
| `creator` | object | 创建者信息 |
| `book` | object | 所属知识库信息 |

### 创建文档

```http
POST /api/v2/repos/{book_id}/docs
Content-Type: application/json

{
  "title": "标题",
  "format": "markdown",
  "body": "内容",
  "public": 0
}
```

**参数**：

| 参数 | 说明 | 默认 |
|------|------|------|
| `title` | 标题（必填） | - |
| `format` | 格式：`markdown` / `html` / `lake` | `markdown` |
| `body` | 正文内容（必填） | - |
| `public` | 0=私有 / 1=公开 | 0 |

⚠️ `slug` 由语雀自动生成，不要手动指定。

⚠️ **重要**：创建文档后**必须添加到目录**，否则文档不会显示在知识库目录中。使用目录 API：
```http
PUT /api/v2/repos/{book_id}/toc
Content-Type: application/json

{
  "action": "appendNode",
  "action_mode": "sibling",
  "type": "DOC",
  "doc_ids": [新建文档ID]
}
```

### 更新文档

```http
PUT /api/v2/repos/{book_id}/docs/{doc_id}
Content-Type: application/json

{
  "title": "新标题",
  "body": "新内容",
  "format": "markdown"
}
```

### 删除文档

> ⚠️ **硬删除**：不可逆。确认提示应明确警告「不可恢复」。

```http
DELETE /api/v2/repos/{book_id}/docs/{doc_id}
```

**确认提示**：「即将删除文档《XXX》。此操作不可恢复，确认删除吗？」

## 搜索 API

> ⚠️ **不要用** `GET /repos/{book_id}/docs` 做搜索，此端点没有 `q` 参数，只能分页列出文档。必须用 `/api/v2/search`。

```http
GET /api/v2/search?q={query}&type={type}&scope={scope}&page={page}
```

**PageSize 固定为 20**，不支持自定义分页大小。

**参数**：

| 参数 | 说明 | 约束 |
|------|------|------|
| `q` | 搜索关键词（必填） | ≤ 200 字符 |
| `type` | `doc`（文档）/ `repo`（知识库）| 必填 |
| `scope` | 搜索范围，不填默认搜索当前用户/团队 | ≤ 400 字符 |
| `page` | 页码 | 1-100 |
| `creator` | 仅搜索指定作者 login（可选） | - |
| `offset` | ⚠️ 已废弃，同 `page`，勿用 | - |

**scope 说明**：
- 不填：搜索当前用户/团队全部
- 搜索团队全部文档：`scope={group}`（如 `yehuoshun`）
- 搜索指定知识库文档：`scope={group}/{book_slug}`（如 `yehuoshun/gi49zs`）
- 只支持 namespace 格式，**不支持 book_id**

**返回结构**：

```json
{
  "meta": {
    "total": 32,
    "pageNo": 1,
    "pageSize": 20
  },
  "data": [
    {
      "id": 123456,
      "type": "doc",
      "title": "文档标题",
      "summary": "摘要（含高亮标记 <em>关键词</em>）",
      "url": "/yehuoshun/xxx/slug",
      "target": {
        "id": 123456,
        "type": "Doc",
        "slug": "abc123",
        "title": "文档标题",
        "book_id": 789,
        "book": {
          "id": 789,
          "name": "知识库名称",
          "namespace": "yehuoshun/xxx"
        }
      },
      "created_at": "2024-01-01T00:00:00.000Z",
      "updated_at": "2024-01-01T00:00:00.000Z"
    }
  ]
}
```

**分页**：
- 总数：`.meta.total`
- 当前页：`.meta.pageNo`
- 每页条数：`.meta.pageSize`
- 结果列表：`.data[]`

**返回字段说明**：

| 字段 | 说明 |
|------|------|
| `.meta.total` | 搜索结果总数 |
| `.meta.pageNo` | 当前页码 |
| `.meta.pageSize` | 每页条数 |
| `.data[].id` | 搜索结果 ID |
| `.data[].type` | 类型：`doc`（文档）/ `repo`（知识库）|
| `.data[].title` | 文档标题 |
| `.data[].summary` | 摘要（含高亮标记 `<em>`） |
| `.data[].url` | 文档相对路径 |
| `.data[].info` | 归属信息 |
| `.data[].target.book_id` | 知识库 ID |
| `.data[].target.book.namespace` | 知识库 namespace |
| `.data[].target.id` | 文档 ID |
| `.data[].target.slug` | 文档 slug |

## 小记 API

### 获取小记列表

```http
GET /api/v2/notes?page={page}&limit={limit}&status={status}
```

**参数**：

| 参数 | 说明 | 默认 |
|------|------|------|
| `page` | 页码 | 1 |
| `limit` | 每页条数 | 20 |
| `status` | 0=正常 / 9=已删除 | 0（不传也默认正常） |

**返回结构**（`.data` 内）：

```json
{
  "pin_notes": [  /* 置顶小记列表 */ ],
  "notes": [     /* 普通小记列表 */ ],
  "has_more": true
}
```

**小记对象字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 小记 ID |
| `slug` | string | 小记 slug |
| `content` | object | 内容对象，详见下方 |
| `word_count` | int | 字数 |
| `tags` | array | 标签列表 |
| `created_at` | string | 创建时间 |
| `updated_at` | string | 更新时间 |
| `pinned_at` | string/null | 置顶时间（null 表示未置顶） |
| `status` | int | 0=正常 / 9=已删除 |
| `likes_count` | int | 点赞数 |
| `comments_count` | int | 评论数 |

⚠️ **列表 vs 详情的 content 差异**：
- **列表 API**：`content` 只有 `abstract` 和 `updated_at`，**没有 `source` 和 `html`**
- **详情 API**（`GET /notes/{id}`）：`content` 包含完整字段 `{source, html, abstract, format, draft_version, doc_dynamic_data}`

### 获取小记详情

```http
GET /api/v2/notes/{note_id}
```

**返回字段**：同上小记对象。

⚠️ **重要**：`content` 是嵌套对象，结构为：
```json
{
  "content": {
    "source": "纯文本内容",
    "html": "<p>HTML 内容</p>",
    "abstract": "摘要文本（前 200 字左右）",
    "format": "markdown",
    "draft_version": 0,
    "updated_at": "2024-01-01T00:00:00.000Z"
  }
}
```

读取小记内容时用 `note.content.source` / `note.content.html` / `note.content.abstract`，不是 `note.content` 直接当字符串。

### 创建小记

```http
POST /api/v2/notes
Content-Type: application/json

{
  "body": "小记内容"
}
```

**参数**：
| 参数 | 说明 |
|------|------|
| `body` | 小记正文内容（必填） |

**返回字段**（`.data` 内）：
| 字段 | 说明 |
|------|------|
| `note_url` | 小记链接 |

⚠️ **实测**：创建小记接口**只返回 `note_url`**，不返回 `id` 和 `slug`。如需获取 id，创建后查小记列表通过 `note_url` 中的 slug 匹配。

### 更新小记

```http
PUT /api/v2/notes/{note_id}
Content-Type: application/json

{
  "source": "新正文（纯文本）",
  "html": "<p>新正文</p>",
  "abstract": "前200字摘要"
}
```

**参数说明**：
| 参数 | 说明 |
|------|------|
| `source` | 纯文本内容（非 Lake 格式也可） |
| `html` | HTML 内容，用 `<p>文本</p>` 包裹 |
| `abstract` | 摘要，取前 200 字 |
| `status` | 可选，9=删除 / 0=恢复 |

**注意**：
- `source` 和 `html` 均非必传 Lake 格式，纯文本包裹 `<p>` 即可正常工作（参考官方 yuque-mcp-server）
- 三个字段不可省略
- 更新后返回完整小记对象（路径 `.data.data`）

### 删除小记

> 💡 **软删除**：小记删除是软删除，移入回收站（status=9），可通过恢复操作还原。确认提示应注明「可恢复」。

**方法**：先获取小记内容，再 PUT 更新并设置 `status: 9`

```http
PUT /api/v2/notes/{note_id}
Content-Type: application/json

{
  "source": "原文内容",
  "html": "原文HTML",
  "abstract": "原文摘要",
  "status": 9
}
```

**注意**：
- 必须先获取小记原文（包含 source、html、abstract），再软删除
- 删除后进入回收站，可通过设置 `status: 0` 恢复
- 确认提示：「即将把小记移入回收站。确认删除吗？（可从回收站恢复）」
- 删除后提示：「小记已移入回收站。如需恢复，请说「恢复这条小记」」

### 恢复小记

**方法**：先获取小记内容，再 PUT 更新并设置 `status: 0`

```http
PUT /api/v2/notes/{note_id}
Content-Type: application/json

{
  "source": "原文内容",
  "html": "原文HTML",
  "abstract": "原文摘要",
  "status": 0
}
```

将 status 从 9（已删除）改回 0（正常）即可恢复。

## 文档版本 API

### 获取文档版本列表

```http
GET /api/v2/doc_versions?doc_id={doc_id}
```

**参数**：

| 参数 | 说明 |
|------|------|
| `doc_id` | 文档 ID（必填） |

**返回字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 版本 ID |
| `doc_id` | int | 关联文档 ID |
| `title` | string | 版本标题 |
| `body` | string | 版本正文内容 |
| `body_draft` | string | 草稿内容 |
| `format` | string | 格式（markdown/lake） |
| `user_id` | int | 编辑者 ID |
| `user` | object | 编辑者信息（name, avatar_url 等） |
| `created_at` | string | 版本创建时间 |

### 获取文档版本详情

```http
GET /api/v2/doc_versions/{version_id}
```

**返回字段**：与版本列表中的单个版本对象结构相同。

## 连通性测试 API

### Hello

```http
GET /api/v2/hello
```

**用途**：测试 API Token 是否有效，可用于验证连通性。

**返回示例**：

```json
{
  "data": {
    "message": "Hello, {user_name}!"
  }
}
```

## 目录 API

### 获取目录

```http
GET /api/v2/repos/{book_id}/toc
```

**返回字段**：

| 字段 | 说明 |
|------|------|
| `uuid` | 节点唯一标识 |
| `type` | `DOC`（文档）/ `TITLE`（分组）/ `LINK`（外链） |
| `title` | 标题 |
| `doc_id` | 文档 ID（仅 DOC 类型） |
| `parent_uuid` | 父节点 UUID |
| `children` | 子节点列表 |

### 更新目录

```http
PUT /api/v2/repos/{book_id}/toc
Content-Type: application/json

{
  "action": "appendNode",
  "action_mode": "child",
  "type": "DOC",
  "doc_ids": [文档ID],
  "target_uuid": "目录UUID"
}
```

**action 说明**：

| 值 | 说明 |
|---|------|
| `appendNode` | 尾插 |
| `prependNode` | 头插 |
| `editNode` | 编辑节点 |
| `removeNode` | 删除节点（不删除关联文档） |

**action_mode 说明**：

| 值 | 说明 |
|---|------|
| `sibling` | 与 target_uuid 同级 |
| `child` | 作为 target_uuid 的子节点 |

## 群组成员 API

> ⚠️ **未测试**：此模块 API 需要团队/群组环境，当前未实际测试，可能存在问题。如有问题请反馈。

### 列出群组成员

```http
GET /api/v2/groups/{login}/users
```

**参数**：
| 参数 | 说明 |
|------|------|
| `login` | 群组 login（团队名） |

**返回字段**：
| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 成员记录 ID |
| `group_id` | int | 群组 ID |
| `user_id` | int | 用户 ID |
| `user` | object | 用户信息（id, login, name, avatar_url） |
| `role` | int | 角色：0=管理员 / 1=成员 / 2=只读成员 |
| `created_at` | string | 加入时间 |
| `updated_at` | string | 更新时间 |

### 更新成员角色

```http
PUT /api/v2/groups/{login}/users/{user_id}
Content-Type: application/json

{
  "role": 1
}
```

**role 说明**：
| 值 | 说明 |
|---|------|
| 0 | 管理员 |
| 1 | 成员 |
| 2 | 只读成员 |

⚠️ 只有群组管理员可以修改成员角色。

### 移除群组成员

> ⚠️ **硬删除**：不可逆。确认提示应明确警告「不可恢复」。

```http
DELETE /api/v2/groups/{login}/users/{user_id}
```

**确认提示**：「即将将成员 @XXX 移出群组。此操作不可恢复，确认移除吗？」

## 统计 API

> ⚠️ **未测试且需额外权限**：此模块 API 需要 `statistic:read` 权限，当前未实际测试，可能存在问题。如需使用，请在语雀设置中生成带该权限的 Token。

### 获取团队整体统计

```http
GET /api/v2/groups/{login}/statistics
```

**返回字段**：
| 字段 | 说明 |
|------|------|
| `books_count` | 知识库总数 |
| `docs_count` | 文档总数 |
| `members_count` | 成员总数 |
| `public_books_count` | 公开知识库数 |
| `public_docs_count` | 公开文档数 |

### 获取成员统计

```http
GET /api/v2/groups/{login}/statistics/members
```

**返回字段**：
| 字段 | 说明 |
|------|------|
| `user_id` | 用户 ID |
| `user` | 用户信息对象 |
| `docs_count` | 文档数 |
| `public_docs_count` | 公开文档数 |
| `words_count` | 字数 |

### 获取知识库统计

```http
GET /api/v2/groups/{login}/statistics/books
```

### 获取文档统计

```http
GET /api/v2/groups/{login}/statistics/docs
```

## API 限制

| 限制项 | 上限 |
|--------|------|
| 单知识库文档数 | 5000 |
| API QPS | 100/s |
| API 每小时请求 | 5000/h |
| 文档标题字数 | 200 |

## 错误处理

### 错误响应格式

```json
{
  "status": 401,
  "message": "Unauthorized",
  "errors": [
    {
      "code": "invalid_token",
      "message": "Token is invalid or expired"
    }
  ]
}
```

### 常见错误码

| 错误码 | 说明 | 处理方式 |
|--------|------|----------|
| 400 | 请求参数错误 | 输出错误信息，检查参数格式 |
| 401 | Token 无效或已过期 | 引导用户到语雀设置重新生成 Token 并更新配置文件 |
| 403 | 权限不足 | 说明缺少的权限，检查 Token 权限范围 |
| 404 | 资源不存在 | 文档/知识库/小记可能已被删除或 ID 错误 |
| 410 | 资源已删除 | 资源已被删除或 API 端点已废弃 |
| 429 | 请求过于频繁 | 检查 `X-RateLimit-Remaining`：`=0` 触及 5000/h → 立即暂停，保存进度，通知用户整点后重新触发；`>0` 触及 100/s → 等待 1s 重试（最多 3 次） |
| 500 | 语雀服务器内部错误 | 稍后重试 |
| 502/503/504 | 网关错误 | 稍后重试 |

## 速率限制

### 响应头

每次 API 调用都会返回以下响应头：

| 响应头 | 说明 |
|--------|------|
| `X-RateLimit-Limit` | 总次数限制（每小时） |
| `X-RateLimit-Remaining` | 剩余可用次数 |

**使用方式**：每次请求后检查 `X-RateLimit-Remaining`，合理控制请求节奏。

### 速率控制策略

#### 批量操作（索引构建）

1. **每批处理后检查**：读取 `X-RateLimit-Remaining`
2. **剩余 < 200 时暂停**：主动暂停当前批次，更新状态文件保存进度，汇报用户「⏳ 剩余配额不足，已保存进度。整点后回复「继续」从断点续传。」
3. **批次间延迟**：每批处理完成后等待 2-3 秒

#### 429 错误处理

检查 `X-RateLimit-Remaining` 响应头区分限制类型：

| 情况 | 限制 | 处理 |
|------|------|------|
| `remaining = 0` | 5000/h | 立即暂停，保存进度，通知用户整点后重新触发 |
| `remaining > 0` | 100/s | 等待 1s 重试（最多 3 次） |

#### 并发控制

- 批量请求时使用 `ThreadPoolExecutor`，并发数 ≤ 5

### 请求预判

开始大批量操作前，估算请求次数：

| 操作 | 单篇文档请求次数 |
|------|------------------|
| 获取内容 | 1 GET |
| 搜索索引（每个关键词）| 1 GET |
| 更新索引（每个关键词）| 1 PUT |

**示例**：100 篇文档，每篇 5 关键词 ≈ 1100 次请求

如果预估请求次数 > 4000，提示用户可能超限，建议分多次执行。

### 状态文件记录

索引构建时，在状态文件中记录：

```json
{
  "rate_limit": {
    "limit": 5000,
    "remaining": 1234,
    "last_checked": "2026-04-26T21:00:00+08:00"
  }
}
```

便于中断后恢复时了解剩余配额。

---

# 故障排查

## Token 问题

### 401 Unauthorized
1. 检查配置文件路径是否正确（从 MEMORY.md 读取）
2. 检查 token 是否正确填写，无多余空格或引号
3. 登录语雀 → 设置 → Token → 确认 token 未过期
4. 确认 token 有**读取**和**写入**权限
5. 重新生成 token → 更新配置文件 → 重试

### 403 Forbidden
1. 确认 token 权限：`repo:read`、`repo:write`、`doc:read`、`doc:write`
2. 确认资源归属：知识库/文档是否属于该用户/团队
3. 团队资源：确认账号有团队访问权限

## 搜索问题

### 搜索无结果
1. 检查索引是否构建（语雀中搜索 `[索引]` 查看）
2. 检查 `scope` 格式是否正确（`group/book_slug`）
3. 尝试用文档标题中的关键词

### 搜索结果不准确
1. 索引可能过时 → 尝试「更新《XXX》的索引」
2. 调整 `candidates_limit` 和 `top_k` 配置

## 索引构建问题

### 构建中途报错
1. 检查 `X-RateLimit-Remaining` → 触及限制则等待
2. 检查状态文件 `status` / `last_indexed_doc_id` / `failed_docs`
3. 重新执行「构建索引」会从断点继续

### 构建卡住
1. `status=in_progress` → 正常，等待
2. `status=awaiting_confirmation` → 有待确认的无意义文档，回复「全部跳过」或「全部索引」

### 索引重复或混乱
手动删除索引库中所有 `[索引]` 开头的文档，重新构建。

## 小记问题

| 症状 | 原因 | 解决 |
|------|------|------|
| 更新返回 400 | `source`/`html`/`abstract` 缺字段 | 先 GET 原小记，再 PUT 更新 |
| 获取内容为空 | 用了列表 API（`content` 只有 `abstract`） | 用详情 API：`GET /notes/{id}` |

## 其他

### 配置文件找不到
1. 检查 MEMORY.md 中记录的路径
2. 不存在则复制 `config.example.json` 到指定位置并填写

### 快速诊断清单
1. 配置文件路径正确？
2. Token 有效且有正确权限？
3. 索引是否构建？
4. scope/namespace 格式正确？
5. 速率限制是否用完？
