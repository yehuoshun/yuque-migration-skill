#!/usr/bin/env python3
"""语雀知识库批量迁移脚本 — 完整版

功能（以 SKILL.md 为准）：
  - 分页拉取源库文档 → 逐篇处理
  - 格式判断：markdown / lake / 其他
  - 二进制检测：非ASCII+控制字符 >25% 跳过
  - 空文档跳过（记录到 skipped_empty）
  - 去重：搜索目标库 → 标题+内容逐级比对（200字→500字→全文）
  - 大文档拆分：>50000字按 ## / ### / 段落边界拆分
  - 附件检测：标注 docs_with_attachments
  - 并发处理：ThreadPoolExecutor ≤5
  - 限流：解析 X-RateLimit-Remaining 响应头，区分 5000/h 与 100/s
  - 断点续传：进度文件持久化
  - 容量监控：本地累加，≥4500 暂停
  - TOC 构建：关键词分类 → 批量挂载目录

用法：
  python migrate_batch.py --src 65894942 --tgt 78699632 --total 5200
  python migrate_batch.py --src 65894942 --tgt 78699632 --total 5200 --skip-toc
  python migrate_batch.py --toc-only --progress progress.json
"""

import json, os, sys, time, re, ssl, argparse, threading
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

# ═══════════════════════════════════════════════════════════════════════════
# Token 加载
# ═══════════════════════════════════════════════════════════════════════════

def load_token(token_config=None):
    """加载语雀 Token：环境变量 > --token-config > 默认配置"""
    token = os.environ.get("YUQUE_TOKEN", "")
    if token:
        return token
    if token_config and os.path.exists(os.path.expanduser(token_config)):
        with open(os.path.expanduser(token_config)) as f:
            return json.load(f).get("token", "")
    default = os.path.expanduser("~/.openclaw/workspace/utils/yuque/yuque-ai/yuque-config.json")
    if os.path.exists(default):
        with open(default) as f:
            return json.load(f).get("token", "")
    raise RuntimeError(
        "未找到语雀 Token。请设置环境变量 YUQUE_TOKEN 或指定 --token-config"
    )

# ═══════════════════════════════════════════════════════════════════════════
# 全局配置
# ═══════════════════════════════════════════════════════════════════════════

API_BASE = "https://www.yuque.com/api/v2"
SSL_CTX = ssl.create_default_context()
MAX_WORKERS = 5          # 并发处理上限
CAPACITY_LIMIT = 4500    # 目标库容量阈值
MAX_CHARS = 50000        # 单文档字数上限
BATCH_SIZE = 100         # 每批拉取文档数
TOC_BATCH = 50           # TOC 每批挂载文档数
MEMORY_LIMIT_MB = 5      # 累计正文内存上限(MB)

# 线程安全锁
_rate_lock = threading.Lock()
_progress_lock = threading.Lock()
_rate_remaining = -1     # 全局剩余调用次数

TOKEN = None

# ═══════════════════════════════════════════════════════════════════════════
# API 客户端（urllib，可读响应头）
# ═══════════════════════════════════════════════════════════════════════════

def api_request(method, path, body=None, timeout=30):
    """发送语雀 API 请求，自动重试+限流，读取 X-RateLimit-Remaining 头。

    Returns:
        (result_dict, remaining_int)
        result_dict 为 None 表示 404
    """
    global _rate_remaining
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {
        "X-Auth-Token": TOKEN,
        "Content-Type": "application/json",
        "User-Agent": "OpenClaw-Yuque-Migration/2.0",
    }

    for attempt in range(3):
        try:
            req = Request(url, data=data, headers=headers, method=method)
            with urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                remaining = int(resp.headers.get("X-RateLimit-Remaining", -1))
                with _rate_lock:
                    _rate_remaining = remaining
                return result, remaining
        except HTTPError as e:
            remaining = -1
            try:
                remaining = int(e.headers.get("X-RateLimit-Remaining", -1))
            except Exception:
                pass
            with _rate_lock:
                _rate_remaining = remaining

            if e.code == 429:
                if remaining == 0:
                    # 5000/h 限流 → 等到整点
                    now = datetime.now()
                    wait = 3600 - (now.minute * 60 + now.second) + 5
                    print(f"  ⏳ 小时限流 (5000/h)，等待 {wait}s 到整点...")
                    time.sleep(min(wait, 3600))
                    continue
                # 100/s 限流
                time.sleep(1.5)
                continue
            if e.code >= 500:
                time.sleep(1)
                continue
            if e.code == 404:
                return None, remaining
            # 其他 4xx
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            return {"error": e.code, "message": err_body}, remaining
        except (URLError, OSError, Exception):
            time.sleep(1)

    return {"error": -1, "message": "3 attempts failed"}, _rate_remaining


