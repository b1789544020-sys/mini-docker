#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 1 课：chroot —— 文件系统的"传送门"

chroot(path) 会把进程眼里的根目录 / 改成 path。从此以后，
无论它怎么 cd / 怎么找绝对路径，都逃不出这个目录（经典的 chroot jail）。

但注意：chroot 【只改文件路径】，它不隔离进程、网络、主机名。
本课故意只用了一个 user namespace（为了让普通用户也能当"容器里的 root"），
没有用 PID / UTS namespace，所以你会观察到两个有趣的"漏风"：
  1. hostname 还是宿主机的名字；
  2. 新根里的 /proc 是空目录（宿主机的 proc 挂载点被挡在外面），
     所以 ps 啥也看不到 —— 这正是第 2 课要补的洞。

用法：
  python3 lesson1_chroot.py ~/mini-docker-rootfs
  python3 lesson1_chroot.py ~/mini-docker-rootfs -- /bin/sh -c 'id; ls /'
"""
import os
import socket
import sys


def become_root_in_user_namespace():
    """新建 user namespace，并把"启动本程序的普通用户"映射成命名空间里的 root。

    这就是 rootless 容器（Podman/Docker rootless）的起点：
    在宿主机上你还是 uid=1000，但在 namespace 内你是 uid=0。
    """
    real_uid, real_gid = os.getuid(), os.getgid()
    os.unshare(os.CLONE_NEWUSER)
    # setgroups 必须先禁掉，否则非特权用户没资格写 gid_map
    with open("/proc/self/setgroups", "w") as f:
        f.write("deny")
    with open("/proc/self/uid_map", "w") as f:
        f.write(f"0 {real_uid} 1")      # 容器内 0  ==  宿主机 real_uid
    with open("/proc/self/gid_map", "w") as f:
        f.write(f"0 {real_gid} 1")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    rootfs = os.path.abspath(sys.argv[1])
    if not os.path.isdir(rootfs):
        sys.exit(f"rootfs 不存在: {rootfs}\n先运行: python3 make_rootfs.py")

    argv = sys.argv[2:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        argv = ["/bin/sh"]

    become_root_in_user_namespace()

    pid = os.fork()
    if pid == 0:
        # ---------- 子进程：跳进新根，留下"证据"，然后变成 shell ----------
        os.chroot(rootfs)
        os.chdir("/")

        print("┌─ 进入 chroot 后的证据 ────────────────────────")
        print(f"│ 我在新根里的 cwd = {os.getcwd()}")
        print(f"│ / 的内容 = {sorted(os.listdir('/'))}")
        with open("/etc/os-release", encoding="utf-8") as f:
            pretty = [l for l in f if l.startswith("PRETTY_NAME")][0].strip()
        print(f"│ {pretty}")
        print(f"│ 看到的 hostname = {socket.gethostname()}  ← 仍是宿主机（没做 UTS 隔离）")
        print(f"│ /proc 里有 {len(os.listdir('/proc'))} 个条目 ← 宿主机的进程全被挡在外面")
        print("└──────────────────────────────────────────────")
        print()

        env = {
            "PATH": "/bin:/usr/bin",
            "HOME": "/root",
            "TERM": os.environ.get("TERM", ""),
            "PS1": "[lesson1 \\u@chroot \\w]# ",
        }
        try:
            # exec 会直接替换进程映像，缓冲区里没刷出去的字会永远丢失
            sys.stdout.flush()
            sys.stderr.flush()
            os.execvpe(argv[0], argv, env)
        except Exception as e:
            print(f"启动命令失败: {e}", file=sys.stderr)
            os._exit(127)

    # ---------- 父进程：在"外面"等容器结束 ----------
    _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status))


if __name__ == "__main__":
    main()
