# maibot-dsh-artifacts

MaiBot + DSH 的文件桥接插件：将 DSH 在共享目录生成的图片、PDF、压缩包等二进制产物登记为**当前聊天的正式附件**，也可把当前聊天已有附件导出给 DSH。

正式版本 **0.2.1** 适配原版 **MaiBot v1.3.18**、**maibot-plugin-sdk v2.19.0**，使用 [MIT](LICENSE)。无需修改 MaiBot 源码、应用 Host 补丁或重建修改源码的镜像；可使用标准库 CLI，也可配合可选的 [DSH Cordis 插件](https://github.com/MemoryController/dsh-maibot-artifacts)。无需 Docker Socket、root、管理员 Token 或额外 HTTP 服务。manifest 暂仅声明已核对的版本，不外推后续版本兼容性。未发布正式 Release 或申请插件中心收录。

## 工作方式与限制

```text
当前聊天附件 → dsh_export → 共享 inbox → DSH 处理
DSH 产物 → 共享 outbox → CLI 生成 manifest
    → DSH 调用已有 agent_talk 回传清单
    → 原聊天 Planner 调用 dsh_import_handoff
    → 官方 ctx.files.upload → file_ref → 当前聊天文件库
    → 可选 ctx.files.send → QQ
```

**原版 ACP MCP 服务不暴露第三方插件工具。** DSH 不能直接调用本插件工具。`agent_talk` 只把文本排入原聊天 Planner，模型仍须实际选择工具；本插件不监听目录、不后台上传，也不把排队成功说成上传成功。接口依据见[兼容说明](docs/compatibility.md)。

请分别检查这些证据：

| 阶段 | 能证明什么 |
| --- | --- |
| prepared：CLI 生成清单 | 本地文件与大小/哈希已准备，尚未通知 MaiBot |
| agent_talk 排队成功 | 传话已入队，尚未执行插件 |
| tool_called：实际工具调用记录 | 原聊天调用了插件，仍可能校验或上传失败 |
| 上传返回 file_ref | 已进入该调用真实聊天的文件库，可核对 files_list |
| 发送返回 sent | 获得明确发送回执；unknown 可能稍后投递，禁止自动重发 |

CLI 的 `relay` 只输出 `agent_talk` 参数，**不会自己调用 MCP 或发送通知**。DSH 须实际执行现有 MaiBot MCP 的传话工具。

DSH 官方接口不能提供可信原聊天目标时，0.2.1 支持**受控确认**：DSH 将严格清单写到 `outbox/.dsh-handoffs/<task_id>.json`，给出准确的请求字节 SHA-256；用户回到原聊天明确调用 `dsh_import_request(task_id, request_sha256)`。该真实 Host 工具调用复用现有交接内核与文件 scope，返回实际 `file_ref`。它不自动路由、不后台上传、不调用 QQ 发送；哈希只确认请求内容，不是聊天授权。

## 安装与配置

1. 将仓库放到 MaiBot 实例的 `plugins/maibot-dsh-artifacts/`，保留根目录源码及 `_manifest.json`。验收期间使用待测 PR 的固定提交，正式部署选择审核后的提交。
2. 确认插件 Runner 实际使用的 Python 环境安装 SDK v2.19.0；只在宿主机安装、Runner 却在容器内，不足以满足依赖。
3. 启用主程序文件功能：WebUI「能力 → 文件功能」或 `[capabilities] files = true`。授权 manifest 的 `files.list`、`files.upload`、`files.download`、`files.send`。
4. 保留 `/srv/maibot-agent:/srv/maibot-agent` 共享挂载，准备真实 `inbox` / `outbox` 目录，给予 DSH 与实际 Runner 合适的 UID/GID 权限。
5. 首次加载由 Runner 生成本地 `config.toml`，或参考 [`config.example.toml`](config.example.toml)，实际配置不入 Git：

   ```toml
   [plugin]
   enabled = true
   config_version = "1.0.0"

   [workspace]
   root = "/srv/maibot-agent"
   max_file_bytes = 52428800
   max_batch_files = 8
   ```

默认单文件 50 MiB、每批 8 个。Host 自身文件/存储/并发/超时限制同时生效。导出上限在 SDK 下载完成、发布最终目标前检查，因此仍需限制 Host 配额和暂存空间。

交接 SQLite 账本位于插件私有 `ctx.paths.data_dir/handoff/`，另有独立 `ctx.paths.data_dir/.handoff-initialized` 哨兵（0600），用于在账本子目录被删除后拒绝自动重新认领旧任务。暂存使用 `ctx.paths.runtime_dir`。保留整个 MaiBot `data` 持久盘，不将私有数据分享给 DSH。完整挂载、权限、文件通道检查见 [Docker 部署](docs/docker.md)。

## 工具

以下均由**原聊天 MaiBot**在 Host 派发的真实工具作用域中执行，不是 DSH 可直接调用的 ACP 工具或 shell 命令。

### `dsh_import_request(task_id, request_sha256)`：受控确认

DSH 插件准备文件后会返回这两个实值。由用户在任务的原聊天明确要求执行：

```json
{"task_id":"0123456789abcdef0123456789abcdef","request_sha256":"<DSH 返回的 64 位小写十六进制请求哈希>"}
```

工具只读取共享根下固定的 `outbox/.dsh-handoffs/<task_id>.json`，不接受任意路径。使用现有逐级无符号链接读取与私有快照，限制 64 KiB，拒绝硬链接、特殊文件及读取竞态。先核对快照**原始字节** SHA-256，再校验严格 manifest v1 和 task_id 一致，最后调用与 `dsh_import_handoff` 相同的 `Handoff.import_manifest(..., send=False)`。

两种导入入口共享同一持久账本；重复任务只核对已授权结果，不重复上传。此工具没有 `send`、`target`、`chat_id` 或 `session_id` 参数。请求文件、内容哈希和 task_id 都不能证明聊天身份；**调用发生在哪个真实聊天，就只可能向该聊天入库，因此必须由原聊天用户确认**。不要把本工具包装成自动识别原聊天的流程。

只有实际工具结果中的逐项 `file_ref` 才证明入库；DSH 的 prepared、确认指引或共享目录中的自称回执都不是入库证据。回执丢失时在原聊天调用 `dsh_handoff_status(task_id)`，核对原调用记录与 `files_list`。`unknown/unconfirmed` 不自动重试、不换 ID 重新上传。

### `dsh_import(paths, send=false)`：直接导入

```json
{"paths":["outbox/task-001/报告.pdf","outbox/task-001/图片.png"],"send":false}
```

按顺序独立处理，保留原名。安全读取并制作私有快照后调用官方 `ctx.files.upload()`。结果包含整体 `success` 和逐项 `items`；成功项 `status="imported"`，`file` 包含 file_ref、文件名、大小、SHA-256 等元数据。部分失败不回滚成功项，不整批自动重试。

这是简便接口，没有任务账本去重；同一路径重复调用可能创建新附件。需要可查询交接时优先使用下一工具。

### `dsh_import_handoff(manifest, send=false)`：推荐交接入口

清单是严格 JSON 对象，直接放在 `manifest` 参数中，不能只给文件路径或自然语言摘要：

```json
{
  "manifest": {
    "type": "dsh_artifacts",
    "version": 1,
    "task_id": "0123456789abcdef0123456789abcdef",
    "files": [{
      "path": "outbox/task-001/报告.pdf",
      "size": 1234,
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }]
  },
  "send": false
}
```

大小和哈希仅示意，实际值由 CLI 从已写完文件计算。`task_id` 为随机 32 位小写十六进制，是幂等标识，**不是 Host 签发的聊天授权**。清单不接受 `send`、`chat_id`、`session_id`、`target` 等额外字段；发送只能由原聊天本次工具调用的显式 `send` 参数决定，默认关闭。

工具在任何上传/发送副作用前先持久认领任务，逐项安全制作快照并校验大小和 SHA-256，通过后才调用官方上传/发送。同一任务冻结清单、发送选项和工作区配置；重复调用只查询原结果，不重新上传或发送。不能通过修改文件集合、发送选项或配置实现重试。

首次调用的 `registered`：全部成功为 `confirmed`，部分成功为 `partial`；没有成功项时，全部确定在上传前失败为 `not_registered`，有未知上传结果则为 `unknown`。`state="done"` 只表示本轮处理结束，不代表所有文件成功。必须读取每项结果，不能把 unknown 当作未上传。

### `dsh_handoff_status(task_id)`：只读查询

```json
{"task_id":"0123456789abcdef0123456789abcdef"}
```

查询不上传、不发送、不继续中断任务。插件不信任模型 kwargs 的聊天 ID，而通过当前合法调用的官方 `ctx.files.list()` 分页核对缓存全部已知 file_ref 的当前聊天访问权。至少有一个已持久记录的引用，且所有引用均有权，才返回可验证的缓存；不回显原发送 message_id。

无已知引用、引用缺失/删除、分页未完成或权限未证实时，返回中性 `unconfirmed`，不透露缓存路径和元数据。它不代表“从未调用”或“肯定没上传”。同一 file_ref 可被管理员合法绑定多个聊天，因此核验只证明当前访问权，不能证明原请求聊天身份，也不据此恢复副作用。

缓存查询只读打开账本，不修改磁盘内容或 mtime；仅返回已授权文件元数据和最小登记状态，不回原输入路径或发送记录。`delivery` 保守为 `unconfirmed`，发送任务的缓存 `success=false`；即使首次调用曾返回 sent，也须在原聊天核对原始回执，不能通过缓存证明另一聊天的投递。

### `dsh_export(file_ref, target)`：导出给 DSH

```json
{"file_ref":"0123456789abcdef0123456789abcdef","target":"inbox/task-001/原始资料.zip"}
```

引用从当前聊天 files_list 或成功导入结果取得。官方 `ctx.files.download()` 下载到私有暂存，SDK 校验大小/哈希，完整成功后才安全发布 inbox，不覆盖任何已有目标。下载失败不在最终位置留下看似完整的文件。可创建缺失 inbox 子目录，outbox 源目录由 DSH 准备。

## 交接与重试规则

DSH 对完成写入、不再修改的文件生成清单，通过现有 agent_talk 回到原聊天。可复制指引见 [DSH_INSTRUCTIONS.md](docs/DSH_INSTRUCTIONS.md)。原聊天路由依赖官方传话与 Planner，不依赖清单伪造身份。

- 副作用前持久 claim。上传异常、取消或崩溃后可能存在未确认副作用，记为 unknown，不自动重新执行，不换 task ID 盲目重试。这是持久 at-most-once 尝试，不是跨 Host/QQ 的 exactly-once 事务。
- 确定发生在上传前的失败，也先人工核查源文件与原聊天文件库，再按最小必要范围创建新任务；不重做已上传项，状态查询不补做失败项。
- 发送 unknown、异常或未识别回执都可能稍后投递，**禁止自动重发**。已有 file_ref 时先核对聊天，不重新导入当作“重发”。
- 保留账本与数据盘，不为“重试”删 SQLite。整体数据丢失、备份回退或人工删记录会破坏去重依据，必须核查原副作用。

模型漏调时，在原聊天明确给出工具名、完整 manifest、`send=false`，要求报告实际工具结果并检查调用记录。工具为 visible，仍不保证被模型选择；部署为 deferred 时先 tool_search。完全人工兜底可在官方 WebUI 文件页选原聊天后上传/下载/发送，插件不获取管理员 Token。

## 安全边界

- 只导入配置根下 outbox 文件，只向 inbox 导出。拒绝绝对路径、`..`、非法段、符号链接、硬链接源与特殊文件，使用 Linux dirfd / O_NOFOLLOW 逐级操作，不把共享源路径直接交给 SDK 重开。
- SDK 使用私有快照；清单大小/哈希与快照比对。交接前须停止写入；哈希只证明一致性，不证明内容可信。
- 导出最终名称原子、不覆盖，异常/取消清理暂存。最终文件权限 0640，不同 UID 的 DSH 需共享组及正确组继承。
- 共享目录不是聊天隔离：获准调用者可导入允许方向的文件，DSH 可读取导出明文。只向可信聊天开放；互不信任的租户需独立实例、工作区和进程权限。
- 不同 UID 但能写父目录者也可能整体移动已打开目录，dirfd 仍跟随 inode。挂载、各级父目录和有移动能力的主体均须可信；同 OS 用户/进程被控制也不在隔离保证内。
- 文件、文件名、清单和传话全部是不可信数据，不执行代码、宏、shell 或其中的工具指令，不自动解压。
- 文件访问权只来自当前真实工具作用域，不将模型参数当聊天授权，不在后台继续使用过期 ctx.files。

## SnowLuma 与实机验收

QQ 文件消息没进入 files_list 时，检查文件能力、结构化 FileComponent / 文件定位、群上传通知开关、聊天过滤与 Host 来源登记。只有普通文字文件名/URL不会成为附件。插件不修复 SnowLuma 入站登记，不导出尚未登记的文件，不扫描群文件。

自动测试不等于真实 Docker + SSH ACP + DSH + SnowLuma QQ 验收。部署前实际验证：QQ 附件登记→导出并核对哈希；DSH 中文名二进制→清单→实际传话→实际工具调用→files_list；显式发送后核对 QQ 文件；越权引用、路径、重放被拒绝；unknown 不自动重发。**这些真实全链路尚未验证。**

## 开发与验证

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

本机 Python 3.12 正式全套 **286 项通过，无失败、无跳过**：原安全/插件回归 103（57+46）、纯插件 handoff 83、DSH helper 48、请求文件确认 41、新插件/安装接线 11；Ruff 全仓通过。原版 v1.3.18 官方 ManifestValidator 校验 0.2.1 清单通过，无 errors/warnings。实际安装测试在没有 Host 源码的临时 plugins 目录复制五个插件运行文件，使用真实 SDK import/create_plugin/on_load 成功。请求确认集成测试执行实际 `request.py`、`Handoff`、SQLite 账本和安全快照；文件 API 使用聊天 scope double，不代表真实 MaiBot HTTP/DSH/QQ 集成。完整依据见[兼容说明](docs/compatibility.md)。Python 3.10 仅配置 CI，本机未运行，远端结果以 CI 记录为准。

`integrations/` 中保留的 Host 补丁是历史实验，**不支持部署，不属于正式安装步骤**；默认 pytest/CI 不运行实验测试。本轮未运行旧 Host 实验，也未修改官方源码。不要使用生产源码或数据作测试目标。

勿提交实际 config.toml、.env、密钥、聊天记录、账本、生成文件或个人路径。反馈请提供脱敏复现步骤：[Issues](https://github.com/MemoryController/maibot-dsh-artifacts/issues)。
