#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 3 课：cgroups v2 —— 给容器套上资源紧箍咒

namespace 管的是"你能看见什么"，cgroup 管的是"你能用多少"：
  * memory.max   内存上限，超了 -> 内核 OOM killer 直接上门杀进程
  * cpu.max      CPU 配额（"quota period"，如 50000 100000 = 每 100ms 最多用 50ms = 半核）

和前两课不同，cgroup 目录创建在 /sys/fs/cgroup 下，WSL 的 /init.scope
没有给普通用户委托，所以【本脚本必须用 sudo 运行】：

  sudo python3 lesson3_cgroups.py

实验内容：
  A. 内存：限制 128MB，让程序申请 1GB -> 观察它被 OOM SIGKILL、memory.events 计数
  B. CPU：限制成 50% 单核，跑 yes 烧 CPU -> 实测用量被压在 ~50%，并看到 nr_throttled
"""
import os
import sys
import time

CG_ROOT = "/sys/fs/cgroup"
BASE = f"{CG_ROOT}/minidocker-lesson3"


def need_root():
    if os.geteuid() != 0:
        sys.exit("需要 root：请用  sudo python3 lesson3_cgroups.py  运行（cgroup 未委托给普通用户）")


def write(path, content, optional=False):
    try:
        with open(path, "w") as f:
            f.write(str(content))
    except OSError as e:
        if optional:
            return False
        raise
    return True


def read(path):
    with open(path) as f:
        return f.read().strip()


def enable_controllers(names):
    """在根 cgroup 的 subtree_control 里打开控制器（systemd 通常已开，这里幂等补开）。"""
    line = " ".join(f"+{n}" for n in names)
    write(f"{CG_ROOT}/cgroup.subtree_control", line + "\n", optional=True)


def cg_mkdir(path):
    os.makedirs(path, exist_ok=False)
    return path


def cg_attach(path, pid):
    # 把一个已经存在的进程塞进 cgroup：写它的 tid 到 cgroup.procs
    write(f"{path}/cgroup.procs", f"{pid}\n")


def cg_rmdir(path):
    # v2 里 cgroup 目录用 rmdir 删除：要求里面没有进程、没有子 cgroup
    try:
        os.rmdir(path)
    except OSError:
        pass


# ---------------- 实验 A：内存上限 + OOM ----------------

ALLOC_CODE = """
import sys, time
blocks = []
try:
    for i in range(64):                  # 最多申请 64 x 16MB = 1GB
        blocks.append(b"x" * 16 * 1024 * 1024)
        print(f"  程序已吃下 {(i+1)*16} MB", flush=True)
except MemoryError:
    print("  malloc 直接返回了 MemoryError（没被杀，侥幸）", flush=True)
    sys.exit(0)
time.sleep(30)
"""


def demo_memory(mem_mb=128):
    print("=" * 60)
    print(f"实验 A：内存上限 memory.max = {mem_mb}MB，程序想吃 1GB")
    print("=" * 60)
    cg = cg_mkdir(f"{BASE}-mem")
    # memory.max 接受字节数（也接受 "128M" 这种带单位的写法）
    write(f"{cg}/memory.max", str(mem_mb * 1024 * 1024) + "\n")
    # 不许用 swap 凑数（某些内核没开 swap 会计，文件可能不存在，optional）
    write(f"{cg}/memory.swap.max", "0\n", optional=True)
    write(f"{cg}/memory.oom.group", "1\n", optional=True)

    pid = os.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-u", "-c", ALLOC_CODE])

    cg_attach(cg, pid)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)

    peak = int(read(f"{cg}/memory.peak"))
    events = dict(
        line.split(maxsplit=1)
        for line in read(f"{cg}/memory.events").splitlines()
    )
    print()
    print(f"  程序退出码 = {code}（负数表示被信号杀死，-9 = SIGKILL，正是 OOM killer 干的）")
    print(f"  内存峰值 memory.peak = {peak / 1024 / 1024:.1f} MB（贴着 {mem_mb}MB 天花板）")
    print(f"  OOM 触发次数 oom = {events.get('oom')}，真正杀进程 oom_kill = {events.get('oom_kill')}")
    print("  结论：容器里的程序再贪婪，也越不过 cgroup 画的线。")

    cg_rmdir(cg)


# ---------------- 实验 B：CPU 配额 + 节流 ----------------

def demo_cpu(percent=50, seconds=3):
    print()
    print("=" * 60)
    print(f"实验 B：CPU 配额 cpu.max = {percent}% 单核，跑 yes 全力烧 {seconds} 秒")
    print("=" * 60)
    cg = cg_mkdir(f"{BASE}-cpu")
    quota = int(100000 * percent / 100)       # period 固定 100000us
    write(f"{cg}/cpu.max", f"{quota} 100000\n")

    pid = os.fork()
    if pid == 0:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.execv("/usr/bin/yes", ["yes"])

    cg_attach(cg, pid)

    def usage_usec():
        for line in read(f"{cg}/cpu.stat").splitlines():
            k, v = line.split()
            if k == "usage_usec":
                return int(v)
        return 0

    t0, u0 = time.monotonic(), usage_usec()
    time.sleep(seconds)
    t1, u1 = time.monotonic(), usage_usec()

    os.kill(pid, 9)
    os.waitpid(pid, 0)

    wall = t1 - t0
    used_cpu = (u1 - u0) / 1_000_000          # 秒
    measured = used_cpu / wall * 100
    throttled = dict(
        line.split() for line in read(f"{cg}/cpu.stat").splitlines()
    )
    print(f"  {wall:.1f} 秒墙钟时间内，yes 实际拿到 {used_cpu:.2f} 秒 CPU")
    print(f"  实测占用 = {measured:.1f}% 个核心（配额 {percent}%）")
    print(f"  被节流次数 nr_throttled = {throttled.get('nr_throttled')}"
          f"，节流周期数 nr_periods = {throttled.get('nr_periods')}")
    print("  结论：配额一到，调度器就把容器按在板凳上，下个周期才能再跑。")

    cg_rmdir(cg)


def main():
    sys.stdout.reconfigure(line_buffering=True)   # 和容器内子进程的输出保持顺序
    need_root()
    enable_controllers(["memory", "cpu"])
    try:
        demo_memory()
        demo_cpu()
    finally:
        cg_rmdir(f"{BASE}-mem")
        cg_rmdir(f"{BASE}-cpu")
    print()
    print("两课合体：下一文件 minidocker.py 会把 namespace + cgroup 一起用上。")


if __name__ == "__main__":
    main()