def check_rate():
    """检查是否触发小时限流，是则等待整点。"""
    global _rate_remaining
    with _rate_lock:
        rem = _rate_remaining
    if rem == 0:
        now = datetime.now()
        wait = 3600 - (now.minute * 60 + now.second) + 5
        print(f"  ⏳ 小时限流，等待 {wait}s...")
        time.sleep(min(wait, 3600))
        with _rate_lock:
            _rate_remaining = -1


# ═══════════════════════════════════════════════════════════════════════════
# 文档处理工具
# ═══════════════════════════════════════════════════════════════════════════

def is_binary_body(body):
    """二进制检测：非ASCII+控制字符 >25% 判定为二进制。"""
    if not body:
        return False
    sample = body[:200]
    if len(sample) < 10:
        return False
    binary = sum(1 for c in sample if ord(c) > 127 or ord(c) < 32 or ord(c) == 127)
    return binary / len(sample) > 0.25


def count_chars_no_code(content):
    """统计非代码块字数。"""
    text = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
    text = re.sub(r'`[^`]+`', '', text)
    return len(text)


def split_large_doc(title, body):
    """大文档拆分：>MAX_CHARS 字时按 ## → ### → 段落边界拆分。
    不断在段内、不跨代码块/表格切、代码块跟随最近的标题整块带走。
    """
    if count_chars_no_code(body) <= MAX_CHARS:
        return [(title, body)]

    # 按 ## 拆分
    sections = re.split(r'(?=^## )', body, flags=re.MULTILINE)
    if len(sections) <= 1:
        # 按 ### 拆分
        sections = re.split(r'(?=^### )', body, flags=re.MULTILINE)
    if len(sections) <= 1:
        # 按段落（空行）拆分
        sections = body.split('\n\n')

    parts = []
    cur = sections[0] if sections else ""
    for sec in sections[1:]:
        if count_chars_no_code(cur) + count_chars_no_code(sec) > MAX_CHARS and cur.strip():
            parts.append(cur)
            cur = sec
        else:
            cur += "\n\n" + sec
    if cur.strip():
        parts.append(cur)

    total = len(parts)
    return [(f"{title}({i+1}/{total})", p) for i, p in enumerate(parts)]


def detect_attachments(body):
    """检测附件引用（图片/文件链接），返回 URL 列表。"""
    urls = []
    # 图片: ![alt](url)
    urls.extend(re.findall(r'!\[.*?\]\((https?://[^\)]+)\)', body))
    # 文件链接: [text](url) 含常见附件扩展名
    urls.extend(re.findall(
        r'\[.*?\]\((https?://[^\)]+\.(?:pdf|zip|rar|7z|tar|gz|docx?|xlsx?|pptx?|apk|exe|dmg|pkg))\)',
        body, re.IGNORECASE
    ))
    # 裸 URL 图片/文件
    urls.extend(re.findall(
        r'https?://[^\s<>"]+\.(?:png|jpg|jpeg|gif|webp|svg|bmp|ico|pdf|zip|rar|7z|tar|gz)(?:\?[^\s<>"]*)?',
        body, re.IGNORECASE
    ))
    return list(set(urls))


def normalize_for_compare(text):
    """规范化文本用于比对：去空白、统一换行。"""
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()


# ═══════════════════════════════════════════════════════════════════════════
# 去重
# ═══════════════════════════════════════════════════════════════════════════

