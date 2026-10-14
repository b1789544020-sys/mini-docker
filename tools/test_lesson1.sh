#!/usr/bin/env bash
# 第 1 课测试：chroot 后文件系统变了，但 hostname/进程视图没变
set -u
cd /mnt/c/Users/86159/.cline/data/workspaces/chat/mini-docker
python3 make_rootfs.py >/dev/null
echo "宿主机视角: hostname=$(hostname)"
echo
python3 lesson1_chroot.py /home/user/mini-docker-rootfs -- /bin/sh -c '
  echo "[容器内执行] id:"; id
  echo "[容器内执行] hostname:"; hostname
  echo "[容器内执行] ls /proc 的结果（空，因为没挂 proc）:"; ls /proc
  echo "[容器内执行] ps 的结果:"; ps aux 2>&1 || true
  echo "[容器内执行] 想偷看宿主机文件?"; cat /etc/hostname 2>/dev/null; ls /mnt 2>/dev/null || echo "  /mnt 不存在，逃不出去"
'
