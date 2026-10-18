# daily-report

自用的日报/周报 skill：吃本地 **Git 提交** + **Claude Code / Codex / DSH（DeepSeek Harness）会话**，产出中文日报。

```
daily-report/
├── SKILL.md                    # 入口：完整工作流（日期参数、核对、归纳、审稿）和硬性约束
├── REFERENCE.md                # 查阅材料：模板、分栏依据、证据线索、周期粒度、反例、判读速查
├── tests/test_collect.py       # 回归测试（python3 -m unittest discover -s tests）
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
python3 $S --force              # 覆盖已有证据包（agent 流程固定带它）
python3 $S --any-author         # 不过滤作者（团队仓库慎用）
python3 $S --no-sessions        # 只看提交
python3 $S --no-redact          # 关闭证据包脱敏（默认开启）
python3 $S --keep 10            # 证据包只留最近 10 份
```

然后在 Claude Code / Codex / DSH 里说「写今天的日报」，agent 会读 `SKILL.md` 走完流程。

## 定时（可选）

macOS `launchd`，每天 19:00 留一份当天证据快照（会话记录会被 Claude Code 等工具定期清理，留存比事后补采可靠）。
agent 出稿时仍会带 `--force` 重新采集，所以 19:00 之后的工作也不会漏：

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

## 已知限制

- **Claude Code / Codex 的会话格式会变**。窗口内有会话文件被改过但解析不出内容时，脚本会打
  `[warn] <源>: ... 会话格式可能变了`，按提示检查 `~/.claude/projects`、`~/.codex/sessions` 的结构。
  此时 sessions 为 0 不代表当天没有会话。
- **DSH 会话是 zstd**，需要 `brew install zstd`；没装会自动跳过并提示。
- 跨天的长会话会算进它覆盖的每一天（区间查询时表现为跨多天的 span），这是有意的：
  那天的日报里确实该提它。三个会话源都按文件 mtime 预筛，Codex 按开始日分目录也不会漏掉跨天会话。
- 会话摘要是有损的：每段只保留头尾各 2 条用户消息（中间标注省略条数）和最后几条结论。
- `--date` 是区间结束日（可写 `today` / `yesterday`），`--days N` 包含结束日并向前取 N 个自然日。
  自然周用 `--week`（周一至今天）或 `--last-week`（上周一至上周日），不要手算天数；二者不能和 `--date` / `--days` 同用。
- 证据包可能含敏感会话摘要。检查证据后再写日报，避免复制内网地址、密码、token 或其他凭据。已有证据包不会自动覆盖；
  确认覆盖时传 `--force`（证据包是可重新生成的快照，开头写有生成时间）。
- `config.dirty` 只在采集区间包含今天时自动采集当前工作区状态；历史或未来区间会忽略。显式
  传 `--dirty` 始终采集运行时快照，脚本会警告它不代表所选历史/未来日期的状态。
- `--dirty` 用 `--porcelain -uno` 只扫 tracked 文件：untracked 文件不统计（大仓库里遍历磁盘太慢），
  所以新文件没进索引时看不到。
- **日期归属按 committer date**（提交落地时间）。rebase 会保留 author date 而改写 committer date，
  所以"几天前写、这周落地"的工作算本周；两者相差超过一天时证据里会标 `（作者 MM-DD）`。
- 证据包默认会脱敏（GitHub/GitLab token、`sk-` key、AWS key、私钥块、内网 IP、`password=...`），
  但不追求完备，写稿前仍要人工过一眼。
- `--since-as-filter` 会遍历完整历史再过滤（避免提交时间不单调时漏提交），大仓库上比剪枝慢，一周区间实测约 3s/19 仓库。
- 只在 macOS + Python 3.11+ 上验证过。
