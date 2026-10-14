#!/usr/bin/env bash
# minidocker.py 综合测试（以 root 运行；第 1 段切到普通用户验证 rootless）
set -u
cd /mnt/c/Users/86159/.cline/data/workspaces/chat/mini-docker
ROOTFS=/home/user/mini-docker-rootfs
export LANG=C.UTF-8
HERE=$(pwd)/tools

echo "########## 1. rootless：普通用户，纯 namespace 隔离 ##########"
su -s /bin/bash user -c "bash $HERE/_inner_rootless.sh"
echo

echo "########## 2. root + 内存限制 96M：容器程序申请 512M，应被 OOM 杀 ##########"
python3 minidocker.py run --name membox --mem 96M "$ROOTFS" -- /usr/bin/python3 -c '
x=[]
for i in range(32):
    x.append(b"x"*16*1024*1024)
    print("吃下", (i+1)*16, "MB", flush=True)
'
echo ">>> minidocker 退出码: $?（247 = 256-9：容器被 OOM 的 SIGKILL 干掉，-9 透传成 shell 退出码）"
echo

echo "########## 3. root + CPU 限制 0.35 核：忙等，实测占用 ##########"
python3 minidocker.py run --name cpubox --cpus 0.35 "$ROOTFS" -- /usr/bin/python3 -c 'while True: pass' &
DPID=$!
sleep 1.5
CG=/sys/fs/cgroup/minidocker/cpubox
U1=$(awk '/usage_usec/{print $2}' "$CG/cpu.stat")
sleep 2
U2=$(awk '/usage_usec/{print $2}' "$CG/cpu.stat")
python3 - "$U1" "$U2" <<'PYEOF'
import sys
u1, u2 = int(sys.argv[1]), int(sys.argv[2])
print(f">>> 2 秒墙钟内拿到 {u2-u1} us CPU = {(u2-u1)/2_000_000*100:.1f}% 个核（配额 35%）")
PYEOF
echo ">>> 节流统计: $(grep -E 'nr_throttled|nr_periods' "$CG/cpu.stat" | tr '\n' ' ')"
echo 1 > "$CG/cgroup.kill"
wait $DPID
echo

echo "########## 4. list（容器都已退出，cgroup 已自动清理；正常应显示为空） ##########"
python3 minidocker.py list
