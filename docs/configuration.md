# 配置说明

在项目根目录的 `.env` 配置单行 `KEY=value`；支持单双引号、空值、注释与 `export` 前缀，不执行 shell 命令、不展开变量、不支持多行值。含空格的值须加引号。配置启动时读取，修改后重启生效；已有绑定配置应保留，勿用示例文件覆盖。CLI 凭证仍由相应 CLI 管理，不写入公开文档、示例或分享包。

## 身份与时间

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `BOT_NAME` | `AI 助手` | 机器人展示名及命令前的文字提及名称 |
| `TIMEZONE` | `Asia/Shanghai` | IANA 时区名称，统一用于日期理解、提醒、展示与统计 |
| `MESSAGE_SIGNATURE_TEMPLATE` | `AI 助手代发` | 只允许 `{bot_name}` 和 `{owner_name}` 占位符 |
| `GROUP_WATCH_ACK_TEXT` | `收到，我会跟进这条消息。` | 群监控默认确认正文，发送时添加一次配置署名 |
| `OWNER_PROFILE_PATH` | `.local/owner-profile.json` | 可选本地主人资料路径；相对项目根目录解析 |

主人资料允许缺省姓名、花名、别名和邮箱等字段；整个文件缺失也不阻止普通聊天。仅读取默认路径或 `OWNER_PROFILE_PATH` 指定的位置，可使用仓库中的虚构示例 `config/owner-profile.example.json` 准备本地资料。资料不扩大访问或发送权限；结构化身份仍由当前飞书绑定核实。署名的 `{owner_name}` 对资料去掉首尾空白后优先使用花名，其次姓名，无有效资料时使用通用称呼。格式化后的最终署名必须非空，校验失败时拒绝启动。

普通问答输入的 `now_local` 是带偏移的当地 ISO 当前时间，`timezone` 为 IANA 时区名称；自主迭代讨论也提供 `now_local`。相对时间据本轮值计算。时间输出使用当地实际偏移，不能给夏令时地区全年固定 `+08:00` 或其他偏移。每日、每周提醒按当地日历推进；既有时间戳任务保持已存储的执行时刻，不重新解释。新通知在审阅前保存完整正文；后续修改名称、资料或署名不重写已审阅通知及既有待发送正文。

## AI 后端与统一隔离

`AGENT_BACKEND` 取 `claudecode`、`codex` 或 `grok`；未填写默认 `codex`，显式空值和其他值报错。普通聊天、请求范围判断、群监控分类与定制回复、`/auto`、报告、自主开发讨论和改码都使用同一个后端。仅解析所选 CLI 的路径、模型和认证，不隐式要求另外两种工具。

| 后端 | CLI 路径 | 模型 |
| --- | --- | --- |
| Claude Code | `CLAUDECODE_BIN`，默认 `claude` | `CLAUDECODE_MODEL` |
| Codex | `CODEX_BIN`，默认 `codex` | `CODEX_MODEL` |
| Grok Build | `GROK_BIN`，默认 `grok` | `GROK_MODEL` |

空模型使用所选工具默认值。Claude 可从 `CLAUDECODE_SETTINGS_FILE`（缺省 `CLAUDE_CONFIG_DIR/settings.json`，未指定目录时为 `~/.claude/settings.json`）只读取认证／端点／模型白名单，不导入 hooks、插件或原生权限设置；项目配置优先于进程环境，再使用原生设置。模型优先采用 `CLAUDECODE_MODEL`，其次 `ANTHROPIC_MODEL`，最后原生 `model`。不会强制启用 `--bare`；用户仍使用自己的 CLI 登录或受支持的 API 认证。

所有 AI 进程经 `agent_isolation` 启动，需要 Linux 的 `bwrap` 和可用的用户／PID／网络命名空间。厂商沙箱显式关闭，不提供无隔离、厂商原生沙箱或嵌套回退；存在尚不支持的厂商受管配置时明确拒绝，避免隐藏管理策略绕过要求。

分类和 `/auto` 无任意工具；讨论源码只读；本地报告在 `.project/reports/report-<任务编号>/` 对应的隔离路径运行；已批准改码在脱敏源码副本中执行，再核对并发改动后应用源码变化。真实 `.env`、`.local/`、`.project/credential/`、自定义主人资料和无关用户目录不可见。输入文件以明确授权的副本提供。任务允许的临时写入不授予宿主其他目录写权限。

模型网络使用命名空间内代理，只能访问所选工具的模型／认证端点和显式增加的目标。`AGENT_NETWORK_ALLOWLIST` 为逗号分隔的精确 `host[:port]`，不支持通配符；自定义模型 BASE_URL 会加入所选后端的目标。忽略代理的客户端直接联网失败。GitLab、飞书和历史数据库由任务级服务入口访问，业务凭证不交给模型；明确合并唯一 MR 的请求可以构成本次授权，不重复确认。纯诊断和 `/auto` 不授予外部写入。

