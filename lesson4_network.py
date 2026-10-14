#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 4 课：网络 namespace + veth pair + bridge —— 给容器插上网线

演示四件事（均为真 root 操作，rootless 无权在初始 netns 里建 veth）：
  A. 宿主机视角：mini0 网桥出现了，宿主机自己的 eth0 不受影响
  B. 容器视角：网卡只有 lo 和 eth0，IP 是 10.87.0.x，看不见宿主机 eth0
  C. 跨容器互通：web 容器在 rootfs 上开 HTTP 服务，client 容器用 HTTP 访问到它
  D. 边界：没有 NAT 之前，容器 ping 外网不通（docker0 能出网是因为 Docker
     还偷偷加了一条 MASQUERADE 规则）

拓扑见 network.py 顶部。rootfs 默认参数：~/mini-docker-rootfs
"""
import os
import subprocess
import sys
import time

import container
import network

ROOTFS = os.path.expanduser(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.expanduser("~/mini-docker-rootfs")


def sh_in(name, script, net, wait=True):
    """在网络容器里跑一段 sh（真 root，不要 NEWUSER）。"""
    return container.spawn(
        ROOTFS, ["/bin/sh", "-c", script],
        flags=container.ROOT_FLAGS,
        hostname=name, net=net, wait=wait,
    )


def host(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def title(t):
    print("\n" + "=" * 64)
    print(t)
    print("=" * 64)


def main():
    if os.geteuid() != 0:
        sys.exit("第 4 课需要真 root（veth/bridge 在初始 netns 创建）：\n"
                 "  wsl -d Ubuntu -u root   然后运行本脚本")
    if not os.path.isdir(ROOTFS):
        sys.exit(f"找不到 rootfs: {ROOTFS}，先 python3 make_rootfs.py")

    network.ensure_bridge()
    web_net = network.allocate()       # 10.87.0.2
    cli_net = network.allocate()       # 10.87.0.3
    web_pid = None

    title("A. 宿主机视角：mini0 这台'虚拟交换机'已经就绪")
    print(host(["ip", "-br", "link", "show"]))
    print("\n宿主机默认路由（容器看不见这些）:")
    print(host(["ip", "route", "show", "default"]))

    try:
        # ---- web 容器：常驻的 HTTP 服务，站点根目录就是容器的 / ----
        title(f"B. 启动 web 容器 {web_net.ip}（python -m http.server 8000）")
        web_pid = sh_in(
            "web",
            "exec python3 -u -m http.server 8000 --bind 0.0.0.0",
            web_net, wait=False,
        )
        print(f"web 容器 PID={web_pid.c_pid}，IP={web_net.ip}（未阻塞，继续往下走）")
        time.sleep(0.5)      # 等容器内 Python 把 socket 监听起来

        title("C. 宿主机 -> 容器：宿主机 ping 得通 web")
        r = subprocess.run(["ping", "-c1", "-W1", web_net.ip],
                           capture_output=True, text=True)
        stats = [ln for ln in r.stdout.splitlines() if "packet loss" in ln]
        print(stats[0] if stats else "(无输出)")

        # ---- client 容器：一系列证据 ----
        title(f"D. client 容器 {cli_net.ip} 视角：隔离 + 互通的全部证据")
        web_ip = web_net.ip
        client_script = f"""
echo "── 1) 我只有 lo 和 eth0，宿主机的 eth0 根本不在我的 netns 里 ──"
ip -br addr
echo
echo "── 2) 路由表：默认网关是 mini0 的 {network.GATEWAY} ──"
ip route
echo
echo "── 3) ping 网关（容器 <-> 宿主机网桥） ──"
ping -c1 -W1 {network.GATEWAY} | tail -2
echo
echo "── 4) ping web 容器 {web_net.ip}（容器 <-> 容器，经 mini0 二层转发） ──"
ping -c2 -W1 {web_net.ip} | tail -2
echo
echo "── 5) 真正的应用层流量：HTTP 取 web 容器根目录里的 welcome.txt ──"
python3 - <<'PY'
import time, urllib.request
url = "http://{web_ip}:8000/welcome.txt"
for i in range(25):
    try:
        body = urllib.request.urlopen(url, timeout=1).read().decode()
        print("HTTP GET", url, "成功，正文：")
        print("   " + body.splitlines()[1])
        break
    except Exception:
        time.sleep(0.2)
else:
    raise SystemExit("连不上 web 容器的 HTTP 服务")
PY
echo
echo "── 6) 外网？没有 NAT MASQUERADE，ping 1.1.1.1 预期失败 ──"
ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 \\
  && echo "意外：居然通了（你的环境可能有别的 NAT）" \\
  || echo "100% 丢包：数据包能到网关，但没人替容器地址做源地址转换"
"""
        rc = sh_in("client", client_script, cli_net)
        if rc != 0:
            sys.exit(f"client 容器实验失败（退出码 {rc}）")

        title("E. 网络拓扑盘点（宿主机视角，两根 veth 都插在 mini0 上）")
        print(host(["ip", "-br", "link", "show", "master", network.BRIDGE]))

        print("\n结论：每个容器有独立 netns/IP，容器经 bridge 二层互通，")
        print("宿主机可经 mini0 访问容器；要出外网还需一条 NAT，例如：")
        print("  nft add table ip minidocker")
        print("  nft add chain ip minidocker postrouting "
              "'{ type nat hook postrouting priority 100; }'")
        print(f"  nft add rule ip minidocker postrouting ip saddr "
              f"10.87.0.0/24 oifname eth0 masquerade")
    finally:
        if web_pid:
            web_pid.kill()
        network.release(web_net)
        network.release(cli_net)
        print(f"\n[清理] veth 与租约已释放（网桥 {network.BRIDGE} 保留，"
              f"删除用: ip link del {network.BRIDGE}）")


if __name__ == "__main__":
    main()
