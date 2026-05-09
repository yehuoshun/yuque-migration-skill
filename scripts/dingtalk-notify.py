#!/usr/bin/env python3
"""DingTalk notification for GitHub repo events — 中英双语 Markdown 模板."""
import json, os, re, urllib.request

WEBHOOK = os.environ["DINGTALK_WEBHOOK"]
EVENT_NAME = os.environ["GITHUB_EVENT_NAME"]
EVENT_PATH = os.environ["GITHUB_EVENT_PATH"]
REPO = os.environ.get("GITHUB_REPOSITORY", "?")

with open(EVENT_PATH) as f:
    ev = json.load(f)

# ── commit type → emoji ──────────────────────────────────
COMMIT_EMOJI = {
    "feat": "✨", "fix": "🐛", "docs": "📝", "style": "💄",
    "refactor": "♻️", "test": "✅", "chore": "🔧", "perf": "⚡",
    "ci": "👷", "build": "📦", "revert": "⏪", "merge": "🔀",
    "wip": "🚧",
}


def _commit_emoji(msg):
    m = re.match(r"(\w+)[(:]", msg)
    return COMMIT_EMOJI.get((m.group(1) if m else "").lower(), "•")


def _truncate(text, n=300):
    if not text:
        return ""
    text = text.strip()
    if len(text) <= n:
        return text
    return text[:n].rsplit("\n", 1)[0] + "\n> ⋯"


# ── Notification builders ────────────────────────────────

def push():
    ref = os.environ["GITHUB_REF_NAME"]
    actor = os.environ["GITHUB_ACTOR"]
    compare = ev.get("compare", "")
    commits = ev.get("commits", [])
    total = len(commits)
    is_tag = ref.startswith("v") or "tag" in ref.lower()

    if is_tag:
        icon = "🏷️" if ref[0].isdigit() or ref.lower().startswith("v") else "🔖"
        title = f"GitHub Release · {REPO}"
        header = f"## {icon} GitHub · 发布 / Release\n\n**{ref}** 由 / by **{actor}**\n"
    else:
        title = f"GitHub Push · {REPO}"
        header = f"## 🚀 GitHub · 代码推送 / Push\n\n**{actor}** 推送到 / pushed to `{ref}` — **{total}** 次提交 / commits\n"

    lines = []
    seen = set()
    for c in commits[:8]:
        msg = c.get("message", "").split("\n")[0][:120]
        author = c.get("author", {}).get("name", "?")
        emoji = _commit_emoji(msg)
        key = f"{msg}|{author}"
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{emoji} {msg}  — *{author}*")

    if total > 8:
        lines.append(f"⋯ 共 **{total}** 条 / *{total} commits*")

    text = header + "\n".join(f"> {l}" for l in lines)
    if compare:
        text += f"\n\n[🔍 查看变更 / View changes]({compare})"
    return title, text


def pull_request():
    pr = ev.get("pull_request", {})
    action = ev.get("action", "?")
    number = pr.get("number", "?")

    action_map = {
        "opened":   ("🟢", "新建 / Opened"),
        "closed":   ("🔴", "关闭 / Closed"),
        "reopened": ("🔄", "重新打开 / Reopened"),
    }
    icon, label = action_map.get(action, ("📌", action.capitalize()))

    if action == "closed" and pr.get("merged"):
        icon, label = "🟣", "已合并 / Merged"

    user = pr.get("user", {}).get("login", "?")
    head = pr.get("head", {}).get("ref", "?")
    base = pr.get("base", {}).get("ref", "?")
    url = pr.get("html_url", "")
    body = _truncate(pr.get("body", ""))
    labels_list = [l["name"] for l in (pr.get("labels") or [])]
    label_str = " · ".join(f"`{l}`" for l in labels_list) if labels_list else "—"

    title = f"GitHub PR {label.split(' / ')[1]} · {REPO}"
    text = f"""## {icon} GitHub · PR #{number} {label}

**[{pr.get('title', '?')}]({url})**

> 👤 作者 / Author: **{user}**
> 🌿 分支 / Branch: `{head}` → `{base}`
> 🏷️ 标签 / Labels: {label_str}"""

    if body:
        text += f"\n\n{body}"

    text += f"\n\n[🔍 查看详情 / View PR]({url})"
    return title, text


def issues():
    issue = ev.get("issue", {})
    action = ev.get("action", "?")
    number = issue.get("number", "?")

    action_map = {
        "opened":   ("📝", "新建 / Opened"),
        "closed":   ("✅", "关闭 / Closed"),
        "reopened": ("🔄", "重新打开 / Reopened"),
    }
    icon, label = action_map.get(action, ("📌", action.capitalize()))

    user = issue.get("user", {}).get("login", "?")
    url = issue.get("html_url", "")
    body = _truncate(issue.get("body", ""))
    labels_list = [l["name"] for l in (issue.get("labels") or [])]
    label_str = " · ".join(f"`{l}`" for l in labels_list) if labels_list else "—"

    title = f"GitHub Issue {label.split(' / ')[1]} · {REPO}"
    text = f"""## {icon} GitHub · Issue #{number} {label}

**[{issue.get('title', '?')}]({url})**

> 👤 作者 / Author: **{user}**
> 🏷️ 标签 / Labels: {label_str}"""

    if body:
        text += f"\n\n{body}"

    text += f"\n\n[🔍 查看详情 / View Issue]({url})"
    return title, text


# ── Dispatch ─────────────────────────────────────────────

handlers = {"push": push, "pull_request": pull_request, "issues": issues}

handler = handlers.get(EVENT_NAME)
if handler:
    title, text = handler()
else:
    title = f"GitHub {EVENT_NAME} · {REPO}"
    text = f"## 📢 GitHub · 事件 / Event: `{EVENT_NAME}`\n\n_{REPO}_"

payload = json.dumps({
    "msgtype": "markdown",
    "markdown": {"title": title, "text": text},
}).encode()

req = urllib.request.Request(WEBHOOK, data=payload, headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req)
print(f"[DingTalk] {resp.status} {resp.read().decode()}")
