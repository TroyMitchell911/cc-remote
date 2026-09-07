# 远程 Viewer：HTTP／裸 IP 兼容设计

本文保留 Bridge 基础版本的设计和验收记录；后续家目录按需发现使用 protocol v55。
设计的发布矩阵并不等于全部已验收；本地完成项与剩余条件见第 7 节。
当前使用方法与兼容限制见 [remote-viewer.md](remote-viewer.md)。

## 1. 决策与范围

默认采用 **Bridge 沙箱模式**：复用用户已能访问的 cc-remote 地址和端口，
不增加 DNS、通配证书、公开端口或远端 Chrome。现有子域名隔离实现保留为可选
**Isolated 模式**，不再是使用远程 Viewer 的前提。

首要验收对象是已有的静态 Three.js／STL 结构查看器：HTML、CSS、ES modules、
已登记的 HTTPS CDN 模块，以及通过相对路径加载的多个模型文件。界面继续使用
桌面右侧面板、手机全屏；旋转、缩放、姿态和零件选择由原页面执行。

这不是任意内网 URL 代理，也不是完整的远程浏览器。带登录、后台 API、
WebSocket、服务端写操作的应用仍是后续独立范围，不能宣称已经支持。

### 访问方式矩阵

| cc-remote 主站访问方式 | 默认 Bridge 模式 | 新增部署前提 |
| --- | --- | --- |
| HTTP + 公网 IPv4／IPv6 | 纳入验收范围 | 原安装已明确允许 HTTP；不自动开启不安全模式 |
| HTTP + 私网／VPN IP | 纳入验收范围 | 原安装已允许该精确访问来源 |
| HTTP + 域名 | 纳入验收范围 | 沿用原安装的 HTTP 许可 |
| HTTPS + 域名 | 纳入验收范围 | 无额外域名或证书 |
| HTTPS + IP | 纳入验收范围 | 主站原证书必须能被客户端正常信任 |
| HTTPS + 独立预览子域名 | 可选 Isolated 模式 | 显式配置预览 DNS／TLS／代理策略 |

此矩阵是发布验收要求，不是当前完成状态。HTTP 传输仍是明文，沙箱不能防止
链路窃听或篡改；不建议把含敏感数据的 HTTP 安装直接暴露公网。
浏览器要求安全上下文的能力不会因为增加 Bridge 而变得可用。
[安全上下文说明](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Secure_Contexts)

Viewer 原地址是 `http://局域网地址:端口` 不影响上述判断：登记后读取的是源设备
上的文件，原地址只作为匹配别名，用户手机不直接连接那个地址。

## 2. 数据与权限结构

```text
源设备的已登记目录
    ⇅ Wrapper 专用 /ws/viewer 二进制通道
Relay：认证、设备权限、预览实例、限流
    ⇅ 主站认证的浏览器资源 WebSocket（新增独立路由）
cc-remote 主页面：每个预览实例的资源代理
    ⇅ 一次绑定的 MessageChannel／可转移 ArrayBuffer
opaque-origin iframe：页面脚本、Three.js、模型渲染
```

- 继续使用现有目录登记、文件描述符读取边界、设备权限与资源传输能力。
  资源不进入聊天 WebSocket、消息历史、回放环或模型上下文。
- 浏览器资源连接和 Wrapper 资源连接使用不同路由、不同认证角色，不能仅凭
  客户端 JSON 中的角色名区分。浏览器连接复用主站 Cookie 和严格 Origin 校验；
  Wrapper 连接继续使用设备绑定的 Authorization。
- 浏览器资源连接由主页面建立。预览 iframe 不接收登录 Cookie、Bearer token
  或能访问整个账号的密钥。预览实例 ID 只是路由标识，单独持有不能读文件。
- Relay 每个实例绑定登录会话、精确页面 Origin、父设备／会话、资源设备、
  登记项和连接代际。每次读取、续租、取消都验证归属和实例状态。
- 允许的 Origin 复用主站已验证的来源规则，包括已开启的私网 IP 访问；不能
  再把所有请求硬编码为 `PUBLIC_ORIGIN`，也不能接受任意 Origin／转发头。
- 主页面的资源代理是受限操作集合，不提供任意 URL fetch、任意命令执行、
  切换目标设备或读取另一个预览实例的接口。

