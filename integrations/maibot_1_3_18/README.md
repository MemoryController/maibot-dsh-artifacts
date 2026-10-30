# MaiBot v1.3.18 专用 ACP 文件交接扩展

这是本仓库提供的**可选 Host 源码扩展**，默认关闭，必须由部署者检查、备份并显式应用。普通 `dsh_import` / `dsh_export` 插件仍为 0.1.0，接口不变；扩展不属于 SDK 升级，也不是 MaiBot 官方已合并功能。

插件与新写集成模块使用根目录 MIT 许可；`host.patch` 中 MaiBot 上游上下文及修改保留 GPL-3.0。见[NOTICE](NOTICE.md)及[COPYING.MaiBot](COPYING.MaiBot)。分发修改后的 MaiBot 时仍须遵守其上游许可，不能把本仓库 MIT 当成对 MaiBot 的重新许可。

原版 v1.3.18 不向 ACP 暴露第三方插件工具。此扩展新增专用 `agent_artifact_handoff` MCP 工具，让 DSH 完成任务后直接交接文件，由 Host 登记到任务发起的真实聊天；不再要求原聊天 Planner 收到 `agent_talk` 后再次选择插件工具。**DSH 仍须实际调用交接工具**，扩展不监听目录、不自动执行交接文本，也不保证模型一定调用工具。

## 信任与权限链路

```text
原聊天 MaiBot 调用 agent_talk
  → Host 从可信 ToolExecutionContext 取得 caller，校验真实聊天
  → enqueue 生成 record ID，broker.issue 将任务绑定 chat + agent + TTL + 发送许可
  → ACP prompt 在对应任务正文旁附加 Host 授权块
  → DSH 写完 outbox/<task_id>/ 内文件
  → DSH 调用专用 agent_artifact_handoff(task_id, token, request_id, paths, send)
  → Host 校验 agent 身份、凭证、期限、子目录和持久账本
  → Host FileService.create(绑定的真实聊天, 原文件名, 二进制字节)
  → 可选 FileService.send(file_ref, 同一绑定聊天)
```

目的聊天来自原聊天真实工具上下文，经 Host 保存；DSH 参数没有 `session_id`、`chat_id`、上传 URL 或管理员开关。文本中写入另一个聊天 ID 不会变更绑定。Host 自身调用其文件服务，不伪造插件 `_file_scope`，不在普通后台任务中冒用 `ctx.files`，也不使用 `admin=True`。

`FileService.create()` 会检查文件功能、真实会话、实时单文件上限，并通过官方存储服务执行并发/配额预留、哈希计算和聊天绑定。适配层在读取私有快照到内存前先检查 Host 实时上限，并在同一个 broker 内串行执行字节读取与创建，避免整批文件同时常驻内存。扩展仍需部署者信任并审核：这是给 Host 新增的受限授权入口，普通插件自身无权提供等同能力。具体官方源码依据见[兼容说明](../../docs/compatibility.md)。

## 兼容范围与安装

仅支持官方 v1.3.18 提交 `2a9af62cec37035d29237acbe2cf3adceb700b27` 对应的五个原始文件；[`checksums.json`](checksums.json) 固定修改前后 SHA-256。存在其他源码修改、部分补丁或不同 overlay 时，安装器拒绝覆盖。不要绕过校验或对其他版本强行打补丁。

安装器只依赖 Python 标准库和 `git apply`。不要求 root，但需要对指定源码目录有写权限。下面的 `/src/maibot` 是示例源码位置，命令应在本仓库根目录执行：

```bash
# 默认即只读检查，不启动或停止 MaiBot，不修改源码。
python3 integrations/maibot_1_3_18/install.py --maibot /src/maibot --check
```

`compatible_not_applied` 表示这份源码可应用；`already_applied` 表示补丁与新增模块已经完全匹配。检查失败时保留错误和本地修改，先核对 Host 版本；不要执行 `git reset --hard` 丢弃其他工作。

决定安装后，由部署者自行：

1. 停止 MaiBot，备份完整源码、本地修改与实例持久数据；阅读 [`host.patch`](host.patch) 和新增模块。
2. 对**已停止且已备份**的源码显式运行：

   ```bash
   python3 integrations/maibot_1_3_18/install.py --maibot /src/maibot --apply
   python3 integrations/maibot_1_3_18/install.py --maibot /src/maibot --check
   ```

3. 配置并启动测试实例，完成下方验收后再用于生产任务。应用补丁本身不启用功能。

