#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
network.py —— 第 4 课的零件：给容器接一根"虚拟网线"

拓扑（和 Docker 默认的 docker0 + veth 完全同构）：

    容器 A netns                容器 B netns
   ┌──────────┐                ┌──────────┐
   │ eth0     │                │ eth0     │
   │.2/24     │                │.3/24     │
   └───┬──────┘                └───┬──────┘
       │ veth pair                  │ veth pair
   mda2/mdo2 ...              mda3/mdo3 ...
       └──────────┬─────┬───────────┘
              mini0 (bridge, 10.87.0.1/24)  ← 住在宿主机初始 netns
                          │
                        eth0（宿主机出口；本环境无 nft/iptables，故不做 NAT）

关键设计：veth 的"宿主机端"必须由仍处在初始 netns 的进程创建并
`ip link set ... netns <pid>` 推进容器，所以 NEWNET 不能由父进程 unshare
（父进程 unshare 了它自己就跑到新 netns 里去了），而是 fork 后由孩子建立，
父子之间用管道握手 —— 详见 container.py 的 spawn()。

全部只用标准库，网络操作通过宿主机的 iproute2（/usr/sbin/ip）完成，
和 Docker 早期调 netlink / 现在的 netavark 干的是同一件事。
"""
import os
import subprocess

# 宿主机上的 ip(8)（root 的 PATH 里有 /usr/sbin，这里写死更稳）
for _cand in ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip"):
    if os.path.exists(_cand):
        IP = _cand
        break
else:
    IP = "ip"

# 容器内 rootfs 里的 ip(8)（usrmerge 后也在 /usr/sbin）
IP_IN_CONTAINER = "/usr/sbin/ip"

BRIDGE = "mini0"
GATEWAY = "10.87.0.1"
_SUBNET = "10.87.0."
_MASK = 24
STATE_DIR = "/run/minidocker-net"   # 一个 id 一个文件，内容是它分到的 IP
_HOST_VETH = "mda{}"                # 网线的宿主机端（接 bridge）
_PEER_VETH = "mdo{}"                # 网线推进容器前的临时名字（进去改名 eth0）


class NetSpec:
    """一个容器分到的网络配置。"""

    def __init__(self, alloc_id):
        self.id = alloc_id
        self.ip = f"{_SUBNET}{alloc_id}"
        self.gw = GATEWAY
        self.host_if = _HOST_VETH.format(alloc_id)
        self.peer_if = _PEER_VETH.format(alloc_id)

    def as_child_dict(self):
        """孩子 chroot 后自配置网卡只需要这三样。"""
        return {"peer": self.peer_if, "ip": self.ip,
                "mask": _MASK, "gw": self.gw}


# ----------------------- ip(8) 小封装 -----------------------

def _ip(args, check=True):
    """在【宿主机侧】调 ip（父进程始终留在初始 netns）。"""
    return subprocess.run([IP] + args, capture_output=True, text=True,
                          check=check)


def _ip_in_container(args):
    """在【容器内】调 ip：chroot 之后由孩子自己执行。"""
    r = subprocess.run([IP_IN_CONTAINER] + args,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[警告] 容器内 ip {' '.join(args)} 失败: "
              f"{r.stderr.strip()}", flush=True)


# ----------------------- 网桥与 IP 分配 -----------------------

def ensure_bridge():
    """幂等创建 mini0 网桥（相当于 docker0）。

    bridge 是一台"虚拟交换机"：所有宿主机端 veth 都插在它上面，
    彼此二层互通；它自己的 .1 地址就是容器默认网关。
    """
    if _ip(["link", "show", BRIDGE], check=False).returncode != 0:
        _ip(["link", "add", BRIDGE, "type", "bridge"])
        _ip(["addr", "add", f"{GATEWAY}/{_MASK}", "dev", BRIDGE])
        _ip(["link", "set", BRIDGE, "up"])
        print(f"[网络] 已创建网桥 {BRIDGE} ({GATEWAY}/{_MASK})")


def allocate():
    """从 10.87.0.2 起分配一个没人占的 id（状态记在 /run）。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    for cand in range(2, 255):
        flag = f"{STATE_DIR}/{cand}"
        try:
            fd = os.open(flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.write(fd, f"{_SUBNET}{cand}\n".encode())
        os.close(fd)
        return NetSpec(cand)
    raise RuntimeError("10.87.0.0/24 地址耗尽")


def release(spec):
    """容器退出：拔网线（删宿主机端会连同对端一起销毁）并释放 IP。"""
    if spec is None:
        return
    _ip(["link", "del", spec.host_if], check=False)
    try:
        os.remove(f"{STATE_DIR}/{spec.id}")
    except FileNotFoundError:
        pass


# ----------------------- 父进程侧：造网线并推进容器 -----------------------

def attach_to_child(spec, child_pid):
    """父进程在握手里调用：建 veth pair，宿主机端插交换机，
    对端塞进 child_pid 所在的新 network namespace。"""
    _ip(["link", "add", spec.host_if, "type", "veth",
         "peer", "name", spec.peer_if])
    _ip(["link", "set", spec.host_if, "master", BRIDGE])
    _ip(["link", "set", spec.host_if, "up"])
    _ip(["link", "set", spec.peer_if, "netns", str(child_pid)])


# ----------------------- 孩子侧：chroot 后自配置 -----------------------

def configure_inside(net):
    """孩子在自己的 netns 里把推进来的网卡改名 eth0、配地址、起默认路由。

    lo 也要手动 up —— 新建 netns 里 lo 默认是 DOWN 的。
    """
    peer, ip, gw = net["peer"], net["ip"], net["gw"]
    _ip_in_container(["link", "set", "lo", "up"])
    _ip_in_container(["link", "set", peer, "name", "eth0"])
    _ip_in_container(["addr", "add", f"{ip}/{net['mask']}", "dev", "eth0"])
    _ip_in_container(["link", "set", "eth0", "up"])
    _ip_in_container(["route", "add", "default", "via", gw])
