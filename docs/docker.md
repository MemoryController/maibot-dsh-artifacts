# Docker 部署：保留原版 MaiBot

正式方案只安装插件和配置挂载，使用原版 MaiBot v1.3.18。**不应用 integrations 中的历史 Host 补丁，不运行其安装器，不修改官方源码，也不需要重建修改源码的 MaiBot 镜像。**

部署基线：MaiBot 在 Linux Docker 容器中，DSH 以宿主机普通用户运行，已有 SSH ACP 连接及工具回连。插件不配置 SSH、不启动 DSH、不增加网络服务。SDK 须是实际 Runner 环境中的 v2.19.0；不同镜像的依赖安装方式以该镜像现有管理方式为准，不要只在宿主机 pip 安装后假设容器里可用。

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
4. DSH 按 [DSH_INSTRUCTIONS](DSH_INSTRUCTIONS.md) 准备中文名二进制产物与清单，真正调用 agent_talk，检查原聊天确实调用 dsh_import_handoff，并获得 file_ref。
5. 只在明确需要时发送，核对 QQ 文件和回执；unknown 不自动重发。另一个聊天不能靠引用或伪造 session_id 取得缓存/附件权限。

仅有 PID、挂载目录或 agent_talk 排队回执不足以确认文件桥接成功。本仓库**尚未完成真实 Docker + DSH + QQ 全链路验收**，上述步骤须由目标部署执行。

更新插件时使用固定已审核提交并保留私有数据；不使用历史 Host 补丁。停用/卸载插件前处理在途操作，保留账本及原聊天附件以便核查，避免卸载后重新安装便盲目重放。
