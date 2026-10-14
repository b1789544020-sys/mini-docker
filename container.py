#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
container.py —— MiniDocker 的零件库

把"造一个容器"拆成可以单独理解的 Linux 原语：
  1. user namespace + uid_map ：普通用户在容器内当 root（rootless 的核心）
  2. PID/UTS/IPC/MNT(/NET) namespace：进程号、主机名、IPC、挂载点、网卡各自独立
  3. chroot                   ：切换根文件系统
  4. 在新 namespace 里挂自己的 /proc（tmpfs /tmp、bind-mount 私有 /etc/hosts）
  5. spawn()：runc 同款两级 fork + 两阶段管道握手（详见函数 docstring）

全部只用 Python 标准库；mount 操作直接用 ctypes 调 mount(2) 系统调用，
网络操作通过 network.py 调宿主机 iproute2，没有任何魔法。
"""
import ctypes
import ctypes.util
import os
import signal
import socket
import sys
import tempfile

import network

# 第 2 课用的"全家桶"：user 必须在最前/与其他 flag 同批 unshare
#  - NEWUSER 我们是新 namespace 里的 root
#  - NEWPID  容器内进程号从 1 重新开始（调用者之后 fork 出的第一个孩子才是 PID 1）
#  - NEWUTS  容器可以有自己的 hostname
#  - NEWIPC  System V IPC、POSIX 消息队列隔离
#  - NEWNS   挂载点视图隔离（挂自己的 /proc 不会影响宿主机）
BASE_FLAGS = (
    os.CLONE_NEWUSER
    | os.CLONE_NEWPID
    | os.CLONE_NEWUTS
    | os.CLONE_NEWIPC
    | os.CLONE_NEWNS
)

# 真 root 时不需要 NEWUSER：没有"普通用户提权"这回事，直接建其余 4 个 ns。
# 反而带上 NEWUSER 会让 rootfs 里 uid=1000 拥有的文件变成"未映射的 nobody"，
# 在 750 的家目录上连读带 traverse 都被 DAC 拒绝（chroot EACCES）。
ROOT_FLAGS = (
    os.CLONE_NEWPID
    | os.CLONE_NEWUTS
    | os.CLONE_NEWIPC
    | os.CLONE_NEWNS
)


def _write(path, content):
    with open(path, "w") as f:
        f.write(content)


def map_caller_as_root(real_uid, real_gid):
    """在当前刚创建的 user namespace 里建立身份映射：

        容器内 uid 0  <->  宿主机上"你"的 uid   （只映射这一个 id）
    没被映射到的宿主机 uid，在容器内一律显示为 nobody(65534)。

    注意：real_uid/gid 必须在 unshare() 【之前】取好 —— unshare 之后
    os.getuid() 看到的就是新 namespace 里的 0 了，再拿去写映射只会 EPERM。
    """
    _write("/proc/self/setgroups", "deny")   # 非特权用户建 gid_map 的前置条件
    _write("/proc/self/uid_map", f"0 {real_uid} 1")
    _write("/proc/self/gid_map", f"0 {real_gid} 1")


def unshare(flags):
    """把当前进程放进一组新的 namespace；含 NEWUSER 时顺手配好 id 映射。

    关键细节：NEWUSER 必须【单独先 unshare】，写完 uid/gid 映射、
    在新 userns 里拿到全部 capability 之后，再 unshare 其余 namespace。
    一次性把 NEWUSER|NEWPID|... 丢进去，写 uid_map 会 EPERM。
    （系统的 unshare(1) 命令内部也是这么分两步做的。）
    """
    user_flag = flags & os.CLONE_NEWUSER
    other_flags = flags & ~os.CLONE_NEWUSER
    if user_flag:
        real_uid, real_gid = os.getuid(), os.getgid()   # 必须在 unshare 前取
        os.unshare(os.CLONE_NEWUSER)
        map_caller_as_root(real_uid, real_gid)
    if other_flags:
        os.unshare(other_flags)


# ---- 挂载：直接调 mount(2) 系统调用，不依赖 rootfs 里的 mount(8) ----
# 这样还有个额外好处：util-linux 的 mount 是 setuid 程序，在 namespace 里
# 会因 suid 语义走"受限模式"；系统调用则直来直去，runc 也是这么干的。
MS_RDONLY = 1
MS_NOSUID = 2
MS_NOEXEC = 8
MS_RELATIME = 1 << 21
MS_REC = 1 << 14
MS_PRIVATE = 1 << 18
MS_BIND = 4096

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_libc.mount.restype = ctypes.c_int
_libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                        ctypes.c_ulong, ctypes.c_void_p]
_libc.umount.restype = ctypes.c_int
_libc.umount.argtypes = [ctypes.c_char_p]


def _mount(source, target, fstype, flags=0):
    """mount(2) 的薄封装，失败按 errno 抛 OSError。"""
    if _libc.mount(source.encode(), target.encode(),
                   fstype.encode() if fstype else None, flags, None) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def make_mounts_private(target="/"):
    """把本挂载 namespace 里的传播属性改成 private（MS_REC 递归）。

    systemd 默认把 / 设成 shared（新挂载会"传播"回宿主机）。
    不先做这一步，我们在容器里 mount proc 可能出现在宿主机上 —— 那就穿帮了。
    """
    _mount("", target, None, MS_REC | MS_PRIVATE)


def mount_in_new_root():
    """chroot 之后调用：给迷你世界挂上 proc 和 tmpfs。

    proc   ：新 PID namespace 的进程快照（PID 从 1 开始），ps/free/top 全靠它
    tmpfs  ：内存文件系统当 /tmp；在 user namespace 里允许挂载
    """
    try:
        _mount("proc", "/proc", "proc", MS_NOSUID | MS_NOEXEC | MS_RELATIME)
    except OSError as e:
        print(f"[警告] 挂载 /proc 失败: {e}", file=sys.stderr)
    try:
        _mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_RELATIME)
        os.chmod("/tmp", 0o1777)
    except OSError:
        pass


def install_hosts(rootfs, hostname=None, net=None):
    """给容器挂一份【私有】 /etc/hosts（在 chroot 前、私有 mnt ns 里做）。

    Docker/runc 也是每容器 bind-mount 自己的 hosts/hostname/resolv.conf。
    不做这一步的真实后果：Python http.server 启动时 getfqdn() 要反查
    容器主机名，files 里查不到就去问 DNS —— 没 NAT 的容器里 DNS 超时，
    监听 socket 停在 bind 完不 listen 的状态能卡半分钟（我们踩过）。
    """
    lines = ["127.0.0.1 localhost localhost.localdomain",
             "::1       localhost ip6-localhost ip6-loopback"]
    if hostname:
        ip = net.ip if net else "127.0.0.1"
        lines.append(f"{ip} {hostname}")
    fd, tmp = tempfile.mkstemp(prefix="minidocker-hosts-", dir="/tmp")
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode())
        os.close(fd)
        try:
            _mount(tmp, rootfs.rstrip("/") + "/etc/hosts", None, MS_BIND)
        except OSError:
            pass   # bind 失败不致命（rootfs 里还有占位 hosts）
    finally:
        try:
            os.unlink(tmp)        # 目录项删掉，挂载点继续持有 inode
        except FileNotFoundError:
            pass


def enter_container(rootfs, hostname=None, mount_proc=True):
    """子进程侧：布置"新世界"。调用后进程已 chroot 在 rootfs 内。"""
    if hostname:
        socket.sethostname(hostname)      # 需要 NEWUTS（Python 把它放在 socket 模块）
    os.chroot(rootfs)                      # 需要 NEWUSER 给的 CAP_SYS_CHROOT
    os.chdir("/")
    if mount_proc:
        mount_in_new_root()


def container_env(extra=None):
    env = {
        "PATH": "/bin:/usr/bin",
        "HOME": "/root",
        "TERM": os.environ.get("TERM", ""),
        "PS1": "[mini \\u@\\h \\w]# ",
        "PYTHONDONTWRITEBYTECODE": "1",   # 别在 rootfs 里落 .pyc（还可能因属主不同污染重建）
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    if extra:
        env.update(extra)
    return env


def prepare_parent(flags=BASE_FLAGS):
    """父进程侧的准备工作，返回时【只差 fork】。

    顺序是踩过坑的：
      1. 先建 user namespace、配 uid 映射；
      2. 再建 mnt/uts/ipc 等 namespace，并用子进程跑 mount --make-rprivate；
      3. 最后才建 PID namespace。
    原因：PID namespace 中第一个 fork 出的孩子就是 PID 1，PID 1 一旦退出，
    整个 namespace 立刻销毁、后续 fork 全部 ENOMEM。所以 NEWPID 之后，
    父进程绝不能再为别的事情（比如跑 mount 命令）fork。
    """
    pid_flag = flags & os.CLONE_NEWPID
    early_flags = flags & ~os.CLONE_NEWPID
    unshare(early_flags)
    if flags & os.CLONE_NEWNS:
        make_mounts_private()
    if pid_flag:
        os.unshare(os.CLONE_NEWPID)


class Container:
    """wait=False 时返回的句柄（持有父进程视角下布置者 C 的 pid）。

    .kill() 顺着 /proc/<c>/task/<c>/children 把容器 PID 1 及其后代、
    以及 C 本身全部 SIGKILL 并回收。父进程一直在宿主机 pidns，随时能 fork。
    """

    def __init__(self, c_pid):
        self.c_pid = c_pid

    @staticmethod
    def _descendants(pid):
        try:
            with open(f"/proc/{pid}/task/{pid}/children") as f:
                kids = [int(x) for x in f.read().split()]
        except (FileNotFoundError, ProcessLookupError, ValueError):
            return []
        out = list(kids)
        for k in kids:
            out.extend(Container._descendants(k))
        return out

    def kill(self):
        for p in self._descendants(self.c_pid) + [self.c_pid]:
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.waitpid(self.c_pid, 0)
        except ChildProcessError:
            pass


def spawn(rootfs, argv, flags=BASE_FLAGS, hostname=None, mount_proc=True,
          after_fork_parent=None, net=None, wait=True):
    """runc 同款两级 fork；父进程【始终留在宿主机 namespace】。

        P (宿主机 ns)
        └─fork─ C 布置者：unshare user/net/uts/ipc/mnt、网络握手、改主机名、
                 chroot；最后才 unshare(NEWPID) 并立刻 fork
                 └─ G：容器 PID 1，挂 /proc、配网卡、exec 用户命令
                 C 阻塞 waitpid(G)，把 G 的退出状态经管道回传 P

    为什么不让父进程自己 unshare（第 1~3 课的简化做法）？
      - 父进程进了新 PID ns 就成为它的成员，wait=False 时容器 PID 1 一死，
        父进程再 fork 直接 ENOMEM；多个容器还会层层嵌套 namespace。
      - 配网络要求父进程身在初始 netns，才能建 veth 并 `ip ... netns pid`。

    after_fork_parent(c_pid)：C 一出生（宿主机 pid、尚未 unshare）即调用，
    用来把 C 塞进 cgroup —— 成员身份随 fork 继承，G 自动落在组里。
    net 给 network.NetSpec 即开网络隔离。wait=False 返回 Container 句柄；
    否则返回容器退出码（被信号杀为负数，与 waitstatus_to_exitcode 一致）。
    """
    # fork 前必须 flush：否则孩子继承块缓冲，会把父进程已缓冲的输出在
    # exec 前再刷一遍（我们真踩过：所有标题被打印两次）。
    sys.stdout.flush()
    sys.stderr.flush()

    # 一根上行一根下行，串起两阶段握手（见 _child_setup 注释）
    rdy_r, rdy_w = os.pipe()                        # C -> P：1=出生 2=netns 就绪
    go_r, go_w = os.pipe()                          # P -> C：g=放行
    err_r, err_w = os.pipe()                        # C -> P：布置阶段报错
    st_r, st_w = os.pipe()                          # C -> P：G 的退出状态

    c_pid = os.fork()
    if c_pid == 0:
        _child_setup(rootfs, argv, flags, hostname, mount_proc, net,
                     rdy_r, rdy_w, go_r, go_w, err_w, st_w)
        raise RuntimeError("unreachable")          # _child_setup 不返回

    for fd in (rdy_w, go_r, err_w, st_w):
        os.close(fd)

    try:
        os.read(rdy_r, 1)                           # 阶段 0：等 C 出生
        # 必须在 C 继续跑（fork 出 G）之前把它挪进 cgroup，否则 G 会生在
        # init.scope —— cgroup 成员只能随 fork 继承，事后再移就晚了（踩过）。
        if after_fork_parent is not None:
            after_fork_parent(c_pid)
        os.write(go_w, b"g")
        if net is not None:
            os.read(rdy_r, 1)                       # 阶段 1：等 C 的 netns 诞生
            network.attach_to_child(net, c_pid)    # C 此时仍在父 PID ns
            os.write(go_w, b"g")
    except BaseException:
        os.close(go_w)                              # C 读到 EOF 自行了断
        try:
            os.kill(c_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(c_pid, 0)
        raise

    if not wait:
        return Container(c_pid)

    _, c_status = os.waitpid(c_pid, 0)
    error = _read_all(err_r)
    payload = _read_all(st_r)
    if error:
        print(error.decode(errors="replace"), file=sys.stderr)
    if len(payload) == 2:                          # 优先 G 的真实状态
        kind, val = payload
        return -val if kind == ord("S") else val
    # C 自己被信号杀（如 cgroup.kill），状态管道来不及写：退回 C 的死因
    return os.waitstatus_to_exitcode(c_status)


def _read_all(fd):
    chunks = []
    while True:
        b = os.read(fd, 4096)
        if not b:
            return b"".join(chunks)
        chunks.append(b)


def _child_setup(rootfs, argv, flags, hostname, mount_proc, net,
                 rdy_r, rdy_w, go_r, go_w, err_w, st_w):
    """孩子 C 的一生：成功不返回 —— 要么变成 wait 循环，G 才是真正的容器。"""
    os.close(rdy_r)
    os.close(go_w)

    def fail(e):
        try:
            os.write(err_w, f"容器启动失败: {e}\n".encode())
        except OSError:
            pass
        os._exit(127)

    def gate(msg):
        """报告状态并等父进程放行；父进程出错关管道则退出。"""
        os.write(rdy_w, msg)
        if os.read(go_r, 1) != b"g":
            os._exit(127)

    try:
        # 0) 阶段握手：一出生先停住，让父进程把 C 挪进 cgroup（G 之后随
        #    fork 继承）。不在这里等，G 可能在 attach 完成前就生到 init.scope。
        gate(b"1")

        # 1) user namespace（若需要）：身份必须在 unshare 前抓；自己写映射，
        #    等价于系统命令 unshare -r（不依赖父进程帮忙写 uid_map）。
        if flags & os.CLONE_NEWUSER:
            real_uid, real_gid = os.getuid(), os.getgid()
            os.unshare(os.CLONE_NEWUSER)
            map_caller_as_root(real_uid, real_gid)

        # 2) net/uts/ipc/mnt 一起建（NEWPID 永远压轴）
        early = flags & ~(os.CLONE_NEWUSER | os.CLONE_NEWPID)
        if net is not None:
            early |= os.CLONE_NEWNET
        if early:
            os.unshare(early)

        # 3) 网络握手：报告"新 netns 已就位"，等父进程把 veth 塞进来
        if net is not None:
            gate(b"2")
        os.close(rdy_w)
        os.close(go_r)

        # 4) 挂载传播私有化、主机名、chroot（G 全部原样继承）
        if flags & os.CLONE_NEWNS:
            make_mounts_private()
        if hostname:
            socket.sethostname(hostname)
        install_hosts(rootfs, hostname=hostname, net=net)
        os.chroot(rootfs)
        os.chdir("/")

        # 5) NEWPID 压轴，之后【立刻】fork 容器 PID 1（README 坑 #3）
        if flags & os.CLONE_NEWPID:
            os.unshare(os.CLONE_NEWPID)
        g_pid = os.fork()
    except Exception as e:                      # noqa: BLE001
        fail(e)

    if g_pid == 0:
        # ---------- G：容器 PID 1 ----------
        try:
            if mount_proc:
                mount_in_new_root()
            if net is not None:
                network.configure_inside(net.as_child_dict())
            sys.stdout.flush()
            sys.stderr.flush()
            os.execvpe(argv[0], argv, container_env())
        except Exception as e:                  # noqa: BLE001
            print(f"容器启动失败: {e}", file=sys.stderr)
            os._exit(127)

    # ---------- C：等待 G，透传真实退出状态后退出 ----------
    _, g_status = os.waitpid(g_pid, 0)
    code = os.waitstatus_to_exitcode(g_status)   # 0..255 正常退出；负数=信号
    try:
        if code < 0:
            os.write(st_w, bytes([ord("S"), -code]))
        else:
            os.write(st_w, bytes([ord("E"), code]))
    except OSError:
        pass
    os._exit(0)

