# Docker 部署：保留原版 MaiBot

正式方案只安装插件和配置挂载，使用原版 MaiBot v1.3.18。**不应用 integrations 中的历史 Host 补丁，不运行其安装器，不修改官方源码，也不需要重建修改源码的 MaiBot 镜像。**

部署基线：MaiBot 在 Linux Docker 容器中，DSH 以宿主机普通用户运行，已有 SSH ACP 连接及工具回连。插件不配置 SSH、不启动 DSH、不增加网络服务。SDK 须是实际 Runner 环境中的 v2.19.0；不同镜像的依赖安装方式以该镜像现有管理方式为准，不要只在宿主机 pip 安装后假设容器里可用。

0.3.0 图片功能另需在**实际 Runner 的 Python 环境**安装根目录 `requirements.txt`（Pillow ≥12.3、<14），并保留 `image_safety.py`、`image_open.py`。按现有依赖安装/持久化机制部署，不修改官方源码；安装依赖不等于重建修改核心的镜像。Python 3.10 自动选择 Pillow 12.x，13.x 需要 Python ≥3.11。manifest 已声明该 Python 包依赖，不假设 Host 会自动下载所有依赖。

先在原聊天完成附件导入，再调用 `dsh_open_image(file_ref)`；历史 octet-stream 图片不必重传。该工具不需要新增能力授权、端口或挂载，仍使用 files.list/download 和 Host 正常工具返回机制。主模型要直接看到图片，还需沿用 MaiBot 视觉配置和支持图片的模型；后续图生图插件是否读取工具媒体索引须单独核对，详见[图片验收步骤](images.md)。

若使用 DSH Cordis 配套插件，固定当前 main **0.2.1** 的 `f008cdc039122d360dee67b86e1e2004083ba019`，它要求 Linux、Node ≥24、DSH runtime **0.2.2-alpha.2**；包版本不是运行时版本。先在现有宿主环境核对 `node --version`、`dsh --version`，保留当前 runtime/DSH_HOME/profile，不擅自重装或重置。核查时配套仓库无 tags，不把 main/package 号当 Release。旧 DSH 插件 0.2.0 的 `0958d8ac…` 有已修复的发布状态缺陷，不建议回滚处理未知任务。

配套 0.2.1 默认 manifest 确认模式兼容 MaiBot 插件当前 main 0.2.1 和历史 0.2.0；显式 request 模式必须有当前 0.2.1 的请求工具及 `request.py`。完整固定组合见[兼容说明](compatibility.md)。安装/更新配套插件后，按现有维护流程核查在途任务再重新连接 ACP 进程；安装新文件不代表旧进程已经加载。

## 可选免复制流程：配置与挂载不变

当前 0.2.1 可以按[免复制指引](automation.md)，由用户在原聊天发起任务时明确授权导入该任务产物，DSH 再将实际 `confirmation` 用已有 `agent_talk` 回传，由 Planner 在原聊天调用现有工具。无需换镜像、应用补丁、增加服务或开放新的端口。标准库 `relay-request` 只辅助校验和生成参数，安装时保留完整仓库即可；不配置后台自动上传。

这只能在模型正确路由并实际选择工具时省去再次复制，不能保证自动化。配套默认 publish 仍只写共享请求、不自动通知。`target` 不是原任务授权；来源或授权不清时继续人工确认。不要通过解析 Agent 提示词伪造可信目标服务，也不要使用保存的旧 scope。

## 挂载

在现有 Compose 服务里添加/保留相应卷。下面是**片段**，不是完整部署；保留现有镜像、连接参数、data 及配置挂载，不重复定义同一容器目标：

```yaml
services:
  maibot:
    volumes:
      # 容器内插件目录按你的 MaiBot 实例路径填写；下面 /app/MaiBot 仅为示例。
      - ./maibot-dsh-artifacts:/app/MaiBot/plugins/maibot-dsh-artifacts
      - /srv/maibot-agent:/srv/maibot-agent
      # 保留现有 MaiBot data 持久盘，勿另挂空卷覆盖它。
```

将本仓库完整放入实际实例 `plugins/maibot-dsh-artifacts/`，不要只复制 plugin.py。若 Runner 在另一容器/进程环境，插件与工作区也必须在其实际文件系统可见。确认插件 SDK 版本、入口加载状态与能力授权。

当前 0.2.1 的请求确认入口需要根目录 `request.py`。DSH 在共享 `outbox/.dsh-handoffs/` 保存严格清单请求；MaiBot 按用户明确选择只读对应请求，安全复制到自己的私有 runtime 目录核对原字节 SHA。配套默认 manifest 模式仍需用户在原聊天明确确认完整清单；只有显式 request 模式才调用 `dsh_import_request(task_id, request_sha256)`。两种模式都不新增服务、管理员 Token 或核心改动。

