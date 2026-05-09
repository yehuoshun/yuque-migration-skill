#!/usr/bin/env python3
"""DingTalk notification for GitHub repo events — 中英双语 Markdown 模板."""
import json, os, re, urllib.request

WEBHOOK = os.environ["DINGTALK_WEBHOOK"]
EVENT_NAME = os.environ["GITHUB_EVENT_NAME"]
EVENT_PATH = os.environ["GITHUB_EVENT_PATH"]
REPO = os.environ.get("GITHUB_REPOSITORY", "?")

with open(EVENT_PATH) as f:
    ev = json.load(f)

# ── helpers ──────────────────────────────────────────────

COMMIT_EMOJI = {
    "feat": "✨", "fix": "🐛", "docs": "📝", "style": "💄",
    "refactor": "♻️", "test": "✅", "chore": "🔧", "perf": "⚡",
    "ci": "👷", "build": "📦", "revert": "⏪", "merge": "🔀",
    "wip": "🚧",
}

def _emoji(msg):
    m = re.match(r"(\w+)[(:]", msg)
    return COMMIT_EMOJI.get((m.group(1) if m else "").lower(), "•")

def _truncate(text, n=300):
    if not text: return ""
    text = text.strip()
    return text if len(text) <= n else text[:n].rsplit("\n", 1)[0] + "\n> ⋯"


# ── builders ─────────────────────────────────────────────

def push():
    ref = os.environ["GITHUB_REF_NAME"]
    actor = os.environ["GITHUB_ACTOR"]
    compare = ev.get("compare", "")
    commits = ev.get("commits", [])
    total = len(commits)

    lines = []
    seen = set()
    for c in commits[:5]:
        msg = c.get("message", "").split("\n")[0][:80]
        author = c.get("author", {}).get("name", "?")
        key = f"{msg}|{author}"
        if key in seen: continue
        seen.add(key)
        lines.append(f"> {_emoji(msg)} {msg}  — *{author}*")

    commit_text = "\n".join(lines)
    if total > 5:
        commit_text += f"\n> ⋯ 共 **{total}** 条 / *{total} commits*"

    title = f"Push · {REPO}"
    text = f"""## 🚀 代码推送 · Code Push  

**仓库** / *Repo*: {REPO}  
**分支** / *Branch*: {ref}  
**提交者** / *Author*: **{actor}**  
**提交数** / *Commits*: **{total}**  

{commit_text}  

[📎 查看变更 / View diff]({compare})  

> GitHub"""
    return title, text


def pull_request():
    pr = ev.get("pull_request", {})
    action = ev.get("action", "?")
    number = pr.get("number", "?")

    action_label = {
        "opened":   "🟢 新建 / Opened",
        "closed":   "🔴 关闭 / Closed",
        "reopened": "🔄 重新打开 / Reopened",
    }.get(action, f"📌 {action}")
    if action == "closed" and pr.get("merged"):
        action_label = "🟣 已合并 / Merged"

    user = pr.get("user", {}).get("login", "?")
    head = pr.get("head", {}).get("ref", "?")
    base = pr.get("base", {}).get("ref", "?")
    url = pr.get("html_url", "")
    body = _truncate(pr.get("body", ""))
    labels_list = [l["name"] for l in (pr.get("labels") or [])]
    label_str = " · ".join(f"`{l}`" for l in labels_list) if labels_list else "—"

    title = f"PR {action} · {REPO}"
    text = f"""## {action_label}  

**{pr.get('title', '?')}**  

- **作者** / *Author*: **{user}**  
- **分支** / *Branch*: {head} → {base}  
- **标签** / *Labels*: {label_str}"""

    if body:
        text += f"\n\n{body}"

    text += f"\n\n[📎 查看详情 / View PR]({url})  \n\n> GitHub"
    return title, text


def issues():
    issue = ev.get("issue", {})
    action = ev.get("action", "?")
    number = issue.get("number", "?")

    action_label = {
        "opened":   "📝 新建 / Opened",
        "closed":   "✅ 关闭 / Closed",
        "reopened": "🔄 重新打开 / Reopened",
    }.get(action, f"📌 {action}")

    user = issue.get("user", {}).get("login", "?")
    url = issue.get("html_url", "")
    body = _truncate(issue.get("body", ""))
    labels_list = [l["name"] for l in (issue.get("labels") or [])]
    label_str = " · ".join(f"`{l}`" for l in labels_list) if labels_list else "—"

    title = f"Issue {action} · {REPO}"
    text = f"""## {action_label}  

**{issue.get('title', '?')}**  

- **作者** / *Author*: **{user}**  
- **标签** / *Labels*: {label_str}"""

    if body:
        text += f"\n\n{body}"

    text += f"\n\n[📎 查看详情 / View Issue]({url})  \n\n> GitHub"
    return title, text


# ── dispatch ─────────────────────────────────────────────

handlers = {"push": push, "pull_request": pull_request, "issues": issues}
handler = handlers.get(EVENT_NAME)
if handler:
    title, text = handler()
else:
    title = f"{EVENT_NAME} · {REPO}"
    text = f"## 📢 事件 / Event: `{EVENT_NAME}`\n\n_{REPO}_\n\n> GitHub"

payload = json.dumps({
    "msgtype": "markdown",
    "markdown": {"title": title, "text": text},
}).encode()

req = urllib.request.Request(WEBHOOK, data=payload, headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req)
print(f"[DingTalk] {resp.status} {resp.read().decode()}")
