#!/usr/bin/env python3
"""语雀知识库迁移脚本 v4
迁移即分类：LLM 清洗+分类合并为一次调用，逐篇处理立即挂目录，
多分类自动复制文档，不再攒数据到后置 TOC 阶段。
"""

import json, time, re, os, gc, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta

BASE = "https://www.yuque.com/api/v2"
CONFIG_FILE = os.path.expanduser("~/.openclaw/workspace/utils/yuque/yuque-ai/yuque-config.json")

# 运行时配置（从进度文件读取）
PROGRESS_FILE = None
SOURCE_ID = None
TARGET_ID = None
TARGET_NS = None

BATCH_SIZE = 100
MAX_WORKERS_INIT = 5
MAX_WORKERS = 5

# ── 内存感知 (K8s OOM 防杀) ──
def _get_pod_mem_limit():
    for p in ['/sys/fs/cgroup/memory/memory.limit_in_bytes', '/sys/fs/cgroup/memory.max']:
        try:
            with open(p) as f:
                v = int(f.read().strip())
                if v < 10 * 1024**4: return v
        except: pass
    return None

def _get_rss_mb():
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'): return int(line.split()[1]) / 1024
    except: pass
    return None

POD_LIMIT_B = _get_pod_mem_limit()
SAFE_LIMIT_MB = (POD_LIMIT_B / 1024 / 1024 * 0.6) if POD_LIMIT_B else 256

def _check_memory():
    global MAX_WORKERS
    rss = _get_rss_mb()
    if rss is None: return True
    ratio = rss / SAFE_LIMIT_MB
    if ratio > 0.85:
        MAX_WORKERS = 1
        print(f"  ⚠️ 内存高压 {rss:.0f}/{SAFE_LIMIT_MB:.0f}MB ({ratio:.0%}), 降为串行", flush=True)
        gc.collect()
        return False
    elif ratio > 0.60:
        new_w = max(1, MAX_WORKERS_INIT // 2)
        if MAX_WORKERS != new_w:
            MAX_WORKERS = new_w
            print(f"  ⚡ 内存中压 {rss:.0f}/{SAFE_LIMIT_MB:.0f}MB ({ratio:.0%}), 并发→{new_w}", flush=True)
        gc.collect()
        return True
    else:
        if MAX_WORKERS < MAX_WORKERS_INIT:
            MAX_WORKERS = MAX_WORKERS_INIT
            print(f"  ✅ 内存恢复 {rss:.0f}/{SAFE_LIMIT_MB:.0f}MB, 并发→{MAX_WORKERS_INIT}", flush=True)
        return True

with open(CONFIG_FILE) as f:
    cfg = json.load(f)
TOKEN = cfg["token"]
LLM_CFG = cfg["llm"]


# ==================== 进度管理 ====================

def load_progress():
    with open(PROGRESS_FILE) as f:
        return json.load(f)

def save_progress(p):
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS_FILE)


# ==================== HTTP 请求 ====================

def http_req(method, path, data=None, timeout=30):
    url = f"{BASE}{path}"
    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, method=method)
    req.add_header("X-Auth-Token", TOKEN)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "YuqueMigration/3.0")

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                headers = dict(resp.headers)
                body = json.loads(resp.read().decode("utf-8"))
                return body, status, headers
        except urllib.error.HTTPError as e:
            status = e.code
            if status == 429:
                remaining = e.headers.get("X-RateLimit-Remaining", "")
                if remaining == "0":
                    return None, 429, {}
                if attempt < 2:
                    time.sleep(1)
                    continue
                return None, 429, {}
            if status == 404:
                return None, 404, {}
            if status in (502, 503, 504):
                time.sleep(2 ** (attempt + 1))
                continue
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
            else:
                return None, status, {}
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
            else:
                return None, str(e), {}

    return None, "max_retries", {}

def api_get(path, params=None, timeout=30):
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        path = f"{path}?{qs}"
    return http_req("GET", path, timeout=timeout)

def api_post(path, data, timeout=30):
    return http_req("POST", path, data, timeout=timeout)

def api_put(path, data, timeout=30):
    return http_req("PUT", path, data, timeout=timeout)


# ==================== 文档检测函数 ====================

def is_binary(body):
    if len(body) < 50: return False
    sample = body[:1024]
    if '\x00' in sample: return True
    control_chars = sum(1 for c in sample if ord(c) < 32 and c not in '\n\r\t')
    return control_chars / max(len(sample), 1) > 0.30

