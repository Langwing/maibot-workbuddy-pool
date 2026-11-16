# WorkBuddy 账号池接入（local.workbuddy-pool）

把 WorkBuddy（国内版 / 国际版）的模型接入 [MaiBot](https://github.com/Mai-with-u/MaiBot) 的 LLM Provider 体系。
支持从 [dsh-connect-workbuddy](https://github.com/dingminhua/dsh-connect-workbuddy) 导出的账号文件批量导入，
插件内部完成 token 刷新、多账号轮换与失败换号，不占用官方 API key。

> 非腾讯官方项目，与 WorkBuddy 官方无任何关联。上游接口与配额策略随时可能变化，
> 每次调用消耗的是**你自己账号**的额度，请自行评估账号风险。

---

## 功能

- **多账号池**：导入多个 WorkBuddy 账号，当前账号优先，其余按最久未用排序
- **自动刷新**：401 时用 refresh_token 单飞刷新，成功后重发原请求；刷新失败才标记账号需重新登录
- **失败换号**：401 / 403 / 429 / 5xx / 流异常时冷却当前账号并换下一个；排队码（6020/6021/6022）不换号
- **请求预处理**：按上游实测规则改写 `developer` 角色、补 system 头、处理渠道指纹（11128）、压平 `tool_choice`，并自动归一化工具 schema（tuple `items`、布尔 `exclusiveMinimum`、字符串/重复 `required`、外部 `$ref`、错误类型——对应拒收码 11129 / 11133）
- **会话亲和（可选）**：`stable_conversation_id` 默认关闭；开启后把 `X-Conversation-ID` 固定为该账号的派生值，便于上游前缀缓存命中（单变量 A/B 开关）
- **多区域**：按账号域名自动区分国内版与国际版，各走自己的网关，互不串号
- **模型目录**：读取上游模型清单（名称、上下文、输出上限、积分倍率、图片/思考能力、促销标签），带本地缓存
- **获取模型列表**：插件内起 `127.0.0.1` 的 OpenAI 兼容 `/v1/models`，修复 MaiBot「获取AI模型」报 502
- **模型开关**：图片能力由你手动勾选（上游 `supportsImages` 是平台声明，纯文本模型也常为 true，不参与判断）；思考档位（关/低/中/高/超高/最大）真实调用时生效
- **积分总览**：只读查询每账号可用积分与企业积分，不签到、不领取
- **诊断**：检查账号池、本地模型服务、目录缓存与 `model_config.toml` 对齐情况
- **设备身份**：按账号稳定派生 `X-Machine-ID` / `X-Session-ID`，可在页面里逐账号覆盖
- **批量测试**：按目录逐个发起一次真实调用，页面轮询结果（后台任务，不阻塞）
- **模型同步**：按目录对齐 `model_config.toml` 中本插件 provider 名下的模型——新增缺失、更新视觉能力、移除已下线；其他提供商的模型不动；写前备份、页面二次确认
- **命令管理**：`/wb` 查看、导入、切换、登录、删除账号
- **加密存储**：账号与 token 使用 AES-256-GCM 加密保存在插件数据目录

> 明确不实现：每日签到、福利/补偿领取，以及任何会改变账号权益的写操作。

---

## 安装

插件目录规范（MaiBot 1.3.x / SDK 2.12.x）：

```
plugins/
  local_workbuddy-pool/     # 文件夹名 = 插件 id 的首个点换成下划线
    _manifest.json
    plugin.py               # create_plugin() 入口
    config.toml             # 插件配置
    webui.json              # WebUI 页面声明（侧边栏入口）
    webui/index.html        # WebUI 自定义页面
    workbuddy/              # 核心实现
```

把整个目录放到实例的 `plugins/` 下（keke 为 `/home/maibot/keke/plugins`），重启：

```bash
docker restart maim-bot-core
```

启动日志出现下面两行即加载成功（账号数为 0 时会有提示）：

```
[_maibot_plugin_local_workbuddy_pool] WorkBuddy 账号池插件已加载：client_type=local.workbuddy-pool 账号数=0
```

依赖：`aiohttp`、`cryptography`、`tomlkit`，MaiBot 镜像内已自带，manifest 中也已声明。

---

## 导入账号

插件支持 dsh-connect-workbuddy 的导出文件：一个 JSON 数组，每项字段为
`access_token` / `refresh_token` / `uid` / `nickname` / `domain` /
`expiresAt`（毫秒）/ `refreshExpiresAt` / `auth_raw`。也接受
`{"accounts": [...]}`、`{"data": [...]}`、`{"records": [...]}` 包装。

导入规则：

- 按 `uin ?? uid ?? nickname` 生成账号 id，同 id 覆盖 token（重复导入是幂等的）
- 缺 `access_token` 的记录跳过
- `domain` 必须能识别（`workbuddy.cn` / `codebuddy.cn` / `copilot.tencent.com` 为国内版，
  `workbuddy.ai` / `codebuddy.ai` 为国际版），无法识别的记录跳过并在结果中说明
- `auth_raw.account.uin` 会被恢复，保证「导出 → 导入」收敛到同一账号

### 方式一：默认文件 + 命令

宿主机路径与容器路径的对应关系：`<实例>/plugin-data/<插件 id>/` ↔ `/MaiMBot/data/plugins/<插件 id>/`。

```bash
cp <导出的账号文件>.json \
   /home/maibot/keke/plugin-data/local.workbuddy-pool/accounts-import.json
```

然后在 QQ 中发送（需要本地操作员或已授权的命令权限）：

```
/wb 导入
```

### 方式二：指定路径

```
/wb 导入 accounts-2026-11-13.json
```

相对路径按插件数据目录解析；也可以传容器内可读的绝对路径。

### 方式三：启动时自动导入

账号池为空且默认导入文件存在时，插件在加载时会自动导入一次。导入成功后不会再覆盖，
后续请用 `/wb 导入` 更新 token。

### 方式四：扫码/浏览器登录新增账号

```
/wb 登录
```

按提示在浏览器完成授权，登录成功后自动加入账号池。

> **不要导入脱敏后的示例文件**：其中的 `***` 会被当成真实 token 存进账号池，
> 调用时只会得到 401。请使用包含真实凭据的导出文件。

---

## 配置模型

在 `config/model_config.toml` 增加 provider 与模型（或在 WebUI 的「模型」页点「同步模型」自动生成）：

```toml
[[api_providers]]
name = "WorkBuddy"
# 指向插件内置的本地模型列表服务；真正的聊天地址由插件按账号区域选择，
# 这个 base_url 只用于 WebUI 的「获取AI模型」。
base_url = "http://127.0.0.1:8765/v1"
api_key = "workbuddy-plugin"                # 占位，鉴权由插件内部完成
client_type = "local.workbuddy-pool"
model_list_endpoint = "/models"
max_retry = 1
timeout = 180
retry_interval = 3

[[models]]
model_identifier = "deepseek-v4.1-flash"   # WorkBuddy 侧的模型 ID
model_type = "llm"
name = "WorkBuddy Deepseek-V4.1-Flash"      # 在 model_task_config 里引用的名字
api_provider = "WorkBuddy"
price_in = 0
price_out = 0
cache_price_in = 0
price_periods = []
send_temperature = true
visual = false
force_stream_mode = true
```

> `base_url` 与 `model_list_endpoint` 是修复「获取AI模型」返回 502 的关键：
> 上游没有 OpenAI 形状的 `/models`，插件用目录缓存自己提供。
> 如果改过 `[models_server].port`，这里的 `base_url` 也要一起改（「同步模型」会自动对齐）。

把某个任务指向它，例如：

```toml
[model_task_config.utils]
model_list = ["workbuddy-flash", "deepseek-flash-planner"]
selection_strategy = "sequential"
```

可用模型 ID 由 WorkBuddy 决定，会随账号与上游策略变化；常见如 `deepseek-v4.1-flash`、
`glm-5.3`、`kimi-k3-1` 等。换模型只需改 `model_identifier`，在 WebUI 模型页即可测试。

> 不建议填 `auto`：它可能被路由到思考模型，`max_tokens` 给小了正文会被推理内容占满。建议直接写具体模型 ID。

---

## 命令

所有命令都需要 operator 权限（本地操作员或在命令权限里单独授权）。

| 命令 | 说明 |
| --- | --- |
| `/wb` 或 `/wb 帮助` | 显示命令列表 |
| `/wb 列表` | 查看账号：序号、昵称、区域、脱敏 uid、状态、冷却 |
| `/wb 状态` | 查看当前账号、默认模型、换号次数、并发/RPM、国内网关 |
| `/wb 导入 [路径]` | 导入导出文件 |
| `/wb 切换 <序号\|id\|昵称>` | 指定当前账号 |
| `/wb 登录` | 浏览器 OAuth 新增账号 |
| `/wb 删除 <序号\|id>` | 从账号池移除账号 |

---

## WebUI 页面

插件通过 `webui.json` 向 MaiBot WebUI 注册一个侧边栏页面：**插件扩展 → WorkBuddy 账号池**。
页面和 `/wb` 命令操作的是同一份账号池与凭据，改完即时生效。

页面包含五个标签页：

- **账号**：概览、账号池表格（设为当前 / 刷新 token / 停用 / 移除）、导入账号、扫码登录
- **模型**：模型目录（名称、上下文、倍率、促销）、逐模型「图片」勾选与「思考」档位、单个/批量测试、刷新目录
- **积分**：只读读取各账号可用积分与积分包明细（不含签到/领取）
- **诊断**：账号池、本地模型服务、目录缓存、`model_config.toml` 对齐检查
- **设置**：只读运行配置、每账号设备身份（`X-Machine-ID` / `X-Session-ID`）的查看与覆盖、重置设备盐

模型页底部是同步计划：列出待追加的模型和 provider 字段差异，「同步模型」会二次确认后执行，
写前备份到 `<数据目录>/backups/model_config-<时间>.toml`。同步只作用于本插件 provider
（默认 `WorkBuddy`）名下的模型：目录新增则追加，页面手动勾选的图片能力变化则更新 `visual`，
目录已下线的模型从 provider 名下移除；其他提供商的模型不会被读写。
若目录抓取时有区域失败（目录可能不完整），本次同步会跳过删除，避免误删。

> 页面里的「移除」只删除本地加密凭据，不影响 WorkBuddy 账号本身；移除前 WebUI 会二次确认。
> 若侧边栏没有出现该页面，重启容器后再看：`docker restart maim-bot-core`。

---

配置文件位于插件目录 `plugins/local_workbuddy-pool/config.toml`，
修改后可由 FileWatcher 热重载，也可在 WebUI 的插件配置页编辑。

### `[plugin]`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用 Provider；关闭后所有 WorkBuddy 请求直接报错 |
| `config_version` | `"1.0.0"` | 配置版本，迁移时使用 |

### `[pool]`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `default_model` | `"deepseek-v4.1-flash"` | 请求未指定模型时使用的 WorkBuddy 模型 ID |
| `max_attempts` | `3` | 一次调用最多尝试几个账号（含当前账号）；账号池小于该值时按池大小 |
| `cooldown_seconds` | `120` | 账号失败后的冷却秒数；429 优先使用上游 `Retry-After` |
| `concurrency` | `1` | 同时在途的聊天请求上限，账号池共用一个闸门 |
| `rpm` | `6` | 每分钟真实上游请求上限，`0` 表示不限制 |
| `import_path` | `""` | 默认导入文件；留空使用数据目录下的 `accounts-import.json` |
| `stable_conversation_id` | `false` | 实验开关：把 `X-Conversation-ID` 固定为该账号的派生值，便于上游前缀缓存命中 |

### `[network]`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `cn_chat_base` | `"https://copilot.tencent.com"` | 国内版聊天/刷新网关；被网络拦截时可改为 `https://www.codebuddy.cn` |
| `global_chat_base` | `"https://www.workbuddy.ai"` | 国际版网关；`codebuddy.ai` 账号会自动改用 `https://www.codebuddy.ai` |
| `request_timeout` | `180` | 单次上游读取超时（秒），范围 30–600 |

### `[catalog]`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否抓取并缓存模型目录；关闭后不启动本地 `/v1/models` |
| `ttl_seconds` | `900` | 目录缓存有效期（秒），过期后按需刷新 |
| `refresh_on_start` | `true` | 插件加载后自动刷新一次目录 |

### `[models_server]`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `auto_start` | `true` | 是否启动本地 OpenAI 兼容 `/v1/models` 服务 |
| `host` | `"127.0.0.1"` | 监听地址，只建议回环地址 |
| `port` | `8765` | 监听端口；改动后需同步 `model_config.toml` 的 `base_url` |

### `[sync]`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `provider_name` | `"WorkBuddy"` | 写入/对齐的 `model_config.toml` 提供商名称 |
| `model_config_path` | `""` | 配置文件路径；留空时从数据目录向上推断（容器内通常是 `/MaiMBot/config/model_config.toml`） |

---

## 凭据与安全

- 账号池：`<实例>/plugin-data/local.workbuddy-pool/accounts.enc`（AES-256-GCM，0600）
- 主密钥：同目录 `.workbuddy_master_key`（0600），或在环境变量 `WORKBUDDY_MASTER_KEY`
  指定（标准 base64、解码后恰好 32 字节）
- **换机或重建容器时，`accounts.enc` 与 `.workbuddy_master_key` 必须一起迁移**，否则无法解密
- 命令回复只显示脱敏 uid；token 不会写入日志或错误信息

---

## 工作原理

```
MaiBot Host
  └─ @LLMProvider("local.workbuddy-pool")
       └─ WorkBuddyProvider   context_items → OpenAI messages（含思考档位）
            └─ WorkBuddyService 选号 / 换号 / 闸门
                 └─ AccountPool  账号持久化、刷新、冷却、设备身份
                      └─ /v2/chat/completions（按账号区域选择网关）

插件本地
  ├─ CatalogService    /v3/config → 模型目录缓存（catalog.json）
  └─ ModelsServer      127.0.0.1:<port>/v1/models ← MaiBot「获取AI模型」
```

- 上游只接受流式请求，插件内部固定 `stream: true`，聚合后一次性返回给 Host
- 401 → 刷新 token 并重发一次；刷新失败或其它错误按配置换号
- 业务码 11128（渠道指纹）会自动加中性 system 头重试一次
- 排队码不换号：队列跟随模型/平台，换账号位次不变

---

## 故障排查

| 现象 | 排查 |
| --- | --- |
| `/wb` 无响应 | 确认发送者是本地操作员；WebUI「插件」页确认插件已启用 |
| 账号显示「需重新登录/导入」 | refresh_token 已失效，重新导出并 `/wb 导入`，或 `/wb 登录` |
| 账号显示「冷却 Ns」 | 刚遇到 429/5xx，等待冷却或 `/wb 切换` 到其它账号 |
| 一直报 401 | 账号 token 与网关区域不匹配，检查导出文件里的 `domain` |
| 国内版连不上 | 把 `network.cn_chat_base` 改成 `https://www.codebuddy.cn` |
| 回复为空 | `max_tokens` 太小，被推理内容占满；换具体模型 ID 或调大上限 |
| 想确认插件是否加载 | 日志搜索 `local_workbuddy_pool`；WebUI 模型页应出现 `local.workbuddy-pool` |
| 「获取AI模型」报 502 / 返回前端页面 | 确认插件已加载、`[models_server].auto_start=true`；容器内访问 `http://127.0.0.1:8765/v1/models` 应有 JSON；模型页点「同步模型」对齐 `base_url` |
| 模型目录为空 | 看诊断页的目录错误；确认账号未过期、国内网关可达；点「刷新目录」 |
| 同步后模型页没有新条目 | MaiBot 需重新读取 `model_config.toml`，刷新页面或重启容器 |
| 端口 8765 被占用 | 改 `[models_server].port` 后点「同步模型」自动改 `base_url` |

---

## 已知限制

- 只做 Chat Completions，不提供 embedding / 语音转写
- 流式在上游内部完成，插件只把最终聚合结果交给 Host
- 不做每日签到、福利/补偿领取等任何会改变账号权益的写操作
- 积分、队列只做只读查询；模型测试会真实消耗账号额度
- 上游模型清单、倍率、配额与区域策略由 WorkBuddy 决定，随时可能变化

---

## 致谢

- [dsh-connect-workbuddy](https://github.com/dingminhua/dsh-connect-workbuddy)：导出格式、网关约定、请求预处理规则
- [MaiBot-WorkBuddy](https://github.com/beichen24a1/MaiBot-WorkBuddy)：Provider 与 SSE 解析的基础实现

License: MIT
