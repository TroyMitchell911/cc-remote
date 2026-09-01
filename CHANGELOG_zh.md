# 更新记录

[English](CHANGELOG.md)

## 未发布

- 新增 Relay 的官方容器化部署：`deploy/Dockerfile` 分阶段构建——Node 阶段从源码
  编译 `web/dist`，Python 阶段以非 root 的 `ccremote` 用户按哈希锁安装依赖；附
  `docker-compose.yml` 与 Docker 版 `env.relay.docker.example`。compose 只把端口
  发布到宿主机 loopback，TLS/WebSocket 终止仍交给现有 nginx；
  `deploy/nginx-reverse-proxy.conf.example` 记录该反代前端。Docker 构建支持
  `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` 构建参数，并补充大陆镜像加速指引。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v35。Codex app-server 的
  精确终态与通过源文件校验的 rollout 终态现在独立于 History 正文投影下发；数百
  MiB 的 rollout 即使仍在补建内容索引，也不会让已经完成的回合继续转圈。终态事实
  始终绑定账号、revision 与源文件，不会猜测“最后一个未完成回合”，也不会重复生成
  完成回执。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v34。主会话完成回执和精确
  Goal generation 的隐藏回执改由 wrapper 有界持久化；任一浏览器已读或隐藏后会
  同步到所有已连接浏览器，重连后仍保持一致，同时不会误隐藏后来替换的新 Goal。
- Work 消息附带的文件和图片现在会保存到当前会话私有的
  `workspace/uploads` 目录，Work 沙箱可以直接读取；上传的输入资料不会被误列为
  生成的 Artifacts，同时旧版相邻目录中的附件路径仍可兼容历史展示。
- 移动端 Markdown 源码编辑器现在会填满可用文件面板；暗色主题下的当前会话卡片
  会保持清晰选中，代码块复制按钮恢复可读对比度，桌面端代码块也会与页面背景
  保持清晰层次。
- 重连补发实时尾部前会恢复当前回合的精确归属，并将与 rollout 源文件绑定的
  浏览器/原生消息身份持久化到已完成的 Codex History；升级时会淘汰身份尚未持久化
  的旧投影，避免刷新后把同一回复重复绘制为两层。旧版 CLI rollout 中相邻的
  user 双记录现在也会复用 app-server 原生 item id，历史刷新不会再把同一次终端输入
  与其实时镜像画成两个回合。
  Remote steer 即使被官方 daemon 延迟写入并归到并行 CLI turn，也只会提取精确
  `clientId`、不会放行外部 CLI 正文；后续原生 History 行会并回原来的乐观消息，
  不再重复绘制。同一条浏览器消息的 live rollout item id 与官方 History item id
  现在可作为两个精确别名并存，不再互相冲突。初始 `turn/start` 输入现在也携带
  同一精确身份；即使超大任务的
  生命周期标记已经落到有界尾部之外，重启恢复仍可通过官方活跃状态与原生
  item/rollout 绑定重新接管，避免误报“已打断”及重复显示同一条输入。
  若有界尾部连当前 `TurnBinding` 也已淘汰，wrapper 现在会用其原始序号严格证明
  并在 Web 消费正文前绑定余下的当前回合后缀；旧版反向顺序写入的错误缓存会一次性失效。
  无实时竞态的
  官方 Codex History 明确报告线程空闲后，其持久化成功或失败终态也会替换临时的
  live 终态，同时保留实时详情，且不会削弱 Claude SDK 的终态权威。
- Wrapper、Relay 与 Web 的协同 wire gate 升级到 protocol v33，并为 Codex Work
  新增多账号支持。新 Work 会话与定时任务都可选择任一已配置 Profile，账号归属会
  独立于当前默认账号持久化，并在重试和 wrapper 重启后保持不变。即使早期账号拓扑
  迁移已经完成，升级时仍会幂等地把旧 Work 数据绑定到当时的默认账号。Profile 被
  移除后既有 Work 仍保持原归属并 fail-closed，不会被改绑；临时目录读取失败会保留
  对应账号最后一次成功的会话投影，不会触发静默回退。wrapper 发布会先快照两个 Work SQLite 注册表，
  验证账号归属迁移，并在失败回滚时先恢复匹配数据再启动旧代码。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v31。wrapper 内部的目录
  变化不再广播无关联会话列表，而是发送不进入重放环的失效提示；当前可见页面会
  将并发提示合并为绑定自身连接 generation 与 surface 的列表读取。流式公式可识别
  跨 delta 拆开的分隔符，暂停 Goal 恢复时会保留一次有界目标锚点，compact 续接也
  不再同时显示运行转圈和真实“已打断”终态。
