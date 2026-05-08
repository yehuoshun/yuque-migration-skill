#!/usr/bin/env python3
"""语雀知识库批量迁移脚本 — 复制不搬，不做清洗，纯搬运

功能：
  - 从源知识库批量复制文档到目标知识库
  - 自动检测并跳过二进制文件（非ASCII+控制字符>25%）
  - 支持 lake 格式文档（直接转为 markdown 链接）
  - 断点续传，进度保存到本地文件

配置：
  - 语雀 Token：环境变量 YUQUE_TOKEN 或配置文件 yuque-config.json
  - 源/目标库 ID：命令行参数或配置文件

用法：
  python migrate_batch.py --src 65894942 --tgt 78699632 --total 5200
  python migrate_batch.py --config migrate_config.json
"""
import subprocess, json, time, sys, os, argparse

# ── Token 加载 ─────────────────────────────────────────────────────────────
# 优先级：环境变量 YUQUE_TOKEN > 配置文件
# 配置文件路径：同级目录的 migrate_config.json 或指定的 --config 文件
def _load_token():
    """加载语雀 API Token，禁止硬编码在脚本中。"""
    token = os.environ.get("YUQUE_TOKEN", "")
    if token:
        return token
    # 尝试从配置文件加载
    cfg = os.path.expanduser("~/.openclaw/workspace/utils/yuque/yuque-ai/yuque-config.json")
    if os.path.exists(cfg):
        with open(cfg) as f:
            return json.load(f).get("token", "")
    raise RuntimeError("未找到语雀 Token。请设置环境变量 YUQUE_TOKEN 或确保配置文件存在。")

TOKEN = _load_token()
UA = "OpenClaw-Migration"
API = "https://www.yuque.com/api/v2"

# ── 配置加载 ─────────────────────────────────────────────────────────────
def _load_config():
    """加载迁移配置（源库、目标库、文档总数）。
    
    优先级：命令行参数 > 配置文件 > 报错
    
    Returns:
        dict: {src_book, tgt_book, total, progress_file}
    """
    parser = argparse.ArgumentParser(description="语雀知识库批量迁移")
    parser.add_argument("--src", type=int, help="源知识库 ID（从语雀 URL 获取）")
    parser.add_argument("--tgt", type=int, help="目标知识库 ID")
    parser.add_argument("--total", type=int, help="源库文档总数（用于进度显示）")
    parser.add_argument("--config", type=str, help="配置文件路径（JSON 格式）")
    parser.add_argument("--progress", type=str, help="进度文件保存路径")
    args = parser.parse_args()
    
    # 从命令行参数
    if args.src and args.tgt:
        return {
            "src_book": args.src,
            "tgt_book": args.tgt,
            "total": args.total or 5000,
            "progress_file": args.progress or f"~/.openclaw/workspace/utils/yuque/yuque-migration/progress/migrate_{args.src}_to_{args.tgt}.json"
        }
    
    # 从配置文件
    config_path = args.config or os.path.join(os.path.dirname(__file__), "migrate_config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            cfg = json.load(f)
        return {
            "src_book": cfg["src_book"],
            "tgt_book": cfg["tgt_book"],
            "total": cfg.get("total", 5000),
            "progress_file": cfg.get("progress_file", f"~/.openclaw/workspace/utils/yuque/yuque-migration/progress/migrate_{cfg['src_book']}_to_{cfg['tgt_book']}.json")
        }
    
    raise RuntimeError("请通过 --src/--tgt 或 --config 指定迁移配置。")

CONFIG = _load_config()
SRC_BOOK = CONFIG["src_book"]
TGT_BOOK = CONFIG["tgt_book"]
TOTAL = CONFIG["total"]
PROGRESS_FILE = os.path.expanduser(CONFIG["progress_file"])
BATCH = 100  # 每批处理文档数

# ── API 封装 ─────────────────────────────────────────────────────────────
def api(method, path, body=None):
    """发送语雀 API 请求，自动重试 3 次。
    
    Args:
        method: HTTP 方法 (GET/POST/PUT)
        path: API 路径，如 /repos/{book_id}/docs
        body: 请求体字典（POST/PUT 时）
    
    Returns:
        dict: API 响应数据
    """
    url = f"{API}{path}"
    args = ["curl", "-s", "--max-time", "30", 
            "-H", f"User-Agent: {UA}", 
            "-H", f"X-Auth-Token: {TOKEN}"]
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
            # 限流处理
            if data.get("status") == 429:
                time.sleep(1.5)
                continue
            return data
        except:
            if attempt < 2:
                time.sleep(1)
    return {"status": -1, "message": "3 attempts failed"}

# ── 进度管理 ─────────────────────────────────────────────────────────────
def load_progress():
    """加载断点续传进度。"""
    try:
        with open(PROGRESS_FILE) as f:
            p = json.load(f)
        # 兼容旧格式
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
    """保存进度到文件。"""
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)