所选 CLI 的必要认证是明确例外：默认只读挂载 Codex 的 `.codex/auth.json`、Claude 的 `.claude/.credentials.json` 或 Grok 的 `.grok/auth.json`，不挂入整个用户配置目录；只传入所选后端必要的认证环境变量。Claude 的白名单原生认证环境可用于自定义模型端点。认证过期、刷新失败或模型不支持当前能力时会失败，不自动切换后端。

高级部署可用 `AGENT_AUTH_FILES` JSON 对象配置所选工具的原生认证文件位置，例如 `{"grok":{".grok/auth.json":"/absolute/private/auth.json"}}`；目标仅允许已支持的原生认证文件名。此设置只配置本地路径，不填写密钥内容。运行时根据所选可执行文件的位置推导，不支持任意目录挂载。

子进程设置文件描述符、进程数、单文件大小和 core 限制；进程数限制按宿主用户计算，并非独立 cgroup 配额，不声称提供总体 CPU 或内存配额。命名空间／挂载／代理建立失败则拒绝任务。超时或取消终止整组任务，模型步骤不会盲目自动重放；外部写入失败或结果不确定时先回读核实。

部署后运行 `./auto_run.sh check`：核验绑定应用、配置，并在统一隔离中启动所选 CLI 的 `--version`；此检查不验证模型登录或推理能力。`./auto_run.sh check --live` 另发起真实模型请求，使用临时图片验证图像和结构化输出；启用报告／自主开发时还验证临时报告生成、源码只读讨论和已授权的样例改写。探针会产生模型用量，不发送飞书消息、不修改真实业务仓库。`/auto on` 也先实际检查隔离启动；认证有效性仍以模型调用结果为准。

## 能力开关与依赖

下列开关默认均为 `false`，只按本次任务需要启用。

| 选项 | 依赖与边界 |
| --- | --- |
| `GITLAB_SKILL_ENABLED` | 启用 GitLab bot skill，需要 `GLAB_BIN`、显式 `GITLAB_HOST` 及相应登录与权限 |
| `ISSUE_SKILL_ENABLED` | 依赖 GitLab；启用 Issue 巡检、全量查询及目标监控，工作日判断不依赖模型 |
| `GROUP_WATCH_SKILL_ENABLED` | 需要主人身份可读目标群；分类与定制回复使用所选后端，发送另需用户身份权限 |
| `EVOLUTION_ENABLED` | 使用所选 AI 后端与统一隔离；开发、验证和重启按已批准方案进行 |
| `SKILLS_REPO_ENABLED` | 恢复 /skill 查询及普通任务使用仓库技能，需要 Git 和显式 SSH 地址，不依赖 GitLab 开关 |
| `REPORT_SKILL_ENABLED` | 三后端本地报告均独立于 GitLab 和发布开关，仅开放本次报告工作区和授权材料 |
| `REPORT_PUBLISH_ENABLED` | 线上报告另需报告功能、GitLab 和显式 SSH 发布目标；当前任务须明确要求发布 |
| `PIPELINE_MONITOR_ENABLED` | 依赖 GitLab，启用监控命令、采集和卡片，不要求报告发布目标 |
| `PIPELINE_PUBLISH_ENABLED` | 依赖监控和显式 SSH 发布目标；不绕过任务确认及站点变更审查 |

`/auto` 默认关闭，重启后保持关闭。`/auto on` 检查所选 CLI 与统一隔离依赖，以及已绑定用户身份和读取／发送权限；不再检查固定 Codex。其结构化推理没有任意文件或命令工具，GitLab 信息通过现有受控只读工具循环提供。启用功能不代表对任意外部操作的长期授权。

## 技能仓库

仅保留原开关及地址、分支配置，不提供内部仓库默认值：

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `SKILLS_REPO_ENABLED` | `false` | 开启 /skill 和普通任务中的候选正文加载 |
| `SKILLS_REPO_URL` | 空 | 启用时必填，支持 SSH 的 scp 与 ssh:// 形式 |
| `SKILLS_REPO_BRANCH` | 空 | 跟随远端默认分支；显式值只选择合法远端分支 |

仓库始终克隆到项目根目录 `.project/skills/`，缓存位于 `.local/skills/`；两者不随源码包导出。已有 origin 或当前分支不匹配、工作区有修改、更新不能快进时拒绝更新，保留现有文件。配置来源/分支变化不会沿用错误缓存；部署者须先处理已有工作区冲突。

首次下载先在临时目录中获取、不检出文件，通过提交树与快照校验后才检出并发布到固定目录；后续更新也在校验通过后才快进。校验失败不发布首次下载，不推进已有工作树或替换正常缓存。`.project/skills/` 是服务保留目录，GitLab 仓库准备不会占用或扫描复用它。显式配置的远端分支不存在时，`/skill` 会提示检查 `SKILLS_REPO_BRANCH`，不回退到默认分支。

