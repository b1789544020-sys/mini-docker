#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_rootfs.py —— 零下载制造一个迷你 Linux 根文件系统（rootfs）

【原理】
  一个动态链接的可执行程序并不能单独运行，它启动时需要：
    1. ELF 二进制本身（如 /usr/bin/dash）
    2. 动态链接器（PT_INTERP，通常是 /lib64/ld-linux-x86-64.so.2）
    3. 它依赖的共享库（libc.so.6 等，可以用 `ldd 程序` 问出来）
  我们把这些文件按宿主机上的【原目录结构】抄进一个新目录，
  再补上 proc/sys/tmp 等空目录和最小的 /etc 文件 —— 它就是一个能 chroot 进去的世界。

用法：
  python3 make_rootfs.py [rootfs目录]        # 默认 ~/mini-docker-rootfs
"""
import glob
import os
import re
import shutil
import subprocess
import sys

# 想塞进迷你世界的命令（用 which 在宿主机 PATH 里找）
WANT_BINARIES = [
    "sh", "ls", "cat", "ps", "mount", "hostname", "id", "free",
    "sleep", "uname", "kill", "mkdir", "echo", "yes", "env", "python3",
    "head", "tail", "grep", "clear", "umount", "wc", "awk", "which",
    "ip", "ping",          # 第 4 课：容器内查看网卡 / 测连通
]

# 普通用户的 PATH 通常不含 sbin，但 ip 等网络命令住在 /usr/sbin
SBIN_EXTRA = ("/usr/sbin", "/sbin", "/usr/local/sbin")

# Python 标准库里这些大件在容器演示中用不到，抄库时跳过
PYLIB_SKIP_DIRS = {
    "test", "tests", "ensurepip", "idlelib", "tkinter",
    "turtledemo", "lib2to3", "site-packages", "dist-packages",
    "__pycache__", "pydoc_data",
}

_installed = set()


def put_file(src, root):
    """把宿主机文件 src 按同样的相对路径装进 rootfs。

    - 自动跟随符号链接：真实文件抄到 realpath 位置；
    - 原路径如果是符号链接（如 /bin/sh -> /usr/bin/dash），照样建一个。
    """
    src = os.path.abspath(src)
    real = os.path.realpath(src)
    if real not in _installed and os.path.isfile(real):
        dst = root + real
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(real, dst)
        shutil.copystat(real, dst)
        # 剥掉 setuid/setgid/sticky 位：副本的属主是构建用户而不是原属主，
        # 保留 suid 会让真 root 执行时 euid 被降到 1000（mount(8) 就这么翻车的）。
        mode = os.stat(real).st_mode & 0o7777
        os.chmod(dst, mode & ~0o7000)
        _installed.add(real)
    if real != src:
        link_path = root + src
        if not os.path.lexists(link_path):
            # link_path 可能位于 /lib 这种"指向 usr/lib 的符号链接"里，
            # 所以按它的【物理目录】计算相对目标，不能按逻辑路径算。
            phys_dir = os.path.realpath(os.path.dirname(link_path))
            os.makedirs(phys_dir, exist_ok=True)
            os.symlink(os.path.relpath(root + real, phys_dir), link_path)


# Ubuntu/Debian usrmerge 后，顶层这些目录全部是指向 usr/ 的符号链接
USRMERGE_LINKS = {
    "/bin": "usr/bin",
    "/sbin": "usr/sbin",
    "/lib": "usr/lib",
    "/lib64": "usr/lib64",
}


def mirror_usrmerge(root):
    for link, target in USRMERGE_LINKS.items():
        os.makedirs(root + "/" + target, exist_ok=True)
        link_path = root + link
        if os.path.islink(link_path):
            continue
        if os.path.isdir(link_path) and not os.listdir(link_path):
            os.rmdir(link_path)      # 空的真目录（旧构建残留）换成符号链接
        if not os.path.exists(link_path):
            os.symlink(target, link_path)


_LDD_RE = re.compile(r"(/[^\s()]+\.so[^\s()]*)")


def ldd_deps(binary):
    """执行 ldd，解析出二进制依赖的所有 .so 绝对路径（含动态链接器）。"""
    out = subprocess.run(
        ["ldd", binary], capture_output=True, text=True
    ).stdout
    deps = []
    for line in out.splitlines():
        if "statically linked" in line:
            continue
        deps.extend(_LDD_RE.findall(line))
    return deps


def find_binary(name):
    """先问 PATH，再到 sbin 目录里找（普通用户 PATH 默认不含 /usr/sbin）。"""
    found = shutil.which(name)
    if found:
        return found
    for d in SBIN_EXTRA:
        cand = os.path.join(d, name)
        if os.path.isfile(cand):
            return cand
    return None


def ignore_pylib(_dir, names):
    return [n for n in names if n in PYLIB_SKIP_DIRS or n.endswith((".pyc", ".pyo"))]


def write_text(root, path, content, mode=0o644):
    full = root + path
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(full, mode)


def build(root):
    print(f"[1/4] 目标 rootfs: {root}")
    mirror_usrmerge(root)
    bins = []
    missing = []
    for name in WANT_BINARIES:
        path = find_binary(name)
        if path:
            bins.append(path)
        else:
            missing.append(name)
    if missing:
        print(f"      宿主机缺少这些命令，已跳过: {' '.join(missing)}")

    print(f"[2/4] 抄入 {len(bins)} 个二进制及其依赖库 ...")
    for b in bins:
        real = os.path.realpath(b)
        put_file(b, root)
        for dep in ldd_deps(real):
            put_file(dep, root)
        print(f"      ✓ {b}")

    # NSS：让 id/ps 能把 uid 0 解析成 root 这个名字
    for nss in glob.glob("/lib/x86_64-linux-gnu/libnss_files*.so*"):
        put_file(nss, root)

    print("[3/4] 抄入 Python 标准库（精简版）...")
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    src_pylib = f"/usr/lib/python{pyver}"
    if os.path.isdir(src_pylib):
        shutil.copytree(
            src_pylib, f"{root}{src_pylib}",
            ignore=ignore_pylib, dirs_exist_ok=True,
        )
        print(f"      ✓ {src_pylib}")
    else:
        print(f"      ! 未找到 {src_pylib}，容器内 python3 将不可用")

    print("[4/4] 写入目录骨架与 /etc 配置 ...")
    for d in ["/proc", "/sys", "/dev", "/tmp", "/root", "/home/user",
              "/etc", "/var/log"]:
        os.makedirs(root + d, exist_ok=True)
    os.chmod(root + "/tmp", 0o1777)

    write_text(root, "/etc/passwd",
               "root:x:0:0:root:/root:/bin/sh\n"
               "user:x:1000:1000:user:/home/user:/bin/sh\n")
    write_text(root, "/etc/group",
               "root:x:0:\nuser:x:1000:\nnogroup:x:65534:\n")
    write_text(root, "/etc/nsswitch.conf",
               "passwd: files\ngroup: files\nhosts: files dns\n")
    write_text(root, "/etc/hostname", "mini-container\n")
    write_text(root, "/etc/hosts",
                "# 占位文件：每次启动容器会被私有 hosts 表 bind-mount 覆盖（Docker 同款）\n"
                "127.0.0.1 localhost localhost.localdomain\n"
                "::1       localhost ip6-localhost ip6-loopback\n")
    write_text(root, "/etc/resolv.conf",
                "# 第 4 课：容器走 mini0 网桥时用它解析域名\n"
                "# （仅当宿主机做了 NAT MASQUERADE 后才能真正出外网）\n"
                "nameserver 1.1.1.1\n"
                "nameserver 8.8.8.8\n")
    write_text(root, "/etc/profile",
               'export PS1="[mini-container \\u@\\h \\w]# "\n'
               "export HOME=/root PATH=/bin:/usr/bin\n")
    write_text(root, "/etc/os-release",
               'NAME="MiniDocker"\n'
               'PRETTY_NAME="MiniDocker 迷你容器 (rootfs 从宿主机 ldd 抠来)"\n'
               'HOME_URL="about:blank"\n')
    write_text(root, "/welcome.txt",
               "====================================================\n"
               "  欢迎来到 MiniDocker 迷你容器！\n"
               "  这里的 / 不是宿主机的 /，你已经被 chroot 关起来了。\n"
               "  试着执行: ls /  cat /etc/os-release  id\n"
               "====================================================\n")

    print("\n构建完成。可以从第 1 课开始：")
    print("  python3 lesson1_chroot.py " + root)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/mini-docker-rootfs")
    build(target)
