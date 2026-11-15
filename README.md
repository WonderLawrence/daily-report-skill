# daily-report

自用的日报/周报 skill：以 **GitHub PR** 为索引采集 **本期本人提交**，加上 **Claude Code / Codex / DSH（DeepSeek Harness）会话**，结合用户补充文档产出中文日报。
提交证据走两层遍历——先搜本期参与的 PR，再在 PR 内筛出本人、落在区间内的提交，最后回本地仓库补齐改动文件；确认工作事项后再读取 PR Description，结合 commit message 分析本期贡献。

```
daily-report/
├── SKILL.md                    # 执行入口：路由、主流程、关键约束和交付
├── references/
│   ├── collection.md           # 作者/项目/日期、采集操作和证据解释
│   ├── interaction.md          # 候选筛选、用户确认和跨轮状态
│   └── writing.md              # 贡献归纳、表达、模板和审稿
├── examples/                   # 虚构的日报、周报示例
├── scripts/
│   ├── collect.py              # 采集 CLI：参数定义、渲染与落盘
│   ├── pr_details.py           # 已选 PR 详情 CLI
│   ├── read_evidence.py        # 回执定位、按对象和字段分页读取
│   ├── sync_install.py         # 安装差异检查、备份与同步
│   ├── config.example.json     # 配置样例
│   └── daily_report/           # CLI 共用的实现模块
│       ├── settings.py         # 参数与配置合成运行设置
│       ├── pipeline.py         # 采集编排：Git → PR → 会话 → 工作区状态
│       ├── config.py           # 默认值、配置加载与校验
│       ├── models.py           # Commit / Session / Event / Identity 等记录
│       ├── diagnostics.py      # 采集诊断字段的唯一定义
│       ├── evidence_reader.py  # JSON 读取、字段校验与输出预算
│       ├── gitlog.py           # 仓库发现、fetch、提交与未提交改动
│       ├── sessions.py         # 会话扫描、解析与汇总
│       ├── session_events.py   # 三种 agent 记录归一成事件
│       ├── analysis.py         # 副本折叠、回退配对、关联线索、摘要额度
│       ├── render.py           # Markdown 证据包渲染
│       ├── evidence.py         # 脱敏、序列化与快照存储
│       ├── github.py           # 只读 GitHub API 与本地提交信息
│       ├── pr_report.py        # 已选 PR 的快照校验、读取与输出
│       ├── text.py             # 正文与标识符的文本处理
│       └── timewindow.py       # 采集区间 Period 与时间戳解析
└── tests/
    ├── helpers.py              # 共用的合成仓库、会话和 CLI 工具
    ├── test_git.py             # 提交读取、作者匹配、去重与回退、工作区状态
    ├── test_sessions.py        # 三种 agent 的会话发现与解析
    ├── test_prs.py             # PR 两层遍历与已选 PR 详情
    ├── test_fetch.py           # 远程同步开关与失败降级（本地裸仓库）
    ├── test_evidence.py        # 覆盖诊断、来源可信度与快照
    ├── test_reader.py          # 回执定位、字段错误、分页与正文无损读取
    ├── test_render.py          # 证据包渲染与记录形状
    ├── test_config.py          # 配置校验与采集区间
    ├── test_cli.py             # 命令行行为：参数错误、交付、脱敏、保留
    ├── test_scope.py           # 严格项目范围
    ├── test_install.py         # 安装同步、备份与失败恢复
    ├── REPORT_ACCEPTANCE.md    # 写稿行为验收方法
    ├── fixtures/               # 固定输入及验收判据
    └── results/                # 实际行为验收记录
```

## 作为 DSH 插件安装

仓库本身是一个 DSH bundle（`package.json` 的 `dsh.bundle` + `cordis.patch.yml` + `lib/index.js`），
可以在 DSH 的 **Plugins → Discover** 里安装，不必手动复制目录：

1. 在 Plugins 页面打开 **Custom plugin indexes**（实验特性）。
2. **Discover → Index sources** 添加索引地址：

   ```
   https://raw.githubusercontent.com/WonderLawrence/daily-report-skill/main/market/index.json
   ```

3. 打开 **Daily Report** 条目并安装。装好后插件在 profile 里启停，`daily-report` skill 随即出现在 skill 目录中；
   同名的本地副本（项目 `.agents/skills`、`~/.agents/skills`）优先级更高，仍然生效。

索引文件是 `market/index.json`（plugin market index format 1），每个版本用 Git commit 钉住，
内容变更后需要把新 commit 更新进索引。插件入口 `lib/index.js` 注册同名 skill provider，
`resourceBase` 指向包根目录，所以 `references/`、`examples/`、`scripts/` 的相对路径与手动安装一致。
配置、采集和写稿方式不变，见下文。

