#!/usr/bin/env python3
"""DingTalk notification for SKILL.md release — 中英双语."""
import json, os, urllib.request

WEBHOOK = os.environ["DINGTALK_WEBHOOK"]
REPO = os.environ.get("GITHUB_REPOSITORY", "?")
ACTOR = os.environ.get("GITHUB_ACTOR", "?")
REF = os.environ.get("GITHUB_REF_NAME", "?")
COMMIT_MSG = os.environ.get("COMMIT_MSG", "")[:100]

url = f"https://github.com/{REPO}/releases/tag/latest"

text = f"""## 🏷️ Skill 发布 / Release  

**SKILL.md** 已更新 / updated  

**仓库** / *Repo*: {REPO}  
**分支** / *Branch*: {REF}  
**提交者** / *Author*: **{ACTOR}**  
**提交信息** / *Message*: {COMMIT_MSG}  

[📎 查看 Release / View Release]({url})  

> GitHub"""

payload = json.dumps({
    "msgtype": "markdown",
    "markdown": {"title": f"Skill Release · {REPO}", "text": text},
}).encode()

req = urllib.request.Request(WEBHOOK, data=payload, headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req)
print(f"[DingTalk Release] {resp.status} {resp.read().decode()}")