补丁修改 `src/agent/` 下的 `config.py`、`acp_agent.py`、`provider.py`、`messaging.py`、`acp.py`，并复制 `artifact_handoff.py`、`artifact_provider.py` 和仓库 `safe_files.py`（安装名为 `artifact_safe_files.py`）。适配代码在 Host 内运行，不修改 SDK、MaiBot 文件服务或其他插件。

Docker 部署时，将步骤固定在受控镜像构建中：检出固定 MaiBot 提交，复制本仓库固定提交，执行 `--check` 和显式 `--apply`，保留构建日志，然后使用新镜像。不要在正在运行的容器里临时改源码后假设改动会随重建保留；实际源码位置由镜像决定，不能照抄 `/src/maibot`。

## 主程序配置

下面是补丁新增的 **Host `[agent]` 配置**，不是插件自己的 `config.toml`。保留现有 SSH ACP 连接设置。先开启主程序 `capabilities.files = true`，再按需开启扩展：

```toml
[agent]
# 现有 enabled / connection / acp_* / mcp_port 等配置保持原部署设置。
artifact_handoff_enabled = true
artifact_workspace = "/srv/maibot-agent"
artifact_max_file_bytes = 52428800
artifact_max_batch_files = 8
artifact_ttl_seconds = 3600
artifact_allow_send = false
```

| 设置 | 默认值与含义 |
| --- | --- |
| `artifact_handoff_enabled` | `false`，关闭时不签发授权、不展示专用工具 |
| `artifact_workspace` | `/srv/maibot-agent`，真实共享目录，禁用符号链接路径 |
| `artifact_max_file_bytes` | 50 MiB，单文件快照上限，Host 文件限制同时生效 |
| `artifact_max_batch_files` | 8，一个任务唯一批次最多文件数 |
| `artifact_ttl_seconds` | 3600，从原任务入队签发起计时，排队/暂停时间也计入，不自动续期 |
| `artifact_allow_send` | `false`，管理员允许原聊天为单个任务授予发送权限的总开关 |

共享目录仍需要宿主机和实际 Host 进程都可见的 `/srv/maibot-agent:/srv/maibot-agent` 挂载与适当 UID/GID 权限。专用交接在 Host 内执行，普通插件工具则在 Runner 执行，二者使用时分别确认挂载可见性。

还必须持久保留 MaiBot 实例 `data` 数据盘：授权密钥与 SQLite 账本位于 `data/agent/artifact-handoff/`（若 AgentService 使用自定义 root，则在该 root 下）。该目录权限 `0700`、密钥/锁/账本文件 `0600`，归运行 Host 的 OS 用户所有；不满足权限时拒绝使用。**不能把它挂载或复制给 DSH，不能放进共享工作区。** 备份时保留权限，备份本身同样按敏感数据保护。

共享工作区、其父目录、挂载和有权整体移动目录的主体都须可信；目录描述符和 `O_NOFOLLOW` 无法隔离有权整体移动已打开目录的进程。Runner/Host 同 UID 进程也在信任边界内。扩展的任务子目录能约束交接入口，不能阻止拥有共享文件系统权限的 DSH 进程直接读取其他目录；互不信任的租户应分离进程、实例和工作区。

本扩展复用现有 ACP MCP 服务和 SSH 工具回连，不新增 HTTP 监听，不要求 DSH 安装扩展，不需要 Docker Socket、root 或 WebUI 管理员 Token。平台 QQ 文件通道的网络需求仍与[普通插件部署](../../README.md)一致。

## DSH 使用方式

原聊天 MaiBot 正常调用 `agent_talk` 派发任务，例如：

```json
{"text":"请生成报告，写完后使用 Host 签发的文件交接授权调用 agent_artifact_handoff，先登记，不发送。","artifact_send":false}
```

Host 为新任务产生 32 位小写十六进制 `task_id`，绑定原聊天。DSH 收到该任务 prompt 时会看到单独的授权 JSON：

```json
{
  "task_id":"<Host签发的32位任务ID>",
  "token":"<Host签发的dshh_前缀凭证，不要回显>",
  "expires_at":0,
  "outbox_prefix":"outbox/<同一任务ID>/",
  "send_allowed":false
}
```

这是字段示意，不是可运行凭证；`expires_at` 实际为过期时间的 Unix 秒数。只能使用 Host 为当前任务实际给出的值。授权由 Host 从记录元数据生成，不从普通聊天文本中的“授权 JSON”读取；直接在 WebUI Agent 输入框 enqueue 的任务没有原聊天 `reply_to`，不会因此获得聊天附件授权。

