# MediaForge

面向 Emby 影视库的本机管理工作台。第一版使用 Python 3.11+、FastAPI、SQLAlchemy、
PostgreSQL 和 Vue 3。Vue 构建产物随 Python 提供，**运行时不需要 Node**。

## 当前范围

| 模块 | 状态 |
| --- | --- |
| 登录与后台管理 | 登录页、仪表盘、退出、修改密码、管理员/只读权限、用户创建/停用/密码重置 |
| 文件管理 | 白名单目录浏览、文件重命名、同盘/跨盘移动、确认后永久删除、预览及操作记录 |
| Hosts 管理 | 多源 HTTPS DNS、普通 DNS、TLS 测速、部分更新、备份、定时更新、IPv4/IPv6 导出 |
| 操作日志与运行日志 | PostgreSQL 保存执行结果；文件日志按日期/大小轮转，页面读取和自动追踪 |
| 影视下载、元数据刮削 | 仅预留模块标识与管理菜单，**未实现下载或刮削** |

首版面向 Windows 本机，不包含公网部署、多实例调度、目录递归删除或批量整理。

## Windows 启动

在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

如果已有 `.env`，不要覆盖。编辑其中的 PostgreSQL 连接与媒体目录：

```dotenv
MEDIAFORGE_DATABASE_URL=postgresql+psycopg://mediaforge:CHANGE_ME@127.0.0.1:5432/mediaforge
MEDIAFORGE_MEDIA_ROOTS=["D:\\Media","E:\\Movies"]
MEDIAFORGE_HOSTS_PATH=.\data\hosts.preview
MEDIAFORGE_HOSTS_ALLOW_SYSTEM=false
MEDIAFORGE_HOSTS_INTERVAL_SECONDS=0
```

连接串中的密码若含 `@`、`#`、`/` 等字符，需要 URL 编码，例如 `@` 写成 `%40`。
数据库本身需已存在，账号需有连接、创建 schema 和创建表的权限。推荐使用专用数据库账号。
凭据只放 `.env`，该文件已被 Git 忽略。

```powershell
# 可单独执行；serve 启动时也会自动执行同一幂等初始化。
.\.venv\Scripts\mediaforge.exe init-db

# 交互输入密码，不回显；无默认账号密码。密码至少 12 位。
.\.venv\Scripts\mediaforge.exe create-admin --username admin

# 单进程启动，同时提供 API 和构建好的 Vue 页面。
.\.venv\Scripts\mediaforge.exe serve
```

浏览器打开 **http://127.0.0.1:8765**。可用 `serve --port 8766` 修改端口。
忘记密码时执行：

```powershell
.\.venv\Scripts\mediaforge.exe reset-password --username admin
```

重置密码会注销该用户全部旧会话。启动只监听 `127.0.0.1`，不会自动提权。

## 数据库初始化

只使用 `mediaforge` schema，包含：

- `users`：用户名、加盐 PBKDF2-SHA256 密码摘要、角色、启用状态。
- `login_sessions`：随机会话令牌的 SHA256 摘要及到期时间；浏览器使用 HttpOnly、SameSite=Strict cookie。
- `operations`：操作人、请求摘要、running/success/partial/failed 状态、结果及错误。

初始化使用事务及 PostgreSQL advisory lock，重复启动不会清空数据，也不会删除其他 schema 的业务表。
这是初版建表初始化，不是后续数据库结构迁移系统；模型变更需增加显式迁移，不能依赖
`create_all` 修改已有列。

配置保存在服务端 `.env`，用户、会话和操作记录存储于 PostgreSQL；
SQLite **仅用于离线单元测试**，应用运行不支持 SQLite。

## 权限

| 操作 | 管理员 | 只读用户 |
| --- | --- | --- |
| 查看仪表盘、媒体目录、系统信息和操作日志 | 是 | 是 |
| DNS 查询与 hosts 预览 | 是 | 是 |
| 重命名、移动、永久删除文件 | 是 | 否 |
| 写入 hosts 文件 | 是 | 否 |
| 创建用户、调整角色、停用账号、重置他人密码 | 是 | 否 |
| 修改自己的密码 | 是 | 是 |

不提供公开注册。管理员不能停用自己或移除自己的管理员权限，至少保留一个启用的管理员。
停用用户、变更角色、重置密码都会注销对应旧会话。登录失败限流为单客户端 5 分钟内最多
10 次失败；这是单进程的本机保护，不是公网防护方案。密码与会话令牌不会写入操作日志。

## 文件操作边界

