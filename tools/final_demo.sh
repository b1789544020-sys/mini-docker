#!/usr/bin/env bash
# 毕业演示：宿主机 vs 迷你容器，一眼看出"两台机器"的幻觉
cd /mnt/c/Users/86159/.cline/data/workspaces/chat/mini-docker
echo "=== 项目文件 ==="
find . -name __pycache__ -prune -o -type f -print | sort
echo
echo "=== 宿主机（外面的世界） ==="
echo "hostname = $(hostname)"
echo "进程数   = $(ps -e --no-headers | wc -l)"
echo "根目录   = $(ls / | wc -l) 个条目"
echo
echo "=== minidocker run 出来的容器 ==="
python3 minidocker.py run --name graduation /home/user/mini-docker-rootfs -- /bin/sh -c '
  echo "hostname = $(hostname)"
  echo "进程数   = $(ps -e --no-headers | wc -l)   （只有 PID 1 的我自己）"
  echo "根目录   = $(ls / | wc -l) 个条目         （是迷你 rootfs，不是宿主机）"
  echo "我的 PID = $$"
  echo "内存视图 = $(free -m | awk "NR==2 {print \$2\" MB total\"}")"
'
echo
if [ "$(id -u)" = "0" ]; then
  echo "=== 第 4 课：容器有自己的网卡和 IP（--network，真 root） ==="
  python3 minidocker.py run --network --name gradnet /home/user/mini-docker-rootfs -- /bin/sh -c '
    ip -br addr
    echo "ping 网关 10.87.0.1：$(ping -c1 -W1 10.87.0.1 >/dev/null 2>&1 && echo 通 || echo 不通)"
  '
  echo
fi
echo "=== 容器退出后，宿主机毫发无伤 ==="
echo "hostname = $(hostname)"