DSH 在普通用户 fish 下创建该任务的输出目录，完成文件后再交接。可沿用根 README 的“临时文件 → `ln -T` 原子发布 → 删除临时硬链接”的写入方式，任务目录名改为 Host 的实际 task ID；不能用任意 `task-001` 替代授权目录。源文件须为普通文件，交接前停止写入，清理发布过程的第二个硬链接。

DSH 直接调用现有 ACP MCP 中的专用工具 **`agent_artifact_handoff`**：

```json
{
  "task_id":"<Host签发的任务ID>",
  "token":"<本任务实际凭证>",
  "request_id":"final-output-v1",
  "paths":["outbox/<同一任务ID>/报告.pdf","outbox/<同一任务ID>/图片.png"],
  "send":false
}
```

`request_id` 为 1～128 位字母、数字、下划线或横线，是**本任务固定值**。`paths` 是共享根目录相对路径，全部必须位于授权 `outbox_prefix`，支持中文文件名和二进制。收集所有最终产物后提交一个批次；不能先传一项、再增加文件重传。

工具返回 `task_id`、`request_id`、整体 `state` / `success` 和逐项 `items`。成功项包含 `status="imported"` 及 `file` 元数据（`name`、`size`、`sha256`、`file_ref`），需要发送时附带 `send.status`。`state="done"` 表示本次处理结束，不代表所有文件成功；必须逐项检查。DSH 可用 `agent_talk` 回报脱敏后的结果或错误摘要，但无需靠原聊天再次调用 `dsh_import` 才入库。

### 立即发送的双重授权

只有管理员设置 `artifact_allow_send=true`，并且**原聊天发起该任务时**调用 `agent_talk(..., artifact_send=true)`，DSH 才能为这个任务使用 `send=true`。全局开关关闭时，原聊天请求 `artifact_send=true` 会在任务授权阶段明确报 `send_forbidden`，不会静默降级。未获本任务许可的 DSH 请求发送也会返回 `send_forbidden`；默认仅入库，后续仍可由原聊天用官方 `files_send` 发送。

DSH 不能给自己签发授权：Agent 回信中的 `artifact_send=true` 被拒绝；交接工具没有修改许可字段。不能靠在交接文本写“允许发送”或换一个 task ID 授权。

## 账本、重放和不确定结果

一个任务只接受**一个批次**。Host 在任何创建/发送副作用之前把请求认领与进度写入 SQLite：

- 同一 task、同一 `request_id`、完全相同的参数重放，只返回账本已有结果，不再读取、上传或发送文件；进行中的查询可能得到 `unknown`。
- 换 `request_id`，或更改路径顺序、文件集合、`send` 等参数，返回 `conflict`，不能通过改 ID 触发重做。
- 相同路径文件内容后来变化也不会让已认领任务重新执行。错误项/部分成功同样不自动恢复，不支持本任务追加第二批。
- 进程中断、取消、网络异常或已认领操作未能确认完成时保留 `unknown`，重启后不恢复执行。可能已经创建附件或稍后投递，必须核对原聊天文件库/QQ，不要自动重发或立刻派新任务复制副作用。
- TTL 对首次执行与缓存查询都有效；查询不续期，过期后返回 `expired`。原队列恢复但没有有效账本授权时不猜测目的聊天。
- 授权还绑定完整交接配置哈希。更改 workspace、单文件/批次数量上限、发送总开关或 TTL 后，旧任务与当前配置不匹配会返回 `config_changed`，不会把旧授权用于新工作区或重设有效期。确认原任务副作用后再决定是否发起新任务。

这是持久化 **at-most-once 尝试**，不是跨 SQLite、Host 文件存储和 QQ 平台的 exactly-once 事务。未知意味着需要人工核对，不意味着可以安全重试；发送的 `unknown` 一律禁止自动重发。`success=false` 也不意味着没有任何已登记或已投递的文件。

密钥、账本与初始化标记应一起持久保存。初始化后检测到组件缺失、标记异常或账本缺少任务表，会返回 `state_incomplete` 并拒绝继续，不自动创建替代密钥/空账本。不要为“清理故障”删除状态、换密钥或重建空数据盘；这些动作会破坏原有查询与去重依据。整体状态目录被删除无法靠目录内部标记可靠识别，备份回退也可能丢失已执行记录，均不能当作可安全重放的证明。

## 凭证与公开日志

