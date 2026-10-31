# 将已登记附件打开为图片上下文

候选版本 **0.3.0** 增加 `dsh_open_image(file_ref)`：从当前聊天文件库下载已登记的附件，验证图片内容，再通过官方工具返回的 `content_items` 提供图片媒体。它不重新上传附件、不修改原件或 file_ref，也不向 QQ 发送文件。版本号表示待评审源码，不表示已经发布 Release。

这补齐“附件已入库”到“Host 能把它作为图片上下文使用”的一段。它不消除 DSH 通知路由、Planner 选择导入/图片工具的模型依赖。已核对用户指定的 **maimai-drawpic-plugin v2.1.3：其 edit_image 不支持工具媒体索引**，因此当前组合不能宣称已完成自动图片编辑。

## 版本、安装与限制

| 组件 | 本轮固定支持组合 |
| --- | --- |
| MaiBot | 原版 v1.3.18，`2a9af62cec37035d29237acbe2cf3adceb700b27` |
| Plugin SDK | v2.19.0，`c2a039a7f4bcb367c7d770e88d6e034337be6b12` |
| 本插件 | 本 PR 候选 0.3.0 |
| DSH 配套插件包 | v0.2.1，`f008cdc039122d360dee67b86e1e2004083ba019` |
| DSH 运行环境 | Linux、Node ≥24、DSH runtime/peers `0.2.2-alpha.2`；与配套插件包版本分别核对 |
| 图片解码依赖 | **Pillow ≥12.3.0、<14**，安装到实际插件 Runner 的 Python 环境；Python 3.10 由 pip 选择兼容的 12.x |

