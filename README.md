# maibot-dsh-artifacts

MaiBot 的 DSH 文件桥接插件：把 DSH 生成的普通文件导入**当前聊天**的官方文件库，也可把当前聊天已有的附件导出给 DSH。二进制文件、中文文件名和批量导入均受支持，插件文件字节通过官方 `ctx.files` 传输。

仓库另提供一个**可选、默认关闭、必须人工应用的 MaiBot v1.3.18 Host 扩展**：让 DSH 使用专用 MCP 工具直接交接产物，Host 按原任务绑定的真实聊天入库，无需原聊天 Planner 再次选插件工具。[安装及使用说明](integrations/maibot_1_3_18/README.md)。它不是单靠安装插件就生效的功能，也不意味着完整 DSH/QQ 环境已验证。

插件本版为 `0.1.0`，插件与新写集成模块使用 [MIT](LICENSE)。Host 补丁中的上游 MaiBot 上下文及修改保留 GPL-3.0 许可，见[集成许可说明](integrations/maibot_1_3_18/NOTICE.md)和[上游许可证副本](integrations/maibot_1_3_18/COPYING.MaiBot)。已核对 MaiBot **v1.3.18** 和 `maibot-plugin-sdk` **v2.19.0** 的源码；manifest 暂仅声明这两个已核对版本。项目要求 SDK 不低于 2.19.0，尚未验证后续 SDK/Host 版本。未发布正式 Release，也未申请插件中心收录。

## 工作方式与范围

```text
QQ 原聊天 → MaiBot dsh_export → 官方文件库下载 → inbox/ → DSH
DSH → outbox/ → agent_talk 文本交接 → 原聊天 Planner → dsh_import
    → 官方文件库上传 → file_ref → files_list / files_send
```

**未应用可选扩展的原版 v1.3.18 中，DSH 无法直接调用这个插件的工具。** ACP MCP 服务没有注册第三方 `PluginToolProvider`；原聊天运行时才有。`agent_talk` 仅将文本交接给原聊天 Planner，不会直接执行插件，不保证模型必然选择工具。详见[源码核对与验证边界](docs/compatibility.md)。

插件只提供两个 `@Tool`，不提供 HTTP 服务、目录监听、后台自动上传、shell 执行或自动解压。无需 Docker Socket、root、MaiBot WebUI 管理员 Token。普通插件安装不改 MaiBot 主项目；可选扩展则是独立交付的 Host 源码补丁，只有部署者检查、备份并显式应用后才生效。

可选扩展的链路为：`原聊天 agent_talk → Host 绑定任务和授权 → ACP prompt 告知 DSH → agent_artifact_handoff → Host FileService.create/send → 原聊天文件库`。DSH 只获准交接 `outbox/<Host签发的task_id>/` 中一个批次的文件，不能指定聊天；发送还要求 Host 全局许可与原聊天本任务的双重授权。持久账本阻止重放和自动恢复不确定操作，属于 **at-most-once 尝试**，不承诺 exactly-once 投递。详见[扩展文档](integrations/maibot_1_3_18/README.md)。

## 安装与配置

1. 使用 Linux 上的 MaiBot v1.3.18，确认运行插件的 Python 环境安装 SDK v2.19.0。仅把 SDK 安装在宿主机、而 Runner 运行在容器内，并不足以满足依赖。
2. 将本仓库完整放到 MaiBot 实例的 `plugins/maibot-dsh-artifacts/`，保留 `_manifest.json`、`plugin.py` 及安全处理模块。开发分支验收期间请检出待测试的 PR 提交；正式安装应选择审核后的固定提交。
3. 启用 Host 的文件能力：在 WebUI「能力 → 文件功能」开启，或在主程序配置中设置 `[capabilities]` 下的 `files = true`。授权插件 manifest 声明的精确文件能力。
4. 在宿主机准备共享目录，按现有 UID/GID 和组权限同时授予 DSH 用户与 MaiBot Runner 读写/遍历权限；插件运行过程无需 root。例如，由有权限的部署用户执行：

   ```fish
   mkdir -p /srv/maibot-agent/inbox /srv/maibot-agent/outbox
   ```

   Docker Compose 中保留共享挂载：

   ```yaml
   volumes:
     - /srv/maibot-agent:/srv/maibot-agent
   ```

   挂载必须能被**实际插件 Runner 进程**看到；Runner 若独立于 MaiBot 容器运行，也要在其运行环境挂载。容器 UID 与宿主机用户名不是同一概念，不要用 `chmod 777` 解决权限问题。插件导出的最终文件权限为 `0640`；DSH 使用不同 UID 时，需要共享文件组及正确的目录组继承（可由部署方设置目录 setgid），并实测 DSH 能读到导出文件。
