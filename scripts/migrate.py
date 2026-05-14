#!/usr/bin/env python3
"""语雀知识库迁移脚本 v5
v5: 去重检测 + 重拟标题 + 容量初始计数 + 配置路径修正 + 代码清理 + LLM截断优化
迁移即分类：LLM 清洗+分类合并为一次调用，逐篇处理立即挂目录，
多分类自动复制文档，不再攒数据到后置 TOC 阶段。
"""

import json, time, re, os, gc, urllib.request, urllib.error, urllib.parse, hashlib
from datetime import datetime, timedelta

# ── 配置路径：skill 目录下的 config/yuque-config.json ──
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(SKILL_DIR, "config", "yuque-config.json")

# 运行时配置（从进度文件读取）
PROGRESS_FILE = None
SOURCE_ID = None
TARGET_ID = None
TARGET_NS = None

BATCH_SIZE = 100
MAX_WORKERS_INIT = 5
MAX_WORKERS = 5

# ── RateLimit 追踪 ──
_last_remaining = None
_prev_logged_remaining = None


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
BASE = cfg.get("base", "https://www.yuque.com/api/v2")
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


def _book_counters(p, book_id=None):
    """获取指定知识库的计数器 dict，不存在则初始化"""
    if book_id is None:
        book_id = str(p["target_book_id"])
    else:
        book_id = str(book_id)
    bc = p.setdefault("created_by_book", {})
    if book_id not in bc:
        bc[book_id] = {"created": 0, "local_created": 0, "multi_category_copies": 0}
    return bc[book_id]


def _migrate_flat_counters(p):
    """将旧的平铺计数器迁移到 created_by_book 结构"""
    if "created_by_book" not in p:
        p["created_by_book"] = {}
        bid = str(p["target_book_id"])
        p["created_by_book"][bid] = {
            "created": p.pop("created", 0),
            "local_created": p.pop("local_created", 0),
            "multi_category_copies": p.pop("multi_category_copies", 0)
        }
    # 清理可能残留的旧字段
    for k in ("created", "local_created", "multi_category_copies"):
        p.pop(k, None)


# ==================== HTTP 请求 ====================

def http_req(method, path, data=None, timeout=30):
    global _last_remaining
    url = f"{BASE}{path}"
    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, method=method)
    req.add_header("X-Auth-Token", TOKEN)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "YuqueMigration/5.0")

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                headers = dict(resp.headers)
                body = json.loads(resp.read().decode("utf-8"))
                # 追踪 X-RateLimit-Remaining
                rem = headers.get("X-RateLimit-Remaining", "")
                if rem:
                    try:
                        _last_remaining = int(rem)
                    except ValueError:
                        pass
                return body, status, headers
        except urllib.error.HTTPError as e:
            status = e.code
            if status == 429:
                remaining = e.headers.get("X-RateLimit-Remaining", "")
                # 追踪 remaining（即使是 429 也记录）
                if remaining:
                    try:
                        _last_remaining = int(remaining)
                    except ValueError:
                        pass
                if remaining == "0":
                    # 配额耗尽，不等了，让调用方决定
                    return None, 429, {"X-RateLimit-Remaining": "0"}
                # 瞬时限流，渐进退避：1s/3s/5s
                delays = [1, 3, 5]
                if attempt < len(delays):
                    delay = delays[attempt]
                    # 有 remaining 值时打印便于调试
                    if remaining:
                        print(f"  ⚡ 429限流(剩余={remaining})，{delay}s后重试...", flush=True)
                    time.sleep(delay)
                    continue
                # 重试耗尽，返回限流状态
                return None, 429, {"X-RateLimit-Remaining": remaining}
            if status == 404:
                return None, 404, {}
            if status in (502, 503, 504):
                time.sleep(1)
                continue
            if attempt < 2:
                time.sleep(1)
            else:
                return None, status, {}
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
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
    """二进制文档检测：\x00 铁定二进制；控制字符+高位字节比例高+绝对量大且无可读文字才判二进制。"""
    if len(body) < 50:
        return False
    sample = body[:4096]
    if '\x00' in sample[:1024]:
        return True
    # 控制字符（0-31，排除换行/回车/制表）
    control_chars = sum(1 for c in sample if ord(c) < 32 and c not in '\n\r\t')
    # 高位字节（>127，base64 编码的二进制文件特征）
    high_bytes = sum(1 for c in sample if ord(c) > 127)
    bad_chars = control_chars + high_bytes
    ratio = bad_chars / max(len(sample), 1)
    if ratio > 0.30 and bad_chars > 50:
        # 高位字节 > 200 → 检查是否孤立散布（base64 二进制特征）
        # 纯中文文档高位字节相邻成句，不会孤立
        if high_bytes > 200:
            isolated = 0
            for i, c in enumerate(sample):
                if ord(c) > 127:
                    left = i == 0 or ord(sample[i-1]) <= 127
                    right = i == len(sample) - 1 or ord(sample[i+1]) <= 127
                    if left and right:
                        isolated += 1
            if isolated / max(high_bytes, 1) > 0.25:
                return True
        # 二次确认：有可读文字就不是二进制（可能是编码损坏的文档）
        readable = len(re.findall(r'[\u4e00-\u9fff\w]{4,}', sample))
        if readable > 5:
            return False
        return True
    return False

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