- Wrapper、Relay 与 Web 的协同 wire gate 升级到 protocol v32，并新增可同时使用的
  Codex 多账号 Profile。每个 `CODEX_HOME` 独立拥有官方 daemon、目录、控制状态和
  历史命名空间；Code 统一展示并提供账号标签/筛选，Work 仍只使用默认 Profile。
  单账号保持原生 id 与原 UI；多账号卡片使用稳定的彩色 `default`/天体 ribbon。
  本地 Profile key 迁移支持崩溃续接，并覆盖 alias、fork 恢复、turn lease、控制状态、
  置顶、Work 归属与 rollback checkpoint。无界面 Profile 现在会为各自账号
  bootstrap 官方 remote-control daemon，不再静默降级到私有 stdio；只有 OAuth
  与会话数据的次账号会安全复用已校验的 managed standalone CLI 入口，同时保持
  登录、rollout、socket 和 daemon 独立，已有自定义目录不会被覆盖。账号控制面
  不可用时会明确失败，单账号的既有 fallback 语义保持不变。已登录账号若本次额度
  读取暂时没有返回窗口，也会显示为可刷新重试的读取失败，而不会再误导为缺少账号。
- Claude 问题在刷新以及 Claude/Codex 页面切换后保持同一条消息。wrapper 不再把
  Claude Code 内部生成的 `promptId` 误当作浏览器消息 id，只持久化按 turn generation
  冻结的 Agent SDK transcript 新增边界（SDK replay 作为兜底），或 broker 精确新增
  边界观察到的原生 user UUID 映射；学习这项 Claude 元数据时也不再误入 Codex
  账号缓存。升级时仅淘汰旧身份模型生成的 Claude 派生页，不清除 Codex 历史页。
  Agent SDK 回合仍在运行时，transcript EOF 会保持为开放投影，首次 ownership 扫描
  也不再重复镜像半成品历史；只有真实 `ResultMessage` 才能完成该回合。满足严格
  证据的延迟 `request_retry` 分叉不再隐藏已经成功完成的兄弟尾段；进入 resident
  会话或重试切换命令时，也会使用新序号发布当前生命周期状态，不再重播过期的
  `running` 帧。
- 新增 protocol v28 Codex 账户活动。现有的一次性状态读取会携带经过校验、限制
  为最近 53 周的每日 Token 序列；Web 提供仅 Codex 可见、仿 Desktop 的活动
  日历，且这些账户数据不会进入实时重放缓存。五档颜色按当前日历中的单日峰值
  相对计算，大数值使用“万 / 亿 / 兆”显示。
- 新增 protocol v27 Codex Code 会话目录迁移。wrapper 会让空闲会话以同一个
  原生 thread ID 在所选的现有目录恢复，保留其排队消息；若新目录恢复失败则回退
  原工作目录。所选目录可跨 wrapper 重启恢复；迁移不会派生新会话，也不会抢走
  浏览器焦点。
- 在协同的 protocol v30 gate 下，为工作目录外预览新增仅面向请求客户端的确认。
  授权同时绑定引擎、空间、会话、规范路径、文件所属 UID、设备号和 inode；文件
  身份变化后会重新确认。用户确认的文件保持只读，本会话结构化写入成功的精确文件
  才保留编辑权限。外部 Markdown 的相对图片按文档所在目录解析，但本地路径始终
  不会成为浏览器 URL。
- 在 Wrapper、Relay 与 Web 协同的 protocol v30 wire gate 下新增隔离的 Artifact
  与文件活动渲染。HTML 产物预览
  会保留文档 CSS，并提供用户显式启动的隔离交互预览；独立 SVG、Markdown SVG
  和对话 SVG 共用同一套有界安全清洗。内置工具成功读取工作目录外图片后，只向
  Remote 提供读取当刻的精确内存快照，不开放其所在目录；文件活动也按读取、创建、
  修改、删除和移动展示，不再统一使用编辑笔图标。
- 将忙碌会话的后续消息队列从浏览器内存移交给常驻 wrapper。Protocol v25
  允许排队消息和打断后的替换消息在所有 Web/PWA 客户端休眠或断线时，仍于当前
  回合结束后立即继续执行；客户端重连时会恢复 wrapper 的权威队列状态。队列标签
  只保留有界摘要，点击后私有按需读取完整指令，并可在 wrapper 中原子编辑而不丢附件。
