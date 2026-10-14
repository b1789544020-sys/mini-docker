#!/usr/bin/env bash
# 第 2 课测试：UTS/PID/MNT 隔离全部生效
set -u
cd /mnt/c/Users/86159/.cline/data/workspaces/chat/mini-docker
ROOTFS=/home/user/mini-docker-rootfs

echo "========== 宿主机（外面的世界） =========="
echo "hostname = $(hostname)"
echo "进程总数 = $(ps -e --no-headers | wc -l)"
echo

python3 lesson2_namespaces.py "$ROOTFS" -- /bin/sh -c '
echo "========== 容器内（新 namespace） =========="
echo "[hostname]   $(hostname)"
echo "[id]"; id
echo
echo "[ps -ef] 注意 PID：sh 是 1 号"
ps -ef
echo
echo "[free] 第1课时 /proc 是空的，现在挂了自己的 proc，free 能用了"
free -m | head -3
echo
echo "[mount] 容器里的 proc 挂载点"
mount | grep " /proc"
echo
echo "[python 看到的自己] 应该是 PID 2（sh fork 出来的）"
python3 -c "import os; print(\"pid =\", os.getpid(), \"ppid =\", os.getppid())"
'
echo
echo "========== 回到宿主机，hostname 还是 =========="
hostname