保持 `skills/**/SKILL.md`、name/description 元数据和原检索规则。`/skill` 查询前同步并返回最多 5 项；普通任务读取当前快照并采用最多 3 个候选正文，实际调用沿用 `AGENT_BACKEND` 与当前任务权限。关联资料提供为只读临时输入，不向模型开放真实 `.local/`。使用情况沿用 `used_skills`，仅提供或阅读候选不算实际使用。

选中技能仅提供其所属资料；目录内另有 `SKILL.md` 时视为独立技能，不因父级或根级技能被选中而一并提供。明确的 Markdown 文件引用仍按同一快照解析。技能材料沿用共享隔离的受保护路径规则；包含 `.env`、`.local`、`.git`、`__pycache__`、`.venv`、`node_modules`、`.cache` 或凭证目录等内容时拒绝提供，不放宽挂载权限。旧快照不符合规则时，在启动 AI 前降级为技能暂不可用，普通任务继续处理。

后台按 `TIMEZONE` 每日 09:00 刷新，首次没有缓存时同步；当天 09:00 前的查询同步不替代这次刷新。后台失败保留上一快照；查询本身同步失败则本次不查找，普通任务检索失败时如实说明并继续原任务。没有新增同步周期、目录配置或查询子命令。

## Issue 巡检

| 选项 | 默认值 | 规则 |
| --- | --- | --- |
| `WORKDAY_MODE` | `weekdays` | `weekdays`、`everyday` 或 `calendar_file` |
| `WORKDAY_CALENDAR_FILE` | 空 | 仅 `calendar_file` 模式要求，路径相对项目根目录解析 |
| `ISSUE_SUMMARY_TIME` | `09:00` | 当地每日全量汇总时刻 |
| `ISSUE_WATCH_START_TIME` | `09:00` | 当地增量巡检开始时刻 |
| `ISSUE_WATCH_END_TIME` | `21:00` | 当地巡检结束时刻 |
| `ISSUE_WATCH_INTERVAL_SECONDS` | `600` | 正数秒级周期，不要求整分钟 |

所有时刻为 `HH:MM`，须满足 `start <= summary <= end`，不支持跨夜。每日汇总成功查询后清池，成功投递后重建池；之前的巡检只查询增量。重启只补当前一次，不重放错过的周期；手动 `/issue` 不改变每日汇总安排。

`weekdays` 只排除周六周日，`everyday` 每天执行，均不读取日历文件。`calendar_file` 中 `workdays` 优先表示额外上班日期，`holidays` 表示额外休息日期，其余日期按周一至周五判断。年度 JSON 格式如下：

```json
{"year": 2030, "holidays": ["2030-01-01"], "workdays": []}
```

数组可为空；日期须为同年度有效的 `YYYY-MM-DD`，两数组不能冲突。缺失文件、错误年份或无效内容明确报错；不要求来源链接、不联网核验、不调用模型，也不回退到旧联网日历缓存。

## 报告目标

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `REPORT_PAGES_REPO_URL` | 空 | 通用线上报告仓库，必须显式提供 SSH 地址 |
| `REPORT_PAGES_BRANCH` | `main` | 通用报告发布分支 |
| `PIPELINE_PAGES_REPO_URL` | 空 | Pipeline 长期报告仓库，必须显式提供 SSH 地址 |
| `PIPELINE_PAGES_BRANCH` | `main` | Pipeline 报告发布分支 |

支持 `git@host:namespace/repo.git` 与 `ssh://git@host[:port]/namespace/repo.git`。仓库按根目录规则保存在 `.project/<仓库名>/`，相同仓库优先核对并复用已有工作区。通用报告 bot skill 使用服务本轮提供的 `REPORT_PAGES_REPO_URL`、`REPORT_PAGES_BRANCH`、`REPORT_REPOSITORY_DIR`，不写死仓库或分支。

`credential`、`pic`、`reports`、`files` 是 `.project/` 下的服务保留目录。同名项目在准备仓库时明确报目录冲突，目录不存在也不会克隆，已有目录也不会作为仓库复用；不会自动改名或改用多层目录。

本地报告存于 `.project/reports/`，不要求上述地址。Pipeline 关闭发布时可无目标正常采集与发卡片；开启发布时必须显式配置。目标变化不自动迁移已有发布状态，也不删除旧数据。旧发布历史缺少 `pages_target` 绑定元数据时，须先核实原仓库并迁移绑定；不会根据新目标或历史默认值猜测归属。Pipeline 的统计口径、不可变发布快照、不确定投递与站点审查详见 [监控说明](pipeline-monitor.md)。

真实 `.env`、主人资料、CLI 凭证、`.local/` 运行数据和 `.project/` 工作区均不随分享包分发。公开源码使用 [README](../README.md) 中的白名单导出入口，历史讨论和 Git 历史不属于分享包。