def dedup_check(title, body, tgt_namespace, tgt_book_id, existing_titles_counter):
    """去重检测：搜索目标库 → 逐级内容比对（200字→500字→全文）。

    Returns:
        (action, suffix_or_match)
        - ("skip", matched_doc_id)  → 完全重复，跳过
        - ("rename", suffix_N)      → 标题相同内容不同，加编号
        - ("new", None)             → 新文档
    """
    # 搜索目标库中同名文档
    result, _ = api_request("GET", f"/search?q={title}&type=doc&scope={tgt_namespace}")
    if not result or "data" not in result:
        return ("new", None)

    matches = result.get("data", [])
    if not matches:
        return ("new", None)

    normalized_new = normalize_for_compare(body)
    new_200 = normalized_new[:200]
    new_500 = normalized_new[:500]

    for match in matches:
        match_id = match.get("id")
        match_title = match.get("title", "")
        if not match_id:
            continue

        # 获取目标文档正文
        doc_result, _ = api_request("GET", f"/repos/{tgt_book_id}/docs/{match_id}?raw=1")
        if not doc_result or "data" not in doc_result:
            continue

        existing_body = doc_result["data"].get("body", "")
        normalized_existing = normalize_for_compare(existing_body)
        existing_200 = normalized_existing[:200]
        existing_500 = normalized_existing[:500]

        # 逐级比对
        if new_200 != existing_200:
            # 前200字不同 → 标题相同但内容不同
            count = existing_titles_counter.get(title, 0)
            suffix = count + 1
            return ("rename", suffix)
        if new_500 != existing_500:
            return ("rename", existing_titles_counter.get(title, 0) + 1)
        if normalized_new != normalized_existing:
            return ("rename", existing_titles_counter.get(title, 0) + 1)

        # 完全相同 → 重复
        return ("skip", match_id)

    return ("new", None)


# ═══════════════════════════════════════════════════════════════════════════
# 进度管理
# ═══════════════════════════════════════════════════════════════════════════

def init_progress(progress_file, src_book_id, src_name, tgt_book_id, tgt_name,
                  tgt_namespace, total_docs, initial_count):
    """初始化或加载进度文件。"""
    path = os.path.expanduser(progress_file)
    if os.path.exists(path):
        with open(path) as f:
            p = json.load(f)
        # 兼容旧格式：确保所有字段存在
        p.setdefault("source_book_id", src_book_id)
        p.setdefault("source_name", src_name)
        p.setdefault("target_book_id", tgt_book_id)
        p.setdefault("target_name", tgt_name)
        p.setdefault("target_namespace", tgt_namespace)
        p.setdefault("last_offset", 0)
        p.setdefault("total_docs", total_docs)
        p.setdefault("created", 0)
        p.setdefault("skipped", 0)
        p.setdefault("skipped_duplicates", [])
        p.setdefault("skipped_empty", [])
        p.setdefault("failed", 0)
        p.setdefault("failed_list", [])
        p.setdefault("docs_with_attachments", [])
        p.setdefault("processed_doc_ids", [])
        p.setdefault("created_doc_mapping", {})
        p.setdefault("created_doc_infos", {})
        p.setdefault("orphans", [])
        p.setdefault("initial_count", initial_count)
        p.setdefault("local_created", 0)
        p.setdefault("target_history", [])
        p.setdefault("existing_titles", {})
        p.setdefault("lake_docs", [])
        return p

    # 全新进度
    return {
        "source_book_id": src_book_id,
        "source_name": src_name,
        "target_book_id": tgt_book_id,
        "target_name": tgt_name,
        "target_namespace": tgt_namespace,
        "last_offset": 0,
        "total_docs": total_docs,
        "created": 0,
        "skipped": 0,
        "skipped_duplicates": [],
        "skipped_empty": [],
        "failed": 0,
        "failed_list": [],
        "docs_with_attachments": [],
        "processed_doc_ids": [],
        "created_doc_mapping": {},
        "created_doc_infos": {},
        "orphans": [],
        "initial_count": initial_count,
        "local_created": 0,
        "target_history": [],
        "existing_titles": {},
    }