## 安装

```bash
# 1. 把整个目录放到全局 skill 目录
cp -R daily-report ~/.agents/skills/

# 2. 创建默认配置
mkdir -p ~/.config/daily-report
cp ~/.agents/skills/daily-report/scripts/config.example.json ~/.config/daily-report/config.json
export DAILY_REPORT_CONFIG="$HOME/.config/daily-report/config.json"

# 3. 先检查身份，再填写 author_emails
python3 ~/.agents/skills/daily-report/scripts/collect.py --whoami
$EDITOR ~/.config/daily-report/config.json

# 4. 验证采集
python3 ~/.agents/skills/daily-report/scripts/collect.py --date 2026-10-16
```

配置路径按 `DAILY_REPORT_CONFIG`、`~/.config/daily-report/config.json`、skill 所在工作区旁的
`reports/daily-report.config.json` 顺序解析。其他位置请设置 `DAILY_REPORT_CONFIG` 或传 `--config <路径>`。
找不到配置时脚本会告警并使用内置默认值；生成日报前应确认采集范围符合预期。

看到 `[ok] N commits, M sessions -> <路径>` 就成了。

更新工作区或远程仓库不会自动更新以前复制到 `~/.agents/skills/daily-report` 的安装副本。
在本工作区执行以下命令检查并同步（也可用 `--dest <路径>` 指定安装目录）：

```bash
python3 scripts/sync_install.py --check
python3 scripts/sync_install.py --apply
```

`--check` 只读比较文件内容，一致时退出码为 0，有差异为 1。
`--apply` 复制并核验完整新版，将旧安装目录（含本地定制）移到同级 `.daily-report-backups/<时间戳>`，再替换安装目录；发布失败会恢复旧目录。
安装包只包含入口、README、忽略规则、references、examples、scripts 和 tests，排除 Python 缓存及 Git 元数据。安装后 `.install-manifest.json` 保存来源和每个文件的 SHA-256。
测试时先核对实际读取的 `SKILL.md` 路径；也可直接指定本工作区的绝对路径。
若会话已经读取过旧版，需让 agent 重新读取指定文件；不能以工作区已修改推断当前会话已加载新规则。

## 身份配置

```bash
python3 ~/.agents/skills/daily-report/scripts/collect.py --whoami
```

输出 git 全局身份、配置里的邮箱、以及每个仓库近 90 天的作者排行（命中配置的标 `<- 匹配`）。
**只把自己的邮箱写进 `author_emails`**：一个 PR 里可能混着多人的提交，写错会把别人的活儿算成你的。
邮箱用于第二层筛选（自动模式下优先用 GitHub login，再按邮箱匹配）和本地未关联提交的计数。
配置邮箱和回退使用的 Git 全局邮箱都按完整邮箱匹配（忽略大小写）；`+`、`.` 等字符按字面值处理。
需要按作者姓名或正则筛选时，可显式传 `--author '<正则>'`，覆盖邮箱配置，并禁用 login 匹配兜底。它只改变 PR 内及本地提交的作者筛选，不改变搜索 PR 所用的登录账号；`--any-author` 则取消作者筛选。

脚本另有一道保险：出现「和你 `user.name` 同名、但邮箱不在配置里」的提交时打 `[warn]`，
这才是"漏配身份"的真实信号；别人的提交不会报警。

## 用法

```bash
S=~/.agents/skills/daily-report/scripts/collect.py
python3 $S                      # 今天
python3 $S --date 2026-10-16    # 指定结束日；单日
python3 $S --days 7             # 最近 7 天（含今天）
python3 $S --date yesterday     # 昨天（也可写 today）
python3 $S --date 2026-10-18 --days 7  # 截至指定日期的 7 天
python3 $S --week               # 本周（周一至今天）
python3 $S --last-week          # 上周（周一至周日）
python3 $S --print              # 打 stdout，不落盘
python3 $S --json > /tmp/collection-run.json # 保存证据并返回路径、计数回执
python3 $S --overwrite          # 允许覆盖已有证据文件，覆盖前保留旧证据快照
python3 $S --any-author         # 不过滤作者（团队仓库慎用）
python3 $S --no-sessions        # 只看提交
python3 $S --target-project ~/Project/acme-node # 只采集该仓库及其项目会话
python3 $S --target-project ~/Project/acme-node --target-project ~/Project/acme-sdk
python3 $S --repo ~/other/repo  # 追加仓库，仍扫描默认根目录
python3 $S --no-prs             # 不查询 GitHub PR
python3 $S --no-fetch           # 离线采集，不更新远程跟踪引用
python3 $S --fetch              # 显式开启 fetch，覆盖配置中的 fetch: false
python3 $S --fetch-timeout 60   # 每次 Git 同步命令最多等待 60 秒
python3 $S --no-redact          # 关闭证据包脱敏（默认开启）
python3 $S --keep 10            # 最新 Markdown 视图只留最近 10 份；不删除历史快照
python3 $S --max-sessions 10    # 展开 10 段详细摘要；完整索引和 JSON 不截断
```

