# daily-report

自用的日报/周报 skill：吃本地 **Git 提交** + **Claude Code / Codex / DSH（DeepSeek Harness）会话**，产出中文日报。

```
daily-report/
├── SKILL.md                    # 入口：完整工作流（日期参数、核对、归纳、审稿）和硬性约束
├── REFERENCE.md                # 查阅材料：模板、分栏依据、证据线索、周期粒度、反例、判读速查
├── tests/                      # 采集器回归测试、报告质量验收说明及固定证据样例
├── examples/2026-10-16.md      # 日报样例输出（项目名、编号均为虚构）
├── examples/2026-10-12~2026-10-18.md # 主题归纳后的周报样例
└── scripts/
    ├── collect.py              # 采集器（仅标准库；DSH 会话走 zstd CLI）
    └── config.example.json     # 配置样例
```

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

## 身份配置

```bash
python3 ~/.agents/skills/daily-report/scripts/collect.py --whoami
```

输出 git 全局身份、配置里的邮箱、以及每个仓库近 90 天的作者排行（命中配置的标 `<- 匹配`）。
**只把自己的邮箱写进 `author_emails`**：团队仓库里同事的提交很常见，写错会把别人的活儿算成你的。
配置邮箱和回退使用的 Git 全局邮箱都按完整邮箱匹配（忽略大小写）；`+`、`.` 等字符按字面值处理。
需要按作者姓名或正则筛选时，可显式传 `--author '<正则>'`，覆盖邮箱配置。

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
python3 $S --force              # 保存独立快照后更新最新视图（agent 重新采集时使用）
python3 $S --any-author         # 不过滤作者（团队仓库慎用）
python3 $S --no-sessions        # 只看提交
python3 $S --no-redact          # 关闭证据包脱敏（默认开启）
python3 $S --keep 10            # 最新 Markdown 视图只留最近 10 份；不删除历史快照
python3 $S --max-sessions 10    # 展开 10 段详细摘要；完整索引和 JSON 不截断
```

然后在 Claude Code / Codex / DSH 里说「写今天的日报」，agent 会读 `SKILL.md` 执行对应流程。
提供完整证据时可直接写稿；仅润色现有文本时只编辑指定内容。受众、格式、篇幅和交付方式以用户要求为准，按需采集。

## 定时（可选）

macOS `launchd`，每天 19:00 留一份当天证据快照（会话记录会被 Claude Code 等工具定期清理，留存比事后补采可靠）。
agent 出稿时仍会带 `--force` 重新采集。历史快照保留原始记录清理前已采到的证据，新快照反映重新采集时能读取的内容：

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

默认最新视图为 `<reports_dir>/.evidence/<日期或区间>.md`。每次落盘都会先在
`.evidence/.history/<最新视图文件名>/<UTC时间戳与唯一后缀>/` 保存：

- `evidence.md`：本次证据，包含采集诊断、完整会话索引和选中的详细摘要。
- `evidence.json`：所有已解析会话在所选区间内的完整请求、助手陈述、工具调用及结果；附带时间、源文件、行号和提交 SHA。
- `previous.md`：覆盖前的最新视图（存在时），也兼容旧版未归档的证据包。

JSON 采用 `schema_version: 1`；默认同样脱敏。每段会话的 `events` 按 `kind` 区分
`user_request`、`assistant_statement`、`tool_call`、`tool_result`，`at` 为事件时间，
`source` 和 `line` 定位原始 JSONL（DSH 行号按解压后的内容计算），`call_id` 用于关联调用与结果。
Markdown 中的来源链接指向本次独立快照，回查不会混入后来生成的版本。

`max_sessions` 只限制 Markdown 详细摘要，按日期、项目轮流选择；所有已解析会话都保留在索引和 JSON 中。
重采的提交数或会话数减少时，stderr 和最新视图都会提示变化；新旧证据不会自动合并。
`--keep` / `evidence_keep` 只清理最新 Markdown 视图，`.history` 不自动清理，因此磁盘占用会随使用增长。
`--print` 只输出 Markdown，不创建历史快照或 JSON；需要保存完整事件时使用默认落盘方式。

采集诊断区分未启用、来源不可用、部分失败和所选范围无记录；Git 以仓库为计数单位，会话以文件为计数单位。
损坏记录会计入诊断，其他文件仍继续采集。扫描采用既定目录模式和 mtime 预筛，诊断不证明这些范围之外不存在记录。

## 已知限制

- 会话格式可能变化。部分文件读取/解析失败会写入诊断；完全无法识别时另发格式漂移提示。
  能解析部分事件不代表支持所有未来格式，需要结合原始记录核对覆盖。
- DSH 需要 `zstd` CLI，缺失时标记来源不可用；未识别的工具结果格式仍需回查原文。
- 跨天会话按区间内事件采集，Codex 目录按开始日组织也会扫描到；mtime 预筛可能跳过时间戳不可靠的拷贝文件。
- Markdown 摘要有损，长请求及输出会缩短，助手只展示末尾几条陈述；完整的已解析事件保存在 JSON。
  助手陈述和本地 commit 均不证明已验证或已合并，关键状态需回查对应工具结果和后续事件。
- `--date` 是结束日，`--days N` 含结束日；自然周用 `--week` 或 `--last-week`。
- `config.dirty` 只在区间包含今天时生效；显式 `--dirty` 始终采集运行时快照。只统计 tracked 文件，未入索引的新文件不可见。
  `stale_days` 仅标记长期无提交的仓库；最近提交时间不代表当前改动持续了多久，不能据此判断积压。
- 日期归属按 committer date。rebase 会改写它；作者时间相差超过一天时另作标注。
- 脱敏规则不完备；证据和快照包含会话原文，写稿时仍需检查，避免复制凭据。
- `--since-as-filter` 遍历完整历史后过滤，大仓库上会更慢。
- 只在 macOS + Python 3.11+ 上验证过。

## 验证

运行 `python3 -m unittest discover -s tests -v` 检查采集、事件定位、覆盖诊断和快照行为。
写稿规则的行为验收见 [tests/REPORT_ACCEPTANCE.md](tests/REPORT_ACCEPTANCE.md)；这部分需要实际生成报告并按样例判据审阅，不能用单元测试通过代替。