# ==================== 去重检测 ====================

def normalize_for_compare(text):
    """标准化文本用于比较：去除多余空白、统一换行"""
    if not text: return ""
    return re.sub(r'\s+', ' ', text.strip())

def check_duplicate(p, title, body, dedup_lock):
    """检测目标库中是否已有同标题文档。

    返回:
        ("skip", matched_doc_id)  — 标题+内容相同，跳过
        ("conflict", matched_doc_id) — 标题同内容不同，需要重拟标题
        ("new", None)  — 没有冲突，正常创建
    """
    # 1. 本地缓存命中（持锁）
    with dedup_lock:
        cache = p.setdefault("_created_title_cache", {})
        if title in cache:
            cached = cache[title]
            if normalize_for_compare(body[:200]) == cached.get("body_200", ""):
                return ("skip", cached["doc_id"])
            if normalize_for_compare(body[:500]) == cached.get("body_500", ""):
                return ("skip", cached["doc_id"])
            return ("conflict", cached["doc_id"])

    # 2. 搜索目标库（无锁，不阻塞其他线程查缓存）
    result, status, _ = api_get("/search", {
        "q": title[:200], "type": "doc", "scope": TARGET_NS
    })
    if result is None:
        return ("new", None)

    matches = result.get("data", [])
    for m in matches:
        if m.get("title", "").strip() != title.strip():
            continue
        match_id = m["target"]["id"]

        doc_result, doc_status, _ = api_get(
            f"/repos/{TARGET_ID}/docs/{match_id}", {"raw": "1"}, timeout=30)
        if doc_result is None:
            continue
        match_body = doc_result.get("data", {}).get("body", "")

        # 逐级比较（200字→500字→全文，按 SKILL.md 规格）
        if normalize_for_compare(body[:200]) == normalize_for_compare(match_body[:200]):
            with dedup_lock:
                cache = p.setdefault("_created_title_cache", {})
                if title not in cache:
                    cache[title] = {
                        "doc_id": match_id,
                        "body_200": normalize_for_compare(match_body[:200]),
                        "body_500": normalize_for_compare(match_body[:500])
                    }
            return ("skip", match_id)

        if normalize_for_compare(body[:500]) == normalize_for_compare(match_body[:500]):
            with dedup_lock:
                cache = p.setdefault("_created_title_cache", {})
                if title not in cache:
                    cache[title] = {
                        "doc_id": match_id,
                        "body_200": normalize_for_compare(match_body[:200]),
                        "body_500": normalize_for_compare(match_body[:500])
                    }
            return ("skip", match_id)

        # 全文比较
        if normalize_for_compare(body) == normalize_for_compare(match_body):
            with dedup_lock:
                cache = p.setdefault("_created_title_cache", {})
                if title not in cache:
                    cache[title] = {
                        "doc_id": match_id,
                        "body_200": normalize_for_compare(match_body[:200]),
                        "body_500": normalize_for_compare(match_body[:500])
                    }
            return ("skip", match_id)

        # 标题同内容不同 → 需重拟标题
        with dedup_lock:
            cache = p.setdefault("_created_title_cache", {})
            if title not in cache:
                cache[title] = {
                    "doc_id": match_id,
                    "body_200": normalize_for_compare(match_body[:200]),
                    "body_500": normalize_for_compare(match_body[:500])
                }
        return ("conflict", match_id)

    return ("new", None)


# ==================== 格式处理 ====================

def fix_table_format(body):
    body = re.sub(r'```\w*\s*\n(\|[^\n]+\|[\s\S]*?)\n```', r'\1', body)
    body = re.sub(r'^(    |\t)\|', r'|', body, flags=re.MULTILINE)
    return body