MessageChannel 可以连接父页面与 iframe；ArrayBuffer 可以转移所有权以减少
一次跨上下文传递中的复制，但这不意味着完整加载、解码和 GPU 上传都是零拷贝。
[MessageChannel](https://developer.mozilla.org/en-US/docs/Web/API/MessageChannel)，
[可转移对象](https://developer.mozilla.org/en-US/docs/Web/API/Web_Workers_API/Transferable_objects)

## 3. 沙箱边界

1. 主站提供独立的、由项目控制的 Viewer runner。它不是用户 HTML 文件的直出
   地址。先加载可信 runner，再通过实例专用通道交付页面与资源。
2. iframe 使用 `sandbox="allow-scripts"`，不加入 `allow-same-origin`。
   runner 的 HTTP 响应也必须带 CSP `sandbox allow-scripts`；不能仅在 HTML
   meta 标签里声明 sandbox。直接打开 runner 时不启动预览、不获得读取权限。
3. runner 的响应策略限制网络、表单、嵌套页面、弹窗、顶层跳转、摄像头等。
   原始页面不能移除 HTTP 响应施加的策略。默认 `connect-src 'none'`；允许的
   CDN 仅进入该登记项的 `script-src`，不放宽主站的脚本／连接策略。
4. 首次握手必须核对具体 `contentWindow`、预期初始化状态、协议和实例代际，
   随后交付唯一 MessagePort 并移除窗口级初始化监听。`event.origin === "null"`
   不能单独作为身份凭据；其他 opaque-origin 页面不能借此加入。
5. 通道绑定后只服务对应登记项。导航、刷新、关闭、注销或代际变化使旧通道失效；
   迟到消息不能重新接管新实例。不能因 iframe 又发来 ready 而重复授权。
6. 用户资源通过专用二进制通道交付，不在主站路径下以可执行 HTML／JS 原样提供。
   如保留下载接口，须独立鉴权、强制附件和 `nosniff`，不能形成同源脚本入口。

Bridge 不用跨站 Cookie，不靠 Service Worker、不关闭浏览器安全检查，也不以
`Access-Control-Allow-Origin: null` 给所有沙箱放行。不能用“换端口”替代完整鉴权：
Cookie 并不按端口隔离。
[iframe 沙箱](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe#sandbox)，
[CSP sandbox](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/sandbox)，
[Cookie 的端口边界](https://www.rfc-editor.org/rfc/rfc6265#section-8.5)

安全目标是隔离主站和未授权资源，不是证明任意 JavaScript 都不会耗尽 CPU／GPU，
也不是对已交给页面的资源提供防泄漏保证。登记范围中不应包含凭据或无关资料。

## 4. 多文件兼容：不是只套一个 iframe

Bridge 需要专门的静态页面适配器。处理结果只存在预览运行时／受限缓存中，
不改项目源文件，不运行项目构建脚本、npm install 或任意插件。

| 内容 | 设计处理方式 | 必须验证的边界 |
| --- | --- | --- |
| HTML、样式和静态图片／字体 | 解析文档和 CSS，解析相对路径；资源在沙箱内部生成 Blob URL | `url()`、`@import`、`srcset`、加载次序，不能正则全局替换 |
| 本地 ES modules | 解析静态依赖图，生成稳定模块标识和 import map；Blob 在沙箱内创建 | 循环依赖、重复模块身份、export-from、字面量动态 import |
| 相对路径和 `import.meta.url` | 保留逻辑资源 URL，适配器映射到登记项内的路径 | `new URL()`、`new Request()`、查询串及 `../` 资源引用 |
| 静态资源 `fetch` | 沙箱内适配为受限通道读取，保留所支持的 Response／流语义 | string／URL／Request 输入，GET／HEAD、取消、错误、Range |
| HTTPS CDN 的 ESM | 仅允许登记过的 HTTPS 脚本来源，由浏览器正常加载，依赖其 CORS 支持 | 固定版本、传递依赖、跨域重定向和断网；不转发账号凭据 |
| STL 和二进制资源 | 分块传输，页面现有 loader 解析；不转 base64 | 并发、多文件、进度、完整性、资源变化与 GPU 内存 |

虚拟 URL 只用于路径语义，例如保留域名 `.invalid` 下的逻辑路径；不是真实
网络请求目标。源页面的 base 设置需受控处理。普通相对 URL 先按页面／模块
语义解析，再由服务端重新验证规范路径；不能为了允许 `../` 而放行目录穿越。

Import map 只解决模块 specifier 解析，不自动处理 `<script src>`、CSS、fetch、
Worker 或所有动态加载。这些路径必须分开适配和测试。
[Import map 的范围](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/script/type/importmap)

不能只拦截 fetch 就宣布支持所有图片和模型：动态创建的 Image、TextureLoader、
glTF 外部纹理、XHR、Worker 等有各自的加载路径。首轮发布最低基线是当前
Three.js／STL 页面所需的能力；其他能力只有独立验收通过后才列为支持。

非字面量动态 import、运行时拼接脚本、复杂文档写入、多页导航、依赖存储的应用、
Worker／WASM 特定加载器等，必须明确检测／报告限制。能提前识别的在启动前报告，
不能静态判定的提供具体运行错误，不能无提示空白或声称任意网页无需适配。
这不是放宽目录权限或关闭 CSP 的理由。

不支持时提供明确原因与可行的下一步：补登记缺失目录、使用已验证的静态导出、
固定并本地化依赖，或由管理员配置可选 Isolated 模式。不要在用户操作时静默
降低隔离强度，也不要弹出没有实际效果的“允许全部”。

## 5. 性能、缓存与生命周期

- 先加载界面、代码依赖，再按页面请求加载模型；不预先把整个项目下载到手机。
  代码解析／依赖整理按需加载，避免扩大聊天页面初始包或长时间阻塞主线程。
- 继续使用 64 KiB 分块和有界预取。浏览器端也需要消费确认、并发上限、总字节
  预算、排队上限、超时和取消，不能只限制 Wrapper。模型流不占用长 HTTP 响应，
  避免把既有代理的 HTTP 写超时直接当作大资源下载期限。
- 初始实现从浏览器每实例最多 4 个资源读取开始压测；设备仍受现有全局限制。
  代码图数量／总大小、Blob 缓存和整体排队需有独立硬上限，最终值随实测锁定。
  传输缓存有界不代表几何解码和 GPU 内存有界，手机必须单独测量。
- 缓存键包含资源设备、登记项、配置版本与文件版本，不能仅以路径为键。代码
  依赖图加载过程中发现文件变化则重建／报错；运行期资源变化不能拼接新旧字节。
  不宣称能对任意正在修改的目录提供原子快照。
- 当前 iframe 和已经显示的资源，不因聊天新增消息、状态更新或普通 React
  重渲染而销毁。Blob URL 只在资源不再使用或实例释放时回收。
- 会话切换只保存预览描述符；关闭 frame、取消读取并撤销实例。返回／刷新后
  重新鉴权。不要承诺保留原网页内部相机或表单状态。
- 断网、设备离线、实例过期分别显示状态；保留当前画面并停止新读取，重连后
  检查版本再决定恢复或提示刷新，不把所有资源瞬间替换成红色重试块。
- 注销／撤销设备／撤销登记是权限变化：清理显示与客户端缓存、取消后续传输，
  不能沿用普通断网时的保留策略。已经交付给页面的数据无法远程收回。

## 6. 配置与上线方式

- 使用 `VIEWER_MODE=off|bridge|isolated`，新安装默认 bridge。v55 默认允许明确
  引用的家目录页面按需发现（`CC_REMOTE_VIEWER_HOME_PREVIEW=0` 可关闭），另保留
  手工登记；不扫描／列出整个家目录，不把自动页面加入全局目录。
- Bridge 不要求 `VIEWER_ORIGIN_TEMPLATE`，不改变既有主站 Cookie 名，不强制
  重新登录。空配置不再显示“必须配置预览域名”。
- 若已有显式隔离域名配置，迁移时保留其意图并明确报告所选模式，不能悄悄改变
  Cookie 或认证模式。Isolated 配置错误应明确失败，不自动降为 Bridge。
- Isolated 需验证主站与预览的同 site／不同 origin、TLS、路由和 CSP；保留
  每实例来源隔离。跨 site 的移动 Safari Cookie 行为不能当作已支持。
- 默认部署仍是同一代码版本更新 Relay、Web、Wrapper。增加 runner 和浏览器
  资源 WS 路由的代理策略须进入仓库安装模板及可回滚事务，覆盖 HTTP 和 HTTPS
  路径，不能靠上线后手工改 Caddyfile。
- 主站只允许受信 runner；它的独立 CSP 不能继承主站的冲突响应头，也不能让
  新规则意外覆盖已有 HTML runner 或 GitHub 图片放行。
- 对同协议资源连接也校验明确的握手版本；旧端拒绝应表现为预览版本不匹配，
  不破坏聊天连接。最终协议版本在实现完成后统一冻结、校验并三端发布。
- 本功能启用不启动模型、不重启原网页 HTTP 服务、不强制接管 CLI 会话。
  登记项热加载；用户在页面中不需再切换“允许交互”。

## 7. 实现验收记录与尚未证明的部分

基础版本使用本地真实 Relay／Wrapper 二进制通道和浏览器进行验收，不连接模型，
不修改线上服务，也不修改源 Viewer。当前通过：

- 在实际私网 IPv4 的 **HTTP 裸 IP** 地址运行 Chromium 和 WebKit 手机配置，
  主页面确认 `isSecureContext === false`。未使用 localhost 特例、TLS 忽略、CSP
  绕过或主站请求拦截来完成这组测试。
- 16 项 Bridge 浏览器回归：模块循环、export-from、动态字面量 import、相对
  `Request`、CSS import/url、srcset、43 路资源突发、Range／HEAD／304、取消、
  登录撤销、刷新、会话切换、BFCache、布局、错误提示及伪造 null-origin 握手。
- 保留的 Isolated HTTP 子域名测试通过；该组使用测试 DNS 路由，不代表真实
  DNS／TLS 或移动 Safari 跨站 Cookie 已通过。
- 真实 Three.js/STL Viewer 的只读源副本在两个引擎均完整加载 **43 个 STL**，
  二进制资源共 **20,248,368 字节**，没有脚本错误；姿态、爆炸视图、切换相机、
  零件选择和发送消息后保持同一 iframe 通过，桌面滚轮缩放也通过，截图已确认
  模型正常显示。手机捏合手势仍待真机验证。
- 一次本地样本从打开页面到模型 ready 为 Chromium 7.9 秒、WebKit 3.0 秒。
  样本受本机缓存与 CDN 网络影响，不是手机蜂窝网络性能承诺。
- 资源通道回归覆盖目录／设备／登录／Origin 隔离、有界预取、撤销和源资源
  清理。终态回执在读取槽位释放之后发送，避免 HEAD／空文件的排队竞争。
- 最终本地门禁：Python 2633 通过／2 平台跳过；聊天浏览器矩阵 389 通过／23
  平台跳过；构建、可靠性、lint、ruff、shell 语法／shellcheck 与差异检查通过。
  初次全量有 WebKit 建页超时，分组到独立进程后整轮通过。另一次单测复跑发现
  选区用例的偶发滚动断言失败，在未修改的 `686bdda` 基线也能复现；未借本功能
  修改聊天滚动实现，该既有用例的稳定性仍需单独跟踪。

仍须在正式发布验收中完成：真实 HTTPS 域名／受信 IP、IPv6 与公网入口，
真实源设备经部署链路的连接，物理 iPhone 的蜂窝网络／锁屏恢复／捏合操作，
大模型峰值内存和 GPU 压力。浏览器 WebKit 手机配置不等于物理 iPhone。
未通过这些项目之前，不能把本地测试称为完整外网验收或成功部署。

### v55 家目录按需发现与本地 Three.js addons

- 不扫描家目录；只处理会话中的 HTML 路径、成功结构化写入、明确的本地 HTTP 链接。
  后者必须经过源设备的地址、监听 socket、OS 用户及 Python 静态服务器 argv 验证，
  不以私网地址推断归属，也不请求原 URL。
- 回归覆盖空手工清单发现、跨设备关联、同 URL 多设备歧义、私有持久化、Relay
  描述符缓存重建、移除后的撤销标记、关闭 home 策略，以及所有资源目录祖先不跟随符号链接。
  macOS 原生 `Python` 与 Linux/venv `python3` 进程都纳入识别；服务 PATH 缺少 sbin
  时仍从固定系统路径寻找 `ip`/`ss`。
- Chromium 和 WebKit 手机配置通过未登记的真实 Python 静态服务发现、点击链接、
  旋转、刷新恢复以及全局清单不膨胀。未改源文件、未调用模型、未放宽 iframe 沙箱。
- 本地 addons 的真实 Three.js 页面只读副本完整加载 103 组主体及 63 组参照模型；
  无缺失模型或脚本错误，切换视图、膝角调整、对话更新后保留 iframe 通过。
  支持精确和最长前缀 import map，不支持 scope；前缀逃逸仍拒绝。
- 页内下载（包括大型 STEP）、后台 API／WebSocket、未知静态服务启动器不在此次
  适配范围。物理手机与上线后的传输链路需要在部署验收时单独检查。

### 设计阶段的先行机制验证

设计阶段进行了不修改项目文件、不接触线上服务的浏览器内存夹具验证：

- 主页面 URL 使用 HTTP 文档测试 IP，实际响应由测试路由提供；主页面与沙箱
  均确认 `isSecureContext === false`，没有使用 localhost 的安全上下文特例。
- Chromium 和 Playwright WebKit 均完成：不透明来源的沙箱、在沙箱内创建的
  Blob ESM／import map、MessageChannel 二进制传递、相对 Request URL 解析和
  WebGL context 创建。
- 两个引擎中，读取父页面 DOM／Cookie 被拒绝，原生网络 fetch 被 CSP 拒绝。
  第一轮 WebKit 夹具误拦截了 Blob 请求；修正夹具后重新验证以上完整项目通过。

以上先行测试只是浏览器基础机制验证，不是完整适配器测试，不验证真实公网连通、三端
认证链、真实模型视觉结果、iPhone 设备内存或 Safari 全部行为。不能据此部署。

## 8. 实现顺序与发布门槛

1. **最小纵向验证**：独立测试 fixture 接通真实 Relay／Wrapper 资源通道与
   Bridge，先跑当前 Three.js／STL 页面。必须在非安全 HTTP IP 场景以及 HTTPS
   下通过桌面与 WebKit；不先做大范围 UI 重构。
2. **边界与兼容**：完善目录／设备／登录隔离、受限文件代理、加载适配器、版本
   校验与准确错误。补循环模块、相对路径、并发和取消回归测试。
3. **界面与部署**：接入已有面板，验证消息更新不重建 frame；补配置迁移、所有
   安装模板、路由、回滚和未启用状态。保留 Markdown、GitHub 图片与原预览能力。
4. **完整验收后才发布**：完整仓库门禁 + Viewer 测试 + 真实源设备资源 + 物理
   手机外网验收。只更新代码但目标 Viewer 打不开，不算功能部署完成。

必须包含的发布测试：

- HTTP IPv4、HTTP IPv6、HTTP 域名、HTTPS 域名／已信任 IP；主站额外允许的私网
  IP 入口；不得靠测试拦截绕过真实 Origin、Cookie、CSP 或 TLS 校验。
- 两个源设备同名路径／同 localhost 别名不串号；不同账号、登录会话和两个
  预览 frame 不能互读未授权资源；伪造 null-origin 消息、旧端口和过期实例拒绝。
- 主站 API／Cookie／存储不可从沙箱访问；目录穿越、编码路径、符号链接／硬链接、
  被替换的根目录、撤销后的读取被拒绝。
- 真实页面所有模型文件完整加载，旋转／缩放／姿态／选择功能正常；CDN 失败明确
  提示；首屏、总耗时、流量与峰值内存有记录，不只有截图或 HTTP 200。
- 大资源与聊天同时运行、快速切换会话、连续刷新／关闭、设备断线、手机锁屏／
  恢复、BFCache、文件更新、登录过期和权限撤销。
- 发送新消息不导致预览变红、模糊或重载；历史耗时、生成图片、Markdown HTML、
  GitHub 图片、旧 HTML 预览与侧边聊天回归通过。

若最小纵向验证失败，应先调整适配范围或方案并说明依据，不得通过增加
`allow-same-origin`、关闭 CSP／鉴权，或强制用户申请域名来掩盖失败。
