# MiniDocker —— 零依赖手写一个迷你容器运行时

一个只用 Python 标准库的教学项目，在 WSL2 (Ubuntu 26.04, 内核 6.18) 上亲手实现
容器的三大基石，并最终得到一个能跑的 `minidocker run`：

| 技术 | 管什么 | 在本项目里 |
|---|---|---|
| **namespace** | 容器"能看见什么"（进程号、主机名、挂载点、IPC、网卡） | `os.unshare()` 建 user/pid/uts/ipc/mnt/net 六个 ns |
| **chroot** | 容器的根目录 `/` 在哪 | `os.chroot(rootfs)` |
| **cgroups v2** | 容器"能用多少"（内存、CPU） | 写 `/sys/fs/cgroup/...` 的 `memory.max` / `cpu.max` |
| **veth + bridge**（第 4 课） | 容器怎么联网 | `ip link add veth` + 网桥 `mini0`，容器各拿一个 `10.87.0.x` |

零 pip 依赖、零镜像下载：rootfs 是用 `ldd` 从宿主机"抠"二进制和动态库拼出来的。
网络部分也只调用宿主机自带的 iproute2（`/usr/sbin/ip`），不装任何额外软件。

## 快速开始

```bash
git clone https://github.com/b1789544020-sys/mini-docker.git
cd mini-docker          # WSL 下如在 C 盘，路径形如 /mnt/c/Users/.../mini-docker

# 0. 环境侦察（普通用户）
bash tools/env_check.sh          # 内核支持哪些 namespace / cgroup 版本
bash tools/cap_probe.sh          # 实测普通用户在 user ns 里能当 root

# 1. 造迷你 rootfs（输出到 ~/mini-docker-rootfs，约十几 MB）
python3 make_rootfs.py

# 2. 三节课（都不需要 sudo）
python3 lesson1_chroot.py  ~/mini-docker-rootfs
python3 lesson2_namespaces.py ~/mini-docker-rootfs

# 3. cgroups 课需要真 root。WSL 里不用 sudo 密码，直接：
wsl -d Ubuntu -u root            # 先进入 root shell，再
cd mini-docker                   # 进入项目目录（按你的实际路径）
python3 lesson3_cgroups.py

# 4. 网络课（真 root）：两个容器各自有独立 IP，能 ping、能 HTTP 互通
python3 lesson4_network.py /home/user/mini-docker-rootfs
# 或用合体程序：
python3 minidocker.py run --network --name box ~/mini-docker-rootfs -- \
  /bin/sh -c 'ip -br addr; ping -c1 10.87.0.1'

# 5. 合体：你自己的 "docker run"
python3 minidocker.py run ~/mini-docker-rootfs                      # 普通用户，进容器 shell
python3 minidocker.py run --name demo ~/mini-docker-rootfs -- ps -ef
python3 minidocker.py run --name api --mem 128M --cpus 0.5 --network \
  ~/mini-docker-rootfs          # 资源限制 + 独立 IP 可叠加（root）
python3 minidocker.py list        # 列出带 cgroup 的容器（需要 root）
```

## 课程设计与观察点

**第 1 课 `lesson1_chroot.py` —— 只有文件系统隔离。**
chroot 后 `/` 变成 rootfs，但刻意不隔离进程和主机名，于是你会看到两个"漏风"：
`hostname` 仍是宿主机名；`/proc` 是空目录（`ps` 报错让你去挂 proc）。

**第 2 课 `lesson2_namespaces.py` —— 五个 namespace 一次到位。**
- `CLONE_NEWUSER`：普通用户在容器内是 uid 0，宿主机上仍是 uid 1000（rootless 容器的核心）
- `CLONE_NEWPID`：容器内进程号从 1 开始，shell 就是 PID 1
- `CLONE_NEWUTS`：hostname 变成 `mini-box`，宿主机毫无知觉
- `CLONE_NEWNS`：独立挂载视图，挂上容器私有的 `/proc`（ps/free 复活）
- `CLONE_NEWIPC`：System V IPC / 消息队列隔离

**第 3 课 `lesson3_cgroups.py` —— 资源紧箍咒（cgroup v2）。**
- 内存上限 128MB 的程序申请 1GB：吃到 128MB 当场被 OOM killer 发 SIGKILL，
  `memory.events` 里 `oom_kill` 计数 +1
