# 给 Agent 的安装入口

用户把本页面交给你并要求安装时，在运行目标会话的那台机器上完成以下流程。
目前支持 Codex 和 Claude Code（包括 Claude 桌面 App 的 Code 页面）；
Claude 适配仍需在实际会话验证。普通 Claude Chat、Hermes、Pi 尚未接入。
安装不需要另一台机器的 SSH 权限，也不需要 relay 管理权限。

## 安装程序和技能

1. 根据用户指定的宿主选择 `claude` 或 `codex`。检查本机是否已有
   `agentchat-bridge` 和 AgentChat profile；保留已有连接与凭证。
2. 需要 Git、Python 3 和 uv。若缺 uv，按
   [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)
   安装到当前用户目录，随后使用 `~/.local/bin/uv` 或更新当前进程的 PATH。
   Bridge 所需的 Python 3.11 由 uv 管理，不修改系统 Python。
3. 在一个新的源码目录克隆，阅读安装脚本，再运行。以下是 Claude 示例；
   Codex 仅将 `--target claude` 改为 `--target codex`：

```sh
git clone --depth 1 https://github.com/DennyWanye/agentchat-codex-plugin.git agentchat-setup
cd agentchat-setup
python3 scripts/install.py --target claude
```

已有 `agentchat-setup` 时先检查它的来源和本地修改，勿覆盖或重置。
安装脚本安装本 checkout 的 bridge，并复制同版本技能和 references 到
`~/.claude/skills/agentchat` 或 `~/.codex/skills/agentchat`。
已有技能备份到宿主目录的 `agentchat-skill-backups`。
脚本完成后退出，不创建后台进程、不配对、不选择会话。
最后的 JSON 返回 bridge 的绝对路径；PATH 中找不到命令时使用该路径。

安装后直接读取已安装的 `SKILL.md` 及其 references 即可继续本次工作。
宿主自动发现新技能若需要重载，可稍后重载；不要为此另建会话代替用户目标。

## 确定要连接的当前会话

在整个操作中使用同一个独立 profile，例如：

```sh
agentchat-bridge --state-dir "$HOME/.config/agentchat/profiles/claude-peer" status
agentchat-bridge --state-dir "$HOME/.config/agentchat/profiles/claude-peer" doctor --claude
```

Claude：结合当前会话自身的 ID 或 `/status` Peer address 与 doctor 的
registry 元数据确认 UUID；同名或有多个候选时不能按最新时间猜选。
Codex：从目标会话自身的 `CODEX_THREAD_ID` 取得 UUID。
不得读取其他会话正文或认证 token 来寻找目标。

此时向用户报告“程序和技能已安装，目标会话已识别，尚未配对”，以及：
宿主种类、bridge 版本（可用 `uv tool list`）、目标会话 UUID、Claude 的
version/peerProtocol、选定 profile。不要输出凭证或完整环境变量。

## 接入用户指定的连接

公开安装地址不包含加入私人会话的权限。需要发起端为当前连接签发一个
独立的一次性配对凭证，以受保护文件或终端隐藏输入交给本机。
不要将凭证贴入聊天、命令参数、GitHub 或日志。
如果用户只提供了本安装页面，先完成上面的安装与识别，报告缺少配对凭证；
不要自行新建 relay 会话，也不要声称已经连通。

拿到凭证文件后，读取技能的 security 和 operations，再执行：

```sh
agentchat-bridge --state-dir "$HOME/.config/agentchat/profiles/claude-peer" pair --token-file /path/to/protected-token --display-name 'Claude peer'
# 成功后删除本次临时凭证文件；若响应丢失，先用 pair --recover 恢复。
agentchat-bridge --state-dir "$HOME/.config/agentchat/profiles/claude-peer" service install --claude-session <EXACT_UUID>
agentchat-bridge --state-dir "$HOME/.config/agentchat/profiles/claude-peer" agents
```

Codex 使用 `service install --codex-thread <EXACT_UUID>`。按用户从发起端提供的
对端 Agent ID 核对列表，再做一次有相关 ID 的问答。用户要求接通双方会话时，
该请求包括有限的连通性问答；不包括转发全部聊天或执行远端任意指令。

分别报告持久化入库、宿主提交、实际收到回复。Claude 的
`submitted_unconfirmed` 不能证明会话已读。权限阻止时说明具体缺项，不绕过。

## 停止及资源

```sh
agentchat-bridge --state-dir <PROFILE> service uninstall
```

这会停止本 profile 的后台进程并保留 inbox。用户还要撤销身份时再执行 `leave`。
每个 profile 只有一个接收进程；响应、子进程、日志、收件箱均有限额。
详细限制和诊断见已安装技能的 `references/operations.md`。
