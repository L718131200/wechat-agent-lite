# -*- coding: utf-8 -*-
"""用 GitHub API 发布（绕开 git push）。

为什么不能直接 push：
  * github.com 在本机被 hosts 指向 127.0.0.1（Steam++ 本地反代）→ 走 HTTPS 就是用
    第三方加速通道，违反本工作区的红线；
  * SSH over 443 是唯一合规通道，但它要求公钥已在账号上，而当前 token 没有
    admin:public_key 权限，加不了公钥。

所以走 REST API：先建仓库，再按 Git Data API 把本地快照推成一个 commit。
这样不需要公钥、不碰 hosts、不经过任何代理。

注意：这一步会把远端历史压成 1 个提交（本地 10 个提交的历史仍然完整保留；
等用户把公钥加上去之后，可以正常 git push 续上）。
"""
import base64
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
TOKEN_FILE = r"E:\dsh\.keys\github-token.txt"
REPO_DIR = r"E:\dsh\wechat-agent-lite"
REPO_NAME = "wechat-agent-lite"
DESC = ("微信 AI 机器人的业务层：只管监听、触发判定、上下文、模型调用、图片识别与分段发送；"
        "收发和 UI 操作交给 wechatauto-replica 引擎。PolyForm Noncommercial 1.0.0。")
report = []


def say(s):
    print(s)
    report.append(str(s))


tok = io.open(TOKEN_FILE, encoding="utf-8").read().strip()
H = {
    "Authorization": "token " + tok,
    "User-Agent": "wechat-agent-lite-publisher",
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json",
}


def api(method, path, body=None, timeout=60):
    url = "https://api.github.com" + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=H, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


say("=== 用 API 发布到 GitHub %s ===" % time.strftime("%H:%M:%S"))

# ---------- 0. 身份 ----------
st, me = api("GET", "/user")
if st != 200:
    say("!! 取身份失败 %s %s" % (st, me))
    raise SystemExit(1)
owner = me["login"]
say("身份：%s" % owner)

# ---------- 1. 仓库 ----------
st, repo = api("GET", "/repos/%s/%s" % (owner, REPO_NAME))
if st == 200:
    say("仓库已存在：%s" % repo["html_url"])
else:
    say("仓库不存在，创建中…")
    st, repo = api("POST", "/user/repos", {
        "name": REPO_NAME, "description": DESC, "private": False,
        "has_issues": True, "has_wiki": False, "auto_init": False,
    })
    if st not in (200, 201):
        say("!! 创建仓库失败 %s %s" % (st, repo))
        raise SystemExit(2)
    say("已创建：%s" % repo["html_url"])

# ---------- 2. 仓库必须先有内容：Git Data API 在空仓库上会返回 409 ----------
say("")
say("---- 确认仓库非空（空仓库不能建 blob）----")
st_ref, cur_ref = api("GET", "/repos/%s/%s/git/ref/heads/main" % (owner, REPO_NAME))
parents = []
if st_ref == 200:
    parents = [cur_ref["object"]["sha"]]
    say("已有 main，parent = %s" % parents[0][:12])
else:
    say("仓库是空的，先用 contents API 放一个文件进去激活它…")
    first = ".gitattributes"
    p0 = os.path.join(REPO_DIR, first.replace("/", os.sep))
    st_a, res_a = api("PUT", "/repos/%s/%s/contents/%s" % (owner, REPO_NAME, first), {
        "message": "激活仓库（Git Data API 要求仓库非空）",
        "content": base64.b64encode(open(p0, "rb").read()).decode("ascii"),
    })
    if st_a not in (200, 201):
        say("!! 激活失败 %s %s" % (st_a, res_a))
        raise SystemExit(3)
    parents = [res_a["commit"]["sha"]]
    say("已激活，parent = %s" % parents[0][:12])