5. 首次加载后 Runner 按插件配置模型生成 `config.toml`；也可从 [`config.example.toml`](config.example.toml) 创建本地配置。修改后检查插件加载状态。实际配置不应提交到 Git。

   ```toml
   [plugin]
   enabled = true
   config_version = "1.0.0"

   [workspace]
   root = "/srv/maibot-agent"
   max_file_bytes = 52428800
   max_batch_files = 8
   ```

| 配置 | 含义 |
| --- | --- |
| `workspace.root` | 预先存在的共享工作区绝对路径；路径各级应为真实目录 |
| `max_file_bytes` | 单文件上限，默认 50 MiB；不能代替 Host 文件服务的配额 |
| `max_batch_files` | 一次 `dsh_import` 最多文件数，默认 8 |

共享工作区根目录应预先创建；DSH 导入源的 `outbox` 及业务子目录也须先存在。导出可安全创建缺失的 `inbox` 子目录。导入路径必须以 `outbox/` 开头，导出目标必须以 `inbox/` 开头，均相对于 `workspace.root`。不要把临时下载目录、插件源码目录或 Host 的 `data/files` 设成工作区。

Host 默认单文件 50 MiB、总量 10 GiB，剩余空间与传输并发也会影响操作。建议 Host 上限与本插件上限一致或更小。SDK 的下载接口不接受客户端流量上限，本插件的导出大小检查发生在下载到私有暂存目录后、发布到 `inbox` 前；因此 Host 限制与暂存磁盘配额仍然必要。

QQ/SnowLuma 的文件传输依赖 MaiBot **公共后端文件通道**，不要求启用 WebUI。QQ 客户端异机运行时，将 `files.transfer_base_url` 设置为客户端可达的后端地址；平台提供私网下载 URL 时，由管理员在 `files.trusted_download_origins` 配置精确 origin。不要把限时传输 URL 当成长期引用，也不要写进公开日志。

## 工具使用

以下是工具参数示例，须由**原聊天 MaiBot**在 Host 派发的工具调用中执行；它们不是 shell 命令，也不是 DSH 可直接调用的 MCP 工具。

### `dsh_import(paths, send=false)`

```json
{"paths":["outbox/task-001/报告.pdf","outbox/task-001/图片.png"],"send":false}
```

按输入顺序逐个处理，保留原始文件名：安全读取普通文件，生成私有快照，然后 `await ctx.files.upload(...)`。上传成功即成为当前聊天的附件，返回包含 `file_ref`、`name`、`size`、`sha256` 等信息的 `file` 对象。之后可由原聊天调用官方 `files_list`、`files_send`。

结果包含整体 `success` 和逐项 `items`；单项 `status` 为 `imported` 或 `failed`，并保留输入 `path`。批次部分失败不会回滚已上传文件，失败项不会阻止后续文件；输入参数或批量数量不合法时拒绝调用。读取结果中的每一项，不要仅凭一句自然语言总结判断整批完成。

`send=true` 时，上传后再调用 `ctx.files.send(file_ref)`。导入与发送是两件事：即使发送未获成功回执，成功上传的 `file_ref` 仍然有效。发送结果见各项 `send`：

| 发送状态 | 处理方式 |
| --- | --- |
| `sent` | 已获得明确成功回执 |
| `unknown` | 可能稍后送达；先核对聊天，**不得自动重试** |

发送调用抛异常或返回未识别的状态，也保守记为 `unknown`，因为网络错误可能发生在平台已接收之后。`retry_safe=false` 表示不能自动重试；整体 `success=false` 不代表没有任何文件上传或发送成功。

