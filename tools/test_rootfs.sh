#!/usr/bin/env bash
# 验证 rootfs 里的程序真的能跑（用系统 unshare 提供 user namespace）
ROOTFS=${1:-/home/user/mini-docker-rootfs}
unshare -Ur chroot "$ROOTFS" /bin/sh <<'EOF'
echo "--- id ---"
id
echo "--- ls / ---"
ls /
echo "--- cat /etc/os-release ---"
cat /etc/os-release
echo "--- python3 计算 ---"
python3 -c "print('2**10 =', 2**10)"
echo "--- ps（没有挂载 /proc，预期失败/为空，这正是第2课要解决的） ---"
ps aux 2>&1 | head -3
echo "--- hostname（第1课没做 UTS 隔离，看到的会是宿主机名） ---"
hostname
EOF