然后在 Claude Code / Codex / DSH 里说「写今天的日报」，agent 从 [SKILL.md](SKILL.md) 按请求进入相应阶段。
采集与证据解释见 [collection.md](references/collection.md)，确认与续写规则见 [interaction.md](references/interaction.md)，报告表达见 [writing.md](references/writing.md)。

每次运行都会重新采集；`--overwrite` 只允许覆盖已有输出文件，不改变采集范围或合并新旧证据。
`collect.py` 和 `pr_details.py` 均保留旧参数 `--force` 作为兼容别名。

严格范围使用 `--target-project`，CLI 优先于 `target_projects` 配置；`--repo` 只追加范围，与前者及非空 `target_projects` 配置互斥。
独立 worktree 需要分别指定。范围外或项目无法确认的会话不进入证据列表，诊断保留计数。
`target_projects` 填关注的项目工作目录，同时限制 Git/PR 和会话的项目范围；不要填会话日志目录。日志仍从 `~/.codex/sessions`、`~/.claude/projects`、`~/.dsh/sessions` 读取。
填写后按项目列表采集；留空 `[]` 时使用默认发现范围。原配置项 `only_repos` 与参数 `--only-repo` 已更名为 `target_projects` 与 `--target-project`，不保留旧名称别名；旧配置会明确报错，需将原项目列表移到新字段并移除旧字段。
启用 Git 时每项必须是仓库根目录；禁用 Git 后才允许用非 Git 工作目录筛选会话。目前不支持在启用 Git 的同时额外加入非 Git 会话工作区。

## Git 同步