def needs_llm_cleaning(body):
    """判断是否需要 LLM 清洗。
    跳过：纯代码文档、附件文档、<500 字符短文档。
    """
    stripped = body.strip()
    if not stripped:
        return False

    code_indicators = (
        'INSERT INTO', 'SELECT ', 'CREATE TABLE', 'ALTER TABLE', 'DROP ',
        '#include', '#define', '#ifndef', '#pragma',
        'import ', 'from ', 'package ', 'func ', 'fn ', 'def ',
        '<?php', '<!DOCTYPE', '<html', '<template',
        '#!/', 'use ', 'module ', 'require(',
    )

    # ── 纯代码文档检测 ──
    code_wrapped = re.match(r'^```\w*\n', stripped)
    if code_wrapped:
        inner = re.sub(r'^```\w*\n|```$', '', stripped, flags=re.DOTALL).strip()
        if inner.startswith(code_indicators):
            return False
        non_code = re.sub(r'```[\s\S]*?```', '', stripped, flags=re.DOTALL).strip()
        if len(non_code) < len(stripped) * 0.1:
            return False

    # ── 纯代码文档检测（无代码块包裹） ──
    if stripped.startswith(code_indicators):
        return False
    if re.match(r'^[\[{]\s*$', stripped.split('\n')[0].strip()):
        non_struct = re.sub(r'[\[\]{}:,"\'.\d\s\-]', '', stripped[:500])
        if len(non_struct) < 20:
            return False

    # ── 附件文档检测 ──
    link_lines = re.findall(r'https?://[^\s<>"\']+\.(?:pdf|zip|rar|7z|tar\.gz|docx?|xlsx?|pptx?|apk|exe|dmg|pkg|jar|war|deb|rpm)',
                            stripped, re.IGNORECASE)
    if link_lines:
        no_links = re.sub(r'https?://[^\s<>"\'\n]+', '', stripped)
        no_links = re.sub(r'[\[\]\(\)\|\-\*#\s]', '', no_links)
        if len(no_links) < 100:
            return False

    return True