def is_img_token(body):
    if not body: return False
    pattern = r'^```\w*\s*\n\["\d+:\d+-\d+"\]\s*\n```\s*$'
    return bool(re.match(pattern, body.strip()))

def is_meaningless_doc(title, body):
    if '\n' in title or '\r' in title: return True
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', title): return True
    cleaned_title = re.sub(r'[\x00-\x1f]', '', title).strip()
    if not cleaned_title or cleaned_title.isdigit(): return True
    return False

def fix_title(title, max_len=200):
    title = title.replace('\n', ' ').replace('\r', ' ')
    title = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', title)
    title = title.strip()
    if len(title) > max_len:
        return title[:max_len-3] + "..."
    if not title: return "无标题"
    return title


# ==================== 格式处理 ====================

def fix_table_format(body):
    body = re.sub(r'```\w*\s*\n(\|[^\n]+\|[\s\S]*?)\n```', r'\1', body)
    body = re.sub(r'^(    |\t)\|', r'|', body, flags=re.MULTILINE)
    return body

def needs_llm_cleaning(body):
    stripped = body.strip()
    if stripped.startswith('INSERT INTO') or stripped.startswith('```c\nINSERT INTO'): return False
    if stripped.startswith('```c\n[') or stripped.startswith('['): return False
    code_wrapped = re.match(r'^```\w*\n', stripped)
    if code_wrapped:
        inner = re.sub(r'^```\w*\n|```$', '', stripped, flags=re.DOTALL)
        if inner.strip().startswith(('INSERT', 'SELECT', 'CREATE', 'ALTER', 'DROP', '[', '{',
                                      '#include', 'import ', 'package ', '<?php', '<!DOCTYPE')):
            return False
    return True