采集前默认执行 `git fetch --all --no-recurse-submodules`。`fetch` 默认 `true`，`fetch_timeout_seconds` 默认 `30`；CLI 的 `--fetch` / `--no-fetch`、`--fetch-timeout` 优先。
`--no-fetch` 用于离线采集；`--print` 仅改变输出方式，仍会执行启用的网络采集。
`--whoami`、禁用 Git 或无远程时不 fetch。同步失败会保存可得的本地证据并输出诊断；详细含义见 [同步与覆盖](references/collection.md#git-证据pr-两层遍历)。

## GitHub PR 证据

**提交证据依赖已安装且已登录的 `gh`**：第一层用 `search/issues` 按登录账号、起始时间和已限定的仓库分页搜索，第二层读每个 PR 的
[提交列表](https://docs.github.com/en/rest/pulls/pulls#list-commits-on-a-pull-request)，按作者条件和 committer date 过滤。分页、平台上限与覆盖缺口见 [两层遍历](references/collection.md#git-证据pr-两层遍历)。
本地仓库通过 github.com 的 HTTPS/SSH 远程映射到 `owner/repo`，用于按 SHA 解析改动文件、行数，以及 `--target-project` 限定范围。

| 配置 | 默认值 | 用途 |
|---|---|---|
| `github_prs` | `true` | 发现提交关联 PR；`--prs` / `--no-prs` 可覆盖。 |
| `github_timeout_seconds` | `30` | 单次 GitHub 命令超时秒数。 |
| `github_max_queries` | `200` | 搜索每页、每个 PR 的提交列表各计一次查询；账号查询不计入，提交列表内部自动分页。额度不足时保留候选并标记缺口。 |

`--no-fetch` 或 `fetch: false` 同时禁用 PR 搜索（`--prs` 不覆盖离线设置），此时没有 Git 证据，只剩会话。
`involves:` 不含提交作者，所以「他人创建、含本人提交、本人从未留言」的 PR 查不到；未在本次 PR 检索结果中找到关联的本地提交只在诊断里留一行计数。
初次采集保存 PR 元数据；选中后用 `pr_details.py --evidence <快照/evidence.json> --pr owner/repo#编号 --out <路径.md>` 获取 Description 与逐条 commit message，多个 PR 重复 `--pr`，覆盖已有同名证据加 `--overwrite`。
已选 PR 不要求有区间内提交；评审、讨论、仅合并活动的 PR 都可读取描述和合并时间，不影响同批其他 PR。本人本期参与的内容需另有依据。
完整 message 优先复用发现阶段的 API 结果，缺失时依次读取本地 Git、GitHub [commit object](https://docs.github.com/en/rest/git/commits#get-a-commit-object)，并保存来源和缺口。详情结果的 `status` / `summary` 区分完整和部分读取；退出码 0 只表示保存成功。
脚本不采集 diff：commit message 说不清改动时，由 agent 按 SHA 自行 `git show` review。
参数、证据含义及不支持的情况见 [PR 详情采集](references/collection.md#pr-description-与本期提交)。脚本不自动登录或修改远端，真实账号连通性需在实际使用时核实。

## 定时（可选）

macOS `launchd`，每天 19:00 留一份当天证据快照（会话记录会被 Claude Code 等工具定期清理，留存比事后补采可靠）。
agent 出稿时仍会重新采集，使用 `--overwrite` 允许更新已有证据文件。历史快照保留原始记录清理前已采到的证据，新快照反映重新采集时能读取的内容：

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.example.daily-report.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.daily-report</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>REPLACE_WITH_HOME/.agents/skills/daily-report/scripts/collect.py</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>19</integer><key>Minute</key><integer>0</integer></dict>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/com.example.daily-report.plist
```

## 证据与历史快照

最新视图默认位于 `<reports_dir>/.evidence/<日期或区间>.md`；每次落盘同时保存独立历史快照：

```
.evidence/.history/<最新视图文件名>/<UTC时间戳与唯一后缀>/
├── evidence.md      # 本次 Markdown 证据
├── evidence.json    # 结构化证据、完整事件及来源定位
└── previous.md      # 覆盖前的最新视图（存在时）
```

`--max-sessions` 仅限制 Markdown 详细摘要，`--print` 不保存 JSON 或快照。
`--keep` / `evidence_keep` 只清理最新视图，历史快照需自行管理，磁盘占用会随使用增长。
字段、脱敏、重采差异和诊断解释见 [采集与证据核对](references/collection.md#输出与重采)。
`read_evidence.py --evidence <回执.json>` 展示实际元数据，支持按 PR、会话及字段分页读取。
参数和示例运行 `python3 scripts/read_evidence.py --help` 或 `python3 scripts/pr_details.py --help` 查看；快照使用、初筛及完整读取的约定见 [读取证据](references/collection.md#读取证据)。

Codex 文字统一读取 `response_item/message` 中的 `input_text`、`output_text`、`text` 块及字符串正文。`event_msg` 文字不进入证据，遇到时报告格式不支持；其中的 token 统计等非文字事件不触发此告警。系统/开发者消息和注入上下文不当作用户请求；图片、音频不转换成文字。
会话诊断中的 `user_messages`、`assistant_messages` 是项目范围过滤前、时间窗口内提取到的文字事件数；`unparsed_messages` 表示格式不支持或内容无法完整解析的消息数（缺少时间戳的此类消息也计入缺口）。即使工具记录正常，文字解析缺口仍会单独告警，并保留可读部分。修复解析器后，旧证据需重新采集才能补齐。
已知运行时注入文本和 Claude API 请求副本不进入会话证据。会话错误通过 `issues` 提供有限的文件、行号和类型定位；fetch 失败保存脱敏后的 stderr 摘要。详见 [证据线索与覆盖缺口](references/collection.md#证据线索与覆盖缺口)。

## 运行环境

Python 3.11+，只使用标准库；提交证据需要已登录的 `gh`（PR 搜索）和 `git`（本地解析），DSH 会话需要 `zstd`。
目前在 macOS 验证；会话格式、目录扫描和时间戳的覆盖限制见 [证据线索与覆盖缺口](references/collection.md#证据线索与覆盖缺口)。

## 验证

Python lint 使用 Google Pylint 配置，项目将行长度上限调整为 **128 字符**，其余规则不变；Ruff 格式化使用同一宽度。配置分别保存在 `.pylintrc` 和 `ruff.toml`，在项目根目录运行：

```bash
pylint --rcfile=.pylintrc --jobs=1 scripts/*.py scripts/daily_report
ruff format --check scripts
ruff check scripts
```

当前验证版本为 Pylint 4.1.4、Ruff 0.11.10；使用 `ruff format scripts` 应用格式化。

运行 `python3 -m unittest discover -s tests -v` 检查采集、事件定位、覆盖诊断和快照行为。
写稿规则的行为验收见 [tests/REPORT_ACCEPTANCE.md](tests/REPORT_ACCEPTANCE.md)；这部分需要实际生成报告并按样例判据审阅，不能用单元测试通过代替。

## 许可证

MIT，见 [LICENSE](LICENSE)。包名 `daily-report-skill`，当前版本 0.1.1；对外的插件清单声明在 `package.json` 的 `dsh.bundle`，
社区索引里的每个版本都钉在仓库的具体提交上。