插件没有去重数据库。重新导入同一路径会新建附件，不能通过重复 `dsh_import` 修复不确定的发送回执。`send=false` 是默认值，建议先检查导入结果再明确发送。

### `dsh_export(file_ref, target)`

```json
{"file_ref":"0123456789abcdef0123456789abcdef","target":"inbox/task-001/原始资料.zip"}
```

`file_ref` 应从**当前聊天**的官方 `files_list` 或成功导入结果取得；上例仅展示格式。文件引用不是路径，不接受 URL 或任意聊天 ID。

插件使用 `await ctx.files.download(file_ref, private_path)`，由 Host 检查当前聊天授权，SDK 校验长度和 SHA-256。下载完成、大小检查通过后，才以安全且不覆盖的方式发布到 `target`。需要显式指定目标文件名，不自动覆盖、猜测或改名。目标已存在时失败，请选择新名字。下载失败不会在最终目标留下看似完整的文件。

## 与 DSH 通过 `agent_talk` 交接

推荐目录：

```text
/srv/maibot-agent/
├── inbox/task-001/     # MaiBot 导出，DSH 读取
└── outbox/task-001/    # DSH 生成，MaiBot 导入
```

任务目录名仅用于组织文件，**不是聊天权限隔离机制**。下面约定为交接文本格式，插件不监听、不解析 `agent_talk`，也不因为看到这段 JSON 自动执行：

```json
{"type":"dsh_artifacts","version":1,"paths":["outbox/task-001/报告.pdf","outbox/task-001/图片.png"],"send":false}
```

字段 `paths` 是工作区内的相对路径，必须指向已经写完的最终文件。交接内容不包含 `session_id`；聊天路由只使用官方 `agent_talk` 的 `target`。DSH 应记住原任务传话头中的回复目标，必要时用 `agent_chats` 核对真实会话，不能猜测其他聊天。

DSH 在普通用户 fish 环境下生成文件的示例：

```fish
set job task-001
mkdir -p /srv/maibot-agent/outbox/$job
# 将实际生成命令的输出先写入同目录临时文件。
set draft (mktemp /srv/maibot-agent/outbox/$job/.report.XXXXXX)
printf '%s\n' '这是一份示例结果。' > "$draft"
# 已关闭写入句柄后，以同目录硬链接原子发布；-T 禁止把目标当作目录。
# 不使用 -f：最终名称存在时必须失败。删除临时链接后才允许交接。
if ln -T -- "$draft" /srv/maibot-agent/outbox/$job/结果.txt
    if rm -- "$draft"
        printf '%s\n' '发布完成，现在可以通过 agent_talk 交接结果.txt。'
    else
        printf '%s\n' '临时链接未删除，停止交接；处理后确认文件只有一个硬链接。' >&2
    end
else
    printf '%s\n' '发布失败，停止交接；检查目标重名或权限并使用新任务目录。' >&2
end
```

每个任务使用新的目录/文件名；若目标已经存在，停止交接并选择新名字，确认本次新文件确实已发布。示例只演示文件交接，不是 PDF/图片生成命令。真实生成器同样应先写临时文件，完成后再发布，交接后不再修改它。

然后由 DSH 调用 MaiBot 提供的 **`agent_talk` MCP 工具**，例如：

```json
{
  "target":"<原任务传话中的真实回复目标>",
  "text":"文件已写完。请在原聊天调用 dsh_import，参数如下；先导入，不发送：\n{\"type\":\"dsh_artifacts\",\"version\":1,\"paths\":[\"outbox/task-001/结果.txt\"],\"send\":false}"
}
```

这段 target 只提供给官方传话工具，不能作为本插件的参数。原聊天 MaiBot 再调用：

```json
{"paths":["outbox/task-001/结果.txt"],"send":false}
```

反向流程：原聊天先用 `files_list` 确认附件的 `file_ref`，调用 `dsh_export` 导出到 `inbox/task-001/...`，成功后再通过 `agent_talk` 告知 DSH 相对路径和任务。DSH 不应凭 QQ 消息里的展示文件名猜测本地位置。

