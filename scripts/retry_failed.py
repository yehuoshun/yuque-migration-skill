#!/usr/bin/env python3
"""重试迁移失败文档 — 通用版

读取进度文件中的 failed_list，从源库重新拉取、清洗、创建到目标库。

用法：
  python retry_failed.py --progress progress.json
  python retry_failed.py --progress progress.json --src 123 --tgt 456
"""

import json, os, sys, time, re, ssl, argparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ── Token 加载 ──
def load_token(token_config=None):
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
    raise RuntimeError("未找到语雀 Token")

API_BASE = "https://www.yuque.com/api/v2"
SSL_CTX = ssl.create_default_context()
TOKEN = None
MAX_CHARS = 50000
MAX_RETRIES = 3


def api_request(method, path, body=None, timeout=30):
    """发送语雀 API 请求，自动重试+限流。"""
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {
        "X-Auth-Token": TOKEN,
        "Content-Type": "application/json",
        "User-Agent": "OpenClaw-Retry/2.0",
    }
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(url, data=data, headers=headers, method=method)
            with urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                return json.loads(resp.read().decode("utf-8")), True
        except HTTPError as e:
            remaining = -1
            try:
                remaining = int(e.headers.get("X-RateLimit-Remaining", -1))
            except Exception:
                pass
            if e.code == 429:
                if remaining == 0:
                    wait = 3600 - (time.localtime().tm_min * 60 + time.localtime().tm_sec) + 5
                    print(f"  ⏳ 小时限流，等 {wait}s...")
                    time.sleep(min(wait, 3600))
                    continue
                time.sleep(1.5)
                continue
            if e.code == 404:
                return None, False
            if e.code >= 500:
                time.sleep(1)
                continue
            return None, False
        except (URLError, OSError):
            time.sleep(1)
    return None, False


def clean_markdown(body):
    """基础 HTML 清洗（非 LLM）。"""
    if not body or not body.strip():
        return body
    c = body
    c = re.sub(r'<!--.*?-->', '', c, flags=re.DOTALL)
    for tag in ['div', 'span', 'font', 'center']:
        c = re.sub(rf'<{tag}[^>]*>', '', c)
        c = re.sub(rf'</{tag}>', '', c)
    c = re.sub(r'<br\s*/?>', '\n', c)
    c = re.sub(r'\[\]\(\)', '', c)
    c = re.sub(r'\[\]\(([^)]+)\)', r'[\1](\1)', c)
    c = re.sub(r'\n{4,}', '\n\n\n', c)
    lines = c.split('\n')
    in_code = False
    out = []
    for line in lines:
        if line.strip().startswith('```'):
            in_code = not in_code
            out.append(line)
        elif in_code:
            out.append(line)
        else:
            out.append(line.rstrip())
    return '\n'.join(out)


def count_chars_no_code(content):
    text = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
    text = re.sub(r'`[^`]+`', '', text)
    return len(text)


def split_large_doc(title, body):
    if count_chars_no_code(body) <= MAX_CHARS:
        return [(title, body)]
    sections = re.split(r'(?=^## )', body, flags=re.MULTILINE)
    if len(sections) <= 1:
        sections = re.split(r'(?=^### )', body, flags=re.MULTILINE)
    if len(sections) <= 1:
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


def main():
    global TOKEN

    parser = argparse.ArgumentParser(description="重试语雀迁移失败文档")
    parser.add_argument("--progress", type=str, required=True, help="进度文件路径")
    parser.add_argument("--src", type=int, help="源知识库 ID（默认从进度文件读取）")
    parser.add_argument("--tgt", type=int, help="目标知识库 ID（默认从进度文件读取）")
    parser.add_argument("--token-config", type=str, help="Token 配置文件路径")
    args = parser.parse_args()

    TOKEN = load_token(args.token_config)

    # 加载进度
    progress_path = os.path.expanduser(args.progress)
    with open(progress_path) as f:
        p = json.load(f)

    failed = p.get("failed_list", [])
    if not failed:
        print("✅ 没有失败文档")
        return

    src_book = args.src or p.get("source_book_id")
    tgt_book = args.tgt or p.get("target_book_id")
    if not src_book or not tgt_book:
        print("❌ 无法确定源/目标库，请用 --src --tgt 指定")
        sys.exit(1)

    print(f"🔁 重试 {len(failed)} 篇失败文档")
    print(f"   源库: {src_book} → 目标库: {tgt_book}\n")

    success = 0
    retry_failed = 0

    for i, item in enumerate(failed):
        doc_id = item["id"]
        title = item.get("title", "无标题")
        print(f"[{i+1}/{len(failed)}] {title[:60]}", end=" ", flush=True)

        # 从源库获取
        raw, ok = api_request("GET", f"/repos/{src_book}/docs/{doc_id}?raw=1")
        if not raw or "data" not in raw:
            print("❌ 获取失败")
            retry_failed += 1
            continue

        ddata = raw["data"]
        body = ddata.get("body", "")
        if not body or not body.strip():
            print("⏭️ 空文档")
            continue

        fmt = ddata.get("format", "markdown")
        if fmt == "lake":
            cleaned = body
        elif fmt == "markdown":
            cleaned = clean_markdown(body)
        else:
            print(f"❌ 格式 {fmt}")
            retry_failed += 1
            continue

        # 大文档拆分
        parts = split_large_doc(title, cleaned)
        all_ok = True
        for part_title, part_body in parts:
            # 不手动指定 slug，由语雀自动生成
            r, ok = api_request("POST", f"/repos/{tgt_book}/docs", {
                "title": part_title,
                "public": 0,
                "body": part_body,
                "format": "markdown",
            })
            if r and r.get("data"):
                continue
            else:
                all_ok = False

        if all_ok:
            print("✅")
            success += 1
            # 从失败列表移除
            p["failed_list"] = [f for f in p["failed_list"] if f["id"] != doc_id]
            p["failed"] = max(0, p.get("failed", 0) - 1)
        else:
            print("❌")
            retry_failed += 1

        # 每 10 篇保存一次
        if (i + 1) % 10 == 0:
            with open(progress_path, "w") as f:
                json.dump(p, f, ensure_ascii=False, indent=2)
            print(f"  💾 已保存进度")

    # 最终保存
    with open(progress_path, "w") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}")
    print(f"🔁 重试完成")
    print(f"   成功: {success}")
    print(f"   再次失败: {retry_failed}")
    print(f"   剩余失败: {len(p.get('failed_list', []))}")


if __name__ == "__main__":
    main()
