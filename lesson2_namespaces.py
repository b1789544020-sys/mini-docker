#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 2 课：namespaces —— 给容器"变出一台新机器"的错觉

第 1 课的 chroot 只关了文件系统，进程、主机名还在和宿主机共享。
这一课一次加入 5 种 namespace（全部通过一个 unshare() 系统调用）：

  CLONE_NEWUSER  容器内你是 root，宿主机上你还是普通用户（免 sudo 的关键）
  CLONE_NEWPID   容器内进程号从 1 重新计数，第一个进程就是 PID 1
  CLONE_NEWUTS   容器有独立 hostname，随便改不影响宿主机
  CLONE_NEWIPC   IPC 对象隔离（消息队列/共享内存互不可见）
  CLONE_NEWNS    挂载点隔离，容器里挂自己的 /proc，宿主机毫无知觉

上完这课你应该能观察到（对比第 1 课）：
  * hostname 变成 mini-box，宿主机 hostname 纹丝不动
  * ps -ef 只能看到容器内的一两个进程，自己就是 PID 1
  * free / ps 恢复正常 —— 因为我们挂载了【属于新 PID namespace】的 /proc

用法：
  python3 lesson2_namespaces.py ~/mini-docker-rootfs
  python3 lesson2_namespaces.py ~/mini-docker-rootfs -- /bin/sh -c 'ps -ef; free; hostname'
"""
import os
import socket
import sys

import container


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

    # 父进程：user/uts/ipc/mnt 先就位、挂载传播改 private，最后才进 PID namespace
    # （PID namespace 里第一个孩子必须就是容器本身，否则短命的 PID 1 会"带走"整个 namespace）
    container.prepare_parent(container.BASE_FLAGS)

    pid = os.fork()
    if pid == 0:
        # 子进程：它是新 PID namespace 里的第一个进程 -> PID 1
        container.enter_container(rootfs, hostname="mini-box")

        proc_pids = sorted(x for x in os.listdir("/proc") if x.isdigit())
        print("┌─ 进入新 namespace 后的证据 ───────────────────")
        print(f"│ 我的 PID = {os.getpid()}（新 PID namespace 的 1 号进程）")
        print(f"│ hostname = {socket.gethostname()}（UTS 已隔离，宿主机不受影响）")
        print(f"│ /proc 里的进程 = {proc_pids}（只有我自己）")
        print("│ 接下来 exec 的 shell 将接替我成为 PID 1")
        print("└──────────────────────────────────────────────")
        print()
        sys.stdout.flush()

        try:
            sys.stdout.flush()
            sys.stderr.flush()
            os.execvpe(argv[0], argv, container.container_env())
        except Exception as e:                                  # noqa: BLE001
            print(f"启动命令失败: {e}", file=sys.stderr)
            os._exit(127)

    _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status))


if __name__ == "__main__":
    main()
