# -*- coding: utf-8 -*-
"""wechat-agent-lite 的本地控制台（只读为主，改配置时只动你改的那个字段）。

设计上刻意与旧包的网页控制台相反 —— 旧包点一次"保存"会把整个 config.py
重写一遍，2026-10-11 那次就是这样把三个监听群清空的。这里：

  * 修改走「逐行正则替换 + 按段缩进定位」：只替换目标 key 所在的那一行，
    注释、键顺序、其他所有内容都原样保留；
  * 每次修改前自动备份，改完全量校验（用程序自己的 load_config），不过就回滚；
  * 只允许改白名单里的字段（密钥类字段根本不在表里，界面上也看不到）；
  * 服务只绑 127.0.0.1:8090，不碰旧控制台的 8080。

用法：python wal-webui.py [端口]
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WAL = os.environ.get("WAL_DIR", r"C:\Users\KFCjt\wal")
CFG = os.path.join(WAL, "config.yaml")
AGENTLOG = os.path.join(WAL, "data", "agent.log")
OUTLOG = os.path.join(WAL, "run-out.log")
MARK = "wechat_agent"
DETACHED = 0x00000008 | 0x00000200
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090

try:
    os.system("chcp 65001 >nul 2>&1")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 可改字段白名单
# (段, 键, 类型, 说明)  段为 None 表示顶层键
FIELDS = [
    (None,    "listener_interval",      "float", "轮询间隔（秒）"),
    (None,    "startup_grace_sec",      "float", "启动前多久算历史消息（秒）"),
    ("group", "at_image_followup_sec",  "int",   "群里 @ 之后多久内的图算续图（秒）"),
    ("group", "image_followup_budget",  "int",   "一次 @ 最多接几张图"),
    ("group", "reply_probability",      "int",   "群聊回复概率（%）"),
    ("vision", "enabled",               "bool",  "开启图片识别"),
    ("vision", "min_image_kb",          "float", "小于这个体积按表情跳过（KB）"),
    ("context", "max_chars",            "int",   "上下文最大字符数"),
    ("context", "max_messages",         "int",   "上下文最大条数"),
    ("send",  "max_segments",           "int",   "一条回复最多拆几段"),
    ("send",  "segment_delay",          "float", "分段之间的间隔（秒）"),
    ("limits", "enabled",               "bool",  "开启限流"),
    ("limits", "per_user_per_minute",   "int",   "单用户每分钟上限"),
    ("limits", "per_chat_per_minute",   "int",   "单会话每分钟上限"),
    ("limits", "daily_replies",         "int",   "每日回复总数上限（0=不限）"),
    ("limits", "cooldown_seconds",      "float", "同一人回复冷却（秒）"),
    ("llm",   "model",                  "str",   "主模型"),
    ("llm",   "temperature",            "float", "温度"),
    ("llm",   "max_tokens",             "int",   "单次最大输出 token"),
    ("log",   "level",                  "str",   "日志级别"),
]


# ---------------------------------------------------------------- 工具
def read_cfg_text():
    return io.open(CFG, encoding="utf-8").read()


def agent_pids():
    pids = []
    try:
        out = subprocess.run(["wmic", "process", "where", "name='python.exe'", "get",
                              "ProcessId,CommandLine"], capture_output=True, timeout=60)
        for row in (out.stdout or b"").decode("utf-8", "replace").splitlines():
            if MARK in row:
                parts = row.split()
                if parts and parts[-1].isdigit():
                    pids.append(int(parts[-1]))
    except Exception:
        pass
    return sorted(pids)


def cfg_value(section, key):
    """从 YAML 文本里读某段的某个键（按缩进定位，能区分同名的 enabled）。"""
    lines = read_cfg_text().splitlines()
    if section is None:
        pat = re.compile(r"^%s\s*:\s*(.*?)\s*(?:#.*)?$" % re.escape(key))
        for ln in lines:
            m = pat.match(ln)
            if m:
                return m.group(1).strip()
        return None
    sec_pat = re.compile(r"^(\s*)%s\s*:\s*$" % re.escape(section))
    for i, ln in enumerate(lines):
        m = sec_pat.match(ln)
        if not m:
            continue
        indent = len(m.group(1))
        for ln2 in lines[i + 1:]:
            if not ln2.strip() or ln2.lstrip().startswith("#"):
                continue
            cur = len(ln2) - len(ln2.lstrip())
            if cur <= indent:
                break
            km = re.match(r"^\s*%s\s*:\s*(.*?)\s*(?:#.*)?$" % re.escape(key), ln2)
            if km:
                return km.group(1).strip()
        return None
    return None


def yaml_literal(section, key, raw, typ):
    """把界面传来的字符串转成 YAML 字面量。"""
    raw = (raw or "").strip()
    if typ == "bool":
        return "true" if raw.lower() in ("1", "true", "yes", "on", "是") else "false"
    if typ in ("int", "float"):
        if raw == "":
            raise ValueError("不能为空")
        float(raw)                      # 校验数字
        return raw
    # 字符串：统一加双引号并转义
    return '"%s"' % raw.replace("\\", "\\\\").replace('"', '\\"')


def patch_one(section, key, raw, typ):
    """只替换目标那一行，其余原样保留。返回 (ok, message, old, new)。"""
    val = yaml_literal(section, key, raw, typ)
    lines = read_cfg_text().splitlines(True)
    target = None
    if section is None:
        pat = re.compile(r"^(%s\s*:\s*)(.*?)(\s*(?:#.*)?)(\r?\n?)$" % re.escape(key))
        for i, ln in enumerate(lines):
            m = pat.match(ln)
            if m:
                target = (i, m, 1, 2, 3, 4)
                break
    else:
        sec_pat = re.compile(r"^(\s*)%s\s*:\s*(\r?\n?)$" % re.escape(section))
        for i, ln in enumerate(lines):
            m = sec_pat.match(ln)
            if not m:
                continue
            indent = len(m.group(1))
            for j in range(i + 1, len(lines)):
                ln2 = lines[j]
                if not ln2.strip() or ln2.lstrip().startswith("#"):
                    continue
                cur = len(ln2) - len(ln2.lstrip())
                if cur <= indent:
                    break
                km = re.match(r"^(\s*%s\s*:\s*)(.*?)(\s*(?:#.*)?)(\r?\n?)$" % re.escape(key), ln2)
                if km:
                    target = (j, km, 1, 2, 3, 4)
                    break
            break
    if not target:
        return False, "配置里找不到 %s%s" % ((section + ".") if section else "", key), None, None
    i, m, g_pre, g_old, g_comment, g_nl = target
    old = (m.group(g_old) or "").strip()
    new_line = "%s%s%s%s" % (m.group(g_pre), val, m.group(g_comment), m.group(g_nl) or "\n")
    if lines[i] == new_line:
        return True, "没有变化", old, old
    lines[i] = new_line
    # 备份 -> 写 -> 校验 -> 失败回滚
    bak = CFG + ".bak-webui-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CFG, bak)
    io.open(CFG, "w", encoding="utf-8", newline="").write("".join(lines))
    try:
        sys.path.insert(0, os.path.join(WAL, "src"))
        for mod in ("wechat_agent.config",):
            sys.modules.pop(mod, None)
        import importlib
        cm = importlib.import_module("wechat_agent.config")
        cm.load_config(CFG)
    except Exception as e:
        io.open(CFG, "w", encoding="utf-8", newline="").write(io.open(bak, encoding="utf-8").read())
        return False, "校验失败已回滚：%r" % (e,), old, None
    return True, "已保存（备份 %s），重启后生效" % os.path.basename(bak), old, val


def tail(path, n):
    if not os.path.exists(path):
        return []
    lines = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
    return lines[-n:]


def start_agent():
    if agent_pids():
        return "已在运行"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(WAL, "src")
    fout = open(OUTLOG, "a", encoding="utf-8")
    p = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "wechat_agent", "-c", CFG],
                         cwd=WAL, env=env, stdout=fout, stderr=subprocess.STDOUT,
                         creationflags=DETACHED)
    time.sleep(8)
    return "已启动 PID=%s 存活=%s" % (p.pid, p.poll() is None)


def stop_agent():
    pids = agent_pids()
    if not pids:
        return "本来就没在运行"
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
    time.sleep(2)
    return "已停止 %s" % ", ".join(map(str, pids))


# ---------------------------------------------------------------- HTTP
PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>wechat-agent-lite 控制台</title>
<style>
 body{font:14px/1.7 "Microsoft YaHei",system-ui,sans-serif;margin:0;background:#f5f6f8;color:#222}
 .wrap{max-width:940px;margin:0 auto;padding:18px}
 h1{font-size:19px;margin:8px 0 14px}
 .card{background:#fff;border:1px solid #e3e6ea;border-radius:8px;padding:14px 16px;margin-bottom:14px}
 .card h2{font-size:15px;margin:0 0 10px;color:#334}
 table{width:100%;border-collapse:collapse}
 td,th{padding:5px 6px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
 th{color:#667;font-weight:600;width:34%}
 input[type=text]{width:110px;padding:3px 6px;border:1px solid #cfd4da;border-radius:4px;font:inherit}
 .btn{padding:6px 14px;border:0;border-radius:6px;background:#3b6ef0;color:#fff;font:inherit;cursor:pointer;margin-right:8px}
 .btn.gray{background:#8a929c}.btn.red{background:#d9534f}.btn.green{background:#2f9e44}
 .ok{color:#2f9e44}.bad{color:#d9534f}
 pre{background:#1f2430;color:#d8dee9;padding:10px;border-radius:6px;overflow:auto;max-height:320px;font-size:12px}
 .muted{color:#8a929c;font-size:12px}
</style></head><body><div class="wrap">
<h1>wechat-agent-lite 控制台 <span class="muted" id="ver"></span></h1>

<div class="card"><h2>运行状态</h2><div id="status">读取中…</div>
 <div style="margin-top:10px">
  <button class="btn green" onclick="ctl('start')">启动</button>
  <button class="btn red" onclick="ctl('stop')">停止</button>
  <button class="btn gray" onclick="ctl('restart')">重启</button>
  <span class="muted">改完配置需要重启才生效</span>
 </div></div>

<div class="card"><h2>可调参数</h2>
 <div class="muted">只改你动过的那一行，注释与其他内容原样保留；改前自动备份，校验不过自动回滚。</div>
 <table id="fields"></table>
 <div style="margin-top:12px"><button class="btn" onclick="save()">保存修改</button>
  <span id="saveres" class="muted"></span></div></div>

<div class="card"><h2>日志尾部</h2>
 <div style="margin-bottom:8px">
  <button class="btn gray" onclick="loadlog(30)">最近 30 行</button>
  <button class="btn gray" onclick="loadlog(120)">最近 120 行</button>
  <button class="btn gray" onclick="loadlog(0)">stdout 日志</button>
 </div>
 <pre id="log">点上面的按钮加载…</pre></div>

<script>
let FIELDS=[];
function esc(s){return (s===null||s===undefined)?'':String(s).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));}
async function j(u,o){const r=await fetch(u,o);return await r.json();}
async function loadstatus(){
 const s=await j('/api/status');
 document.getElementById('status').innerHTML=
  '<table>'+
  '<tr><th>业务进程</th><td>'+(s.pids.length?('<span class="ok">运行中 PID '+s.pids.join(', ')+'</span>'):'<span class="bad">未运行</span>')+'</td></tr>'+
  '<tr><th>配置文件</th><td>'+esc(s.cfg)+' '+(s.cfg_exists?'':'<span class="bad">缺失</span>')+'</td></tr>'+
  '<tr><th>监听目标</th><td>'+s.listen.map(x=>esc(x.chat)+' <span class="muted">'+esc(x.type)+' / '+esc(x.reply)+'</span>').join('<br>')+'</td></tr>'+
  '<tr><th>日志最后写入</th><td>'+esc(s.log_mtime)+'</td></tr>'+
  '</table>';
}
async function loadfields(){
 FIELDS=await j('/api/fields');
 document.getElementById('fields').innerHTML=FIELDS.map((f,i)=>
  '<tr><th>'+esc(f.label)+'<br><span class="muted">'+esc(f.path)+'</span></th>'+
  '<td><input type="text" id="f'+i+'" value="'+esc(f.value)+'"> '+
  (f.dirty?'<span class="bad">已改</span>':'')+'</td></tr>').join('');
}
async function save(){
 const upd={};
 FIELDS.forEach((f,i)=>{const v=document.getElementById('f'+i).value;
   if(String(v)!==String(f.value)) upd[f.path]=v;});
 if(Object.keys(upd).length===0){document.getElementById('saveres').textContent='没有修改';return;}
 const r=await j('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(upd)});
 document.getElementById('saveres').innerHTML=r.results.map(x=>
   '<div class="'+(x.ok?'ok':'bad')+'">'+esc(x.path)+': '+esc(x.msg)+'</div>').join('');
 loadfields();
}
async function ctl(a){const r=await j('/api/control?action='+a,{method:'POST'});
 alert(r.msg); loadstatus();}
async function loadlog(n){
 const r=await j('/api/log?n='+n);
 document.getElementById('log').textContent=(r.lines||[]).join('\\n')||'(空)';
 document.getElementById('log').scrollTop=1e9;
}
loadstatus(); loadfields(); loadlog(30);
setInterval(loadstatus,8000);
</script>
</div></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                # 别刷屏

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/status":
            mt = ""
            for p in (AGENTLOG, OUTLOG):
                if os.path.exists(p):
                    t = time.strftime("%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p)))
                    mt += "%s %s  " % (os.path.basename(p), t)
            listen = []
            try:
                import yaml
                cfg = yaml.safe_load(read_cfg_text())
                listen = cfg.get("listen") or []
            except Exception:
                pass
            return self._send(200, {"pids": agent_pids(), "cfg": CFG,
                                    "cfg_exists": os.path.exists(CFG),
                                    "log_mtime": mt.strip(), "listen": listen})
        if u.path == "/api/fields":
            out = []
            for sec, key, typ, label in FIELDS:
                out.append({"path": ("%s.%s" % (sec, key)) if sec else key,
                            "value": cfg_value(sec, key), "label": label, "type": typ})
            return self._send(200, out)
        if u.path == "/api/log":
            n = int((q.get("n") or ["30"])[0])
            if n <= 0:
                return self._send(200, {"lines": tail(OUTLOG, 60)})
            return self._send(200, {"lines": tail(AGENTLOG, n)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/control":
            a = (q.get("action") or [""])[0]
            try:
                if a == "start":
                    msg = start_agent()
                elif a == "stop":
                    msg = stop_agent()
                elif a == "restart":
                    msg = stop_agent() + " / " + start_agent()
                else:
                    return self._send(400, {"msg": "未知动作 " + a})
            except Exception as e:
                return self._send(200, {"msg": "失败：%r" % (e,)})
            return self._send(200, {"msg": msg})
        if u.path == "/api/config":
            ln = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(ln).decode("utf-8"))
            except Exception as e:
                return self._send(400, {"results": [{"ok": False, "path": "?", "msg": "请求体解析失败 %r" % (e,)}]})
            allowed = {("%s.%s" % (s, k)) if s else k: (s, k, t) for s, k, t, _ in FIELDS}
            results = []
            for path, raw in body.items():
                if path not in allowed:
                    results.append({"ok": False, "path": path, "msg": "不在白名单里，拒绝修改"})
                    continue
                sec, key, typ = allowed[path]
                try:
                    ok, msg, old, new = patch_one(sec, key, raw, typ)
                except Exception as e:
                    ok, msg = False, "转换失败：%r" % (e,)
                results.append({"ok": ok, "path": path, "msg": msg})
            return self._send(200, {"results": results})
        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("wechat-agent-lite 控制台已启动： http://127.0.0.1:%d/" % PORT)
    print("配置：%s" % CFG)
    print("（只绑本机可用地址；要停掉直接关窗口或 Ctrl+C）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