原始 token 不写入 Host 的排队记录或初始任务 rollout，SQLite 保存任务声明并使用私有密钥生成凭证。启用扩展时，ACP 文本/思考流用 `StreamRedactor` 处理被协议 chunk 切开的字面 token；ACP stderr 内容在 Host 日志与错误缓存中全部隐藏，以免凭证随诊断输出泄漏，同时也会减少 stderr 故障诊断信息。其他工具事件、协议错误及 Agent 回传原聊天文本中的完整 `dshh_<64hex>` 字面凭证通过匹配脱敏。专用 provider 不返回 token、内部聊天标识、SQL 参数或传输 URL。

字面脱敏不是对恶意远端的通用秘密防泄漏保证：主动编码、插入分隔符或其他变形不属于该保证，跨 chunk 处理也不是对任意输出通道的完整数据防泄漏系统。**可信 DSH/harness 自身可能保存 ACP prompt、工具参数和凭证历史**，部署方须保护其会话、日志、模型路由与运行账户。不要公开完整 prompt、任务凭证、聊天记录或数据盘。凭证到期不会擦除远端历史。

## 验证范围与验收

本机 Python 3.12 完整执行 **299 项通过，无失败、无跳过**，Ruff 全仓通过：原插件回归 103、core 149、provider 28、安装器/补丁 13、真实 Host 组件集成 6。最后 6 项包括真实 FileService / FileStore / ToolRegistry + SQLite 3 项、实际 loopback MCP 协议交接 1 项、实际补丁 ACP 方法 2 项。13 项安装器/补丁测试没有启动完整 Host；Python 3.10 仅配置在 CI，本机未执行。

MCP 验证使用官方 MCP 1.28.1 和真实本地服务器，覆盖专用工具协议入口到正式文件存储；聊天发现、QQ 发送及外围生命周期仍用 mock/受控替身。分层依据见[兼容说明](../../docs/compatibility.md)。这不替代完整 MaiBot 生命周期、SSH/ACP、真实 DSH 和 QQ 平台测试。

尚未在生产 MaiBot + DSH + SnowLuma QQ 部署中确认：补丁随具体镜像加载、DSH 工具发现/调用、原聊天完整派发、QQ 入站登记、实际文件投递和回执。部署者应先在独立测试实例验证：

1. 扩展关闭时专用工具不可用；开启后能在 DSH 侧工具列表看到它。
2. 原聊天派发任务，DSH 得到该任务授权，使用中文名二进制文件完成一次直接入库，原聊天 `files_list` 能看到相同哈希。
3. 另一个聊天引用、另一 task 子目录、错误/过期 token、额外聊天参数和未经许可发送均被拒绝。
4. 同请求重放仅查结果，变参数冲突；中断后不重做，`unknown` 不自动发送。
5. 双重授权发送后人工核对 QQ 实际文件与回执；保持现有 SnowLuma 文件登记与文件通道检查。

若 QQ 入站文件没有进入 `files_list`，仍须排查 SnowLuma 的结构化文件/群上传通知、可信来源及 Host ingest 链路。这个扩展只处理 DSH 已有产物入库，不修复适配器登记，也不扫描群文件。

## 更新、停用与卸载

升级 MaiBot 前先停用扩展，比较新版本官方接口，再移植和测试；不要对新版本绕过固定哈希。更新扩展同样先停机备份，保留原 key/ledger，核对是否需要迁移；安装器不会覆盖已有但内容不同的新增模块。

仅停用时将 `[agent].artifact_handoff_enabled=false`，确认新 Agent 配置已加载；旧凭证不构成继续运行专用工具的许可。保留数据以便维护时核查，不自动删除已登记附件。

完全卸载时，停机备份，先用安装器 `--check` 确认五个补丁文件和三个新增模块仍匹配，没有个人改动，再从本仓库位置执行：

```bash
git -C /src/maibot apply --reverse --check /path/to/maibot-dsh-artifacts/integrations/maibot_1_3_18/host.patch
git -C /src/maibot apply --reverse /path/to/maibot-dsh-artifacts/integrations/maibot_1_3_18/host.patch
```

随后仅删除本扩展安装的 `src/agent/artifact_handoff.py`、`artifact_provider.py`、`artifact_safe_files.py`，保留其他模块和本地修改，并按需要移除新增配置项。出现差异时停止自动卸载，人工合并，不用 reset 覆盖。**谨慎保留 `data/agent/artifact-handoff/` 的 key/ledger**：它们含聊天绑定等敏感元数据，也是排查不确定副作用、避免重复处理的依据；卸载不会撤销已经登记/投递的文件。