# ── 文档处理 ─────────────────────────────────────────────────────────────
def is_binary_body(body):
    """检测是否为二进制内容（非文本文件）。
    
    通过采样前 100 字符，统计非 ASCII + 控制字符比例。
    若 > 25% 则判定为二进制，跳过迁移。
    """
    if not body:
        return False
    sample = body[:100]
    if len(sample) < 10:
        return False
    binary = sum(1 for c in sample if ord(c) > 127 or ord(c) < 32 or ord(c) == 127)
    return binary / len(sample) > 0.25

# ── 主流程 ─────────────────────────────────────────────────────────────
def main():
    p = load_progress()
    offset = p.get("last_offset", 0)

    print(f"源库 ID: {SRC_BOOK}")
    print(f"目标库 ID: {TGT_BOOK}")
    print(f"从 offset={offset} 开始，共 {TOTAL} 篇")
    print(f"已创建: {p.get('created', 0)}, 跳过: {p.get('skipped', 0)}, 失败: {p.get('failed', 0)}")

    while offset < TOTAL:
        # 分页获取文档列表
        res = api("GET", f"/repos/{SRC_BOOK}/docs?offset={offset}&limit={BATCH}")
        docs = res.get("data", [])
        if not docs:
            offset += BATCH
            p["last_offset"] = offset
            save_progress(p)
            continue

        print(f"\noffset={offset}: 处理 {len(docs)} 篇...")

        # 每 500 篇检查目标库容量
        if offset % 500 == 0:
            info = api("GET", f"/repos/{TGT_BOOK}")
            cnt = info.get("data", {}).get("items_count", 0)
            if cnt >= 5000:
                save_progress(p)
                print(f"\n⚠️ 目标库已达上限 {cnt}/5000 篇，请提供新目标库 ID")
                sys.exit(1)

        for i, doc in enumerate(docs):
            doc_id = doc["id"]
            title = doc["title"]

            # 获取原文内容
            raw = api("GET", f"/repos/{SRC_BOOK}/docs/{doc_id}?raw=1")
            ddata = raw.get("data", {})
            fmt = ddata.get("format", "unknown")
            raw_body = ddata.get("body", "")

            # lake 格式：直接使用 body（已是 markdown 链接文本）
            if fmt == "lake":
                body = raw_body.strip()
                if not body:
                    p["skipped"] = p.get("skipped", 0) + 1
                    continue
            elif fmt == "markdown":
                body = raw_body
            else:
                p["failed"] = p.get("failed", 0) + 1
                p.setdefault("failed_list", []).append({
                    "id": doc_id, "title": title, "reason": f"未知格式: {fmt}"
                })
                continue

            # 跳过二进制文件
            if is_binary_body(body):
                p["skipped"] = p.get("skipped", 0) + 1
                continue

            # 跳过空文档
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
                p.setdefault("failed_list", []).append({
                    "id": doc_id, "title": title, "reason": err
                })

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