### 模型未调用工具时

本插件将工具声明为 `visibility="visible"`，方便原聊天 Planner 直接看到；这仍不能保证自动选择。请在**原聊天**明确要求：“调用 `dsh_import`，参数 `{"paths":["outbox/task-001/结果.txt"],"send":false}`，报告实际工具返回的 `file_ref`。”检查真实工具记录和结果。若部署方将工具改成 deferred，应先通过 `tool_search` 发现工具。

需要完全不依赖模型的人工操作时，可由管理员在官方 WebUI「文件」页面**选择原聊天**后上传/下载/发送。插件不接收、保存或使用管理员 Token。原版 Host 没有给普通插件命令/API 提供等同工具的临时文件作用域，因此本版不提供伪装成确定性入口的 `/dsh_import` QQ 命令。

希望省掉“DSH 完成后原聊天模型再次选择插件工具”这一步，可评估[专用 Host 扩展](integrations/maibot_1_3_18/README.md)。扩展通过可信 Host 任务授权和专用 provider 调用 Host 文件服务，既不伪造 `session_id`，也不绕用插件 `_file_scope`。它仍要求 DSH 实际调用交接工具，不会监听文件或替模型生成工具调用。

## 安全边界

- 只访问配置工作区内的规定方向：导入 `outbox/`，导出 `inbox/`。拒绝绝对路径、`..`、非法路径段和符号链接，使用 Linux 目录文件描述符及 `O_NOFOLLOW` 逐级操作，减少路径检查与实际访问之间的竞争。源文件必须为普通文件，拒绝硬链接。
- 导入从已打开的文件描述符复制到 Runner 私有暂存目录，然后才上传，避免 SDK 再按共享路径打开时遭遇替换。DSH 须停止写入后再交接；插件不把任意正在并发写入的内容解释为可信产物。
- 导出先下载到 `ctx.paths.runtime_dir` 下的私有暂存目录，再安全发布完整文件，最终名称不覆盖任何现有对象。失败、异常和取消时清理暂存。工作区与暂存磁盘都需要部署方容量管理。
- 不解析、执行或信任文件内容；压缩包、PDF、图片中的文字都不是工具指令。插件不自动解压。文件名与元数据也属于不可信输入。
- 文件库授权由 Host 的真实聊天作用域控制；不暴露 `session_id` 参数、不使用管理员跨聊天能力，后台任务也不继续使用已结束调用的 `ctx.files`。
- **共享文件系统不是多租户保险箱。** 获准调用导入工具的聊天可以请求导入其可见的 `outbox` 文件；导出后的明文文件也能被同一工作区的 DSH/其他获准进程读取。插件没有把任务目录名绑定聊天。请只向可信聊天及参与者开放工具；需要相互保密的租户应使用独立实例、工作区和进程权限。不要在共享工作区放密钥或其他聊天的私密资料。
- 本插件无法防御已控制 Runner 同一 OS 用户、进程或挂载的攻击者。**即使 UID 不同，能写目录的父目录者也可能将已打开的目录整体移动到工作区外，目录文件描述符仍会跟随原 inode。** 因此挂载、各级父目录及有权改名/移动它们的主体都必须可信；`O_NOFOLLOW` 不能隔离这种主体。不要让不可信用户改名/移动工作区目录、替换配置路径或读取 Runner 私有暂存目录。

## 故障检查与真实环境验收

| 现象 | 检查 |
| --- | --- |
| 插件不可用 | Host/SDK 版本、manifest 能力授权、插件加载状态、配置格式 |
| 文件能力拒绝 | `capabilities.files` 是否开启；是否由原聊天工具调用；是否误在 API/后台调用 |
| 路径不存在/权限不足 | 相对路径前缀、目录是否创建、容器和 Runner 挂载、UID/GID 及各级目录权限 |
| 路径被安全校验拒绝 | 符号链接、硬链接、`..`、绝对路径、特殊文件；生成普通文件并放到真实目录 |
| 超过限制 | 插件单文件/批次数量、Host 单文件/总量配额、磁盘空间 |
| 导出提示无权或无效引用 | 使用当前聊天 `files_list` 的真实 `file_ref`；不要使用别的聊天引用 |
| 发送 `unknown` | 查看 QQ 是否稍后收到，禁止自动重发或重新导入 |

