#!/usr/bin/env bash
# 能力验证：普通用户到底能玩转多少容器特性（全程不需要 sudo）
echo "========== A. user namespace 里改 hostname（UTS 隔离） =========="
unshare -Uru sh -c 'id; hostname mini-host-probe; echo "容器内 hostname -> $(hostname)"'
echo "宿主机 hostname 还是 -> $(hostname)"

echo
echo "========== B. user + PID + mount namespace 里挂自己的 /proc =========="
unshare -Urmpf sh -c '
  mount -t proc proc /proc && echo "挂载 proc 成功" || echo "挂载 proc 失败"
  echo "--- 新 PID 命名空间里能看到的进程（应该只有 sh/ps 两个） ---"
  ps -e -o pid,ppid,comm
'

echo
echo "========== C. cgroup v2 用户委托 =========="
echo "--- 我当前所在的 cgroup ---"
cat /proc/self/cgroup
CGPATH=$(awk -F: '$1=="0"{print $3}' /proc/self/cgroup)
echo "路径: $CGPATH"
echo "--- 该 cgroup 的 subtree_control / 委托情况 ---"
cat "/sys/fs/cgroup${CGPATH}/cgroup.controllers" 2>/dev/null
ls -ld "/sys/fs/cgroup${CGPATH}" 2>/dev/null
TESTDIR="/sys/fs/cgroup${CGPATH}/minidocker-probe"
if mkdir "$TESTDIR" 2>/dev/null; then
  echo "CGROUP_DELEGATE_OK: 普通用户可建子 cgroup"
  rmdir "$TESTDIR"
else
  echo "CGROUP_DELEGATE_FAIL: 第 3 课需要 sudo"
fi