用户指定的下游 [maimai-drawpic-plugin v2.1.3](https://github.com/WhiteCloudOL/maimai-drawpic-plugin/tree/56284ac69bed57829ea67ab68ce31309d10b9551) 另列为**已调查、媒体索引不兼容**，不包含在上述功能支持承诺中；其声明的 SDK 上限还低于 2.19.0，详见下文。没有为了本功能修改它、自动升级它或降低本插件要求的 SDK。

按[Docker 部署说明](docker.md)安装完整插件，确保 Runner 能加载新增图片模块和 Pillow。根目录 `requirements.txt` 与 manifest 声明该依赖；只在宿主机安装 Pillow 不代表容器/Runner 已安装。保留原版 Host、现有镜像、文件服务与持久卷，不应用历史 Host 补丁，不读取生产凭据。

文件功能须已开启并授权插件现有 `files.list/download/upload/send` 能力。图片工具只使用其中的 list/download，不额外申请上下文写入或图片解析能力。工作区挂载、任务协议、私有账本及去重规则不变。

图片工具要求附件已缓存且为 ready，DSH 正常上传的产物满足此条件。对只有远端登记、尚未缓存的 QQ 附件，可先按用户请求使用官方 `files_download` 准备缓存，再核对大小并打开；本工具不据不完整的元数据绕过下载或展示限制。

| 图片上下文限制 | 值 |
| --- | --- |
| 支持格式 | 按内容验证的静态 PNG、JPEG、WebP |
| 单次 | 一张已登记附件 |
| 编码后的原文件字节 | 最多 8 MiB，即 8 × 1024 × 1024 字节 |
| 像素数 | 最多 16,000,000 |
| 任一边长 | 最多 8192 像素 |
| 插件返回 JSON 预算 | 最多 12 MiB，为 Host 的 16 MiB RPC 单帧限制保留余量 |

不支持 SVG、GIF、动画 PNG/WebP、损坏或截断图片；文件后缀和历史 MIME 都不能代替解码验证。不转码、不缩放、不挑选动画首帧，不执行文件内容。图片字节、像素数和边长必须同时满足限制；两个维度各自不超过 8192 不代表总像素数一定合格。

## MIME 与历史附件

新导入使用安全私有快照按内容探测：在上述限制内通过完整校验与解码的静态图片，上传时分别声明 `image/png`、`image/jpeg` 或 `image/webp`。普通文件、扩展名伪装、损坏图片以及超过显示限制的图片仍可作为 `application/octet-stream` 附件导入，前提是满足既有文件导入大小、路径、批量和 Host 配额限制。不能把“作为普通文件入库成功”解释为“可作为图片打开”。

历史已经登记为 `application/octet-stream` 的真实图片，可以直接把原 file_ref 交给 `dsh_open_image`。工具忽略文件名和历史 MIME，依据官方下载的真实字节校验；满足限制便能返回图片媒体。不会为此重新上传、生成另一个附件引用或回写历史 MIME，原附件名称、内容和登记信息不变。

调用路径如下：

1. 在本次合法 Host 工具作用域中，通过官方 `ctx.files.list()` 查当前附件是否 ready，并预检字节上限。不能用模型提供的 chat_id、路径或 URL 指定下载目标。
2. 用官方 `ctx.files.download(file_ref, destination)` 下载到受保护的临时目录；SDK 校验下载字节大小和 SHA-256，Host 独立校验当前聊天权限。
3. 有界解码验证后，在 `content_items` 中返回一个 `type="image"`、`data=<Base64>`、`mime_type` 的媒体项。普通 `content` 只有简短说明，不放图片 Base64。成功、失败和取消都会清理私有暂存；失败不返回伪图片或仅凭元数据宣称打开成功。

同一引用重复打开只会重新读取图片，不增加上传或发送副作用。下载、打开超时或结果丢失不能触发重新导入，也不能换 task_id 绕过既有账本。此前导入/发送结果若为 unknown，继续按原流程核查，图片工具不负责恢复未知副作用。

## Host 如何产生可交接的媒体索引

这里使用官方支持的工具媒体返回路径，不伪造用户消息，不把 Base64 塞进普通聊天文字，也不调用私有上下文 Hook：

1. Host [ComponentQueryService 解析工具结果](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L848)，将 [`content_items`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L899) 转成 `ToolContentItem`。
2. Host [媒体方法](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/reasoning_engine.py#L1766)把真实图片字节转为 `ImageComponent`，再建立[关联工具调用和当前运行时的 ToolResultMediaMessage](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/reasoning_engine.py#L1816)。这是 Host 自己处理工具媒体的流程。
3. Host [生成索引](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/reasoning_engine.py#L1630)，例如 `tool_result:call_actual_id:1`。后续插件可经官方 SDK [`ctx.maisaka.context.resolve_image(stream_id, source_id, index=0)`](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/maisaka.py#L20)及 Host [图片解析器](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/context/image_attachment.py#L14)读取已有上下文中的图片。

必须使用**实际 Host 产生并展示的索引**。`call_actual_id` 来自本次真实 ToolCall，插件不生成、猜测或接受它作为输入。最后的 `1` 是整个 `content_items` 列表从 1 开始的内容项位置；当前工具只返回一项图片，所以该位置为 1。SDK 解析器的 `index=0` 则表示该媒体消息内部从 0 开始的第一张图片，两者含义不同。

file_ref 是文件库引用，media_index 是临时聊天上下文中的工具图片索引。两者不能互换；文件名、共享路径、图片哈希或形似索引的普通文字也不能替代实际媒体。上下文压缩、重启或历史清理后，旧 media_index 可能不再可解析；确认原附件仍有访问权后，可重新调用图片工具产生新索引，无需重新上传。

## 后续 `edit_image` 的兼容边界

已只读下载并审查用户指定仓库 [WhiteCloudOL/maimai-drawpic-plugin](https://github.com/WhiteCloudOL/maimai-drawpic-plugin)，固定 **v2.1.3** 注释标签对象 `f7c676712c9043fce51e8f8bd2164da76a4175df`，实际源码提交 **`56284ac69bed57829ea67ab68ce31309d10b9551`**；[_manifest.json](https://github.com/WhiteCloudOL/maimai-drawpic-plugin/blob/56284ac69bed57829ea67ab68ce31309d10b9551/_manifest.json#L4)的包版本同为 2.1.3。未使用远端 main 或较新标签冒充这个固定版本，也没有安装它的完整运行时、平台配置或凭据。

| 核对项 | v2.1.3 源码事实 |
| --- | --- |
| 工具参数 | [schema](https://github.com/WhiteCloudOL/maimai-drawpic-plugin/blob/56284ac69bed57829ea67ab68ce31309d10b9551/src/maimai_drawpic/interfaces/tools/schemas.py#L26)只有必填 `prompt`，可选 `source_message_id`、`source_image_base64`、`model`、`negative_prompt`，且 additionalProperties=false；没有 media_index、image_refs 或 source_id。 |
| 工具入口 | [handle_edit_image](https://github.com/WhiteCloudOL/maimai-drawpic-plugin/blob/56284ac69bed57829ea67ab68ce31309d10b9551/src/maimai_drawpic/interfaces/tools/drawing.py#L65)取得聊天服务上下文后，调用 [find_source_image_inputs](https://github.com/WhiteCloudOL/maimai-drawpic-plugin/blob/56284ac69bed57829ea67ab68ce31309d10b9551/src/maimai_drawpic/interfaces/tools/drawing.py#L181)收集原图，再创建独立后台编辑任务。 |
| 按消息查图 | 使用插件图片缓存或 [message.get_by_id](https://github.com/WhiteCloudOL/maimai-drawpic-plugin/blob/56284ac69bed57829ea67ab68ce31309d10b9551/src/maimai_drawpic/interfaces/chat/message_images.py#L469)，不读取 `ToolResultMediaMessage`；该固定源码未调用官方 maisaka.context.resolve_image。 |
| 指定消息找不到 | [查图逻辑](https://github.com/WhiteCloudOL/maimai-drawpic-plugin/blob/56284ac69bed57829ea67ab68ce31309d10b9551/src/maimai_drawpic/interfaces/chat/message_images.py#L721)会继续查最近 8 条消息，不是立即拒绝指定来源。错误地把 tool_result 索引填成 source_message_id，可能改为编辑聊天中的另一张图片。 |
| 版本声明 | [manifest](https://github.com/WhiteCloudOL/maimai-drawpic-plugin/blob/56284ac69bed57829ea67ab68ce31309d10b9551/_manifest.json#L18)声明 Host 1.3.6–1.3.99、SDK 2.12.0–2.17.0；SDK 2.19.0 超出它的声明范围。原版 MaiBot [校验器](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/runner/manifest_validator.py#L1102)对此记录警告后放行，不能说一定加载失败，也不能说已得到该版本的正式兼容保证。 |

因此本仓库**不提供**把媒体索引直接交给该版本 edit_image 的“可用调用示例”。`source_message_id` 不接受工具索引的事实，不能靠参数名称相似来忽略；也不把图片 Base64 复制进普通模型文字，或强行注入该插件的私有缓存来绕过缺口。

该版本原有参数形式是 `edit_image(prompt="…", source_message_id="真实 QQ 图片消息 ID")`，或由可信调用方提供真实 `source_image_base64`。前者要求平台图片消息实际存在，不是本工具的 media_index；后者不是现成的文件库/工具媒体桥。图片附件已入库和返回工具媒体，并不会自动产生 QQ 图片消息。本轮不增加自动发送来迁就这个接口；若采用人工发送图片再按该插件原流程编辑，属于额外操作，需要用户明确选择，并单独核对 SDK 兼容警告和发送结果。

与本功能直接衔接的最小下游改动应在该图片插件上游单独审查：增加显式的工具媒体引用参数（或明确扩展 source_message_id 的工具索引语义），在其**自己可信的当前聊天上下文**中调用官方 `ctx.maisaka.context.resolve_image(..., index=0)`，检查成功结果并读取 `image.binary_data_base64` 与 `image.binary_hash`，再复用已有编辑流程。指定工具媒体无法解析时应停止，不能回退到另一张最近图片；还需声明解析能力，并测试/更新 SDK 兼容范围。以上是未实现的适配要求，本 PR 不修改第三方插件、不替它申请权限，也不把附件授权转交给它。

这里“可信当前聊天”不能简单取模型参数或普通 kwargs：Host 的 [工具参数构建](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L809)只补缺失/空的 chat_id/stream_id，非空模型参数可能占据这些字段。图片解析能力[按指定现存 runtime 查询](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/capabilities/core.py#L214)，不是当前文件作用域的授权接口。下游插件必须另行核对其聊天上下文来源和权限边界；本文不提供用任意 stream_id 绕过文件权限的代码。

在 SDK 2.19.0 下，若该插件无法证明用于解析的当前 stream_id 来源，另一个权限更直接的上游接口是 `edit_image(..., file_ref=...)`：声明 `files.download`，在本次 Host 工具 scope 内下载并校验附件，将已授权的图片字节交给原编辑任务；不得把临时 scope 保存到后台任务中再下载。这样权限由 Host 文件服务直接限制，不依赖模型指定聊天。该接口同样**尚未在图片插件实现**；本 PR 不新增跨插件私有调用，也不替它修改后台任务。

当前 v2.1.3 不会自动发现本工具媒体，应如实提示“不支持工具媒体索引”，保留附件和真实 file_ref；不要修改 MaiBot 核心、伪造 QQ 消息或私自改写第三方插件来制造成功。新版是否实现适配必须依据它实际的提交、schema 和读取实现重新验收，不能推断升级后必然可用。

## 人工联合验收

以下步骤用于**独立测试部署**，不是本仓库已经完成的生产验收。不要提供管理员 Token、生产凭据或聊天记录；记录可公开的状态与脱敏测试结果即可。

1. 核对实际 Runner 中的 Host/SDK/插件/Pillow 版本、插件加载和文件能力。保持 Docker MaiBot、宿主机 DSH、共享工作区和既有 SSH ACP 配置不变。使用一组只含测试聊天的附件。
2. 让 DSH 准备小型静态 PNG、JPEG、WebP，至少一个中文文件名。按配套 v0.2.1 原有 prepare/publish/confirmation 流程导入原聊天，检查真正的工具执行记录和 upload 返回的 file_ref，再用同聊天 `files_list` 核对每项 ready、名称、大小和新图片 MIME。submitted 或 queued 不是入库证据。
3. 在原聊天明确请求调用 `dsh_open_image`，每次仅传一个真实 file_ref。查看工具返回的格式、尺寸、字节数和成功状态；再检查 Host 上下文实际出现图片媒体与 `tool_result:...:1` 索引。仅有 `success=true` 或一段“已看到图片”的模型文字，不足以证明后续消费者已读取图片。
4. 检查 Planner 视觉能力：原版 [视觉模式判断](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/visual/mode_utils.py#L13)要求 planner 配置引用的模型存在且均声明 visual。模型实际也须支持图像。视觉输入关闭或模型不支持时，不能声称模型理解了图片；工具媒体是否存在与模型是否看图是两个验收项。
5. 若部署的是指定 maimai-drawpic-plugin v2.1.3，到图片上下文阶段为止将后续编辑标为“不支持工具媒体索引”；**不要**把实际索引填进 source_message_id 试图适配，因为它可能回退到近期另一张图。将来若使用经过审查、明确增加工具媒体支持的版本，再按它实际新增参数传 Host 索引、消息内 `index=0`，检查官方解析器取得原图片并完成一次可核对的处理；记录确切版本和结果，不能把 Pillow 测试代替这一步。
6. 选择升级前已按 `application/octet-stream` 登记的一张合格图片，用原 file_ref 打开；比较前后 `files_list`，引用、名称、大小、历史 MIME 均不变，附件数量没有增加。重复打开和重复相同交接请求，也不能新增上传或发送。
7. 测试文件名伪装：普通文本命名为 `.png` 应作为普通文件导入，图片工具拒绝；真实 PNG 即使命名为 `.bin` 也应按字节识别。损坏/截断图片、SVG、动画以及超过 8 MiB、1600 万像素或单边 8192 的图片，打开时应失败并且没有图片媒体。符合通用文件导入限制的大图可以留在库中作为普通附件，不能因此绕过显示限制。
8. 在另一测试聊天提供原聊天 file_ref，或附加伪造 chat_id/session_id/path/URL，必须不能下载/打开原附件；不存在、无效、已删除或权限失效的引用同样不得产生图片。错误不泄漏其他聊天名称、原路径或限时下载 URL。不要为测试跨聊天访问而人为给同一附件添加额外合法绑定。
9. 确认本工具没有上传/发送：对照调用记录、附件数量和 QQ 对话，不应出现新的附件副本、QQ 图片或文件消息。需要发送时另行明确调用现有发送能力；unknown 只表示投递结果未知，禁止自动补发。
10. 测试取消、工具返回丢失和上下文丢失：不能自动重做原导入。检查账本/同聊天 `files_list` 确认登记事实；需要重新打开时仍使用有权限的原 file_ref。旧媒体索引不存在、下游读取失败或 Planner 未选择工具，都应报告对应事实，不能改说已发送/已编辑。

验收应分别记录：附件入库、files_list 可查、图片工具返回媒体、Host 产生实际索引、下游成功解析、模型看图、编辑完成及 QQ 发送回执。某一步通过不代表后续步骤通过。

## 自动化测试证明的范围

默认 pytest 套件使用模拟 Host Files API 检查当前聊天引用、list 预检、下载、真实 Pillow 校验、限制、清理、无上传/发送和异常结果。它不能证明真实 Runner 签发了聊天 scope，也不证明 QQ 或图片编辑插件可用。

独立 [官方文件/媒体组件检查](../host_checks/README.md)执行固定未改动 Host 的临时 SQLite 文件登记、HTTP 字节传输、官方 SDK 和图片能力解析器。为避免启动完整 Planner，媒体转换方法从固定源码经 AST 抽取，保留方法体、参数和装饰器；相关真实 `ImageComponent`、`ToolResultMediaMessage`、上下文图片投影和解析器用于验证媒体契约。这类检查应称为“官方源方法/组件检查”，不是完整 Planner/Runner 派发集成。

独立 [drawpic_2_1_3.py](../protocol_checks/drawpic_2_1_3.py) 直接导入固定 v2.1.3 的工具 schema 和 `find_source_image_inputs`，实际运行 **2 项负向契约检查**，与 Host 组件检查分开统计。它使用消息能力 double 提供一张不相关近期测试图，确认 tool_result 索引被当成普通消息 ID 查询、查不到后回退到该近期图；没有调用图片生成平台、编辑任务或 QQ 发送。这是已执行的负向来源选择证据，不是整套第三方插件集成测试。

在独立测试 checkout 上复现，所需 Pillow/pytest/pytest-asyncio 已在本仓库开发依赖中：

```sh
git clone --branch v2.1.3 --depth 1 https://github.com/WhiteCloudOL/maimai-drawpic-plugin.git /tmp/drawpic-2.1.3-test
DRAW_PIC_SOURCE=/tmp/drawpic-2.1.3-test \
.venv/bin/python -m pytest -q protocol_checks/drawpic_2_1_3.py
```

脚本要求 HEAD 恰好为上述完整提交、src 与 manifest 无本地修改、包版本和 SDK 声明符合固定审查来源。不会导入插件主入口、读取配置或连接外部平台；正式插件运行时代码也不导入该仓库。

当前测试结果与命令以[README](../README.md)和 PR 最终 CI 记录为准。本轮**没有完成真实 Docker + SSH ACP + DSH + SnowLuma + QQ 联合测试，也没有实际运行该下游 edit_image 的编辑任务**。本文人工验收步骤不能冒充已执行结果。
