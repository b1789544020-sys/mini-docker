#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
minidocker.py —— 四课合体：一个零依赖的迷你 "docker run"

做的事（和真正的 runc/Docker 启动容器时做的一模一样，只是少了镜像层）：
  1. （可选，需要 sudo）建 cgroup v2，写好 memory.max / cpu.max 紧箍咒
  2. （可选，需要 sudo）建/接入 mini0 网桥，从 10.87.0.0/24 分配 IP、插 veth
  3. 两级 fork：C unshare user/pid/uts/ipc/mnt(/net) namespace 并 chroot，
     G 成为容器 PID 1；cgroup/网络通过两阶段握手就位
  4. 挂容器私有 /proc、tmpfs、/etc/hosts，exec 用户命令
  5. wait 并回收；退出时拔 veth、释放 IP、清掉 cgroup

用法（选项都在 rootfs 之前，和 docker run [选项] 镜像 命令 一样）：
  # 纯隔离，普通用户即可：
  python3 minidocker.py run ~/mini-docker-rootfs
  python3 minidocker.py run --name demo ~/mini-docker-rootfs -- /bin/sh -c 'ps -ef'

  # 带资源限制（cgroup 需要真 root；WSL 里也可用 wsl -u root）：
  sudo python3 minidocker.py run --name demo --mem 128M --cpus 0.5 ~/mini-docker-rootfs
  sudo python3 minidocker.py list