# ---------- 3. 收集要推的文件（只取 git 跟踪的，避免把 config.yaml 推上去）----------
say("")
say("---- 收集文件（只取 git 跟踪的，确保不含 config.yaml / data）----")
out = subprocess.run(["git", "ls-files"], cwd=REPO_DIR, capture_output=True, timeout=60)
tracked = [l for l in (out.stdout or b"").decode("utf-8", "replace").splitlines() if l.strip()]
say("git 跟踪文件 %d 个" % len(tracked))
assert not any(f.endswith("config.yaml") for f in tracked), "配置里有密钥，绝不能推！"

# ---------- 3. blob ----------
say("")
say("---- 上传 blobs ----")
tree_items = []
for rel in tracked:
    p = os.path.join(REPO_DIR, rel.replace("/", os.sep))
    with open(p, "rb") as f:
        content = f.read()
    st, blob = api("POST", "/repos/%s/%s/git/blobs" % (owner, REPO_NAME), {
        "content": base64.b64encode(content).decode("ascii"),
        "encoding": "base64",
    })
    if st not in (200, 201):
        say("!! blob 失败 %s %s -> %s" % (rel, st, blob))
        raise SystemExit(3)
    tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    say("  %-42s %6d B" % (rel, len(content)))

# ---------- 4. tree ----------
say("")
say("---- 建 tree ----")
st, tree = api("POST", "/repos/%s/%s/git/trees" % (owner, REPO_NAME), {"tree": tree_items})
if st not in (200, 201):
    say("!! tree 失败 %s %s" % (st, tree))
    raise SystemExit(4)
say("tree sha = %s" % tree["sha"][:12])

# ---------- 5. commit ----------
msg = ("初始发布：wechat-agent-lite 微信 AI 机器人业务层\n\n"
       "自研业务层，替换旧包 wechatbot-new 的上层；收发与 UI 操作仍由\n"
       "wechatauto-replica（Apache-2.0，pip 依赖，不含其源码）承担。\n\n"
       "真机实测（VMware + 微信 4.1.15 + Python 3.11）：\n"
       "- 群 @ 文本 0.97s / 私聊 0.86s / @+图片(下载+识别+回复) 2.54s（业务层）\n"
       "- 端到端（含引擎 UI 发送）约 3-4 秒；旧包同场景约 28 秒\n"
       "- 78 项离线测试；隐私路径（他人图片不会被上传）有专门测试覆盖\n")
st, commit = api("POST", "/repos/%s/%s/git/commits" % (owner, REPO_NAME), {
    "message": msg, "tree": tree["sha"], "parents": parents,
})
if st not in (200, 201):
    say("!! commit 失败 %s %s" % (st, commit))
    raise SystemExit(5)
say("commit sha = %s" % commit["sha"][:12])

# ---------- 6. 指向分支 ----------
say("")
say("---- 建分支 main ----")
st, ref = api("POST", "/repos/%s/%s/git/refs" % (owner, REPO_NAME), {
    "ref": "refs/heads/main", "sha": commit["sha"],
})
if st not in (200, 201):
    # 已存在就更新
    st2, ref2 = api("PATCH", "/repos/%s/%s/git/refs/heads/main" % (owner, REPO_NAME), {
        "sha": commit["sha"], "force": True,
    })
    if st2 not in (200, 201):
        say("!! 建/更新分支失败 %s %s" % (st, ref))
        raise SystemExit(6)
    say("已更新 main -> %s" % commit["sha"][:12])
else:
    say("已建 main -> %s" % commit["sha"][:12])

# ---------- 7. 设默认分支 ----------
api("PATCH", "/repos/%s/%s" % (owner, REPO_NAME), {"default_branch": "main"})

say("")
say("=== 发布完成 ===")
say("仓库地址：https://github.com/%s/%s" % (owner, REPO_NAME))
say("文件数：%d" % len(tracked))
say("")
say("提示：远端只有 1 个提交（本地 10 个提交的历史完整保留在 E:\\dsh\\wechat-agent-lite\\.git）。")
say("     把公钥加到账号后，就能用 git push 续上完整历史。")

outp = os.path.join(os.environ.get("TEMP", "."), "publish-result.txt")
io.open(outp, "w", encoding="utf-8").write("\n".join(report))
print("结果已写入", outp)
