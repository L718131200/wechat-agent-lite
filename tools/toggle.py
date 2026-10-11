# -*- coding: utf-8 -*-
"""wechat-agent-lite 的一键开关（替代旧包那个已经半废的 toggle）。

用法（双击桌面「微信AI接管-一键开关.bat」后按提示选，或直接带参数跑）：
    toggle.py            显示状态
    toggle.py start      启动
    toggle.py stop       停止
    toggle.py restart    重启（改完 config.yaml 用这个）
    toggle.py log        看最近日志
    toggle.py check      跑一次环境自检（不启动）

它只管命令行里带 wechat_agent 的进程，**不会碰旧包的 run-webui**。
"""
import os
import subprocess
import sys
import time

WAL = os.environ.get("WAL_DIR", r"C:\Users\KFCjt\wal")
CFG = os.path.join(WAL, "config.yaml")
OUTLOG = os.path.join(WAL, "run-out.log")
AGENTLOG = os.path.join(WAL, "data", "agent.log")
MARK = "wechat_agent"
DETACHED = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

try:
    os.system("chcp 65001 >nul 2>&1")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def line(ch="=", n=60):
    print(ch * n)


def agent_pids():
    """命令行含 wechat_agent 的 python 进程；读不到就返回空。"""
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


def show_status():
    line()
    print(" wechat-agent-lite 状态")
    line()
    pids = agent_pids()
    print("  业务进程 : %s" % (("运行中 PID=%s" % ", ".join(map(str, pids))) if pids else "未运行"))
    print("  配置文件 : %s  %s" % (CFG, "存在" if os.path.exists(CFG) else "★缺失★"))
    for p in (OUTLOG, AGENTLOG):
        if os.path.exists(p):
            mt = time.strftime("%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p)))
            print("  日志     : %-42s 最后写入 %s" % (os.path.basename(p), mt))
    # 监听目标一览（直接读配置，失败不报错）
    try:
        import yaml
        cfg = yaml.safe_load(open(CFG, encoding="utf-8"))
        print("  监听目标 :")
        for it in (cfg.get("listen") or []):
            print("     - %-14s %s / %s" % (it.get("chat"), it.get("type"), it.get("reply")))
        g = cfg.get("group") or {}
        v = cfg.get("vision") or {}
        print("  接图窗口 : %ss（@ 之后多久内发图算续图）" % g.get("at_image_followup_sec"))
        print("  图片门槛 : %sKB（更小的按表情跳过）" % v.get("min_image_kb"))
    except Exception as e:
        print("  （读配置失败：%r）" % (e,))
    line()


def start():
    pids = agent_pids()
    if pids:
        print("已经在运行（PID=%s），不重复启动。" % ", ".join(map(str, pids)))
        return 0
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(WAL, "src")
    fout = open(OUTLOG, "a", encoding="utf-8")
    p = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "wechat_agent", "-c", CFG],
                         cwd=WAL, env=env, stdout=fout, stderr=subprocess.STDOUT,
                         creationflags=DETACHED)
    time.sleep(10)
    ok = p.poll() is None
    print("已启动 PID=%s  存活=%s" % (p.pid, "是" if ok else "否（退出码 %s）" % p.poll()))
    if not ok:
        print("启动失败，日志尾部：")
        tail(15)
    return 0 if ok else 1


def stop():
    pids = agent_pids()
    if not pids:
        print("本来就没在运行。")
        return 0
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
        print("已结束 PID=%s" % pid)
    time.sleep(2)
    print("剩余：%s" % (agent_pids() or "无"))
    return 0


def restart():
    stop()
    time.sleep(1)
    return start()


def tail(n=25, path=AGENTLOG):
    if not os.path.exists(path):
        print("日志不存在：%s" % path)
        return 1
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    line()
    print(" %s 最后 %d 行（共 %d 行）" % (os.path.basename(path), n, len(lines)))
    line()
    for l in lines[-n:]:
        print("  " + l[:170])
    return 0


def check():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(WAL, "src")
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "wechat_agent",
                        "-c", CFG, "--check"], cwd=WAL, env=env,
                       capture_output=True, timeout=300)
    out = (r.stdout or b"").decode("utf-8", "replace")
    for l in out.splitlines()[-25:]:
        print("  " + l[:170])
    print("自检退出码 = %d" % r.returncode)
    return r.returncode


MENU = """
  1) 查看状态        2) 启动        3) 停止
  4) 重启（改完配置用这个）          5) 看日志
  6) 环境自检        0) 退出
"""


def menu():
    while True:
        show_status()
        print(MENU)
        try:
            c = input("  选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if c == "0" or c == "":
            return 0
        elif c == "1":
            continue
        elif c == "2":
            start()
        elif c == "3":
            stop()
        elif c == "4":
            restart()
        elif c == "5":
            tail()
        elif c == "6":
            check()
        else:
            print("  没这个选项。")
        input("  按回车继续…")


if __name__ == "__main__":
    cmd = (sys.argv[1].lower() if len(sys.argv) > 1 else "")
    rc = 0
    if cmd == "start":
        rc = start()
    elif cmd == "stop":
        rc = stop()
    elif cmd == "restart":
        rc = restart()
    elif cmd == "log":
        rc = tail()
    elif cmd == "check":
        rc = check()
    elif cmd == "status":
        # 显式 status 永远只打印状态，绝不进菜单
        # （踩过坑：被 subprocess 调用时 stdin 仍可能是控制台，isatty() 为真，
        #   结果进了菜单卡在 input() 上等一个永远不来的回车）
        show_status()
    elif cmd == "":
        try:
            interactive = bool(sys.stdin) and sys.stdin.isatty()
        except Exception:
            interactive = False
        if interactive:
            rc = menu()
        else:
            show_status()
    else:
        print("不认识：%s" % cmd)
        print(__doc__)
        rc = 2
    sys.exit(rc)