"""
import argparse
import os
import string
import sys
import time
import uuid

import container
import network

CG_ROOT = "/sys/fs/cgroup"
CG_BASE = f"{CG_ROOT}/minidocker"


# ---------------- cgroup v2 小工具 ----------------

def _w(path, content, optional=False):
    try:
        with open(path, "w") as f:
            f.write(str(content))
    except OSError:
        if optional:
            return False
        raise
    return True


def _r(path):
    with open(path) as f:
        return f.read().strip()


def parse_size(s):
    """128M / 2G / 102400 -> 字节数（1024 进制）。"""
    s = s.strip()
    units = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
    if s[-1].upper() in units:
        return int(float(s[:-1]) * units[s[-1].upper()])
    return int(s)


def safe_name(name):
    ok = set(string.ascii_lowercase + string.digits + "-_.")
    return "".join(c if c in ok else "-" for c in name.lower())[:48]


def require_root_for_privileged(args):
    if (args.mem or args.cpus) and os.geteuid() != 0:
        sys.exit("--mem/--cpus 依赖 cgroup，需要 sudo 运行（见第 3 课）")
    if getattr(args, "network", False) and os.geteuid() != 0:
        sys.exit("--network 要在宿主机初始 netns 建 veth/bridge，需要 sudo 运行（见第 4 课）")


def make_cgroup(name, mem, cpus):
    """建 /sys/fs/cgroup/minidocker/<name> 并写限制，返回路径。"""
    need = (["memory"] if mem else []) + (["cpu"] if cpus else [])
    line = " ".join(f"+{c}" for c in need) + "\n"
    # v2 的控制器必须【逐层】下发：根 -> /minidocker -> /minidocker/<name>。
    # 少开中间一层，子 cgroup 里连 memory.max 这个文件都不存在。
    os.makedirs(CG_BASE, exist_ok=True)
    _w(f"{CG_ROOT}/cgroup.subtree_control", line, optional=True)
    _w(f"{CG_BASE}/cgroup.subtree_control", line)
    os.makedirs(f"{CG_BASE}/{name}", exist_ok=False)
    cg = f"{CG_BASE}/{name}"
    if mem:
        _w(f"{cg}/memory.max", f"{parse_size(mem)}\n")
        _w(f"{cg}/memory.swap.max", "0\n", optional=True)
    if cpus:
        quota = max(1000, round(cpus * 100000))   # period = 100000us
        _w(f"{cg}/cpu.max", f"{quota} 100000\n")
    return cg


def cleanup_cgroup(cg):
    """先一锅端（cgroup.kill，内核 5.14+），再删目录。

    外部（如测试脚本）也可能写 cgroup.kill：P waitpid 到 C 退出后，
    内核把死掉的任务从 cgroup 摘掉有短暂延迟，立刻 rmdir 会 EBUSY，重试几轮。
    """
    if not cg:
        return
    _w(f"{cg}/cgroup.kill", "1\n", optional=True)
    for _ in range(20):
        try:
            os.rmdir(cg)
            return
        except OSError:
            time.sleep(0.05)
    print(f"[警告] cgroup 暂时没删掉（内核延迟），残留: {cg}", file=sys.stderr)


# ---------------- run ----------------

def cmd_run(args):
    require_root_for_privileged(args)
    rootfs = os.path.abspath(args.rootfs)
    if not os.path.isdir(rootfs):
        sys.exit(f"rootfs 不存在: {rootfs}\n先运行: python3 make_rootfs.py")

    argv = args.argv
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        argv = ["/bin/sh"]

    name = safe_name(args.name or f"box-{uuid.uuid4().hex[:6]}")
    cg = None
    net_spec = None
    try:
        if args.mem or args.cpus:
            cg = make_cgroup(name, args.mem, args.cpus)
            print(f"[minidocker] cgroup 就绪: {cg}"
                  f"{f'  mem<={args.mem}' if args.mem else ''}"
                  f"{f'  cpu<={args.cpus}核' if args.cpus else ''}",
                  file=sys.stderr)
            attach = lambda pid: _w(f"{cg}/cgroup.procs", f"{pid}\n")
        else:
            attach = None

        if args.network:
            network.ensure_bridge()
            net_spec = network.allocate()
            print(f"[minidocker] 网络就绪: eth0={net_spec.ip}/24 "
                  f"gw={net_spec.gw}（bridge {network.BRIDGE}）",
                  file=sys.stderr)

        flags = container.BASE_FLAGS if os.geteuid() != 0 else container.ROOT_FLAGS
        print(f"[minidocker] 启动容器 {name}: {' '.join(argv)}", file=sys.stderr)
        rc = container.spawn(
            rootfs, argv,
            flags=flags,
            hostname=name,
            after_fork_parent=attach,
            net=net_spec,
        )
        print(f"[minidocker] 容器 {name} 退出，code={rc}", file=sys.stderr)
        return rc
    finally:
        cleanup_cgroup(cg)
        if net_spec is not None:
            network.release(net_spec)
        try:
            os.rmdir(CG_BASE)
        except OSError:
            pass


# ---------------- list ----------------

def cmd_list(_args):
    if not os.path.isdir(CG_BASE):
        print("当前没有 minidocker 管理的 cgroup")
        return
    rows = []
    for name in sorted(os.listdir(CG_BASE)):
        cg = f"{CG_BASE}/{name}"
        if not os.path.isdir(cg):
            continue
        procs = _r(f"{cg}/cgroup.procs").split()
        mem = int(_r(f"{cg}/memory.current")) if os.path.exists(f"{cg}/memory.current") else 0
        rows.append((name, len(procs), f"{mem/1024/1024:.1f} MB"))
    if not rows:
        print("当前没有 minidocker 管理的 cgroup")
        return
    print(f"{'NAME':24} {'PROCS':>6} {'MEM':>10}")
    for name, n, mem in rows:
        print(f"{name:24} {n:>6} {mem:>10}")


def main():
    p = argparse.ArgumentParser(description="零依赖迷你 Docker（namespace + cgroups + chroot）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="启动一个容器")
    pr.add_argument("rootfs", help="根文件系统目录，如 ~/mini-docker-rootfs")
    pr.add_argument("--name", help="容器名（同时是容器内 hostname）")
    pr.add_argument("--mem", help="内存上限，如 128M / 2G（需要 sudo）")
    pr.add_argument("--cpus", type=float, help="CPU 核数上限，如 0.5 / 2（需要 sudo）")
    pr.add_argument("--network", action="store_true",
                    help="接入 mini0 网桥，分配 10.87.0.x 独立 IP（需要 sudo，见第 4 课）")
    pr.add_argument("argv", nargs=argparse.REMAINDER, help="-- 后面跟容器内要执行的命令")
    pr.set_defaults(func=cmd_run)

    pl = sub.add_parser("list", help="列出带 cgroup 限制的 minidocker 容器")
    pl.set_defaults(func=cmd_list)

    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
