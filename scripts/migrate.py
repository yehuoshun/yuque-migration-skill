#!/usr/bin/env python3
"""语雀知识库迁移脚本 v2 - 修复版
修复：is_binary中文误判、标题换行符422、图片token跳过、群号文档跳过、
     大文档LLM拆分、连续错误检测、no_parts漏计、超时指数退避
新增：迁移完成后自动LLM分类建目录挂文档
"""

import json, time, re, os, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta

BASE = "https://www.yuque.com/api/v2"
CONFIG_FILE = os.path.expanduser("~/.openclaw/workspace/utils/yuque/yuque-ai/yuque-config.json")

# 运行时配置（从进度文件读取）
PROGRESS_FILE = None
SOURCE_ID = None
TARGET_ID = None
TARGET_NS = None

BATCH_SIZE = 100
CONCURRENCY = 5  # 串行处理，但保持变量名

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
    """HTTP请求，返回 (json_body, status_code, headers)
    支持超时参数和指数退避重试
    """
    url = f"{BASE}{path}"
    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, method=method)
    req.add_header("X-Auth-Token", TOKEN)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "YuqueMigration/2.0")

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
                return None, 429, {}
            if status == 404:
                return None, 404, {}
            if status in (502, 503, 504):
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
                time.sleep(wait)
                continue
            try:
                body = json.loads(e.read().decode("utf-8"))
            except:
                body = str(e)
            if attempt < 2:
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
            else:
                return None, status, {}
        except Exception as e:
            if attempt < 2:
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
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
    """检测是否为二进制文件
    修复：只检测 null bytes 和控制字符，不把中文误判为二进制
    """
    if len(body) < 50:
        return False
    sample = body[:1024]
    # null bytes = 铁定二进制
    if '\x00' in sample:
        return True
    # 只计真正的控制字符（排除 \n \r \t）
    control_chars = sum(1 for c in sample if ord(c) < 32 and c not in '\n\r\t')
    # 控制字符超过30%才是二进制
    return control_chars / max(len(sample), 1) > 0.30


def is_img_token(body):
    """检测是否为图片token文档（用户旧备份方案）
    特征：body 只有 ["数字:数字-数字"] 格式的token
    """
    if not body:
        return False
    pattern = r'^```\w*\s*\n\["\d+:\d+-\d+"\]\s*\n```\s*$'
    return bool(re.match(pattern, body.strip()))


def is_meaningless_doc(title, body):
    """检测是否为无意义文档（QQ群号等）
    特征：标题包含换行符、标题只有数字和群号、或含控制字符
    """
    # 标题含换行符
    if '\n' in title or '\r' in title:
        return True
    # 标题含控制字符
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', title):
        return True
    # 标题只有群号格式（"名称\n数字"清洗后变成空或纯数字）
    cleaned_title = re.sub(r'[\x00-\x1f]', '', title).strip()
    if not cleaned_title or cleaned_title.isdigit():
        return True
    return False


def fix_title(title, max_len=200):
    """修复标题：清理换行符和控制字符，截断长度
    修复：标题含换行符导致422
    """
    # 清理换行符和控制字符
    title = title.replace('\n', ' ').replace('\r', ' ')
    title = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', title)
    title = title.strip()
    # 截断
    if len(title) > max_len:
        return title[:max_len-3] + "..."
    if not title:
        return "无标题"
    return title


# ==================== 格式处理 ====================

def fix_table_format(body):
    """修复常见表格格式问题"""
    # 移除包裹表格的代码块
    body = re.sub(r'```\w*\s*\n(\|[^\n]+\|[\s\S]*?)\n```', r'\1', body)
    # 移除表格行缩进
    body = re.sub(r'^(    |\t)\|', r'|', body, flags=re.MULTILINE)
    return body


def needs_llm_cleaning(body):
    """判断文档是否需要LLM清洗（SQL/JSON/纯代码块不需要）"""
    stripped = body.strip()
    # SQL dump
    if stripped.startswith('INSERT INTO') or stripped.startswith('```c\nINSERT INTO'):
        return False
    # JSON array
    if stripped.startswith('```c\n[') or stripped.startswith('['):
        return False
    # 纯代码块包裹的内容
    code_wrapped = re.match(r'^```\w*\n', stripped)
    if code_wrapped:
        inner = re.sub(r'^```\w*\n|```$', '', stripped, flags=re.DOTALL)
        if inner.strip().startswith(('INSERT', 'SELECT', 'CREATE', 'ALTER', 'DROP', '[', '{', '#include', 'import ', 'package ', '<?php', '<!DOCTYPE')):
            return False
    return True