- CPU 配额 50% 单核：`yes` 全速空转，实测稳定在 ~50.8%，`cpu.stat` 里
  `nr_throttled` 记录被调度器"按住"的周期数

**第 4 课 `lesson4_network.py` —— 网络 namespace + veth + bridge（真 root）。**
拓扑和 Docker 默认的 `docker0` 完全同构：每个容器一根 veth 虚拟网线，
一端在容器里叫 `eth0`（`10.87.0.x/24`），另一端插在宿主机网桥 `mini0`
（`10.87.0.1`，容器的默认网关）上。当堂证据：
- 容器里 `ip link` 只有 `lo` 和 `eth0`，宿主机的 `eth0` 完全不可见；
- 容器 ping 网关、容器之间互 ping 全部 0% 丢包；
- web 容器在自己的 rootfs 上开 `python -m http.server 8000`，
  client 容器用 HTTP 跨容器取回 `welcome.txt`（真实二层转发）；
- 宿主机也能直接 ping/访问容器 IP；
- 容器 ping 外网 `1.1.1.1` **预期 100% 丢包**：数据到了网关但没有 NAT
  MASQUERADE。宿主机装了 `nftables` 后补一条规则即可出网（讲义末尾有命令）。

## 和真 Docker/runc 的对应关系

| 本项目 | Docker / OCI |
|---|---|
| `make_rootfs.py` 抠出的目录 | 镜像层（image layers）解包后的 rootfs |
| `chroot` | `pivot_root`（更强：把旧根卸载，无法逃回去） |
| `spawn()` 两级 fork：父留宿主机 ns，C 建 ns，G 当 PID 1 | runc 的 init 进程 + `clone(cloneflags)` + `proc/self/ns` |
| `CLONE_NEWNET` + `ip link set … netns <pid>` | runc/libnetwork 的 netns 移交 |
| `mini0` 网桥 + `veth pair` + 每容器私有 `/etc/hosts` | `docker0`（或 cni0）+ veth + libnetwork 写的 hosts |
| 没装 nft/iptables，故容器不能出外网 | docker 自动加的 `MASQUERADE` 规则 |
| `/sys/fs/cgroup/minidocker/*`（C 进组，G 随 fork 继承） | runc 写的 cgroup（docker 用 cgroupfs/systemd driver） |
| `--mem / --cpus` | `docker run --memory / --cpus` |
| `--network` | `docker run --network bridge`（默认行为） |
| 容器内 hostname=容器名 | `docker run --name/--hostname` |
| **没有**：overlayfs、capabilities 收权、seccomp | Docker 还做了这些（见下） |

## 踩坑记录（都是真知识点）

1. **unshare 顺序**：`CLONE_NEWUSER` 必须先单独建、写好 uid_map 拿到全权能之后，
   再建其余 namespace；一把梭写 uid_map 直接 EPERM。
2. **uid_map 的取值时机**：必须在 unshare 之前保存 `os.getuid()`，
   unshare 之后它就是新 ns 里的 0，拿 0 去写映射 `0 0 1` 会 EPERM。
3. **短命 PID 1 会杀死整个 namespace**：unshare(NEWPID) 后第一个 fork 出的孩子
   就是 PID 1；我们曾在此时用 subprocess 跑了个 mount 命令，它成了 PID 1 又秒退，
   整个 PID namespace 当场销毁，之后所有 fork 报 ENOMEM。
   正解：挂载传播改 private 等准备工作全部前置，**最后**才 unshare PID 并立刻 fork 容器进程。
4. **cgroup v2 控制器逐层下发**：要在 `cgroup.subtree_control` 给
   `/minidocker` 和它的父 cgroup **都**写 `+memory +cpu`，子 cgroup 里
   才会存在 `memory.max` 文件（少一层时 Python 的 open("w") 报 EACCES，
   因为它带 O_CREAT，像是在 cgroupfs 里"建新文件"）。
5. **setuid 程序的跨 namespace 陷阱**：Ubuntu 的 `mount(8)` 是 setuid root，
   抄进 rootfs 后属主变成构建用户 (1000)，suid 位却被 `copystat` 保留——
   真 root 执行它时 euid 反被降到 1000，报 "must be superuser"；
   rootless 时 1000 正好映射为容器 root，反而没事。
   修法：构建时剥掉所有 suid/sgid 位；容器内挂载直接用 ctypes 调 `mount(2)`
   系统调用，不再经过 mount(8)（runc 也是直接发 syscall）。
