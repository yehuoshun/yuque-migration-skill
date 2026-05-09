#!/usr/bin/env python3
"""DingTalk notification for GitHub Actions push events."""
import json, os, sys, urllib.request

webhook = os.environ["DINGTALK_WEBHOOK"]
event_name = os.environ["GITHUB_EVENT_NAME"]
event_path = os.environ["GITHUB_EVENT_PATH"]

with open(event_path) as f:
    ev = json.load(f)

if event_name == "push":
    ref = os.environ["GITHUB_REF_NAME"]
    actor = os.environ["GITHUB_ACTOR"]
    compare = ev.get("compare", "")
    commits = ev.get("commits", [])

    lines = []
    for c in commits[:5]:
        author = c.get("author", {}).get("name", "?")
        msg = c.get("message", "").split("\n")[0][:80]
        lines.append(f"- **{author}**: {msg}")
    commit_text = "\n".join(lines)
    if len(commits) > 5:
        commit_text += f"\n...共{len(commits)}条提交"

    title = "yuque-migration-skill · push"
    text = f"## 🚀 Push · yuque-migration-skill\n\n"
    text += f"**分支**: {ref}\n"
    text += f"**提交者**: {actor}\n\n"
    text += f"{commit_text}\n\n"
    text += f"[查看详情]({compare})"

elif event_name == "pull_request":
    pr = ev.get("pull_request", {})
    action = ev.get("action", "?")
    emoji = {"opened": "🔵", "closed": "🔴", "reopened": "🔄"}.get(action, "📌")
    if action == "closed" and pr.get("merged"):
        emoji = "🟣"
    title = "yuque-migration-skill · PR " + action
    text = f"## {emoji} PR {action} · yuque-migration-skill\n\n"
    text += f"**{pr.get('title', '?')}**\n\n"
    text += f"**提交者**: {pr.get('user', {}).get('login', '?')}\n"
    text += f"**分支**: {pr.get('head', {}).get('ref', '?')} → {pr.get('base', {}).get('ref', '?')}\n\n"
    text += f"{pr.get('html_url', '')}"

elif event_name == "issues":
    issue = ev.get("issue", {})
    action = ev.get("action", "?")
    emoji = {"opened": "📝", "closed": "✅", "reopened": "🔄"}.get(action, "📌")
    title = "yuque-migration-skill · Issue " + action
    text = f"## {emoji} Issue {action} · yuque-migration-skill\n\n"
    text += f"**{issue.get('title', '?')}**\n\n"
    text += f"**提交者**: {issue.get('user', {}).get('login', '?')}\n\n"
    text += f"{issue.get('html_url', '')}"

else:
    title = "yuque-migration-skill · " + event_name
    text = f"Event: {event_name}"

body = json.dumps({
    "msgtype": "markdown",
    "markdown": {"title": title, "text": text}
}).encode()

req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req)
print(f"DingTalk response: {resp.status} {resp.read().decode()}")
