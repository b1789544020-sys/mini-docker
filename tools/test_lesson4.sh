#!/usr/bin/env bash
# 第 4 课（网络 namespace + veth + bridge）自动化验证，以 root 运行
set -u
cd /mnt/c/Users/86159/.cline/data/workspaces/chat/mini-docker
ROOTFS=/home/user/mini-docker-rootfs
export LANG=C.UTF-8
PASS=0; FAIL=0

check() {   # check <描述> <文件> <grep 正则...>
  local desc=$1 f=$2; shift 2
  for pat in "$@"; do
    if grep -qE "$pat" "$f"; then
      echo "PASS  $desc  /$pat/"; PASS=$((PASS+1))
    else
      echo "FAIL  $desc  /$pat/（没在输出里找到）"; FAIL=$((FAIL+1))
    fi
  done

}

ip link del mini0 2>/dev/null
rm -rf /run/minidocker-net

echo "########## 1. 完整第 4 课演示 ##########"
OUT=$(mktemp)
python3 lesson4_network.py "$ROOTFS" >"$OUT" 2>&1
RC=$?
[ $RC -eq 0 ] && { echo "PASS  课程脚本退出码 0"; PASS=$((PASS+1)); } \
               || { echo "FAIL  课程脚本退出码 $RC"; FAIL=$((FAIL+1)); }
cat "$OUT"
check "网卡隔离"      "$OUT" 'eth0@if[0-9]+ +UP +10\.87\.0\.3/24'
check "默认路由"      "$OUT" 'default via 10\.87\.0\.1 dev eth0'
check "容器->网关ping" "$OUT" 'ping 网关.*'
check "跨容器ping"    "$OUT" 'ping web 容器 10\.87\.0\.2'
check "HTTP跨容器"    "$OUT" 'HTTP GET http://10\.87\.0\.2:8000/welcome\.txt 成功'
check "HTTP正文"      "$OUT" '欢迎来到 MiniDocker'
check "无NAT不出网"   "$OUT" '100% 丢包'
# 网关/web 的 ping 统计必须 0% 丢包（统计行各出现一次）
LOSS=$(grep -c '0% packet loss' "$OUT")
[ "$LOSS" -ge 3 ] && { echo "PASS  网关+web 共 $LOSS 行 0% 丢包"; PASS=$((PASS+1)); } \
                  || { echo "FAIL  0% 丢包行只有 $LOSS（期望>=3）"; FAIL=$((FAIL+1)); }
rm -f "$OUT"

echo
echo "########## 2. CLI --network：容器 ping 网关 ##########"
OUT=$(mktemp)
python3 minidocker.py run --network --name smoke "$ROOTFS" -- \
  /bin/sh -c 'ip -br addr | grep 10.87.0; ping -c1 -W1 10.87.0.1' >"$OUT" 2>&1
grep -q '1 packets transmitted, 1 received, 0% packet loss' "$OUT" \
  && { echo "PASS  CLI 容器 ping 10.87.0.1 通"; PASS=$((PASS+1)); } \
  || { echo "FAIL  CLI 容器 ping 网关失败:"; cat "$OUT"; FAIL=$((FAIL+1)); }
rm -f "$OUT"

echo
echo "########## 3. 非 root 用 --network 必须被礼貌拒绝 ##########"
OUT=$(mktemp)
su -s /bin/bash user -c "python3 minidocker.py run --network $ROOTFS -- true" >"$OUT" 2>&1
grep -q '需要 sudo' "$OUT" \
  && { echo "PASS  rootless --network 被拒并提示原因"; PASS=$((PASS+1)); } \
  || { echo "FAIL  拒绝提示不对:"; cat "$OUT"; FAIL=$((FAIL+1)); }
rm -f "$OUT"

echo
echo "########## 4. 清理检查：无残留 veth/租约，rootfs 的 hosts 占位未被污染 ##########"
sleep 0.5
[ -z "$(ls /run/minidocker-net 2>/dev/null)" ] \
  && { echo "PASS  IP 租约目录已空"; PASS=$((PASS+1)); } \
  || { echo "FAIL  租约残留: $(ls /run/minidocker-net)"; FAIL=$((FAIL+1)); }
! ip link show mda2 >/dev/null 2>&1 \
  && { echo "PASS  veth mda2 已随容器销毁"; PASS=$((PASS+1)); } \
  || { echo "FAIL  veth mda2 残留"; FAIL=$((FAIL+1)); }
grep -q '占位文件' "$ROOTFS/etc/hosts" \
  && { echo "PASS  rootfs /etc/hosts 仍是共享占位（私有 hosts 走 bind-mount）"; PASS=$((PASS+1)); } \
  || { echo "FAIL  rootfs /etc/hosts 被污染:"; cat "$ROOTFS/etc/hosts"; FAIL=$((FAIL+1)); }

echo
echo "================ 第 4 课测试结果: $PASS passed, $FAIL failed ================"
[ $FAIL -eq 0 ]