- Wrapper、Relay 与 Web 的协同 wire gate 升级到 protocol v26，并为 Codex
  的 `$` 补全增加轻量 Skills-only 目录。缓慢的 Apps 或 MCP 枚举不再隐藏已经
  返回的 Skills；完整 Extensions 面板仍保留原生全量目录。
- 新增 Codex 官方 named permission profile 控制，并与审批策略分开管理。紧凑的
  权限面板可选择 Read Only、Workspace、Full Access 及按 cwd 生效的自定义
  profile，不增加输入框底栏控件；protocol v24 在 Wrapper、Relay 与 Web
  之间传递这些新控制项。
- 新增 Codex 会话级网页搜索模式（`cached` / `live`）；切换后会无损重连，
  wrapper 重启后仍保留，同时不修改用户的全局 `config.toml`。

- Claude Agent SDK 升级到 `0.2.128`，同时让 wrapper 显式运行用户日常使用的
  `~/.local/bin/claude`，不再静默选择 SDK 内置副本，使 Remote 与终端的凭据和
  CLI 更新保持一致。
- 在隔离的 Work 策略中保留用户的 Claude 订阅 OAuth 设置，并将内置
  `AskUserQuestion` 按原始单选/多选问题展示，不再误显示为通用工具权限审批。
- Codex 忙碌时发送默认与官方客户端一致，使用原生 `turn/steer` 引导当前任务；
  排队仍可选，停止保持为独立操作。Claude 继续使用原有打断并发送语义。
- 对配置的安全源窗口内、单个超长回合的重型过程做回合内分页，不再仅因浏览器
  256 块展示上限而用“较早过程已省略”替换本可读取的真实过程。
- shared daemon 恢复绑定期间拒绝其他 thread 的生命周期帧，并在服务端明确确认
  自动回合已不存在时安全解除假运行状态。
- Wrapper、Relay 与 Web 的协同 wire gate 升级到 protocol v22，增加可安全重放的
  用户问题关闭事件与多选回答。
- Codex 切号后会在新 daemon 上继续同一个正在运行的任务，续跑完成前不会提前发送
  queued 消息；Goal 走原生目标循环，普通回合使用隐藏的上下文续跑。若 daemon
  重启时正在运行的正是 Goal 自动回合，也会按同一规则迁移；app-server 只恢复 Goal
  状态却没有启动下一回合时，cc-remote 会自动补发隐藏续跑请求。
- 协同 wire contract 升级到 protocol v23；Codex 状态响应携带源 `request_id`，
  避免切号后延迟返回的旧账号快照覆盖新额度。
- 在上下文用量旁显示当前 Codex 账号的 5 小时和每周剩余额度，并在切号后按 daemon
  代际安全刷新。

## v3.0.0 — 2026-07-24

cc-remote v3 在原有 Claude Code + Codex 远程控制面之上新增隔离的 Cowork 风格
Work 工作台，并重新设计历史恢复、原生客户端协同、多机器路由、移动端可靠性和
发布运维。

### Code 与 Work

- 为 Claude 和 Codex 新增彼此独立的 Code / Work 双空间；会话列表、焦点、目录、
  基础提示词、权限和恢复状态均分开管理。
- 新增按引擎隔离的 Work 项目、文件/链接/笔记资料库、可复用工作模板，以及创建
  工作时物化到私有目录的上下文。
- 新增一次性、每日和每周定时任务；运行记录、租约、心跳、失败重试与防重叠状态
  持久化保存。
- 每项 Work 只能访问注册表确认属于它的私有目录；外部资料必须通过附件或项目
  资料库显式加入。
- 自动列出 Work 产生的 Artifacts，并在本机预览源码、Markdown、安全清理后的
  HTML、图片、PDF 及沙箱临时转换的 Office 文档。

### 会话、控制与扩展

- 补齐可靠的删除、重命名、归档、消息级派生、临时侧聊、排队、打断和后台会话
  控制，避免后台响应抢走当前焦点。
- 接入 Codex 原生 compact、Review 和独立 Git worktree 派生。尚未完成的 Codex
  Rollback 与 Claude Rewind 不对用户开放。
- 模型、思考强度、服务档位、协作/Plan 模式、权限、上下文、目标、状态、用量和
  限额均绑定当前会话。
- 新增真实 Skills、Plugins、Apps、MCP 和 Hooks 目录。Code 可在引擎支持时管理
  Skills、插件和 Claude Hooks；Codex Hooks 与 Work 中的全部扩展保持只读。