6. **exec/fork 前记得 flush**：exec 会替换进程映像，stdout 缓冲区里没刷的输出直接消失；
   fork 还会**复制**父进程的缓冲区——我们曾忘了在 fork 前 flush，孩子 exec 前把父进程
   已缓冲的全部标题又刷了一遍，所有输出打印两次。
7. **网络 namespace 必须由"孩子"建，父进程要留在初始 netns**：veth 网线是宿主机侧
   创建再 `ip link set … netns <pid>` 塞进容器的；父进程若自己先 unshare(NEWNET)，
   就没有任何网身处初始 netns 能创建网桥。最终架构改成 runc 同款**两级 fork**：
   P 永远留在宿主机 ns；C（布置者）unshare 全部 namespace、chroot；最后才
   unshare(NEWPID) 并 fork 出 G（容器 PID 1）；C 等待并透传 G 的退出码。
   这样多个容器互不嵌套，父进程杀掉一个容器后自己仍能继续 fork（否则会撞上坑 #3 的 ENOMEM）。
8. **cgroup 挂早了没用，挂晚了逃逸**：cgroup 成员身份只能随 `fork` 继承。C 出生后
   若不拦着它，它会一路狂奔 fork 出 G，父进程事后再写 `cgroup.procs` 只能移走 C，
   G 已经生在 `init.scope` 里——表现为内存限不住、OOM 不触发。修法是**两阶段管道
   握手**：C 一出生先停下等父亲把它挪进 cgroup（放行①），建好 NEWNET 再停下等
   父亲插 veth（放行②），之后才 chroot、fork G。
9. **缺 `/etc/hosts` 会让 TCP 服务"bind 完不 listen"**：Python 3.14 的 `http.server`
   在 `server_bind()` 里对绑定地址调 `getfqdn()`；rootfs 没有 hosts、容器又没 NAT
   能问 DNS 时，反查主机名要等好几轮 5 秒 DNS 超时，监听 socket 长时间停在 UNCONN
   状态——ping 全通（netns 还在，内核对关闭端口回 RST），TCP 却连不上，极其迷惑。
   Docker 的做法是每个容器 bind-mount 一份私有 `/etc/hosts`（含 `<IP> <主机名>`），
   本项目照做，故障当场消失。
10. **`cgroup.kill` 后 rmdir 会短暂 EBUSY**：内核把死任务从 cgroup 摘除有毫秒级
    延迟，清理时要重试几轮。

## 不足与下一步（真正的容器还做了什么）

- NAT 出网：本机没装 nftables，`lesson4` 末尾给了现成的 `nft add rule … masquerade`
  三条命令；装上即可让容器访问外网（DNS 配置 `/etc/resolv.conf` 已在 rootfs 中备好）
- `pivot_root` 替代 chroot，并用 overlayfs 叠一个可写层（`docker run` 的 Copy-on-Write）
- capabilities 收权：容器内 root 默认丢掉 CAP_SYS_ADMIN 等大部分权能
- seccomp 系统调用过滤、只读根、resource limits（nofile/nproc）
- 按 OCI runtime-spec 组织 config.json，让 runc/docker 能直接启动你的"镜像"

## 文件清单

```
make_rootfs.py          零下载 rootfs 制造机（ldd 解析依赖 + usrmerge 符号链接处理）
container.py            容器原语零件库：unshare/uid_map/挂载 syscall/两级 fork+握手/exec
network.py              第 4 课：mini0 网桥、IP 租约、veth 创建与移交、容器内网卡自配置
lesson1_chroot.py       第 1 课：文件系统隔离与它的两个"漏风"
lesson2_namespaces.py   第 2 课：五个 namespace
lesson3_cgroups.py      第 3 课：内存 OOM 与 CPU 节流（root）
lesson4_network.py      第 4 课：net namespace + veth + bridge 的隔离与互通（root）
minidocker.py           合体：run / list，支持 --mem --cpus --network --name
tools/env_check.sh      环境侦察
tools/cap_probe.sh      普通用户 namespace 能力实测
tools/test_*.sh         各课自动化验证脚本（test_lesson4.sh 共 14 项断言）
```
