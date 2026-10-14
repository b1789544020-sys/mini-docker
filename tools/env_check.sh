#!/usr/bin/env bash
# 第 0 课：环境侦察 —— 摸清这台 WSL2 对容器技术的支持情况
set +e

echo "==================== 1. 用户与系统 ===================="
id
uname -r
grep PRETTY_NAME /etc/os-release

echo
echo "==================== 2. namespace 支持 ===================="
ls -la /proc/self/ns/
echo
echo "--- /proc/sys/user 限制 ---"
cat /proc/sys/user/max_user_namespaces 2>/dev/null
cat /proc/sys/user/max_pid_namespaces 2>/dev/null
echo
echo "--- unprivileged userns 相关 sysctl ---"
sysctl kernel.unprivileged_userns_clone 2>/dev/null || echo "(无 kernel.unprivileged_userns_clone，默认允许)"
sysctl kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo "(无 apparmor userns 限制项)"

echo
echo "==================== 3. Python 标准库能力 ===================="
python3 -V
python3 - <<'PYEOF'
import os
names = ["unshare", "CLONE_NEWNS", "CLONE_NEWPID", "CLONE_NEWUTS",
         "CLONE_NEWIPC", "CLONE_NEWNET", "CLONE_NEWUSER"]
for n in names:
    print(f"os.{n}:", hasattr(os, n), getattr(os, n, ""))
PYEOF

echo
echo "==================== 4. cgroup 版本与控制器 ===================="
echo -n "cgroup fs type: "; stat -fc %T /sys/fs/cgroup
echo -n "controllers: "; cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null
mount | grep cgroup | head -5

echo
echo "==================== 5. 工具链 ===================="
for t in unshare chroot debootstrap busybox tar nsenter mount ip capsh; do
  p=$(which $t 2>/dev/null) && echo "$t -> $p" || echo "$t -> (未安装)"
done

echo
echo "==================== 6. sudo 与网络 ===================="
sudo -n true 2>/dev/null && echo "sudo: 免密可用" || echo "sudo: 需要密码（或不可用）"
getent hosts archive.ubuntu.com >/dev/null && echo "network: 可解析外网" || echo "network: 外网不通"

echo
echo "==================== 7. systemd? ===================="
ps -p 1 -o comm= 2>/dev/null