def llm_clean_and_classify(body, title, timeout=90):
    """LLM清洗 + 分类合并调用
    返回: (cleaned_body, categories)
    categories: ["分类1", "分类2/子分类", ...]
    """
    body = fix_table_format(body)

    if len(body) < 500 or not needs_llm_cleaning(body):
        # 不调 LLM，默认未分类
        return body, ["未分类"]

    prompt = """你是语雀文档格式清洗 + 分类助手。

输入：一篇从语雀导出的 Markdown 文档。

## 清洗要求
1. 删除：广告横幅、纯表情/灌水评论、HTML 注释、废弃的 HTML 标签
2. 保留：正文全部技术内容、转载来源标记（"本文来自"/"原文链接"等）、有实质讨论的评论（标注"评论："）、文档内部超链接、代码块、表格、Mermaid 图表
3. 修复：断裂的 Markdown 格式、中文全角标点混用、空链接 []()
4. 不改动：标题层级、代码块内容、表格数据

⚠️ 表格铁律：
- 绝对不要用代码块包裹表格
- 绝对不要缩进表格行
- 表格前必须保留一个空行，表格后也必须保留一个空行
- 表格分隔行列数必须与表头一致
- 表格单元格内的竖线必须转义
- 表格中不要使用 HTML 标签

## 分类要求
阅读文档全文，判断它属于哪些主题分类（可多选）。
- 分类名简洁（2-8个字），可用 / 表示层级（如 "Python/异步编程"）
- 以分类准确为先，宁可少分类，不要乱分类
- 确保每个分类都能涵盖文档核心主题
- 如果文档内容确实无法归类，用 ["未分类"]

## 输出格式
输出清洗后的完整 Markdown。

在清洗后正文的末尾，另起一行输出分类标记行（不要放在代码块内）：
<!-- CATEGORIES: ["分类1", "分类2"] -->

只输出清洗后正文和分类标记，不做摘要。"""

    llm_data = json.dumps({
        "model": LLM_CFG["model"],
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": body}
        ],
        "temperature": 0.1,
        "max_tokens": 16000
    }).encode("utf-8")

    llm_req = urllib.request.Request(LLM_CFG["url"], data=llm_data, method="POST")
    llm_req.add_header("Authorization", f"Bearer {LLM_CFG['api_key']}")
    llm_req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(llm_req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            raw = result["choices"][0]["message"]["content"]
            # 提取分类
            cat_match = re.search(r'<!--\s*CATEGORIES:\s*(\[[^\]]*\])', raw)
            categories = ["未分类"]
            if cat_match:
                try:
                    cats = json.loads(cat_match.group(1))
                    if cats and isinstance(cats, list):
                        categories = cats
                except: pass
            # 移除分类标记行
            cleaned = re.sub(r'\s*<!--\s*CATEGORIES:\s*\[[^\]]*\]\s*-->\s*$', '', raw).rstrip()
            return fix_table_format(cleaned), categories
    except Exception as e:
        print(f"  ⚠️ LLM异常: {e}，使用原始内容")
        return body, ["未分类"]


def split_large(body, max_len=50000):
    """大文档拆分，优先按标题切"""
    if len(body) <= max_len:
        return [body]

    lines = body.split('\n')
    in_code = False
    non_code_len = 0
    for line in lines:
        if line.strip().startswith('```'):
            in_code = not in_code
            continue
        if not in_code:
            non_code_len += len(line)
    if non_code_len <= max_len:
        return [body]

    sections = []
    current = ""
    in_code = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('```'):
            in_code = not in_code
        if not in_code and (stripped.startswith('## ') or stripped.startswith('### ')):
            if current:
                sections.append(current.rstrip('\n'))
            current = line + '\n'
        else:
            current += line + '\n'
    if current.strip():
        sections.append(current.rstrip('\n'))

    if len(sections) <= 1:
        sections = re.split(r'\n\n+', body)

    parts = []
    current = ""
    for sec in sections:
        if len(current) + len(sec) > max_len and current:
            parts.append(current.strip())
            current = sec
        else:
            current += "\n" + sec if current else sec
    if current.strip():
        parts.append(current.strip())

    if len(parts) == 1 and len(parts[0]) > max_len:
        hard_parts = []
        hard_current = []
        hard_len = 0
        for line in parts[0].split('\n'):
            if hard_len + len(line) > max_len and hard_current:
                hard_parts.append('\n'.join(hard_current))
                hard_current = [line]
                hard_len = len(line)
            else:
                hard_current.append(line)
                hard_len += len(line) + 1
        if hard_current:
            hard_parts.append('\n'.join(hard_current))
        return hard_parts if len(hard_parts) > 1 else parts

    return parts if len(parts) > 1 else [body]


# ==================== 目录管理 ====================

def wait_until_next_hour():
    now = datetime.now()
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    wait_sec = max(60, (next_hour - now).total_seconds())
    print(f"  ⏳ 限流，等待 {wait_sec/60:.0f} 分钟到 {next_hour.strftime('%H:%M')}...", flush=True)
    time.sleep(wait_sec)
    print(f"  ✅ 恢复执行", flush=True)


def ensure_category(p, cat_name):
    """确保分类目录存在，返回最终层级的 uuid
    自动处理 / 分隔的层级路径（如 "技术/前端" → 建两级 TITLE）
    429 时等整点重试，失败降级返回父层级 uuid
    """
    global TARGET_ID
    toc_map = p.setdefault("toc_map", {})
    if cat_name in toc_map:
        return toc_map[cat_name]

    cat_parts = cat_name.split("/")
    parent_uuid = None

    for part in cat_parts:
        result, status, _ = api_put(f"/repos/{TARGET_ID}/toc", {
            "action": "appendNode",
            "action_mode": "child",
            "type": "TITLE",
            "title": part,
            "target_uuid": parent_uuid
        })
        if result is None:
            if status == 429:
                wait_until_next_hour()
                result, status, _ = api_put(f"/repos/{TARGET_ID}/toc", {
                    "action": "appendNode", "action_mode": "child",
                    "type": "TITLE", "title": part, "target_uuid": parent_uuid
                })
            if result is None:
                print(f"  ⚠️ 创建目录 '{part}' 失败(status={status})，降级到父层级")
                if parent_uuid and parent_uuid not in toc_map.values():
                    toc_map[cat_name] = parent_uuid  # 降级缓存
                return parent_uuid
        parent_uuid = result["data"]["uuid"]

    toc_map[cat_name] = parent_uuid
    return parent_uuid


def mount_docs_to_uuid(p, doc_ids, uuid, cat_name):
    """批量挂文档到指定 uuid，429 等整点重试，失败记 orphans"""
    for j in range(0, len(doc_ids), 50):
        batch = doc_ids[j:j+50]
        result, status, _ = api_put(f"/repos/{TARGET_ID}/toc", {
            "action": "appendNode",
            "action_mode": "child",
            "type": "DOC",
            "target_uuid": uuid,
            "doc_ids": [int(d) for d in batch]
        })
        if result is None:
            if status == 429:
                wait_until_next_hour()
                result, status, _ = api_put(f"/repos/{TARGET_ID}/toc", {
                    "action": "appendNode", "action_mode": "child",
                    "type": "DOC", "target_uuid": uuid,
                    "doc_ids": [int(d) for d in batch]
                })
            if result is None:
                print(f"  ⚠️ 挂载失败 status={status}")
                for d in batch:
                    p.setdefault("orphans", []).append({
                        "doc_id": d, "reason": f"挂载到'{cat_name}'失败(status={status})"
                    })
                time.sleep(1)
            else:
                print(f"  ✅ 挂载 {len(batch)} 篇 (429重试)", flush=True)
        else:
            print(f"  ✅ '{cat_name}' 挂载 {len(batch)} 篇", flush=True)


def mount_docs_to_categories(p, doc_ids, part_bodies, categories):
    """将文档挂载到所有分类目录。
    主分类（第一个）挂原始 doc_ids，
    其余分类复制文档后挂副本 doc_ids。
    """
    if not categories:
        categories = ["未分类"]

    p.setdefault("toc_map", {})
    p.setdefault("multi_category_copies", 0)

    for i, cat_name in enumerate(categories):
        if i == 0:
            # 主分类：直接挂原始 doc_ids
            uuid = ensure_category(p, cat_name)
            if uuid:
                mount_docs_to_uuid(p, doc_ids, uuid, cat_name)
            else:
                print(f"  ⚠️ 目录 '{cat_name}' 创建失败，无法挂载")
                for d in doc_ids:
                    p.setdefault("orphans", []).append({
                        "doc_id": d, "reason": f"目录'{cat_name}'创建失败"
                    })
        else:
            # 额外分类：复制文档 → 挂副本
            copied_ids = []
            for ptitle, pbody in part_bodies:
                result, status, _ = api_post(f"/repos/{TARGET_ID}/docs", {
                    "title": ptitle,
                    "body": pbody,
                    "format": "markdown"
                }, timeout=60)
                if result is None:
                    if status == 429:
                        wait_until_next_hour()
                        result, status, _ = api_post(f"/repos/{TARGET_ID}/docs", {
                            "title": ptitle, "body": pbody, "format": "markdown"
                        }, timeout=60)
                    if result is None:
                        print(f"  ⚠️ 复制文档 '{ptitle}' 失败: {status}")
                        continue
                copied_ids.append(result["data"]["id"])

            if copied_ids:
                p["created"] = p.get("created", 0) + len(copied_ids)
                p["local_created"] = p.get("local_created", 0) + len(copied_ids)
                p["multi_category_copies"] += len(copied_ids)
                uuid = ensure_category(p, cat_name)
                if uuid:
                    mount_docs_to_uuid(p, copied_ids, uuid, cat_name)
                else:
                    for d in copied_ids:
                        p.setdefault("orphans", []).append({
                            "doc_id": d, "reason": f"目录'{cat_name}'创建失败"
                        })


# ==================== 核心处理 ====================

def process_doc(doc, p):
    """处理单篇文档：读取→清洗+分类→拆分→创建→挂目录"""
    global TARGET_ID, SOURCE_ID
    doc_id = doc["id"]
    orig_title = doc["title"]
    title = fix_title(orig_title)

    # 获取文档内容
    result, status, headers = api_get(
        f"/repos/{SOURCE_ID}/docs/{doc_id}", {"raw": "1"}, timeout=90)
    if result is None:
        if status == 404:
            p.setdefault("skipped_empty", []).append({"doc_id": doc_id, "title": title})
            p["skipped"] = p.get("skipped", 0) + 1
            return "empty_404"
        if status == 429:
            return "rate_limit"
        p.setdefault("failed_list", []).append(
            {"id": doc_id, "title": title, "reason": f"获取失败: {status}"})
        p["failed"] = p.get("failed", 0) + 1
        return "fetch_error"

    data = result.get("data", {})
    fmt = data.get("format", "markdown")
    body = data.get("body", "")

    # ── Lake 格式无损搬运 ──
    if fmt == "lake":
        body_lake = data.get("body_lake", body)
        result2, status2, _ = api_post(f"/repos/{TARGET_ID}/docs", {
            "title": title, "body": body_lake, "format": "lake"
        }, timeout=60)
        if result2 is None:
            if status2 == 429: return "rate_limit"
            p.setdefault("failed_list", []).append(
                {"id": doc_id, "title": title, "reason": f"lake创建失败: {status2}"})
            p["failed"] = p.get("failed", 0) + 1
            return "lake_failed"
        new_id = result2["data"]["id"]
        p.setdefault("lake_docs", []).append(
            {"doc_id": doc_id, "new_id": new_id, "title": title, "reason": "lake格式无损搬运"})
        p["created_doc_mapping"][str(doc_id)] = new_id
        p["created"] = p.get("created", 0) + 1
        p["local_created"] = p.get("local_created", 0) + 1
        # Lake 无法 LLM 清洗，归入"未分类"
        mount_docs_to_categories(p, [new_id], [(title, body_lake)], ["未分类"])
        return "lake_created"

    # ── 格式过滤 ──
    UNSUPPORTED_FORMATS = {"doc", "docx", "pdf", "image", "png", "jpg", "jpeg",
                           "gif", "ppt", "pptx", "xls", "xlsx", "zip", "rar"}
    if fmt in UNSUPPORTED_FORMATS:
        p.setdefault("skipped_unsupported", []).append(
            {"doc_id": doc_id, "title": title, "format": fmt, "reason": "不支持的文件格式"})
        p["skipped"] = p.get("skipped", 0) + 1
        return f"skipped_format_{fmt}"
    if fmt not in ("markdown", "lake"):
        p.setdefault("failed_list", []).append(
            {"id": doc_id, "title": title, "reason": f"未知格式: {fmt}"})
        p["failed"] = p.get("failed", 0) + 1
        return f"unknown_format_{fmt}"

    if not body or not body.strip():
        p.setdefault("skipped_empty", []).append({"doc_id": doc_id, "title": title})
        p["skipped"] = p.get("skipped", 0) + 1
        return "empty"

    if is_img_token(body):
        p.setdefault("skipped_img_token", []).append(
            {"doc_id": doc_id, "title": title, "reason": "图片token文档"})
        p["skipped"] = p.get("skipped", 0) + 1
        return "skipped_img_token"

    if is_meaningless_doc(orig_title, body):
        p.setdefault("skipped_meaningless", []).append(
            {"doc_id": doc_id, "title": title, "reason": "无意义文档"})
        p["skipped"] = p.get("skipped", 0) + 1
        return "skipped_meaningless"

    if is_binary(body):
        p.setdefault("skipped_binary", []).append({"doc_id": doc_id, "title": title})
        p["skipped"] = p.get("skipped", 0) + 1
        return "binary"

    # ── LLM 清洗 + 分类（一次调用） ──
    cleaned, categories = llm_clean_and_classify(body, title)

    # ── 大文档拆分 ──
    parts = split_large(cleaned)
    if not parts:
        p.setdefault("failed_list", []).append(
            {"id": doc_id, "title": title, "reason": "拆分后无有效内容"})
        p["failed"] = p.get("failed", 0) + 1
        return "no_parts"

    # ── 创建文档 ──
    created_ids = []
    part_bodies = []
    for i, part in enumerate(parts):
        final_title = f"{title}({i+1}/{len(parts)})" if len(parts) > 1 else title
        result2, status2, _ = api_post(f"/repos/{TARGET_ID}/docs", {
            "title": final_title, "body": part, "format": "markdown"
        }, timeout=60)
        if result2 is None:
            if status2 == 429: return "rate_limit"
            p.setdefault("failed_list", []).append(
                {"id": doc_id, "title": title, "reason": f"创建失败(part {i+1}): {status2}"})
            p["failed"] = p.get("failed", 0) + 1
            return "create_failed"
        new_id = result2["data"]["id"]
        created_ids.append(new_id)
        part_bodies.append((final_title, part))
        p["created"] = p.get("created", 0) + 1
        p["local_created"] = p.get("local_created", 0) + 1

    if not created_ids:
        p.setdefault("failed_list", []).append(
            {"id": doc_id, "title": title, "reason": "拆分后无创建结果"})
        p["failed"] = p.get("failed", 0) + 1
        return "no_parts_created"

    p["created_doc_mapping"][str(doc_id)] = created_ids[0] if len(created_ids) == 1 else created_ids

    # ── 挂目录（主分类 + 额外分类复制） ──
    mount_docs_to_categories(p, created_ids, part_bodies, categories)

    n_cats = len(categories)
    suffix = f"_split{len(parts)}_cats{n_cats}" if len(parts) > 1 else (
        f"_cats{n_cats}" if n_cats > 1 else "")
    return f"created{suffix}"


# ==================== 主流程 ====================

def main():
    global PROGRESS_FILE, SOURCE_ID, TARGET_ID, TARGET_NS

    import sys
    if len(sys.argv) > 1:
        PROGRESS_FILE = os.path.expanduser(sys.argv[1])
    else:
        PROGRESS_FILE = os.path.expanduser(
            "~/.openclaw/workspace/utils/yuque/yuque-migration/progress/78699632_废弃19.json")

    print(f"📋 进度文件: {PROGRESS_FILE}")
    p = load_progress()
    SOURCE_ID = p["source_book_id"]
    TARGET_ID = p["target_book_id"]
    TARGET_NS = p["target_namespace"]

    offset = p["last_offset"]
    total = p["total_docs"]
    print(f"📦 续传: offset={offset}, 已创建={p['created']}, 跳过={p['skipped']}, 失败={p['failed']}")
    print(f"   源库: {p['source_name']} ({SOURCE_ID})")
    print(f"   目标库: {p['target_name']} ({TARGET_ID})")
    print(f"   目标库累计: {p.get('local_created', 0)}/4500", flush=True)
    rss = _get_rss_mb()
    print(f"   内存: 安全水位{SAFE_LIMIT_MB:.0f}MB, "
          f"当前RSS={rss:.0f}MB" if rss else f"   内存: 安全水位{SAFE_LIMIT_MB:.0f}MB", flush=True)

    FATAL_RESULTS = {"fetch_error", "create_failed", "lake_failed", "no_parts", "no_parts_created", "error"}
    consecutive_errors = 0

    while offset < total:
        if p.get("local_created", 0) >= 4500:
            print(f"\n⚠️ 目标库已达切换阈值 4500 篇！已迁移 {p['local_created']} 篇。", flush=True)
            save_progress(p)
            return

        print(f"\n📄 offset={offset} 获取 {BATCH_SIZE} 篇...", flush=True)
        result, status, headers = api_get(
            f"/repos/{SOURCE_ID}/docs", {"offset": str(offset), "limit": str(BATCH_SIZE)})
        if result is None:
            if status == 429:
                wait_until_next_hour()
                continue
            print(f"  ❌ 获取列表失败: {status}", flush=True)
            time.sleep(5)
            continue

        docs = result.get("data", [])
        if not docs:
            print("  无更多文档，完成。", flush=True)
            break

        already_done = [d for d in docs if d["id"] in p.get("processed_doc_ids", [])]
        pending = [d for d in docs if d["id"] not in p.get("processed_doc_ids", [])]
        print(f"  已处理 {len(already_done)}，待处理 {len(pending)}", flush=True)

        _check_memory()

        for doc in pending:
            doc_id = doc["id"]
            short_title = doc["title"][:60]
            print(f"  🔄 [{doc_id}] {short_title}...", end=" ", flush=True)
            result = process_doc(doc, p)
            print(result, flush=True)
            p.setdefault("processed_doc_ids", []).append(doc_id)

            if result == "rate_limit":
                save_progress(p)
                wait_until_next_hour()
                p["processed_doc_ids"].remove(doc_id)
                retry = process_doc(doc, p)
                print(f"  🔄 重试 [{doc_id}]: {retry}", flush=True)
                p.setdefault("processed_doc_ids", []).append(doc_id)

            if result in FATAL_RESULTS or result.startswith("unknown_format_"):
                consecutive_errors += 1
                if consecutive_errors > 10:
                    print(f"\n❌ 连续 {consecutive_errors} 次致命错误，暂停。最后错误: {result}", flush=True)
                    save_progress(p)
                    return
            else:
                consecutive_errors = 0

            time.sleep(0.2)
            save_progress(p)

        all_done = all(d["id"] in p.get("processed_doc_ids", []) for d in docs)
        if all_done:
            offset += len(docs)
            p["last_offset"] = offset
        else:
            print(f"  ⚠️ 本批未完全处理，offset保持 {offset}", flush=True)
            p["last_offset"] = offset
        save_progress(p)
        print(f"  📊 {offset}/{total} ({offset*100//total}%), "
              f"创={p['created']} 跳={p['skipped']} 败={p['failed']}", flush=True)

    # 汇报
    copies = p.get("multi_category_copies", 0)
    print(f"\n✅ 迁移完成！创={p['created']}(含多目录副本{copies}篇) "
          f"跳={p['skipped']} 败={p['failed']}", flush=True)
    cats = len(p.get("toc_map", {}))
    if cats:
        print(f"📂 已建 {cats} 个目录", flush=True)
    orphans = len(p.get("orphans", []))
    if orphans:
        print(f"⚠️ {orphans} 篇孤儿文档（已创建但挂载失败）", flush=True)
    save_progress(p)


if __name__ == "__main__":
    main()