def llm_clean_and_classify(body, title, need_new_title=False, timeout=120):
    """LLM清洗 + 分类 + 重拟标题（合并一次调用）

    返回: (cleaned_body, categories, new_title_or_None)
    """
    body = fix_table_format(body)

    if not body.strip():
        return body, ["未分类"], None
    if not needs_llm_cleaning(body):
        return body, ["未分类"], None

    # 短文档也送 LLM 分类，只要 needs_llm_cleaning 判定有意义
    if len(body) < 500:
        body = body + "\n\n（短文档，请根据现有内容分类）"

    MAX_CHARS = 20000
    truncated = False
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS]
        truncated = True

    prompt = f"""你是语雀文档格式清洗 + 分类助手。

输入：一篇从语雀导出的 Markdown 文档{"（已截取前 " + str(MAX_CHARS) + " 字符）" if truncated else ""}。

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

{"## 截断要求\n已给你文档前 " + str(MAX_CHARS) + " 字符。请找到**你可见文本内最后一个**完整的段落/章节边界（如 ## 标题后、段落结束、代码块结束），输出到该边界为止。不要把输出结束在句子中间或代码块内部。" if truncated else ""}
{"## 重拟标题要求\n⚠️ 特殊任务：此文档标题「" + title + "」在目标库中已存在同名文档，但内容不同，需要你根据文档内容生成一个新的、有区分度的标题。\n要求：新标题简洁（≤30字）、准确反映内容核心，避免与原标题重复。\n在输出末尾添加：<!-- NEW_TITLE: \"新标题\" -->" if need_new_title else ""}
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

            # 提取新标题（如果有）
            new_title = None
            title_match = re.search(r'<!--\s*NEW_TITLE:\s*"([^"]+)"', raw)
            if title_match:
                new_title = title_match.group(1).strip()

            # 移除所有标记行
            cleaned = re.sub(r'\s*<!--\s*(?:CATEGORIES:\s*\[[^\]]*\]|NEW_TITLE:\s*"[^"]+")\s*-->\s*', '', raw).rstrip()
            return cleaned, categories, new_title
    except Exception as e:
        print(f"  ⚠️ LLM异常: {e}，使用原始内容")
        return body, ["未分类"], None


# ==================== 目录管理 ====================

def wait_until_next_hour():
    """等待到下一个整点。max(60, ...) 保证至少等 60 秒，
    即使恰好在整点附近触发也不会因 reset 延迟而立即重试失败。
    """
    now = datetime.now()
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    wait_sec = max(60, (next_hour - now).total_seconds())
    print(f"  ⏳ 限流，等待 {wait_sec/60:.0f} 分钟到 {next_hour.strftime('%H:%M')}...", flush=True)
    time.sleep(wait_sec)
    print(f"  ✅ 恢复执行", flush=True)


def _find_title_in_toc(nodes, target_title, parent_uuid):
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "TITLE" and node.get("title") == target_title:
            np = node.get("parent_uuid")
            if (parent_uuid is None and (np is None or np == "")) or np == parent_uuid:
                return node["uuid"]
        found = _find_title_in_toc(node.get("children", []), target_title, parent_uuid)
        if found:
            return found
    return None

def _get_toc_cache(p):
    global TARGET_ID
    cache = p.get("_toc_cache")
    if cache is None or not isinstance(cache, dict):
        cache = {}
        p["_toc_cache"] = cache
    if cache.get("tree") is not None:
        return cache["tree"]
    toc_result, _, _ = api_get(f"/repos/{TARGET_ID}/toc")
    tree = toc_result.get("data", []) if toc_result else []
    cache["tree"] = tree
    cache["fetched_at"] = datetime.now().isoformat()
    return tree

def _insert_to_toc_cache(p, parent_uuid, part, new_uuid):
    cache = p.get("_toc_cache", {})
    tree = cache.get("tree", [])
    new_node = {"uuid": new_uuid, "title": part, "type": "TITLE",
                "parent_uuid": parent_uuid, "children": []}
    if parent_uuid is None or parent_uuid == "":
        if isinstance(tree, list):
            tree.append(new_node)
    else:
        _insert_child_to_node(tree, parent_uuid, new_node)
    cache["tree"] = tree

def _insert_child_to_node(nodes, target_uuid, child):
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list):
        return False
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("uuid") == target_uuid:
            node.setdefault("children", []).append(child)
            return True
        if _insert_child_to_node(node.get("children", []), target_uuid, child):
            return True
    return False

def ensure_category(p, cat_name):
    global TARGET_ID
    toc_map = p.setdefault("toc_map", {})
    if cat_name in toc_map:
        return toc_map[cat_name]

    toc_tree = _get_toc_cache(p)
    cat_parts = cat_name.split("/")
    parent_uuid = None

    for part in cat_parts:
        existing = _find_title_in_toc(toc_tree, part, parent_uuid)
        if existing:
            parent_uuid = existing
            continue

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
                    toc_map[cat_name] = parent_uuid
                    return parent_uuid
            else:
                print(f"  ⚠️ 创建目录 '{part}' 失败(status={status})，降级到父层级")
                toc_map[cat_name] = parent_uuid
                return parent_uuid
        found = False
        for item in result.get("data", []):
            if item.get("title") == part:
                parent_uuid = item["uuid"]
                _insert_to_toc_cache(p, item.get("parent_uuid"), part, parent_uuid)
                found = True
                break
        if not found:
            print(f"  ⚠️ 创建目录 '{part}' 后找不到节点，降级")
            toc_map[cat_name] = parent_uuid
            return parent_uuid

    toc_map[cat_name] = parent_uuid
    return parent_uuid


def mount_docs_to_uuid(p, doc_ids, uuid, cat_name):
    if uuid is None:
        print(f"  ⚠️ 目录 '{cat_name}' uuid=None，跳过挂载", flush=True)
        for d in doc_ids:
            p.setdefault("orphans", []).append({
                "doc_id": d, "reason": f"目录'{cat_name}'uuid为None"
            })
        return
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


def mount_docs_to_categories(p, doc_ids, part_bodies, categories, p_lock=None):
    if not categories:
        categories = ["未分类"]

    p.setdefault("toc_map", {})

    for i, cat_name in enumerate(categories):
        if i == 0:
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
                if p_lock:
                    with p_lock:
                        bc3 = _book_counters(p)
                        bc3["created"] += len(copied_ids)
                        bc3["local_created"] += len(copied_ids)
                        bc3["multi_category_copies"] += len(copied_ids)
                else:
                    bc3 = _book_counters(p)
                    bc3["created"] += len(copied_ids)
                    bc3["local_created"] += len(copied_ids)
                    bc3["multi_category_copies"] += len(copied_ids)
                uuid = ensure_category(p, cat_name)
                if uuid:
                    mount_docs_to_uuid(p, copied_ids, uuid, cat_name)
                else:
                    for d in copied_ids:
                        p.setdefault("orphans", []).append({
                            "doc_id": d, "reason": f"目录'{cat_name}'创建失败"
                        })


# ==================== 主流程 ====================

def generate_report(p):
    """生成 Markdown 汇总报告，返回文件路径"""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    source_ns = p.get("source_namespace", "")
    def doc_link(doc_id, title=""):
        """生成语雀文档链接"""
        url = f"https://www.yuque.com/{source_ns}/{doc_id}"
        label = title or str(doc_id)
        return f"[{label}]({url})"
    def get_id(item):
        return item.get("doc_id") or item.get("id", "?")

    copies = sum(b.get("multi_category_copies", 0) for b in p.get("created_by_book", {}).values())
    cats = len(p.get("toc_map", {}))
    total = p.get("total_docs", 0)
    created = sum(b.get("created", 0) for b in p.get("created_by_book", {}).values())
    skipped = p.get("skipped", 0)
    failed = p.get("failed", 0)
    initial = p.get("initial_count", 0)
    local = sum(b.get("local_created", 0) for b in p.get("created_by_book", {}).values())
    current = initial + local

    # 跳过明细
    dup = p.get("skipped_duplicates", [])
    empty = p.get("skipped_empty", [])
    lake = p.get("skipped_lake", [])
    binary = p.get("skipped_binary", [])
    meaningless = p.get("skipped_meaningless", [])
    unsupported = p.get("skipped_unsupported", [])
    fail_list = p.get("failed_list", [])
    orphans = p.get("orphans", [])

    lines = []
    lines.append("# 语雀知识库迁移报告")
    lines.append("")
    lines.append(f"- **源库**: {p['source_name']} (ID: {p['source_book_id']})")
    lines.append(f"- **目标库**: {p['target_name']} (ID: {p['target_book_id']})")
    lines.append(f"- **完成时间**: {now}")
    lines.append("")

    # 概览
    lines.append("## 概览")
    lines.append("")
    lines.append("| 指标 | 数量 |")
    lines.append("|------|------|")
    lines.append(f"| 源文档总数 | {total} |")
    dup_create = copies + created
    line_created = f"{dup_create}（含 {copies} 篇多目录副本）" if copies else str(created)
    lines.append(f"| 成功创建 | {line_created} |")
    skip_parts = []
    if dup: skip_parts.append(f"去重 {len(dup)}")
    if empty: skip_parts.append(f"空文档 {len(empty)}")
    if lake: skip_parts.append(f"Lake {len(lake)}")
    if binary: skip_parts.append(f"二进制 {len(binary)}")
    if meaningless: skip_parts.append(f"无意义 {len(meaningless)}")
    if unsupported: skip_parts.append(f"不支持格式 {len(unsupported)}")
    skip_detail = "、".join(skip_parts) if skip_parts else "—"
    lines.append(f"| 跳过 | {skipped}（{skip_detail}） |")
    lines.append(f"| 失败 | {failed} |")
    lines.append(f"| 已建目录 | {cats} |")
    lines.append(f"| 目标库用量 | {current}/5000 |")
    lines.append("")

    # 跳过明细
    sections = [
        ("去重", dup, lambda x: f"- {doc_link(get_id(x), x['title'])} → 匹配: {x.get('matched', '?')}" if 'matched' in x else f"- {doc_link(get_id(x), x['title'])}"),
        ("空文档", empty, lambda x: f"- {doc_link(get_id(x), x['title'])}"),
        ("Lake 文档", lake, lambda x: f"- {doc_link(get_id(x), x['title'])}（{x.get('reason', '')}）"),
        ("二进制文件", binary, lambda x: f"- {doc_link(get_id(x), x['title'])}"),
        ("无意义文档", meaningless, lambda x: f"- {doc_link(get_id(x), x['title'])}（{x.get('reason', '')}）"),
        ("不支持格式", unsupported, lambda x: f"- {doc_link(get_id(x), x['title'])}（{x.get('format', '?')}）"),
    ]
    has_skip_detail = any(s[1] for s in sections)
    if has_skip_detail:
        lines.append("## 跳过明细")
        lines.append("")
        for name, items, fmt_fn in sections:
            if not items:
                continue
            lines.append(f"### {name}（{len(items)} 篇）")
            lines.append("")
            for item in items:
                lines.append(fmt_fn(item))
            lines.append("")

    # 失败
    if fail_list:
        lines.append("## 失败（{} 篇）".format(len(fail_list)))
        lines.append("")
        for f in fail_list:
            lines.append(f"- {doc_link(get_id(f), f.get('title', '?'))}（{f.get('reason', '?')}）")
        lines.append("")

    # 孤儿
    if orphans:
        lines.append("## 孤儿文档（{} 篇）".format(len(orphans)))
        lines.append("")
        for o in orphans:
            errors = ', '.join(o.get('errors', []))
            lines.append(f"- {doc_link(get_id(o), o['title'])}（{errors}）")
        lines.append("")

    # 目录结构
    if cats:
        lines.append("## 目录结构（{} 个）".format(cats))
        lines.append("")
        for cat in sorted(p.get("toc_map", {}).keys()):
            lines.append(f"- {cat}")
        lines.append("")

    # 写入文件
    report_path = PROGRESS_FILE + ".report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return report_path


def main():
    global PROGRESS_FILE, SOURCE_ID, TARGET_ID, TARGET_NS, MAX_WORKERS

    import sys
    from concurrent.futures import ThreadPoolExecutor, Future, as_completed
    from threading import Lock

    if len(sys.argv) < 2:
        print("用法: python migrate.py <进度文件路径>", file=sys.stderr)
        print("进度文件位于 progress/", file=sys.stderr)
        sys.exit(1)
    PROGRESS_FILE = sys.argv[1]
    if not os.path.isabs(PROGRESS_FILE):
        PROGRESS_FILE = os.path.join(SKILL_DIR, PROGRESS_FILE)
    if not os.path.exists(PROGRESS_FILE):
        print(f"❌ 进度文件不存在: {PROGRESS_FILE}", file=sys.stderr)
        sys.exit(1)

    # ── 进程锁 ──
    LOCK_FILE = PROGRESS_FILE + ".lock"
    my_pid = os.getpid()
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as lf:
                old_pid = int(lf.read().strip())
            if old_pid == my_pid:
                pass
            else:
                try:
                    os.kill(old_pid, 0)
                    print(f"❌ 已有迁移任务在运行 (PID={old_pid})，拒绝重复启动", file=sys.stderr)
                    sys.exit(1)
                except OSError:
                    print(f"⚠️ 旧锁文件 (PID={old_pid}) 对应进程已退出，覆盖", file=sys.stderr)
        except (ValueError, FileNotFoundError):
            pass
    with open(LOCK_FILE, 'w') as lf:
        lf.write(str(my_pid))
    import atexit
    def _clean_lock():
        try:
            with open(LOCK_FILE) as lf:
                if int(lf.read().strip()) == my_pid:
                    os.remove(LOCK_FILE)
        except:
            pass
    atexit.register(_clean_lock)

    print(f"📋 进度文件: {PROGRESS_FILE}")
    p = load_progress()
    _migrate_flat_counters(p)  # v5→v6: 计数器改为按知识库ID分组
    SOURCE_ID = p["source_book_id"]
    TARGET_ID = p["target_book_id"]
    TARGET_NS = p["target_namespace"]

    offset = p["last_offset"]
    total = p["total_docs"]

    # ── 容量追踪：获取目标库初始文档数 ──
    if "initial_count" not in p:
        repo_result, _, _ = api_get(f"/repos/{TARGET_ID}")
        if repo_result:
            p["initial_count"] = repo_result.get("data", {}).get("items_count", 0)
        else:
            p["initial_count"] = 0
        save_progress(p)
    initial_count = p["initial_count"]
    bc = _book_counters(p)
    local_created = bc["local_created"]
    current_total = initial_count + local_created

    print(f"📦 续传: offset={offset}, 已创建={bc['created']}, 跳过={p['skipped']}, 失败={p['failed']}")
    print(f"   源库: {p['source_name']} ({SOURCE_ID})")
    print(f"   目标库: {p['target_name']} ({TARGET_ID})")
    print(f"   目标库容量: 初始{initial_count} + 本次{local_created} = {current_total}/5000", flush=True)
    if POD_LIMIT_B:
        print(f"   容器内存上限: {POD_LIMIT_B/1024/1024:.0f}MB, 安全水位: {SAFE_LIMIT_MB:.0f}MB")
    else:
        print(f"   安全水位: {SAFE_LIMIT_MB:.0f}MB")
    rss = _get_rss_mb()
    print(f"   RSS={rss:.0f}MB" if rss else "", flush=True)

    # ── 线程安全锁 ──
    p_lock = Lock()
    toc_lock = Lock()
    dedup_lock = Lock()  # 保护 _created_title_cache 读写

    FATAL_RESULTS = {"fetch_error", "create_failed", "error"}
    consecutive_errors = 0
    error_lock = Lock()

    def process_one_doc(doc):
        nonlocal consecutive_errors
        doc_id = doc["id"]
        short_title = doc["title"][:60]
        orig_title = doc["title"]
        title = fix_title(orig_title)

        # ── 阶段 1：获取 body ──
        result, status, headers = api_get(
            f"/repos/{SOURCE_ID}/docs/{doc_id}", {"raw": "1"}, timeout=90)

        # ── 阶段 2：格式检查 ──
        if result is None:
            if status == 404:
                with p_lock:
                    p.setdefault("skipped_empty", []).append({"doc_id": doc_id, "title": title})
                    p["skipped"] = p.get("skipped", 0) + 1
                    p.setdefault("processed_doc_ids", []).append(doc_id)
                print(f"  🔄 [{doc_id}] {short_title}... empty_404", flush=True)
                return "empty_404"
            if status == 429:
                return "rate_limit"
            with p_lock:
                p.setdefault("failed_list", []).append(
                    {"id": doc_id, "title": title, "reason": f"获取失败: {status}"})
                p["failed"] = p.get("failed", 0) + 1
                p.setdefault("processed_doc_ids", []).append(doc_id)
            print(f"  🔄 [{doc_id}] {short_title}... fetch_error", flush=True)
            return "fetch_error"

        data = result.get("data", {})
        fmt = data.get("format", "markdown")
        body = data.get("body", "")

        # ── Lake 跳过 ──
        if fmt == "lake":
            with p_lock:
                p.setdefault("skipped_lake", []).append(
                    {"doc_id": doc_id, "title": title, "reason": "lake文档无法完美迁移，已跳过"})
                p["skipped"] = p.get("skipped", 0) + 1
                p.setdefault("processed_doc_ids", []).append(doc_id)
            print(f"  ⏭ [{doc_id}] {short_title}... skipped_lake", flush=True)
            return "skipped_lake"

        # 格式过滤
        UNSUPPORTED_FORMATS = {"doc", "docx", "pdf", "image", "png", "jpg", "jpeg",
                               "gif", "ppt", "pptx", "xls", "xlsx", "zip", "rar"}
        if fmt in UNSUPPORTED_FORMATS:
            with p_lock:
                p.setdefault("skipped_unsupported", []).append(
                    {"doc_id": doc_id, "title": title, "format": fmt, "reason": "不支持的文件格式"})
                p["skipped"] = p.get("skipped", 0) + 1
                p.setdefault("processed_doc_ids", []).append(doc_id)
            print(f"  🔄 [{doc_id}] {short_title}... skipped_format_{fmt}", flush=True)
            return f"skipped_format_{fmt}"
        if fmt not in ("markdown",):
            with p_lock:
                p.setdefault("failed_list", []).append(
                    {"id": doc_id, "title": title, "reason": f"未知格式: {fmt}"})
                p["failed"] = p.get("failed", 0) + 1
                p.setdefault("processed_doc_ids", []).append(doc_id)
            print(f"  🔄 [{doc_id}] {short_title}... unknown_format_{fmt}", flush=True)
            return f"unknown_format_{fmt}"

        # 空文档 / 无意义 / 二进制
        if not body or not body.strip():
            with p_lock:
                p.setdefault("skipped_empty", []).append({"doc_id": doc_id, "title": title})
                p["skipped"] = p.get("skipped", 0) + 1
                p.setdefault("processed_doc_ids", []).append(doc_id)
            print(f"  🔄 [{doc_id}] {short_title}... empty", flush=True)
            return "empty"
        if is_meaningless_doc(orig_title, body):
            with p_lock:
                p.setdefault("skipped_meaningless", []).append(
                    {"doc_id": doc_id, "title": title, "reason": "无意义文档"})
                p["skipped"] = p.get("skipped", 0) + 1
                p.setdefault("processed_doc_ids", []).append(doc_id)
            print(f"  🔄 [{doc_id}] {short_title}... skipped_meaningless", flush=True)
            return "skipped_meaningless"
        if is_binary(body):
            with p_lock:
                p.setdefault("skipped_binary", []).append({"doc_id": doc_id, "title": title})
                p["skipped"] = p.get("skipped", 0) + 1
                p.setdefault("processed_doc_ids", []).append(doc_id)
            print(f"  🔄 [{doc_id}] {short_title}... binary", flush=True)
            return "binary"

        # ── 阶段 2.5：去重检测 ──
        dup_result, dup_matched = check_duplicate(p, title, body, dedup_lock)
        if dup_result == "skip":
            with p_lock:
                p.setdefault("skipped_duplicates", []).append(
                    {"doc_id": doc_id, "title": title, "matched": dup_matched})
                p["skipped"] = p.get("skipped", 0) + 1
                p.setdefault("processed_doc_ids", []).append(doc_id)
            print(f"  🔄 [{doc_id}] {short_title}... dup_same(→{dup_matched})", flush=True)
            return "dup_same"
        need_new_title = (dup_result == "conflict")

        # ── 阶段 3：LLM 清洗 + 分类 + 重拟标题 ──
        cleaned, categories, new_title = llm_clean_and_classify(body, title, need_new_title)
        final_title = new_title if new_title else title
        if need_new_title and new_title:
            final_title = f"{title}（{new_title}）"
        elif need_new_title and not new_title:
            # LLM 未生成新标题，取正文前500字hash做确定性后缀防冲突
            body_hash = hashlib.md5(body[:500].encode('utf-8', errors='replace')).hexdigest()[:8]
            final_title = f"{title}（{body_hash}）"

        # ── 阶段 4：创建文档 + 挂目录 ──
        result2, status2, _ = api_post(f"/repos/{TARGET_ID}/docs", {
            "title": final_title, "body": cleaned, "format": "markdown"
        }, timeout=60)
        if result2 is None:
            if status2 == 429: return "rate_limit"
            with p_lock:
                p.setdefault("failed_list", []).append(
                    {"id": doc_id, "title": final_title, "reason": f"创建失败: {status2}"})
                p["failed"] = p.get("failed", 0) + 1
                p.setdefault("processed_doc_ids", []).append(doc_id)
            print(f"  🔄 [{doc_id}] {short_title}... create_failed", flush=True)
            return "create_failed"
        new_id = result2["data"]["id"]

        # 去重缓存更新
        with dedup_lock:
            cache = p.setdefault("_created_title_cache", {})
            if final_title not in cache:  # 二次检查防覆盖
                cache[final_title] = {
                    "doc_id": new_id,
                    "body_200": normalize_for_compare(cleaned[:200]),
                    "body_500": normalize_for_compare(cleaned[:500])
                }

        n_cats = len(categories)
        # 先更新进度（p_lock 毫秒级，不包含 IO）
        with p_lock:
            p.setdefault("created_doc_mapping", {})[str(doc_id)] = new_id
            bc2 = _book_counters(p)
            bc2["created"] += 1
            bc2["local_created"] += 1
            p.setdefault("processed_doc_ids", []).append(doc_id)

        # 再挂目录（只持 toc_lock，p_lock 已释放，不阻塞其他线程更新进度）
        with toc_lock:
            mount_docs_to_categories(p, [new_id], [(final_title, cleaned)], categories, p_lock)

        suffix = f"_cats{n_cats}"
        if need_new_title:
            suffix += "_renamed"
        res = f"created{suffix}"
        print(f"  🔄 [{doc_id}] {short_title}... {res}", flush=True)

        with error_lock:
            if res in FATAL_RESULTS or res.startswith("unknown_format_"):
                consecutive_errors += 1
            else:
                consecutive_errors = 0

        return res

    print(f"  🧵 并发数: {MAX_WORKERS}", flush=True)

    while offset < total:
        bc = _book_counters(p)
        current_total = initial_count + bc["local_created"]
        if current_total >= 4500:
            next_target = p.get('next_target')
            if next_target:
                print(f"\n🔄 目标库 {p['target_name']} 已满（{current_total}/5000），自动切换到 {next_target['book_name']} ({next_target['book_id']})", flush=True)
                # 记录旧目标
                if 'target_history' not in p: p['target_history'] = []
                p['target_history'].append({
                    'book_id': p['target_book_id'], 'book_name': p['target_name'],
                    'namespace': p['target_namespace'],
                    'created': bc['created'], 'local_created': bc['local_created'],
                    'multi_category_copies': bc['multi_category_copies']
                })
                # 切换到新目标（计数器由 _book_counters 自动初始化为 0）
                p['target_book_id'] = next_target['book_id']
                p['target_name'] = next_target['book_name']
                p['target_namespace'] = next_target['namespace']
                p['_created_title_cache'] = {}
                p['toc_map'] = {}
                p['initial_count'] = 0
                p['next_target'] = None
                TARGET_ID = next_target['book_id']
                TARGET_NS = next_target['namespace']
                # 获取新目标库初始文档数
                repo_result2, _, _ = api_get(f"/repos/{TARGET_ID}")
                if repo_result2:
                    init2 = repo_result2.get("data", {}).get("items_count", 0)
                    p['initial_count'] = init2
                    initial_count = init2
                    print(f"   新目标库初始: {init2} 篇", flush=True)
                save_progress(p)
                continue
            else:
                print(f"\n⚠️ 目标库已满（{current_total}/5000）且无备用目标，暂停！", flush=True)
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

        if not pending:
            offset += len(docs)
            p["last_offset"] = offset
            save_progress(p)
            continue

        _check_memory()

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process_one_doc, doc): doc for doc in pending}
            for future in as_completed(futures):
                doc = futures[future]
                try:
                    res = future.result()
                except Exception as e:
                    print(f"  ❌ [{doc['id']}] 线程异常: {e}", flush=True)
                    with p_lock:
                        p.setdefault("failed_list", []).append(
                            {"id": doc["id"], "title": doc["title"], "reason": f"线程异常: {e}"})
                        p["failed"] = p.get("failed", 0) + 1
                        p.setdefault("processed_doc_ids", []).append(doc["id"])

            if consecutive_errors > 10:
                print(f"\n❌ 连续 {consecutive_errors} 次致命错误，暂停。", flush=True)
                save_progress(p)
                return

            gc.collect()
            save_progress(p)
            time.sleep(0.1)

        with p_lock:
            all_done = all(d["id"] in p.get("processed_doc_ids", []) for d in docs)
            if all_done:
                offset += len(docs)
                p["last_offset"] = offset
            else:
                print(f"  ⚠️ 本批未完全处理，offset保持 {offset}", flush=True)
                p["last_offset"] = offset

        save_progress(p)
        bc4 = _book_counters(p)
        current_total = initial_count + bc4["local_created"]
        # ── RateLimit 变化追踪 ──
        global _last_remaining, _prev_logged_remaining
        rl_str = ""
        if _last_remaining is not None:
            rl_str = f" 剩余={_last_remaining}"
            # 显著下降时单独记录
            if _prev_logged_remaining is not None and _prev_logged_remaining - _last_remaining >= 50:
                print(f"  📉 RateLimit-Remaining: {_prev_logged_remaining} → {_last_remaining}", flush=True)
            _prev_logged_remaining = _last_remaining
        print(f"  📊 {offset}/{total} ({offset*100//total}%), "
              f"创={bc4['created']} 跳={p['skipped']} 败={p['failed']} "
              f"容量={current_total}/5000{rl_str}", flush=True)

    # 汇报
    bc5 = _book_counters(p)
    copies = bc5.get("multi_category_copies", 0)
    dup_count = len(p.get("skipped_duplicates", []))
    print(f"\n✅ 迁移完成！创={bc5['created']}(含多目录副本{copies}篇) "
          f"跳={p['skipped']}(含去重{dup_count}篇) 败={p['failed']}", flush=True)
    cats = len(p.get("toc_map", {}))
    if cats:
        print(f"📂 已建 {cats} 个目录", flush=True)
    orphan_count = len(p.get("orphans", []))
    if orphan_count:
        print(f"⚠️ {orphan_count} 篇孤儿文档（已创建但挂载失败）", flush=True)
    save_progress(p)

    # ── 生成 Markdown 汇总报告 ──
    report_path = generate_report(p)
    print(f"📄 汇总报告: {report_path}", flush=True)


if __name__ == "__main__":
    main()