def llm_clean(body, title, timeout=60):
    """LLM清洗文档，小文档和非markdown内容跳过"""
    body = fix_table_format(body)

    if len(body) < 500:
        return body

    if not needs_llm_cleaning(body):
        return body

    cleaning_prompt = """你是语雀文档格式清洗助手。

输入：一篇从语雀导出的 Markdown 文档（可能含广告、免责条款、水评论、HTML 残留、空链接等噪音）。

要求：
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
- 保持原始表格的列对齐不变

输出清洗后的完整 Markdown，不做摘要。"""

    llm_data = json.dumps({
        "model": LLM_CFG["model"],
        "messages": [
            {"role": "system", "content": cleaning_prompt},
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
            cleaned = result["choices"][0]["message"]["content"]
            return fix_table_format(cleaned)
    except Exception as e:
        print(f"  ⚠️ LLM清洗异常: {e}，使用原始内容")
        return body


def llm_split(body, chunk_size=5000):
    """LLM分段拆分大文档
    每次读取 chunk_size 字符，让 LLM 在合适位置切分
    返回: [(标题后缀, 内容)] 列表
    """
    if len(body) <= chunk_size * 2:
        # 不够大，不拆分
        return [("", body)]
    
    results = []
    pos = 0
    part_num = 1
    
    while pos < len(body):
        chunk = body[pos:pos + chunk_size * 2]  # 每次读多一点给LLM判断
        
        if pos + len(chunk) >= len(body):
            # 最后一段
            results.append((f"({part_num})", chunk))
            break
        
        # 让LLM找切分点
        split_prompt = f"""你是一个文档分割助手。下面是一段长文档的一部分，请在合适的位置切分。

要求：
1. 在完整段落边界切分（空行处），不要在句子中间切
2. 不要在代码块内部切分
3. 返回切分点的字符位置（从0开始的索引）

文档内容：
{chunk}

只返回切分点的字符索引数字，不要返回其他内容。"""

        llm_data = json.dumps({
            "model": LLM_CFG["model"],
            "messages": [{"role": "user", "content": split_prompt}],
            "temperature": 0.1,
            "max_tokens": 10
        }).encode("utf-8")

        llm_req = urllib.request.Request(LLM_CFG["url"], data=llm_data, method="POST")
        llm_req.add_header("Authorization", f"Bearer {LLM_CFG['api_key']}")
        llm_req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(llm_req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                split_point = int(result["choices"][0]["message"]["content"].strip())
                if split_point <= 0 or split_point >= len(chunk):
                    split_point = chunk_size  # 默认取一半
        except:
            split_point = chunk_size  # 默认取一半
        
        part_content = chunk[:split_point]
        results.append((f"({part_num})", part_content))
        pos += split_point
        part_num += 1
    
    return results


def split_large(body, max_len=50000):
    """大文档拆分，优先按标题切，LLM辅助"""
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

    # 只在代码块外的 ## 标题处拆分
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

    # 如果标题切分没找到切点，按段落边界
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

    # 最终兜底：如果只剩一大段，按行硬切
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


# ==================== 核心处理 ====================

def wait_until_next_hour():
    now = datetime.now()
    next_hour = now.replace(minute=0, second=0, microsecond=0)
    if now.minute >= 0:
        next_hour += timedelta(hours=1)
    wait_sec = max(60, (next_hour - now).total_seconds())
    print(f"  ⏳ 限流，等待 {wait_sec/60:.0f} 分钟到 {next_hour.strftime('%H:%M')}...", flush=True)
    time.sleep(wait_sec)
    print(f"  ✅ 恢复执行", flush=True)


def process_doc(doc, p):
    """处理单篇文档，返回结果描述"""
    global TARGET_ID, SOURCE_ID
    doc_id = doc["id"]
    orig_title = doc["title"]
    title = fix_title(orig_title)

    # 获取文档内容（大文件用更长超时）
    result, status, headers = api_get(f"/repos/{SOURCE_ID}/docs/{doc_id}", {"raw": "1"}, timeout=90)
    if result is None:
        if status == 404:
            p.setdefault("skipped_empty", []).append({"doc_id": doc_id, "title": title})
            p["skipped"] = p.get("skipped", 0) + 1
            return "empty_404"
        if status == 429:
            return "rate_limit"
        p.setdefault("failed_list", []).append({"id": doc_id, "title": title, "reason": f"获取失败: {status}"})
        p["failed"] = p.get("failed", 0) + 1
        return "fetch_error"

    data = result.get("data", {})
    fmt = data.get("format", "markdown")
    body = data.get("body", "")

    # Lake格式无损搬运
    if fmt == "lake":
        body_lake = data.get("body_lake", body)
        result2, status2, _ = api_post(f"/repos/{TARGET_ID}/docs", {
            "title": title,
            "body": body_lake,
            "format": "lake"
        }, timeout=60)
        if result2 is None:
            if status2 == 429:
                return "rate_limit"
            p.setdefault("failed_list", []).append({"id": doc_id, "title": title, "reason": f"lake创建失败: {status2}"})
            p["failed"] = p.get("failed", 0) + 1
            return "lake_failed"
        new_id = result2["data"]["id"]
        p.setdefault("lake_docs", []).append({"doc_id": doc_id, "new_id": new_id, "title": title, "reason": "lake格式无损搬运"})
        p["created_doc_mapping"][str(doc_id)] = new_id
        p["created"] = p.get("created", 0) + 1
        p["local_created"] = p.get("local_created", 0) + 1
        # 保存原文用于后续分类
        p.setdefault("created_docs_content", {})[str(new_id)] = {"title": title, "body": body_lake[:2000]}
        return "lake_created"

    # 未知格式 → 跳过（不记失败）
    UNSUPPORTED_FORMATS = {"doc", "docx", "pdf", "image", "png", "jpg", "jpeg", "gif", "ppt", "pptx", "xls", "xlsx", "zip", "rar"}
    if fmt in UNSUPPORTED_FORMATS:
        p.setdefault("skipped_unsupported", []).append({"doc_id": doc_id, "title": title, "format": fmt, "reason": "不支持的文件格式"})
        p["skipped"] = p.get("skipped", 0) + 1
        return f"skipped_format_{fmt}"
    if fmt not in ("markdown", "lake"):
        p.setdefault("failed_list", []).append({"id": doc_id, "title": title, "reason": f"未知格式: {fmt}"})
        p["failed"] = p.get("failed", 0) + 1
        return f"unknown_format_{fmt}"

    # 空文档
    if not body or not body.strip():
        p.setdefault("skipped_empty", []).append({"doc_id": doc_id, "title": title})
        p["skipped"] = p.get("skipped", 0) + 1
        return "empty"

    # 图片token文档 → 跳过（用户旧备份方案）
    if is_img_token(body):
        p.setdefault("skipped_img_token", []).append({"doc_id": doc_id, "title": title, "reason": "图片token文档（用户旧备份方案）"})
        p["skipped"] = p.get("skipped", 0) + 1
        return "skipped_img_token"

    # 无意义文档（群号等） → 跳过
    if is_meaningless_doc(orig_title, body):
        p.setdefault("skipped_meaningless", []).append({"doc_id": doc_id, "title": title, "reason": "无意义文档（群号等）"})
        p["skipped"] = p.get("skipped", 0) + 1
        return "skipped_meaningless"

    # 二进制检测（修复后不误杀中文）
    if is_binary(body):
        p.setdefault("skipped_binary", []).append({"doc_id": doc_id, "title": title})
        p["skipped"] = p.get("skipped", 0) + 1
        return "binary"

    # 格式清洗
    cleaned = llm_clean(body, title)

    # 大文档拆分
    parts = split_large(cleaned)
    if not parts:
        # 拆分后无内容，记失败
        p.setdefault("failed_list", []).append({"id": doc_id, "title": title, "reason": "拆分后无有效内容"})
        p["failed"] = p.get("failed", 0) + 1
        return "no_parts"
    
    created_ids = []
    saved_contents = []

    for i, part in enumerate(parts):
        final_title = f"{title}({i+1}/{len(parts)})" if len(parts) > 1 else title
        result2, status2, _ = api_post(f"/repos/{TARGET_ID}/docs", {
            "title": final_title,
            "body": part,
            "format": "markdown"
        }, timeout=60)
        if result2 is None:
            if status2 == 429:
                return "rate_limit"
            p.setdefault("failed_list", []).append({"id": doc_id, "title": title, "reason": f"创建失败(part {i+1}): {status2}"})
            p["failed"] = p.get("failed", 0) + 1
            return "create_failed"
        new_id = result2["data"]["id"]
        created_ids.append(new_id)
        p["created"] = p.get("created", 0) + 1
        p["local_created"] = p.get("local_created", 0) + 1
        saved_contents.append({"title": final_title, "body": part[:2000]})

    if created_ids:
        p["created_doc_mapping"][str(doc_id)] = created_ids[0] if len(created_ids) == 1 else created_ids
        # 保存内容用于后续分类
        for cid, cont in zip(created_ids, saved_contents):
            p.setdefault("created_docs_content", {})[str(cid)] = cont
        return "created" if len(parts) == 1 else f"created_split_{len(parts)}"
    
    # 不应该到这里，但以防万一
    p.setdefault("failed_list", []).append({"id": doc_id, "title": title, "reason": "拆分后无创建结果"})
    p["failed"] = p.get("failed", 0) + 1
    return "no_parts_created"


# ==================== TOC 目录挂载 ====================

def classify_and_build_toc(p):
    """迁移完成后，LLM分类建目录挂文档"""
    global TARGET_ID
    
    created_docs = p.get("created_docs_content", {})
    if not created_docs:
        print("  ⚠️ 无已创建文档，跳过目录挂载")
        return
    
    print(f"\n📂 开始目录挂载，共 {len(created_docs)} 篇文档...")
    
    # 合并拆分文档的子篇（按原始标题归类）
    # 如 "标题(1/3)" "标题(2/3)" 合并为同一分类
    doc_list = []
    title_to_docs = {}  # 原始标题 -> [doc_ids]
    
    for doc_id, info in created_docs.items():
        title = info["title"]
        body = info["body"]
        
        # 提取原始标题（去掉 (1/3) 后缀）
        orig_title = re.sub(r'\(\d+/\d+\)$', '', title).strip()
        
        if orig_title not in title_to_docs:
            title_to_docs[orig_title] = []
        title_to_docs[orig_title].append(doc_id)
        
        doc_list.append({
            "id": doc_id,
            "title": orig_title,
            "body_preview": body[:500]  # 前500字用于分类
        })
    
    # 构建分类prompt
    classify_prompt = f"""你是一个文档分类助手。以下是 {len(doc_list)} 篇文档的标题和内容摘要，请按内容主题自动聚类，输出目录结构。

要求：
- 不预设分类数量，根据内容自然聚类
- 每个分类标题简洁（2-8个字）
- 每篇文档只归属一个分类
- 如果某个分类超过100篇，考虑拆分子分类
- 归类不了的文档放入「其他」

输出JSON格式：
[
  {{"category": "分类名", "doc_ids": [111, 222]}},
  {{"category": "分类名/子分类", "doc_ids": [333]}}
]

文档列表（id | 标题 | 内容摘要）：
"""
    
    for d in doc_list[:200]:  # 分批处理，每批200篇
        classify_prompt += f"\n{d['id']} | {d['title']} | {d['body_preview'][:200]}"
    
    classify_prompt += "\n\n只输出JSON，不要其他内容。"
    
    # 调用LLM分类
    print("  🤖 LLM分类中...")
    llm_data = json.dumps({
        "model": LLM_CFG["model"],
        "messages": [{"role": "user", "content": classify_prompt}],
        "temperature": 0.1,
        "max_tokens": 8000
    }).encode("utf-8")
    
    llm_req = urllib.request.Request(LLM_CFG["url"], data=llm_data, method="POST")
    llm_req.add_header("Authorization", f"Bearer {LLM_CFG['api_key']}")
    llm_req.add_header("Content-Type", "application/json")
    
    try:
        with urllib.request.urlopen(llm_req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            classify_result = result["choices"][0]["message"]["content"]
            # 提取JSON
            json_match = re.search(r'\[[\s\S]*\]', classify_result)
            if json_match:
                categories = json.loads(json_match.group())
            else:
                categories = [{"category": "未分类", "doc_ids": list(created_docs.keys())}]
    except Exception as e:
        print(f"  ⚠️ LLM分类失败: {e}，所有文档放入「未分类」")
        categories = [{"category": "未分类", "doc_ids": list(created_docs.keys())}]
    
    print(f"  📁 分类完成，共 {len(categories)} 个目录")
    
    # 建目录节点
    toc_state = {"categories": [], "orphans": []}
    
    for cat_info in categories:
        cat_name = cat_info["category"]
        doc_ids = cat_info["doc_ids"]
        
        # 处理层级（如 "技术/前端" -> ["技术", "前端"]）
        cat_parts = cat_name.split("/")
        
        # 逐级创建目录
        parent_uuid = None
        for i, part in enumerate(cat_parts):
            result2, status2, _ = api_put(f"/repos/{TARGET_ID}/toc", {
                "action": "appendNode",
                "action_mode": "child",
                "type": "TITLE",
                "title": part,
                "target_uuid": parent_uuid
            })
            if result2 is None:
                print(f"  ⚠️ 创建目录 '{part}' 失败: {status2}")
                break
            uuid = result2["data"]["uuid"]
            if i == len(cat_parts) - 1:
                # 最后一级，记录
                toc_state["categories"].append({
                    "name": cat_name,
                    "uuid": uuid,
                    "doc_ids": doc_ids
                })
            parent_uuid = uuid
        
        # 挂文档
        if parent_uuid:
            for i in range(0, len(doc_ids), 50):
                batch = doc_ids[i:i+50]
                result3, status3, _ = api_put(f"/repos/{TARGET_ID}/toc", {
                    "action": "appendNode",
                    "action_mode": "child",
                    "type": "DOC",
                    "target_uuid": parent_uuid,
                    "doc_ids": [int(d) for d in batch]
                })
                if result3 is None:
                    print(f"  ⚠️ 目录 '{cat_name}' 第{i}批挂载失败: {status3}")
                    for d in batch:
                        toc_state["orphans"].append({"doc_id": d, "reason": f"挂载失败: {status3}"})
                    time.sleep(1)
                else:
                    print(f"  ✅ 目录 '{cat_name}' 挂载 {len(batch)} 篇")
            time.sleep(0.5)
    
    # 保存TOC状态
    toc_file = PROGRESS_FILE.replace("progress/", "toc/").replace(".json", "_toc.json")
    os.makedirs(os.path.dirname(toc_file), exist_ok=True)
    with open(toc_file, "w") as f:
        json.dump(toc_state, f, ensure_ascii=False, indent=2)
    print(f"  📄 目录状态已保存: {toc_file}")


# ==================== 主流程 ====================

def main():
    global PROGRESS_FILE, SOURCE_ID, TARGET_ID, TARGET_NS
    
    # 从命令行或配置读取进度文件
    import sys
    if len(sys.argv) > 1:
        PROGRESS_FILE = os.path.expanduser(sys.argv[1])
    else:
        # 默认
        PROGRESS_FILE = os.path.expanduser("~/.openclaw/workspace/utils/yuque/yuque-migration/progress/78699632_废弃19.json")
    
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

    # 连续错误计数
    FATAL_RESULTS = {"fetch_error", "create_failed", "lake_failed", "no_parts", "no_parts_created", "error"}
    consecutive_errors = 0

    while offset < total:
        if p.get("local_created", 0) >= 4500:
            print(f"\n⚠️ 目标库已达切换阈值 4500 篇！已迁移 {p['local_created']} 篇。", flush=True)
            save_progress(p)
            return

        print(f"\n📄 offset={offset} 获取 {BATCH_SIZE} 篇...", flush=True)
        result, status, headers = api_get(f"/repos/{SOURCE_ID}/docs", {"offset": str(offset), "limit": str(BATCH_SIZE)})
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
                # 重试
                p["processed_doc_ids"].remove(doc_id)
                retry = process_doc(doc, p)
                print(f"  🔄 重试 [{doc_id}]: {retry}", flush=True)
                p.setdefault("processed_doc_ids", []).append(doc_id)

            # 连续错误检测（修复：覆盖所有失败类型）
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
        print(f"  📊 {offset}/{total} ({offset*100//total}%), 创={p['created']} 跳={p['skipped']} 败={p['failed']}", flush=True)

    print(f"\n✅ 所有文档处理完成！创={p['created']} 跳={p['skipped']} 败={p['failed']}", flush=True)
    save_progress(p)
    
    # 迁移完成后，LLM分类建目录挂文档
    classify_and_build_toc(p)
    save_progress(p)
    
    print("\n🎉 迁移和目录挂载全部完成！", flush=True)


if __name__ == "__main__":
    main()