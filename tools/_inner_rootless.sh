#!/usr/bin/env bash
# 综合测试第 1 段：以普通用户身份跑 rootless 容器（被 test_minidocker.sh 用 su 调起）
cd /mnt/c/Users/86159/.cline/data/workspaces/chat/mini-docker || exit 1
export LANG=C.UTF-8
python3 minidocker.py run --name web01 /home/user/mini-docker-rootfs -- /bin/sh -c '
  echo "hostname=$(hostname)"
  echo "shell 自己的 pid=$$（应该是 1）"
  ps -ef
  free -m | head -2
'