DSH 正常创建 `.dsh-handoffs` 为 0750，请求文件为 0640，归 DSH 用户并继承共享组；确保不同 UID 的实际 Runner 可读及可遍历。不要把该目录改为软链接或 chmod 777，不把 DSH 私有状态目录和 MaiBot 私有账本混进工作区。

原聊天人类确认是操作/模型策略要求。SDK 2.19.0 没有插件可验证的批准事件，部署说明不能当作代码已证明人类同意；本插件只验证 Host 文件 scope，不从历史消息或 Agent 的“已批准”文字造授权。请求哈希只固定内容，不能代替正确聊天与人类意图核对。

宿主机目录：

```fish
mkdir -p /srv/maibot-agent/inbox /srv/maibot-agent/outbox
```

容器 UID/GID 与宿主机用户名不同。通过现有共享组、合适的目录组继承和最小权限，给 DSH 与 Runner 读写/遍历权限；不要 chmod 777。导出最终文件权限 0640，不同 UID 的 DSH 需要共同文件组，可由部署者配置 setgid 目录并实测组继承。插件运行不需要 root。

工作区每级目录须是真实目录，不接受符号链接路径。DSH 生成普通单硬链接文件，停止写入后再交接。不同 UID 但可改名/整体移动父目录的进程也必须可信；dirfd 并不隔离此类主体。

## 配置与持久数据

主程序开启：

```toml
[capabilities]
files = true
```

授权插件声明的 files.list/upload/download/send。插件本地 config.toml 参考根目录示例：共享根 `/srv/maibot-agent`，默认单文件 50 MiB、每批 8 个。首次加载会由 Runner 创建配置；实际配置、私有路径或凭据不提交 Git。

保留 MaiBot 原有 `data` 持久卷，插件账本使用 Host 分配的 `ctx.paths.data_dir/handoff/`（默认位于实例 data/plugins 下），其同级还有独立 `.handoff-initialized` 哨兵（0600）。哨兵使账本子目录被删除后拒绝自动重建/重新执行；不能单独删哨兵绕过检查。暂存使用其 runtime_dir。不要把私有账本、哨兵或暂存挂给 DSH，不放进共享 outbox。整个 data_dir 损失、重建空卷或恢复旧备份仍无法自证旧执行，必须人工核对原副作用，不能视为安全重试。

## QQ 文件通道

文件字节使用 MaiBot 公共后端 `/api/files/transfers/...`，不要求开启 WebUI 页面，也不需要插件持有管理员 Token。保留现有后端服务与适配器配置，不为插件新建 HTTP 服务。

QQ 客户端在另一主机时，按官方文件服务说明设置 `files.transfer_base_url` 为客户端可达的后端地址，并配置现有 `[http]` 监听/反向代理。平台下载 URL 位于私网时，管理员在 `files.trusted_download_origins` 添加**精确 origin**，不开放任意 URL。监听变更按 Host 要求重启。不要公开限时文件 URL 或将其当长期引用。

## 验收与排错

1. 查看插件加载成功、SDK 版本和文件功能开启，检查实际 Runner 内能看到共享目录且有权限。
2. QQ 发送小文件，原聊天 files_list 应登记引用；未登记先查 SnowLuma 结构化附件、群上传通知设置与聊天过滤。
3. 当前聊天 dsh_export 到 inbox，宿主机 DSH 读取并核对哈希；目标已存在时必须拒绝。
4. 按 [DSH_INSTRUCTIONS](DSH_INSTRUCTIONS.md) 准备中文名二进制和实际清单。CLI 路线须真正调用 agent_talk；Cordis 默认只发布，人工确认或可选模型接力都须使用实际 confirmation 参数。检查真实 dsh_import_handoff / dsh_import_request 调用和官方 file_ref，submitted/queued 均不足以证明入库。
5. 只在明确需要时发送，核对 QQ 文件和回执；unknown 不自动重发。另一个聊天不能靠引用或伪造 session_id 取得缓存/附件权限。

当前分层测试及实际结果见[README](../README.md)和[官方组件检查](../host_checks/README.md)；默认套件/协议检查使用 Files API double，组件检查使用临时聊天和测试签发 scope，都不是生产聊天工具派发或 QQ。仅有 PID、挂载目录或 agent_talk 排队回执不足以确认桥接成功。本仓库**尚未完成真实 Docker + SSH ACP + DSH + QQ 全链路验收**，上述步骤须由目标部署执行。

更新插件时使用固定已审核提交并保留私有数据；不使用历史 Host 补丁。停用/卸载插件前处理在途操作，保留账本及原聊天附件以便核查，避免卸载后重新安装便盲目重放。