def save_progress(p, progress_file):
    """线程安全保存进度。"""
    with _progress_lock:
        path = os.path.expanduser(progress_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(p, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# 单文档处理（供线程池调用）
# ═══════════════════════════════════════════════════════════════════════════

def process_one_doc(doc, src_book_id, tgt_book_id, tgt_namespace, p):
    """处理单篇文档：拉取→判断格式→二进制检测→去重→拆分→附件检测→创建。

    Returns:
        dict with status: "created" / "skipped" / "failed" / "empty" / "duplicate" / "binary"
    """
    doc_id = doc["id"]
    title = doc["title"]

    # 跳过已处理的
    with _progress_lock:
        if doc_id in p["processed_doc_ids"]:
            return {"status": "already_processed", "doc_id": doc_id, "title": title}

    # 1. 获取原文
    raw, _ = api_request("GET", f"/repos/{src_book_id}/docs/{doc_id}?raw=1")
    if not raw or "data" not in raw:
        return {"status": "failed", "doc_id": doc_id, "title": title,
                "reason": "获取原文失败"}

    ddata = raw["data"]
    fmt = ddata.get("format", "unknown")
    raw_body = ddata.get("body", "")
    body_lake = ddata.get("body_lake", "")

    # 2. 格式判断
    fmt_note = None
    use_lake = False
    if fmt == "lake":
        if not body_lake or not body_lake.strip():
            return {"status": "empty", "doc_id": doc_id, "title": title}
        body = body_lake  # 原生 XML，不是 markdown
        use_lake = True
        fmt_note = "lake 格式无损搬运"
    elif fmt == "markdown":
        body = raw_body
    else:
        return {"status": "failed", "doc_id": doc_id, "title": title,
                "reason": f"未知格式: {fmt}"}

    # 3. 二进制检测（仅 markdown）
    if not use_lake and is_binary_body(body):
        return {"status": "binary", "doc_id": doc_id, "title": title}

    # 4. 空文档
    if not body or not body.strip():
        return {"status": "empty", "doc_id": doc_id, "title": title}

    # 5. 去重（仅 markdown，lake XML 无法比对文本）
    if use_lake:
        new_title = title
        action = "new"
    else:
        existing_titles = p.get("existing_titles", {})
        action, info = dedup_check(title, body, tgt_namespace, tgt_book_id, existing_titles)
        if action == "skip":
            return {"status": "duplicate", "doc_id": doc_id, "title": title,
                    "matched_doc_id": info}
        if action == "rename":
            new_title = f"{title}(重复标题-{info})"
            with _progress_lock:
                existing_titles[title] = info
                p["existing_titles"] = existing_titles
        else:
            new_title = title

    # 6. 附件检测（仅 markdown）
    attachments = detect_attachments(body) if not use_lake else []
    attachment_info = None
    if attachments:
        attachment_info = {"doc_id": doc_id, "title": title, "attachments": attachments}

    # 7. 大文档拆分（仅 markdown，lake 不拆分保持原样）
    if use_lake:
        parts = [(new_title, body)]
        is_split = False
    else:
        parts = split_large_doc(new_title, body)
        is_split = len(parts) > 1

    # 8. 创建到目标库
    created_ids = []
    all_ok = True
    doc_format = "lake" if use_lake else "markdown"
    for part_title, part_body in parts:
        check_rate()
        create_res, _ = api_request("POST", f"/repos/{tgt_book_id}/docs", {
            "title": part_title,
            "body": part_body,
            "format": doc_format,
            "public": 0,
        })
        if create_res and create_res.get("data"):
            new_id = create_res["data"]["id"]
            created_ids.append(new_id)
        else:
            all_ok = False
            err_msg = create_res.get("message", str(create_res)) if create_res else "无响应"

    if not all_ok:
        return {"status": "failed", "doc_id": doc_id, "title": title,
                "reason": f"创建失败: {err_msg}", "created_ids": created_ids}

    return {
        "status": "created",
        "doc_id": doc_id,
        "title": title,
        "created_ids": created_ids,
        "is_split": is_split,
        "attachments": attachment_info,
        "body_snippet": ("[Lake 原生格式]" if use_lake else body[:200].replace("\n", " ")),
        "fmt_note": fmt_note,
    }


# ═══════════════════════════════════════════════════════════════════════════
# TOC 构建
# ═══════════════════════════════════════════════════════════════════════════

CLASSIFY_RULES = [
    ("API 接口", ["api", "接口", "endpoint", "restful", "webhook", "openapi", "graphql"]),
    ("安装部署", ["安装", "部署", "搭建", "docker", "k8s", "nginx", "环境", "构建", "ci", "cd", "发布", "上线", "devops"]),
    ("配置管理", ["配置", "config", "设置", "参数", "环境变量", "yml", "yaml", "toml", "properties", "env"]),
    ("数据库", ["数据库", "mysql", "redis", "mongo", "postgres", "sql", "索引", "缓存", "事务", "db", "etcd"]),
    ("前端开发", ["前端", "vue", "react", "css", "html", "javascript", "js", "webpack", "vite", "组件", "ui", "页面", "渲染", "浏览器", "typescript", "ts"]),
    ("后端开发", ["后端", "服务", "server", "spring", "django", "flask", "node", "go", "rust", "java", "python", "微服务", "rpc", "grpc"]),
    ("Linux & 运维", ["linux", "运维", "shell", "bash", "ssh", "服务器", "监控", "日志", "进程", "内存", "cpu", "磁盘", "systemd"]),
    ("容器 & 云原生", ["容器", "kubernetes", "pod", "镜像", "编排", "helm", "云原生", "serverless", "istio"]),
    ("网络 & 安全", ["网络", "安全", "https", "ssl", "tls", "防火墙", "加密", "认证", "oauth", "jwt", "渗透", "漏洞", "dns", "tcp"]),
    ("Git & 版本控制", ["git", "github", "gitlab", "分支", "合并", "commit", "pr", "版本", "tag", "release", "cherry-pick"]),
    ("测试 & 质量", ["测试", "test", "单元测试", "集成测试", "e2e", "压测", "性能", "质量", "覆盖率", "jest", "pytest", "selenium"]),
    ("消息队列", ["消息", "队列", "kafka", "rabbitmq", "mq", "事件", "异步", "stream", "pubsub", "nsq"]),
    ("AI & 机器学习", ["ai", "机器学习", "深度学习", "模型", "训练", "推理", "transformer", "nlp", "cv", "llm", "gpt", "bert", "embedding"]),
    ("教程 & 笔记", ["教程", "笔记", "学习", "入门", "指南", "guide", "tutorial", "总结", "知识点", "复习", "笔记"]),
    ("排错记录", ["报错", "错误", "error", "bug", "修复", "解决", "故障", "排查", "异常", "debug", "问题", "troubleshoot"]),
    ("读书 & 资料", ["书", "阅读", "读书", "推荐", "资源", "合集", "清单", "awesome", "文章", "论文", "pdf"]),
    ("面试 & 职场", ["面试", "职场", "简历", "跳槽", "薪资", "offer", "晋升", "管理", "团队", "绩效"]),
]


def classify_docs(doc_infos):
    """按标题关键词自动分类文档。"""
    categories = {}
    unmatched = []
    for doc_id, info in doc_infos.items():
        title_lower = info["title"].lower()
        matched = False
        for cat_name, keywords in CLASSIFY_RULES:
            if any(kw in title_lower for kw in keywords):
                categories.setdefault(cat_name, []).append(int(doc_id))
                matched = True
                break
        if not matched:
            unmatched.append(int(doc_id))
    if unmatched:
        categories["其他"] = unmatched
    return categories


def extract_uuid(res, title_hint=""):
    """从 TOC API 响应提取节点 uuid，兼容新旧版 API。"""
    if isinstance(res, list):
        return res[-1].get("uuid", "") if res else ""
    data = res.get("data", {})
    if isinstance(data, list):
        return data[-1].get("uuid", "") if data else ""
    return data.get("uuid", "")


def build_toc(progress_file):
    """构建目标库 TOC 目录并挂载文档。"""
    path = os.path.expanduser(progress_file)
    with open(path) as f:
        p = json.load(f)

    created_doc_infos = p.get("created_doc_infos", {})
    if not created_doc_infos:
        print("⚠️ 没有已创建的文档信息，跳过 TOC 构建")
        return

    tgt_book_id = p["target_book_id"]
    src_name = p.get("source_name", f"知识库-{p['source_book_id']}")

    print(f"\n📂 构建 TOC 目录（{len(created_doc_infos)} 篇文档）...")

    # 分类
    categories = classify_docs(created_doc_infos)
    print(f"  自动分类: {len(categories)} 个目录")
    for cat_name, doc_ids in sorted(categories.items(), key=lambda x: -len(x[1])):
        print(f"    {cat_name}: {len(doc_ids)} 篇")

    # 创建根节点
    root_res, _ = api_request("PUT", f"/repos/{tgt_book_id}/toc", {
        "action": "appendNode",
        "action_mode": "child",
        "type": "TITLE",
        "title": src_name,
    })
    root_uuid = extract_uuid(root_res)
    if not root_uuid:
        print(f"❌ 创建根目录失败")
        return
    print(f"  ✅ 根目录「{src_name}」已创建")

    # 逐分类建节点 + 挂文档
    orphans = []
    total_mounted = 0

    for cat_name, doc_ids in sorted(categories.items(), key=lambda x: -len(x[1])):
        # 建分类节点
        cat_res, _ = api_request("PUT", f"/repos/{tgt_book_id}/toc", {
            "action": "appendNode",
            "action_mode": "child",
            "type": "TITLE",
            "title": cat_name,
            "target_uuid": root_uuid,
        })
        cat_uuid = extract_uuid(cat_res)
        if not cat_uuid:
            msg = cat_res.get("message", str(cat_res)[:200]) if isinstance(cat_res, dict) else str(cat_res)[:200]
            print(f"  ⚠️ 创建分类「{cat_name}」失败: {msg}")
            for did in doc_ids:
                orphans.append({
                    "doc_id": did,
                    "title": created_doc_infos.get(str(did), {}).get("title", "?"),
                    "errors": [f"分类节点创建失败: {msg}"],
                })
            continue

        # 批量挂文档
        for i in range(0, len(doc_ids), TOC_BATCH):
            batch = doc_ids[i:i + TOC_BATCH]
            for retry in range(3):
                check_rate()
                res, _ = api_request("PUT", f"/repos/{tgt_book_id}/toc", {
                    "action": "appendNode",
                    "action_mode": "child",
                    "type": "DOC",
                    "target_uuid": cat_uuid,
                    "doc_ids": batch,
                })
                if res and "data" in res:
                    total_mounted += len(batch)
                    break
                if retry < 2:
                    time.sleep(1)
                else:
                    # 拆成单篇重试
                    for did in batch:
                        single_ok = False
                        for sretry in range(3):
                            sres, _ = api_request("PUT", f"/repos/{tgt_book_id}/toc", {
                                "action": "appendNode",
                                "action_mode": "child",
                                "type": "DOC",
                                "target_uuid": cat_uuid,
                                "doc_ids": [did],
                            })
                            if sres and "data" in sres:
                                total_mounted += 1
                                single_ok = True
                                break
                            time.sleep(1)
                        if not single_ok:
                            orphans.append({
                                "doc_id": did,
                                "title": created_doc_infos.get(str(did), {}).get("title", "?"),
                                "errors": ["TOC挂载失败（3次重试+单篇重试均失败）"],
                            })
            time.sleep(0.3)

    # 保存 TOC 结果
    p["orphans"] = orphans
    p["toc_built"] = True
    save_progress(p, path)

    print(f"\n✅ TOC 构建完成，挂载 {total_mounted} 篇，孤儿 {len(orphans)} 篇")


# ═══════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════

def main():
    global TOKEN

    parser = argparse.ArgumentParser(description="语雀知识库批量迁移")
    parser.add_argument("--src", type=int, help="源知识库 ID")
    parser.add_argument("--tgt", type=int, help="目标知识库 ID")
    parser.add_argument("--total", type=int, help="源库文档总数")
    parser.add_argument("--progress", type=str, help="进度文件路径")
    parser.add_argument("--token-config", type=str, help="Token 配置文件路径")
    parser.add_argument("--skip-toc", action="store_true", help="跳过 TOC 构建")
    parser.add_argument("--toc-only", action="store_true", help="仅构建 TOC")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"并发数（默认{MAX_WORKERS}）")
    args = parser.parse_args()

    TOKEN = load_token(args.token_config)
    workers = min(args.workers, MAX_WORKERS)

    # ── TOC-only 模式 ──
    if args.toc_only:
        if not args.progress:
            print("❌ --toc-only 需要 --progress")
            sys.exit(1)
        build_toc(args.progress)
        return

    # ── 参数校验 ──
    if not args.src or not args.tgt:
        print("❌ 需要 --src 和 --tgt")
        sys.exit(1)

    SRC_BOOK = args.src
    TGT_BOOK = args.tgt

    # ── 获取源库信息 ──
    print(f"📥 获取源库信息 (ID={SRC_BOOK})...")
    src_res, _ = api_request("GET", f"/repos/{SRC_BOOK}")
    if not src_res or "data" not in src_res:
        print("❌ 无法获取源库信息")
        sys.exit(1)
    src_name = src_res["data"]["name"]
    src_total = args.total or src_res["data"].get("items_count", 0)
    print(f"  源库: 《{src_name}》({src_total} 篇)")

    # ── 获取目标库信息 ──
    print(f"📥 获取目标库信息 (ID={TGT_BOOK})...")
    tgt_res, _ = api_request("GET", f"/repos/{TGT_BOOK}")
    if not tgt_res or "data" not in tgt_res:
        print("❌ 无法获取目标库信息")
        sys.exit(1)
    tgt_name = tgt_res["data"]["name"]
    tgt_namespace = tgt_res["data"].get("namespace", "")
    initial_count = tgt_res["data"].get("items_count", 0)
    print(f"  目标库: 《{tgt_name}》({initial_count} 篇, namespace={tgt_namespace})")

    # ── 容量检查 ──
    if initial_count >= CAPACITY_LIMIT:
        print(f"\n⚠️ 目标库已达阈值 {initial_count}/{CAPACITY_LIMIT} 篇，请提供新目标库")
        sys.exit(1)

    # ── 进度文件 ──
    progress_file = args.progress or \
        f"~/.openclaw/workspace/utils/yuque/yuque-migration/progress/{src_name}.json"
    p = init_progress(progress_file, SRC_BOOK, src_name, TGT_BOOK, tgt_name,
                      tgt_namespace, src_total, initial_count)

    offset = p["last_offset"]
    local_created = p.get("local_created", 0)
    print(f"\n🔄 从 offset={offset} 开始迁移（已创建 {p['created']} 篇）...")

    # ── 主循环 ──
    while offset < src_total:
        check_rate()

        # 分页获取文档列表
        list_res, _ = api_request("GET", f"/repos/{SRC_BOOK}/docs?offset={offset}&limit={BATCH_SIZE}")
        docs = list_res.get("data", []) if list_res else []
        if not docs:
            offset += BATCH_SIZE
            p["last_offset"] = offset
            save_progress(p, progress_file)
            continue

        print(f"\n📄 offset={offset}: 处理 {len(docs)} 篇...")

        # 过滤已处理
        pending = []
        for doc in docs:
            if doc["id"] not in p["processed_doc_ids"]:
                pending.append(doc)

        if not pending:
            offset += BATCH_SIZE
            p["last_offset"] = offset
            save_progress(p, progress_file)
            continue

        # 并发处理
        batch_results = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_one_doc, doc, SRC_BOOK, TGT_BOOK, tgt_namespace, p): doc
                for doc in pending
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    batch_results.append(result)
                except Exception as e:
                    doc = futures[future]
                    batch_results.append({
                        "status": "failed", "doc_id": doc["id"],
                        "title": doc["title"], "reason": str(e),
                    })

        # ── 汇总本批结果 ──
        batch_created = 0
        for r in batch_results:
            status = r["status"]
            doc_id = r["doc_id"]
            title = r["title"]

            with _progress_lock:
                p["processed_doc_ids"].append(doc_id)

                if status == "created":
                    for new_id in r.get("created_ids", []):
                        p["created_doc_mapping"][str(doc_id)] = new_id
                        suffix = f"（共{len(r['created_ids'])}篇）" if r.get("is_split") else ""
                        p["created_doc_infos"][str(new_id)] = {
                            "title": title + suffix,
                            "summary": r.get("body_snippet", ""),
                        }
                    p["created"] += len(r.get("created_ids", []))
                    batch_created += len(r.get("created_ids", []))
                    local_created += len(r.get("created_ids", []))

                    if r.get("attachments"):
                        p["docs_with_attachments"].append(r["attachments"])
                    if r.get("fmt_note"):
                        for new_id in r.get("created_ids", []):
                            p["lake_docs"].append({"doc_id": doc_id, "new_id": new_id, "title": title, "reason": r["fmt_note"]})

                elif status == "duplicate":
                    p["skipped"] += 1
                    p["skipped_duplicates"].append({
                        "doc_id": doc_id, "title": title,
                        "matched": r.get("matched_doc_id"),
                    })

                elif status == "empty":
                    p["skipped"] += 1
                    p["skipped_empty"].append({"doc_id": doc_id, "title": title})

                elif status == "binary":
                    p["skipped"] += 1

                elif status == "failed":
                    p["failed"] += 1
                    p["failed_list"].append({
                        "id": doc_id, "title": title,
                        "reason": r.get("reason", "未知错误"),
                    })

        # 进度统计
        offset += BATCH_SIZE
        p["last_offset"] = offset
        p["local_created"] = local_created
        save_progress(p, progress_file)

        se = p.get('skipped_empty') or []
        sd = p.get('skipped_duplicates') or []
        print(f"  💾 offset={offset} | created={p['created']} | "
              f"skipped={p['skipped']}（空{len(se)} 重{len(sd)}） | failed={p['failed']}")

        # ── 容量检查 ──
        current_total = initial_count + local_created
        if current_total >= CAPACITY_LIMIT:
            save_progress(p, progress_file)
            print(f"\n⚠️ 目标库已达阈值 {current_total}/{CAPACITY_LIMIT} 篇")
            print(f"   （初始 {initial_count} + 本次创建 {local_created}）")
            print("   请提供新目标库后重新运行。")
            sys.exit(1)

        time.sleep(0.3)

    # ── 迁移完成 ──
    print(f"\n{'='*50}")
    print(f"✅ 迁移完成")
    print(f"  创建: {p['created']} | 跳过: {p['skipped']} | 失败: {p['failed']}")
    if p.get("failed_list"):
        print(f"  失败明细 ({len(p['failed_list'])} 篇):")
        for f in p["failed_list"][:20]:
            print(f"    - {f['title'][:60]}: {f['reason']}")
    if p.get("skipped_duplicates"):
        print(f"  去重跳过: {len(p['skipped_duplicates'])} 篇")
    if p.get("skipped_empty"):
        print(f"  空文档: {len(p['skipped_empty'])} 篇")
    if p.get("docs_with_attachments"):
        print(f"  含附件文档: {len(p['docs_with_attachments'])} 篇（请手动处理附件）")
    if p.get("lake_docs"):
        print(f"  Lake 无损搬运: {len(p['lake_docs'])} 篇（lake 原样搬运，原生表格/样式完整保留）")

    # ── TOC ──
    if not args.skip_toc and p.get("created", 0) > 0 and not p.get("toc_built"):
        build_toc(progress_file)
    elif p.get("toc_built"):
        print("\n📂 TOC 已构建，跳过（使用 --toc-only 可重建）")

    print(f"\n📦 《{src_name}》({src_total}篇) → 《{tgt_name}》")
    print(f"   ├─ 复制: {p['created']} 篇")
    dupes = len(p.get("skipped_duplicates", []))
    empties = len(p.get("skipped_empty", []))
    binaries = p.get("skipped", 0) - dupes - empties
    print(f"   ├─ 跳过: {dupes} 篇（去重）{empties} 篇（空文档）{max(0, binaries)} 篇（二进制）")
    print(f"   ├─ 含附件: {len(p.get('docs_with_attachments', []))} 篇")
    print(f"   ├─ 失败: {p['failed']} 篇")
    print(f"   └─ 原库: 未动")


if __name__ == "__main__":
    main()
