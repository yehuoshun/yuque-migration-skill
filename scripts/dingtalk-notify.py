#!/usr/bin/env python3
"""DingTalk notification for GitHub repo events — 中英双语模板."""
import json, os, sys, urllib.request

WEBHOOK = os.environ["DINGTALK_WEBHOOK"]
EVENT_NAME = os.environ["GITHUB_EVENT_NAME"]
EVENT_PATH = os.environ["GITHUB_EVENT_PATH"]
REPO = os.environ.get("GITHUB_REPOSITORY", "?")

with open(EVENT_PATH) as f:
    ev = json.load(f)


def push():
    ref = os.environ["GITHUB_REF_NAME"]
    actor = os.environ["GITHUB_ACTOR"]
    compare = ev.get("compare", "")
    commits = ev.get("commits", [])
    total = len(commits)

    lines = []
    for c in commits[:5]:
        author = c.get("author", {}).get("name", "?")
        msg = c.get("message", "").split("\n")[0][:80]
        lines.append(f"  · {msg} — {author}")
    commit_text = "\n".join(lines)
    if total > 5:
        commit_text += f"\n  ··· 共 {total} 条 / {total} commits total"

    return f"""🚀 代码推送 · Code Push

仓库 / Repo: {REPO}
分支 / Branch: {ref}
提交者 / Author: {actor}
提交数 / Commits: {total}

{commit_text}

查看变更: {compare}"""


def pull_request():
    pr = ev.get("pull_request", {})
    action = ev.get("action", "?")
    labels = {
        "opened":   "🟢 新建 / Opened",
        "closed":   "🔴 关闭 / Closed",
        "reopened": "🔄 重新打开 / Reopened",
    }
    label = labels.get(action, f"📌 {action}")
    if action == "closed" and pr.get("merged"):
        label = "🟣 已合并 / Merged"

    user = pr.get("user", {}).get("login", "?")
    head = pr.get("head", {}).get("ref", "?")
    base = pr.get("base", {}).get("ref", "?")
    url = pr.get("html_url", "")

    return f"""{label}

{pr.get('title', '?')}

作者 / Author: {user}
分支 / Branch: {head} → {base}

查看详情: {url}"""


def issues():
    issue = ev.get("issue", {})
    action = ev.get("action", "?")
    labels = {
        "opened":   "📝 新建 / Opened",
        "closed":   "✅ 关闭 / Closed",
        "reopened": "🔄 重新打开 / Reopened",
    }
    label = labels.get(action, f"📌 {action}")

    user = issue.get("user", {}).get("login", "?")
    url = issue.get("html_url", "")

    return f"""{label}

{issue.get('title', '?')}

作者 / Author: {user}

查看详情: {url}"""


handlers = {
    "push": push,
    "pull_request": pull_request,
    "issues": issues,
}

handler = handlers.get(EVENT_NAME)
if handler:
    text = handler()
else:
    text = f"📢 事件: {EVENT_NAME}"

payload = json.dumps({"msgtype": "text", "text": {"content": text}}).encode()
req = urllib.request.Request(WEBHOOK, data=payload, headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req)
print(f"[DingTalk] {resp.status} {resp.read().decode()}")