- 将 Claude 工具审批，以及 Codex 命令、文件修改、用户输入、通用权限和 MCP
  elicitation 请求回传给当前控制浏览器。

### 本地优先历史

- 网络校验前先绘制浏览器 IndexedDB 中最近一次验证的本地投影。
- wrapper 使用可重建的 SQLite 索引物化与源文件指纹绑定的回合摘要。
- 优先加载最新回合；工具输出、reasoning 和超长正文只在展开对应回合时按需获取。
- 向上翻页加载更早历史时保持当前阅读锚点，并在后台收敛追加中的源文件。
- 历史图片按需读取，不再嵌入每一个历史分页。

### Codex 超长会话与原生命周期

- 按回合从 rollout 尾部向前读取 Codex 历史，不把历史重新上传给模型，也不替换
  app-server 原生 resume 与 compact 状态。
- 对 Codex Desktop + OpenAI 的特定超大恢复场景，增加严格限定的官方 HTTP 传输
  兜底，处理 WebSocket 在完成前关闭的问题。
- 区分 Codex shared-daemon CLI 活动与私有 Codex App 所有权。
- 将 prompt、steer、commentary、工具、compact、abort 和 completion 绑定到权威
  turn，避免历史内容漂移到会话末尾。
- 正确镜像被打断和外部正在运行的工作，不留下错误只读锁或永久“思考中”状态。

### 设备与所有权

- 新增 Device Center、会过期的一次性配对码、哈希保存的机器凭据、重命名/撤销和
  在线状态。
- 新增可选多用户策略，把每个账号限制到明确允许的 wrapper 机器。
- 在设备发现、命令、事件和 Push 订阅上执行账号到机器的授权检查。
- 按设备、Code/Work、引擎、WebSocket generation 和会话归属隔离工作目录及延迟
  focus/rekey 事件。
- 在 Darwin/Linux 上共享精确进程身份扫描；Claude 接管只处理同一用户且精确匹配
  的进程。
- 新增按用户和机器隔离的 Web Push；旧用户迁移到不含会话信息的通用提醒。只有用户
  主动选择会话模式后，通知才携带有界显示名和经过验证的设备/空间/会话精确路由，
  始终不包含 prompt、回复、路径或工具内容。

### 移动端与 Artifact 体验

- 稳定向上分页、本地优先会话切换和有界实时尾巴补流。
- 历史图片按需加载；触屏灯箱支持点击关闭和双指缩放。
- 支持多图附件、稳定的待发送图片预览，以及跨会话和引擎切换保留各自输入草稿。
- Markdown 相对链接/图片、源码、安全 HTML、PDF 和 Office 沙箱预览均留在 wrapper
  的本地安全边界内。
- 更新 PWA 和通知资源，修复窄屏弹层、过程时间线及无法关闭的错误提示。
- 登录后的通知、主题和退出登录统一收入三点菜单；桌面使用可访问 popover，手机
  使用适配安全区和虚拟键盘的底部 Sheet。
- 让运行标志保持在排队/打断控件上方，保留 Claude 回合耗时，并将重复工具活动紧凑
  展示且不隐藏最终答复。

### 发布与运维

- Python、Codex `clientInfo`、Web package metadata 和公开构建清单统一为产品版本
  `3.0.0`。
- 严格 wire gate 升级为 protocol v20。
- 为 Linux x86_64、Linux arm64、macOS Intel 和 macOS Apple Silicon 发布可复现、
  带校验和及 GitHub artifact attestation 的 Relay/Wrapper 安装包。
- 新增校验后的角色引导程序、托管 Python 3.13 环境、macOS LaunchAgent 安装器和
  Linux Wrapper systemd 安装器；设备凭据始终放在不可变 release 与服务定义之外。
- staging 或激活 release 前同时校验产品版本和协议版本。
- VPS 使用不可变 release、release 内独立虚拟环境、原子切换、就绪检查和失败回滚。

### 升级注意事项

- v3.0.0 使用 wire protocol v20。Wrapper、Relay 和 Web 必须一起升级；混用协议
  版本会被拒绝。
- 部署后对已打开的浏览器页面执行硬刷新，使其加载 v3 哈希资源，并按 protocol v20
  重建本地投影。
- 运行密钥和机器状态必须放在 release 目录之外；不要覆盖 `.env`、`~/.cc-remote`、
  Claude transcripts 或 Codex rollouts。
- Claude 集成继续固定为 `claude-agent-sdk==0.2.119`。
- 浏览历史仍然只是本地读取，不会 resume Claude/Codex，也不会创建模型回合。
