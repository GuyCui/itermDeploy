# 安装、升级和回滚

日常用法见[首页](../README.md)。此页只在安装、维护或排错时使用。

## 适用环境

现有 Debian 12、Python 3.11、systemd、curl、iproute2、Mihomo。代码最初在 Mihomo `478af2a` 构建上验证，其他版本应先检查兼容性。

完整安装器目前面向以下固定布局，不会自动适配任意路由环境：

| 项目 | 要求 |
|---|---|
| 主程序与服务 | `/usr/bin/mihomo`、`mihomo.service` |
| 配置与运行用户 | `/etc/mihomo/config.yaml`、`mihomo` 用户组 |
| 节点提供器 | 现有 `Airport_01`，类型 HTTP 或本工具管理的 File |
| 业务组 | `Proxy-Auto`，可选 `Docker-Auto`；`Proxy` 应选择 `Proxy-Auto` |
| 出口网卡 | `eth0` |
| 主控制 API | `http://127.0.0.1:9093`，密钥读取现有配置 |
| 测速端口 | 回环地址 `17892`、`19094`，须未被其他服务占用 |
| 测速策略路由 | IPv4 优先级 `8990`、标记 `19784`；冲突时拒绝安装 |

安装需要本机原订阅 URL。HTTP 提供器从现有主配置读取；已迁移的 File 提供器从本机私有 `settings.json` 读取。下载不到有效订阅时不会凭空创建节点。

## 首次安装或升级自动检测模块

下载仓库、检查本次变更后，在 Debian 执行：

```bash
git clone https://github.com/GuyCui/itermDeploy.git
cd itermDeploy/mihomo
sudo sh install-v3.sh
sudo python3 install-control.py
sudo mihomoctl check --no-source
```

已有仓库时用 `git pull --ff-only` 更新，再进入 `mihomo` 目录执行需要的安装步骤。`install-v3.sh` 安装核心模块；`install-control.py` 单独安装检查命令。单独升级检查入口无需重装核心模块。

`install-v3.sh` 会备份、迁移受管组、将提供器改为本地文件、验证配置、重载主进程并启用定时器；首次实测可能需要几分钟。缺少 PyYAML 时通过 apt 安装 `python3-yaml`。安装会影响正在运行的网络配置，不能当作仅查看状态的命令。

原提供器的过滤、名称前缀和健康检查会保留。订阅下载失败时保留上一份有效缓存。源订阅节点数会变化；运行时节点数还受过滤影响，不能将某次的数量当作固定上限。

## 本机文件

| 路径 | 内容 |
|---|---|
| `/usr/local/lib/mihomo-health-guard` | 已安装的核心 Python 模块 |
| `/opt/mihomo-control` | 已安装的检查入口 |
| `/usr/local/sbin/mihomoctl` | 检查命令 |
| `/etc/mihomo/provider-cache/Airport_01.yaml` | 两个 Mihomo 进程共同读取的私有节点缓存 |
| `/var/lib/mihomo-speed-guard/settings.json` | 原订阅 URL、测速密钥和参数，0600 权限 |
| `/var/lib/mihomo-speed-guard/state.json` | 测速结果与当日预算 |
| `/var/lib/mihomo-health-guard/state.json` | 健康检查历史 |

不要把这些实际配置、设置、节点缓存、日志或回滚备份提交到仓库。检查入口的 JSON 汇总隐藏订阅、密钥、完整节点名称和地址；服务日志可能包含节点名称。

## 定时与测速

`mihomo-provider-cache.timer` 下载并校验订阅，然后原子发布缓存；`mihomo-speed-guard.timer` 运行健康检查和到期的测速；`mihomo-speed-probe.service` 是独立测速进程。它仅在回环监听，关闭 TUN/DNS，通过路由标记使用主路由表，避免测试再次进入业务 TUN。

核心 v3.0.2 默认每轮测速当前节点及最多 3 个候选，使用最多 4 MiB 的下载样本；每天下载载荷预算 1 GiB，不包含健康检查、TLS、订阅或业务流量。更快候选还要重复测速和通过 Google、GitHub、Docker 健康检查；正常情况下需快 25% 且增加至少 0.5 MiB/s 才切换，并有保持时间。

这些结果用于挑选本轮验证过的候选，不能保证所有网站、时段或长时间大文件吞吐量。切换影响新连接，已有连接可能需应用重试。工具也不自动解决 NAT 类型、Apple ID 登录或全部线路失效的问题。

```bash
systemctl list-timers mihomo-speed-guard.timer mihomo-provider-cache.timer
systemctl status mihomo-speed-probe.service --no-pager
sudo journalctl -u mihomo-provider-cache.service -n 30 --no-pager
sudo journalctl -u mihomo-speed-guard.service -n 30 --no-pager
```

## 回滚核心安装

安装器打印私有备份目录。使用同一版仓库代码，将路径替换为本机实际目录：

```bash
sudo python3 upgrade_speed.py --rollback /var/lib/mihomo-speed-guard/upgrade-v3-实际时间戳
```

核心回滚会恢复备份的配置、脚本、单元和定时器状态。若打印 `V3_ROLLBACK_NEEDS_ATTENTION`，需检查所示备份及服务状态。

仅暂停自动更新与测速：

```bash
sudo systemctl disable --now mihomo-speed-guard.timer mihomo-provider-cache.timer
sudo systemctl stop mihomo-speed-guard.service mihomo-provider-cache.service
sudo systemctl disable --now mihomo-speed-probe.service
```

暂停后主程序继续使用最后的缓存与选择，不再自动更新或恢复失效节点。

## 维护者验证

测试使用虚构节点和密钥，不需真实订阅，也不连接路由器：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -W error::ResourceWarning -m unittest discover -p 'test*.py' -v
```

仓库的 GitHub Actions 在改动此目录时执行离线测试。测试不能替代 Debian 实机验收。

Mihomo 参数含义参见[官方提供器文档](https://wiki.metacubex.one/config/proxy-providers/)和[规则提供器文档](https://wiki.metacubex.one/config/rule-providers/)。