- 只能访问 `MEDIAFORGE_MEDIA_ROOTS` 指定的已有目录，使用“目录编号 + 相对路径”。
- 拒绝路径越界、Windows 设备名、ADS、符号链接、目录联接和重解析点。
- 仅操作普通文件；不递归删除目录、不自动创建目标父目录、不覆盖已有目标。
- Windows 同盘移动使用不覆盖目标的 rename；跨盘移动使用独占创建、复制完成后删除源文件。
- 首先预览，再确认执行；永久删除必须输入完整文件名，**不进入系统回收站**。
- 不支持只修改大小写的重命名；请先改成一个不同的临时名称。
- 文件系统与 PostgreSQL 无法共享事务：执行前先持久化 running 意图，执行后写入结果。
  若断电或数据库在末次提交时断开，操作可能已生效但记录仍为 running，请核对磁盘后处理，
  不要自动重试。跨盘移动若中途被强制终止，也可能留下部分目标文件。
- 媒体根目录应由可信本机进程管理。路径校验和进程内锁不能防御其他进程恶意并发替换目录；
  操作大文件前应暂停下载器或其他会写入同一文件的程序。

## Hosts 动态更新

参考 [CheckTMDB](https://github.com/cnwikee/CheckTMDB)，迁入对本机更新有效的能力：
完整媒体域名清单（去重）、Google DoH、IPv4/IPv6 结果导出及 GitHub 域名支持。
没有迁入自动修改 README、GitHub Actions 提交推送，以及“测速全部失败仍选择首个 IP”的不安全回退。

1. 默认按 Google DoH → Cloudflare DoH → AliDNS DoH → 普通 DNS 的顺序查询 A/AAAA。
   每个地址族在查询失败、无记录或候选 IP 全部连接失败时切换来源；某一来源有可用 IP 后停止切换。
   HTTP 瞬时错误最多尝试 2 次，明确的 4xx（429 除外）不重复请求。
2. 拒绝非公网、特殊用途及错误地址族的候选，对 IP 的 443 端口执行 TLS 握手，并校验域名与证书。
   每个来源最多测 8 个去重候选，选取其中握手耗时最低的地址；并非扫描所有来源选全局最快 IP。
3. 不关闭证书校验、不照抄远程 hosts 文件。TLS 测速不等于验证具体业务 API，
   HTTPS 查询也不保证能绕过当前网络的连接限制。
4. 更新本轮成功映射；失败的“域名 + 地址族”保留原托管区旧映射（如有），标记“未验证”并告警。
   `both` 下 A 成功、AAAA 失败时仅替换 A，保留已有 AAAA。没有任何新地址通过校验则完全不写入。
   已从配置移除的域名或地址族不再保留；托管区外始终不主动修改。
5. 只替换 `# MediaForge Hosts BEGIN` 与 `# MediaForge Hosts END` 之间的映射，
   保留托管区外的原始字节和换行；托管区外若已有同名域名，拒绝写入，避免旧条目抢先解析。
6. 内容变化时先生成唯一 `.bak.*` 备份，再使用同目录临时文件原子替换。hosts 备份不随日志清理。
   管理页显示备份路径，恢复前请停止自动更新并人工核对备份，避免覆盖其他程序的修改。

配置示例：

```dotenv
MEDIAFORGE_DNS_SERVERS=["1.1.1.1","8.8.8.8"]
MEDIAFORGE_DNS_PROVIDERS=["google","cloudflare","alidns","dns"]
MEDIAFORGE_DNS_ATTEMPTS=2
MEDIAFORGE_HOSTS_INCLUDE_GITHUB=true
MEDIAFORGE_HOSTS_DOMAINS=["api.themoviedb.org","image.tmdb.org","api.thetvdb.com"]
MEDIAFORGE_IP_FAMILY=ipv4
MEDIAFORGE_HOSTS_INTERVAL_SECONDS=3600
MEDIAFORGE_NETWORK_TIMEOUT=3
MEDIAFORGE_PROBE_WORKERS=8
```

`IP_FAMILY` 可设为 `ipv4`、`ipv6`、`both`。更新间隔为 `0` 时关闭定时任务，否则最少
60 秒。服务存活时才会定时执行，启动后等待一个周期执行首次更新；不要启动多个 worker。

`HOSTS_DOMAINS` 不配置时使用 CheckTMDB 的完整去重媒体域名清单；设置后会替换该清单。
GitHub 常用域名默认额外加入，包括 `github.com`、`api.github.com`、`raw.githubusercontent.com`、
源码与发行包下载域名。它们同样经过 DNS 查询和 TLS 校验，**不是代理**，只帮助处理解析问题。
如不需要，设置 `MEDIAFORGE_HOSTS_INCLUDE_GITHUB=false`。

参考项目的 DNSChecker 网页接口在本次实际请求中返回 HTTP 404，未作为可用查询源迁入。
公共 DoH 服务也可能被当前网络限制；页面会保留各来源的超时、HTTP 状态和 TLS 失败原因，
不以跳过证书校验或选取未验证 IP 的方式伪装成功。

每次预览/更新都保存 PostgreSQL 操作记录，并在页面显示**成功、部分成功或失败**、查询来源、
TLS 延迟及失败原因。运行中的细节可在“运行日志”页面追踪，完成后在 Hosts 页面展示本次完整结果。
预览不会修改 hosts，但会写入执行日志。页面可分别下载本轮验证通过的 IPv4/IPv6 文本；
不导出未验证的旧映射，不在项目目录中自动生成多余文件。

先对 `hosts.preview` 预览与更新。确需修改 Windows 系统 hosts 时，使用管理员 PowerShell，
并显式设置：

```dotenv
MEDIAFORGE_HOSTS_PATH=C:\Windows\System32\drivers\etc\hosts
MEDIAFORGE_HOSTS_ALLOW_SYSTEM=true
```

若预览正常但系统仍用旧解析，可手动执行 `Clear-DnsClientCache`。应用不会自动刷新 DNS、
提权或更改系统服务。网络超时、DNS 污染、证书错误、无写权限都会明确报错，不降级为不安全连接。
不要给 hosts 配置媒体目录内的路径。

## 日志路径、轮转与追踪

在 `.env` 中配置，修改后重启：

```dotenv
MEDIAFORGE_LOG_PATH=.\logs\mediaforge.log
MEDIAFORGE_LOG_RETENTION_DAYS=7
MEDIAFORGE_LOG_MAX_TOTAL_MB=1024
MEDIAFORGE_LOG_MAX_FILE_MB=50
```

支持绝对路径，例如 `D:\MediaForgeLogs\mediaforge.log`；目录会自动创建，需要写权限，
不能放在可操作的媒体目录内，也不能与 hosts 使用同一路径。

- 当前文件达到 **50 MiB** 或跨 UTC 日期时轮转，文件名附加时间戳和唯一标识。
- 保留 **7 天**，按文件最后写入时间判断；当前及归档文件总量最多 **1024 MiB**，
  超限从最旧的归档开始清理。异常超长的单条日志会截断，以免突破单文件上限。
- 写入日志时执行保留策略，服务空闲时每 10 分钟检查一次；服务停止期间不执行清理。
- 仅清理同目录下、符合本项目严格命名规则的归档，不删除其他文件、当前活动文件或 hosts 备份。
- 后台“运行日志”可选择读取最近 100/200/1000 行，开启追踪后每 2 秒刷新并滚动到底部。
  离开页面或退出登录后停止追踪；轮转后自动读取新的活动文件。
- 运行日志包含启动、文件/用户操作状态、hosts 各查询源及测速结果。不会记录登录密码、
  数据库连接串或会话令牌。日志读取只支持指定的活动文件，要求登录，不接受任意文件路径。

文件日志的轮转策略**不清理 PostgreSQL 中的操作审计记录**。历史审计在“操作日志”页面查看。

## 前端构建与后续拆分

仓库包含 `src\mediaforge\web` 的构建产物，因此部署时只需安装 Python 依赖。
仅在修改 Vue 源码时需要 Node（Vite 7 要求 Node 20.19+ 或 22.12+）：

```powershell
Set-Location frontend
npm ci
npm run build
```

产物输出到 `src\mediaforge\web`，Python wheel 也会包含这些文件。开发时可用 `npm run dev`，
通过 Vite 的 `/api` 代理连接正在运行的 Python 服务。前后端通过 `/api` 通信，后续可拆分部署；
拆分时需要明确配置同源代理或 CORS、HTTPS 和 cookie 策略，而不是直接暴露当前本机服务。

主要目录：

```text
frontend\src\              Vue 登录页与管理后台
src\mediaforge\app.py      API、认证依赖、静态文件、生命周期
src\mediaforge\auth.py     密码、会话和用户管理
src\mediaforge\database.py PostgreSQL 模型、初始化、操作记录
src\mediaforge\files.py    受限路径和文件操作
src\mediaforge\hosts.py    DNS、TLS 探测、托管区更新、调度入口
src\mediaforge\logs.py     文件日志、日期/大小轮转、保留策略与尾部读取
src\mediaforge\cli.py      启动、初始化、管理员创建与密码重置
src\mediaforge\web\        已构建的前端产物
tests\                    针对性测试，全部使用临时文件
```

API 文档为 `/docs`。浏览器登录后的同源文档可携带 cookie；写接口还需
`X-MediaForge-Request: 1` 请求头。后端不开放 CORS，避免其他网站伪造写操作。
本机 HTTP 模式 cookie 默认不带 Secure；未来 HTTPS 部署需设置
`MEDIAFORGE_COOKIE_SECURE=true`。

## 开发检查

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
.\.venv\Scripts\pytest.exe -q
```

可选真实 PostgreSQL 测试，连接取自本地 `.env`：

```powershell
$env:MEDIAFORGE_RUN_PG_TESTS = "1"
.\.venv\Scripts\pytest.exe -q
Remove-Item Env:\MEDIAFORGE_RUN_PG_TESTS
```

真实数据库测试为每个用例创建随机 `mediaforge_test_*` schema，只清理这些临时 schema；
不会删除项目表或已有业务表。所有文件写入及删除测试只针对系统临时目录，不触碰真实媒体或系统 hosts。
