#!/usr/bin/env python3
"""语雀知识库批量迁移脚本 — 复制不搬，不做清洗，纯搬运"""
import subprocess, json, time, sys, os

def _load_token():
    import json, os
    cfg = os.path.expanduser("~/.openclaw/workspace/utils/yuque/yuque-ai/yuque-config.json")
    with open(cfg) as f:
        return json.load(f)["token"]
TOKEN = _load_token()
UA = "OpenClaw-Migration"
API = "https://www.yuque.com/api/v2"
SRC_BOOK = 65894942
TGT_BOOK = 78699632
TOTAL = 5200
BATCH = 100
PROGRESS_FILE = os.path.expanduser("~/.openclaw/workspace/utils/yuque/yuque-migration/progress/废弃0.json")

def api(method, path, body=None):
    url = f"{API}{path}"
    args = ["curl", "-s", "--max-time", "30", "-H", f"User-Agent: {UA}", "-H", f"X-Auth-Token: {TOKEN}"]
    if method == "POST":
        args += ["-X", "POST", "-H", "Content-Type: application/json"]
        if body:
            args += ["-d", json.dumps(body, ensure_ascii=False)]
    elif method == "PUT":
        args += ["-X", "PUT", "-H", "Content-Type: application/json"]
        if body:
            args += ["-d", json.dumps(body, ensure_ascii=False)]
    args.append(url)

    for attempt in range(3):
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=35)
            data = json.loads(r.stdout)
            if data.get("status") == 429:
                time.sleep(1.5)
                continue
            return data
        except:
            if attempt < 2:
                time.sleep(1)
    return {"status": -1, "message": "3 attempts failed"}

def load_progress():
    try:
        with open(PROGRESS_FILE) as f:
            p = json.load(f)
        for k in ["created", "skipped", "failed"]:
            if isinstance(p.get(k), list):
                p[k] = len(p[k])
            elif k not in p:
                p[k] = 0
        if "failed_list" not in p:
            p["failed_list"] = []
        return p
    except:
        return {"last_offset": 0, "skipped": 0, "created": 0, "failed": 0, "failed_list": []}

def save_progress(p):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)

def is_binary_body(body):
    """内容检测: 非ASCII+控制字符比例 > 25% 判定为二进制"""
    if not body:
        return False
    sample = body[:100]
    if len(sample) < 10:
        return False
    binary = sum(1 for c in sample if ord(c) > 127 or ord(c) < 32 or ord(c) == 127)
    return binary / len(sample) > 0.25

def main():
    p = load_progress()
    offset = p.get("last_offset", 0)

    print(f"从 offset={offset} 开始, 共 {TOTAL} 篇")
    print(f"已创建: {p.get('created', 0)}, 跳过: {p.get('skipped', 0)}, 失败: {p.get('failed', 0)}")

    while offset < TOTAL:
        res = api("GET", f"/repos/{SRC_BOOK}/docs?offset={offset}&limit={BATCH}")
        docs = res.get("data", [])
        if not docs:
            offset += BATCH
            p["last_offset"] = offset
            save_progress(p)
            continue

        print(f"\noffset={offset}: 处理 {len(docs)} 篇...")

        # 每批次前检查目标库容量
        if offset % 500 == 0:
            info = api("GET", f"/repos/{TGT_BOOK}")
            cnt = info.get("data", {}).get("items_count", 0)
            if cnt >= 5000:
                save_progress(p)
                print(f"\n⚠️ 目标库已达上限 {cnt}/5000 篇，请提供新目标库 ID，然后说「继续整理《废弃0》」")
                sys.exit(1)

        for i, doc in enumerate(docs):
            doc_id = doc["id"]
            title = doc["title"]

            # 获取原文
            raw = api("GET", f"/repos/{SRC_BOOK}/docs/{doc_id}?raw=1")
            ddata = raw.get("data", {})
            fmt = ddata.get("format", "unknown")
            raw_body = ddata.get("body", "")

            if fmt == "lake":
                body = raw_body.strip()
                if not body:
                    p["skipped"] = p.get("skipped", 0) + 1
                    continue
            elif fmt == "markdown":
                body = raw_body
            else:
                p["failed"] = p.get("failed", 0) + 1
                p.setdefault("failed_list", []).append({"id": doc_id, "title": title, "reason": f"未知格式: {fmt}"})
                continue

            # 二进制检测（内容采样，不靠标题）
            if is_binary_body(body):
                p["skipped"] = p.get("skipped", 0) + 1
                continue

            # 空文档
            if not body or not body.strip():
                p["skipped"] = p.get("skipped", 0) + 1
                continue

            # 创建到目标库
            create_res = api("POST", f"/repos/{TGT_BOOK}/docs", {
                "title": title,
                "body": body,
                "format": "markdown",
                "public": 0
            })

            if create_res.get("data"):
                p["created"] = p.get("created", 0) + 1
                if p["created"] % 50 == 0:
                    print(f"  已创建 {p['created']} 篇...")
            else:
                p["failed"] = p.get("failed", 0) + 1
                err = create_res.get("message", str(create_res))
                p.setdefault("failed_list", []).append({"id": doc_id, "title": title, "reason": err})

        offset += BATCH
        p["last_offset"] = offset
        save_progress(p)
        print(f"  💾 offset={offset} created={p['created']} skipped={p['skipped']} failed={p['failed']}")
        time.sleep(0.3)

    print(f"\n========== 完成 ==========")
    print(f"创建: {p['created']} | 跳过: {p['skipped']} | 失败: {p['failed']}")
    if p.get("failed_list"):
        print(f"失败明细 ({len(p['failed_list'])} 篇):")
        for f in p["failed_list"][:20]:
            print(f"  - {f['title'][:60]}: {f['reason']}")

if __name__ == "__main__":
    main()