**SnowLuma 外部依赖问题：** 若 QQ 文件消息没有进入 `files_list`，先排查适配器是否把文件消息/群上传通知转换为带 `file_id`、必要定位信息的结构化 `FileComponent`，对应通知开关与聊天过滤是否允许，Host 是否签注可信文件来源并完成登记。只有聊天文字中的“文件名”不会自动变成附件。插件不能凭文件名修复未登记的 QQ 文件；只有已经取得的 `file_ref` 才能导出。检查适配器与 Host 日志时避免公开令牌、传输 URL、聊天记录和个人信息。

本仓库的 pytest 使用临时工作区和模拟 `ctx.files` 验证文件安全、异常及权限边界；不等同于真实 MaiBot/DSH/SnowLuma 集成测试。发布前请在目标部署完成：

1. QQ 发送小文件，确认当前聊天 `files_list` 有引用，再导出，核对 DSH 端文件哈希。
2. DSH 生成中文名二进制文件并交接，原聊天实际调用导入，检查 `files_list` 与上传内容哈希。
3. 显式发送，核对 QQ 回执与文件内容；出现 `unknown` 时人工确认，不自动重试。
4. 用另一个聊天尝试原文件引用，确认 Host 拒绝；验证越权路径、已存在目标和权限不足也被拒绝。

截至本版，以上真实环境步骤尚未在 MaiBot + DSH + QQ 部署中验证；没有宣称模型自动交接、SnowLuma 入站登记或 QQ 实际投递已经完成实机验收。可选扩展的组件测试与实机验收边界单独记录在[集成文档](integrations/maibot_1_3_18/README.md)。

## 开发与测试

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q --ignore=tests/host_integration --ignore=tests/test_host_patch.py
.venv/bin/python -m ruff check .
```

本机 Python 3.12 使用真实 PyPI SDK 2.19.0 wheel，完整执行 **299 项全部通过，无失败、无跳过**：原插件回归 103、交接 core 149、provider 28、安装器/补丁 13、真实 Host 组件 6。Ruff 全仓检查通过，MaiBot v1.3.18 官方 `ManifestValidator` 校验清单通过，无 error/warning。

Host 组件覆盖真实 FileService / FileStore / ToolRegistry + SQLite、一次官方 MCP 1.28.1 loopback 协议交接及实际补丁 ACP 方法；聊天发现与 QQ 发送边界仍使用 mock。13 项安装器/补丁测试不代表启动了完整 Host。以上均不代表 SSH、真实 DSH、SnowLuma QQ 全链路通过，分层依据见[兼容说明](docs/compatibility.md)。

需要运行 Host 补丁与组件测试时，另外安装测试依赖并提供独立、未修改的固定 Host 源码：

```bash
.venv/bin/python -m pip install -r requirements-host-test.txt
git clone --depth 1 --branch v1.3.18 https://github.com/Mai-with-u/MaiBot.git /tmp/maibot-v1.3.18
MAIBOT_SOURCE_PATH=/tmp/maibot-v1.3.18 .venv/bin/python -m pytest -q
```

没有 `MAIBOT_SOURCE_PATH` 时相关测试会跳过，不能据此宣称 Host 集成通过。测试在临时源码副本和数据目录中操作，不应把生产路径用作测试目标。无需 root、真实 QQ、DSH、Docker、WebUI Token 或外部在线服务；MCP 组件测试会暂时监听本机回环地址。CI 的普通测试矩阵包含 Python 3.10/3.12，Host 组件 job 使用 3.12；远端实际结果须以 CI 记录为准，本机仅验证 Python 3.12。请勿提交实际 `config.toml`、`.env`、密钥、聊天记录、生成文件或个人路径。

兼容性来源、各接口权限及自动化局限见 [docs/compatibility.md](docs/compatibility.md)。欢迎通过 [Issues](https://github.com/MemoryController/maibot-dsh-artifacts/issues) 提交去除敏感信息的复现步骤。
