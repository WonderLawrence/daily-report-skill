"""User-facing messages and CLI help.

Wording iterations happen here; the modules that use these names don't need to change.
Strings with `{name}` placeholders are templates — call sites apply them via `.format(...)`.

Sections are ordered by life cycle: CLI → config → settings → collection → rendering.
"""
from __future__ import annotations


# =============================================================================
# CLI argument errors (raised to the user via parser.error / ValueError)
# =============================================================================
CLI_ERR_DAYS_MIN = "--days 必须大于等于 1"
CLI_ERR_WEEK_CONFLICT = "--week / --last-week 不能和 --date / --days 同时使用"
CLI_ERR_MAX_SESSIONS_MIN = "--max-sessions / config.max_sessions 必须大于等于 1"
CLI_ERR_KEEP_MIN = "--keep 必须大于等于 0"
CLI_ERR_FETCH_TIMEOUT_MIN = "--fetch-timeout 必须大于等于 1"
CLI_ERR_TIMEOUT_MIN = "--timeout 必须大于等于 1"
CLI_ERR_OUT_EXISTS = "输出已存在；需要覆盖时加 --overwrite，旧证据会保留为快照"
CLI_ERR_JSON_INCOMPATIBLE = "--json 需要保存证据，不能与 --print 或 --whoami 同用"
CLI_ERR_REPO_CONFLICT = "--repo 不能与 only_repos 配置同时使用；请用 --only-repo 指定完整范围"
CLI_ERR_ONLY_REPO_NOT_DIR = "关注项目路径不存在或不是目录：{path}"
CLI_ERR_ONLY_REPO_NOT_REPO = "--only-repo / only_repos 必须指向仓库根目录：{path}"
CLI_ERR_LOAD_SNAPSHOT = "无法读取有效采集快照：{detail}"

MISSING_GIT = "找不到 git 命令，请确认已安装并在 PATH 中"
CONFIG_INVALID = "配置无效：{details}"


# =============================================================================
# Config validation errors (validate_config)
# =============================================================================
CFG_ERR_AUTHOR_EMAILS = "author_emails 必须是非空邮箱组成的字符串数组（允许整个数组为空）"
CFG_ERR_ONLY_REPOS = "only_repos 必须是路径字符串数组（允许为空）"
CFG_ERR_PATH_STRING = "{key} 必须是非空路径字符串"
CFG_ERR_UTC_OFFSET_TYPE = "utc_offset_hours 必须是数字"
CFG_ERR_UTC_OFFSET_RANGE = "utc_offset_hours 必须在 -12 到 14 之间"
CFG_ERR_INT_RANGE = "{key} 必须是大于等于 {minimum} 的整数"
CFG_ERR_BOOL = "{key} 必须是布尔值"
CFG_ERR_SOURCES = "sources 必须是由布尔值组成的对象"
CFG_ERR_LOAD_FAILED = "配置文件解析失败：{detail}"
CFG_ERR_TOP_LEVEL = "配置文件顶层必须是 JSON 对象"
CFG_WARN_MISSING = "[warn] 配置文件不存在，使用默认值：{path}"


# =============================================================================
# Author regex errors (gitlog.author_regex)
# =============================================================================
AUTHOR_REGEX_INVALID = "--author / author_emails 不是合法正则：{detail}"


# =============================================================================
# Time window errors (timewindow.resolve_period)
# =============================================================================
TW_ERR_DATE_FORMAT = "--date 需要 YYYY-MM-DD 格式（或 today / yesterday），收到 {value!r}"
TW_ERR_DAY_OVERFLOW = "日期超出可表示范围（{day}）：请改用更近的 --date"
TW_ERR_SHIFT_OVERFLOW = "日期超出可表示范围（{day} {days:+} 天）：请改用更近的 --date"
TW_ERR_DAYS_OVERFLOW = "日期区间超出可表示范围（{days} 天）：请缩小 --days"


# =============================================================================
# GitHub API errors (github.GitHubError)
# =============================================================================
GH_ERR_QUERY_FAILED = "GitHub 查询无法执行或超时"
GH_ERR_HTTP_STATUS = "GitHub 查询失败（HTTP {code}）"
GH_ERR_UNRECOGNIZED = "GitHub 返回了无法识别的响应"
GH_ERR_INVALID_PR_ID = "无效的 PR 标识"
GH_ERR_PR_DETAIL_FORMAT = "PR 详情格式不完整"
GH_ERR_PR_DESCRIPTION = "PR Description 格式不完整"
GH_ERR_PR_HEAD = "PR head 格式不完整"
GH_ERR_REMOTE_COMMIT_MISMATCH = "远程提交 SHA 不匹配或 message 格式不完整"

# PR discovery coverage details (PullRequestDiagnostics.detail / status explanation)
GH_DETAIL_REPO_NO_REMOTE = "所选仓库没有可识别的 GitHub remote，未执行 PR 搜索"
GH_DETAIL_GH_MISSING = "未安装 gh，无法按 PR 发现本期工作"
GH_DETAIL_NO_LOGIN_EXPLICIT = "无法确认登录账号，PR 内提交只按显式作者正则匹配"
GH_DETAIL_NO_LOGIN_EMAILS = "无法确认登录账号，PR 内提交只按配置邮箱匹配"
GH_SEARCH_LABEL_ALL = "全部仓库"
GH_DETAIL_SEARCHES_SKIPPED = "另有 {count} 个仓库搜索未执行"
GH_DETAIL_SEARCH_PAGE_ERROR = "{label} 第 {page} 页：{error}"
GH_DETAIL_SEARCH_INCOMPLETE = "{label} 第 {page} 页：GitHub 声明搜索结果不完整"
GH_DETAIL_SEARCH_TRUNCATED = "{label}：平台声明 {total} 条结果，仅读取 {received} 条（{reason}）"
GH_REASON_SHORT_PAGE = "返回短页"
GH_REASON_HIT_LIMIT = "达到搜索上限"
GH_REASON_QUERY_BUDGET = "查询额度不足"
GH_DETAIL_COMMIT_LIST_CAPPED = "{pr_id} 的提交列表达到 250 条，可能还有未返回的提交"

# Local git read failures that feed PeriodCommit.local_message_error
GH_LOCAL_INVALID_SHA = "无效的提交 SHA"
GH_LOCAL_UNREADABLE = "本地提交不可读"
GH_LOCAL_TIMEOUT = "读取本地提交信息失败或超时"
GH_LOCAL_NO_REPO = "未找到本地仓库"
GH_REMOTE_FALLBACK_FAILED = "{local_error}；GitHub 回退失败：{exc}"


# =============================================================================
# PR snapshot / selection errors (pr_report.load_selection)
# =============================================================================
PR_ERR_TZ_MISSING = "缺少时区信息"
PR_ERR_SNAPSHOT_BOUNDS = "快照必须包含有效的带时区起止时间：{detail}"
PR_ERR_SNAPSHOT_RANGE = "快照起止时间不合法：start 必须早于 end_exclusive"
PR_ERR_SNAPSHOT_SHAPE = "快照 PR 结构不完整：{detail}"
PR_ERR_UNKNOWN = "所选 PR 不在该快照中：{key}"
PR_ERR_COMMIT_TIME = "PR 关联提交时间不可用：{detail}"
PR_ERR_COMMIT_OUT_OF_RANGE = "PR 关联提交超出快照时间范围"


# =============================================================================
# Evidence reader errors (evidence_reader)
# =============================================================================
ER_ERR_READ_JSON = "无法读取 JSON 文件 {path}：{detail}"
ER_ERR_TOP_LEVEL = "JSON 顶层必须是对象：{path}"
ER_ERR_RECEIPT_SCHEMA = "不支持的回执 schema_version"
ER_ERR_RECEIPT_PATH = "回执缺少有效的 evidence_path"
ER_ERR_RECEIPT_RECURSIVE = "回执必须指向证据 JSON，不能指向另一个回执"
ER_ERR_EVIDENCE_SCHEMA = "不支持的证据 schema_version；请提供采集或 PR 详情快照"
ER_ERR_KIND_UNKNOWN = "无法识别证据类型；请提供 collection 或 pr_details 的 evidence.json"
ER_ERR_FIELD_UNKNOWN = "未知字段 {part!r}；可用字段：{available}"
ER_ERR_INDEX_INVALID = "列表索引 {part!r} 无效；列表长度 {length}，索引从 0 开始"
ER_ERR_SCALAR_LOOKUP = "无法在标量值下读取 {part!r}"
ER_ERR_LIST_SHAPE = "{name} 必须是对象列表"
ER_ERR_KEY_NOT_FOUND = "{name} 中的 {key!r} {detail}"
ER_DETAIL_NOT_FOUND = "不存在"
ER_DETAIL_AMBIGUOUS = "匹配多个对象，请使用完整 ID"
ER_ERR_PROJECTION_UNKNOWN = "未知投影字段：{unknown}；可用字段：{available}"
ER_ERR_PROJECTION_SHAPE = "--fields 只适用于对象列表"
ER_ERR_PROJECTION_NEEDS_FIELD = "--fields 需要非空字段名列表，并且 --field 必须指向对象列表"
ER_ERR_PAGE_OFFSET = "--offset 超出范围：{offset}，总长度 {total}"
ER_ERR_PAGE_BUDGET = "输出预算不足以容纳文字及定位信息；请增大 --max-chars"
ER_ERR_PAGE_ITEM_BUDGET = ("第 {index} 项超过输出预算；请用 --fields 缩小列表字段，"
                          "或用 --field 指向具体字段（如 events.0.text）分段读取")
ER_ERR_PAGE_OVERSIZE = "输出超过 --max-chars；请用 --field 缩小读取范围"
ER_ERR_PR_SESSION_CONFLICT = "--pr 和 --session 不能同时使用"
ER_ERR_OFFSET_LIMIT = "--offset 必须非负，--limit 必须为正数，--max-chars 至少为 {min_chars}"
ER_ERR_FIELDS_NEEDS_FIELD = "--fields、--offset、--limit 需要与 --field 一起使用"
ER_ERR_METADATA_BUDGET = "元数据超过输出预算；请用 --field 指定具体字段"


# =============================================================================
# Evidence writer messages (evidence.write_evidence)
# =============================================================================
EV_ERR_EXISTS = "证据包已存在：{path}；需要覆盖时加 --overwrite，旧证据会保留为快照"
EV_ERR_SERIALIZE = "无法序列化结构化证据：{detail}"
EV_ERR_WRITE_FAILED = "无法写入证据包 {path}：{detail}"
EV_ERR_UNSERIALIZABLE_TYPE = "证据无法序列化的类型：{type}"
EV_WARN_COUNTS_DECREASED_STDERR = "[warn] {warning}；旧快照已保留"
EV_SNAPSHOT_DIFF_HEADING = "## 快照差异"
EV_COUNTS_DECREASED = "重采证据数量减少：{details}"
EV_SNAPSHOT_DIFF_BODY = ("{warning}。旧证据保存在 `{previous}`；"
                        "可能是采集范围变化或原始记录缺失，请核对；未自动合并新旧证据。")
EV_STRUCTURED_FOOTER = "结构化证据（完整会话事件与定位）：`{path}`"
EV_SNAPSHOT_FOOTER = "本次快照：`{path}`"
EV_WARN_PRUNE_DIR = "[warn] 无法整理证据目录 {directory}: {error}"
EV_WARN_PRUNE_FILE = "[warn] 无法删除旧证据包 {path}: {error}"


# =============================================================================
# Collection diagnostics / log output (pipeline, sessions, gitlog, collect, pr_details)
# =============================================================================
LOG_PR_COVERAGE_PARTIAL = "[warn] PR 覆盖不完整；本期 Git 证据可能缺少工作，详见采集诊断"
LOG_PATCH_ID_COLLAPSED = ("[note] 折叠了 {count} 个 patch-id 相同的 rebase/cherry-pick 副本，"
                          "写报告时按同一事项处理")
LOG_LOCAL_UNASSOCIATED = ("[note] 本地有 {count} 条本人提交未在本次 PR 检索结果中找到关联，"
                         "未纳入证据；如果其中有要写的工作，用会话记录或你自己的说明补充")
LOG_ALIAS_IDENTITIES = ("[warn] 有和 {name} 同名、但邮箱没配的提交：{detail}\n"
                        "       如果这些是你（换过邮箱），把它们加进 config.author_emails 后重跑；"
                        "如果是重名同事，忽略即可")
LOG_ALIAS_ENTRY = "{who} ({count} 个提交)"

# Session scan warnings
LOG_SESSION_SOURCE_MISSING = "[note] 未找到 {tool}，跳过 {source} 会话（brew install {tool}）"
LOG_SESSION_PARTIAL = "[warn] {name}: {count} 个文件读取或解析不完整；详见证据包采集诊断"
LOG_SESSION_UNPARSED = ("[warn] {name}: {count} 条文字消息未能完整解析；"
                       "格式不支持或内容损坏，检查 {location}{hint}。已保留可读内容及工具记录")
LOG_SESSION_NO_EVENTS = ("[warn] {name}: 窗口内有 {count} 个会话文件被修改，但没解析出任何事件；"
                        "会话格式可能变了，检查 {location}。这不代表当天没有会话")
LOG_SESSION_NO_EXTRACT = ("[warn] {name}: 解析到 {count} 条事件，但没提取出任何用户消息或工具调用；"
                         "会话格式可能变了，检查 {location}")
LOG_SESSION_CODEX_HINT = "；Codex 文字仅支持 response_item/message"

# Fetch warnings
LOG_FETCH_WARN = "[warn] {name}: {detail}；继续读取本地记录，远程提交覆盖可能不完整"
LOG_FETCH_READ_WARN = "[warn] 无法读取仓库 {repo}: {detail}"
LOG_FETCH_UNSUPPORTED = "[note] {name}: Git 不支持 --since-as-filter，改为遍历完整历史后过滤日期"

# Fetch result details
FETCH_SKIPPED_DISABLED = "已禁用 fetch，使用本地引用"
FETCH_REMOTE_FAILED = "无法列出远程"
FETCH_FAIL_PREFIX = "fetch 失败"
FETCH_NO_REMOTE = "未配置远程，使用本地记录"
FETCH_OK = "fetch 成功，已按远程配置更新引用"
FETCH_TIMEOUT = "Git 同步操作超时（{seconds} 秒）"
FETCH_EXEC_FAILED = "无法执行或读取 Git 同步操作"
FETCH_DETAIL_WITH_EXCERPT = "{label}（退出码 {code}）：{excerpt}"
FETCH_DETAIL_NO_EXCERPT = "{label}（退出码 {code}）"
FETCH_READ_REMOTE_FAILED = "无法读取仓库远程配置：{repo}"

# Dirty state notes
LOG_DIRTY_NOT_IN_WINDOW = ("[note] 历史/未来日期范围忽略 config.dirty；"
                          "当前工作区状态只在范围包含今天时自动采集")
LOG_DIRTY_FUTURE = "[warn] --dirty 采集的是运行时工作区状态，不代表所选历史/未来日期的状态"

# collect.py status lines
STATUS_COUNTS = "{commits} commits, {sessions} sessions"
STATUS_OK_TO_STDOUT = "[ok] {counts}"
STATUS_OK_TO_PATH = "[ok] {counts} -> {path}"
STATUS_ERROR_PREFIX = "[error] {detail}"
STATUS_DETAILS_HEADER = "[{status}] {loaded}/{requested} PR details -> {out}"
# Shared fragment: pr-details stdout summary and D_SUMMARY_LINE must stay in sync on
# wording because both describe the same counts to different audiences.
_SUMMARY_TAIL = ("提交 message 缺失 {failed_commit_messages}/{commit_messages}；"
                 "提交列表未确认 {incomplete_commit_lists}")
STATUS_DETAILS_SUMMARY = "[summary] PR 失败 {failed_prs}；" + _SUMMARY_TAIL
STATUS_DETAILS_WARN = "[warn] 部分 PR 详情或提交信息不完整；已保存可得证据，不能据此声称完整覆盖"

# Identity report (gitlog.print_identity_report)
IDENTITY_USER_NAME = "git user.name  : {name}"
IDENTITY_USER_EMAIL = "git user.email : {email}"
IDENTITY_EMAILS_EMPTY = "(空，回退到 git user.email)"
IDENTITY_EMAILS = "author_emails  : {emails}"
IDENTITY_REGEX = "匹配正则       : {regex}"
IDENTITY_REPOS_ROOT = "仓库根         : {root}  ({count} 个仓库)"
IDENTITY_MATCH_MARK = "  <- 匹配"


# =============================================================================
# CLI help text (argparse add_argument help=...)
# =============================================================================
CLI_HELP_DATE = "Period end date: YYYY-MM-DD, today, or yesterday (default: today)"
CLI_HELP_DAYS = "Number of days up to and including the end date (default: 1)"
CLI_HELP_WEEK = "This Monday through today; incompatible with --date/--days"
CLI_HELP_LAST_WEEK = "Last Monday through Sunday; incompatible with --date/--days"
CLI_HELP_CONFIG = "Config file (default lookup: DAILY_REPORT_CONFIG, user config, then workspace config)"
CLI_HELP_REPOS_ROOT = "Override repos_root from the config"
CLI_HELP_AUTHOR = "Author regex, overriding email and GitHub login matching; use 'a|b' for multiple identities"
CLI_HELP_REPO = "Add a repository while still scanning the default root; repeatable"
CLI_HELP_ONLY_REPO = "Collect only these repositories and their project sessions; repeatable"
CLI_HELP_ANY_AUTHOR = "Disable commit author filtering; PR search still uses the logged-in account"
CLI_HELP_FETCH = "Fetch all configured remotes before collection (enabled by default; --no-fetch for offline use)"
CLI_HELP_FETCH_TIMEOUT = "Timeout per Git sync command in seconds (default: 30)"
CLI_HELP_PRS = "Discover period commits through PRs (enabled by default; disabled by --no-fetch)"
CLI_HELP_DIRTY = "Include current uncommitted changes to tracked files; does not infer their age"
CLI_HELP_NO_REDACT = "Disable evidence redaction"
CLI_HELP_KEEP = "Number of latest views to keep; 0 disables cleanup; historical snapshots are retained"
CLI_HELP_MAX_SESSIONS = "Detailed summary limit; the full session index and JSON remain intact"
CLI_HELP_WHOAMI = "Inspect identity only: Git identity, configured emails, and recent authors per repository"
CLI_HELP_OUT = "Evidence output path"
CLI_HELP_OVERWRITE = "Allow replacing existing evidence after preserving it in a snapshot"
CLI_HELP_PRINT = "Write Markdown to stdout without saving files; enabled collection still runs"
CLI_HELP_JSON = "Save evidence and write a JSON receipt with paths and counts to stdout; diagnostics go to stderr; incompatible with --print/--whoami"
CLI_HELP_NO_SESSIONS = "Disable agent session collection"
CLI_HELP_NO_GIT = "Disable Git collection"

CLI_HELP_DETAILS_EVIDENCE = "Collection evidence.json or a receipt from collect.py --json"
CLI_HELP_DETAILS_PR = "PR identifier (owner/repo#number) or URL from the collection snapshot; repeatable"
CLI_HELP_DETAILS_OUT = "Markdown output path for selected PR evidence"
CLI_HELP_DETAILS_JSON = "Write a JSON receipt with paths, completeness, and counts to stdout; diagnostics go to stderr"
CLI_HELP_DETAILS_TIMEOUT = "Timeout per external command in seconds (default: %(default)s; minimum: 1)"
CLI_HELP_DETAILS_OVERWRITE = "Allow replacing existing evidence after preserving it in a snapshot"

CLI_DETAILS_DESCRIPTION = "Fetch selected PR descriptions and the user's commits within the period, then save redacted evidence snapshots."
CLI_DETAILS_EPILOG = """Example:
  %(prog)s --evidence run.json --pr owner/repo#43 --out selected.md --json

Exit 0 means evidence was saved, even if partial.
Check status and summary to assess completeness.
"""

CLI_READ_DESCRIPTION = "Read saved local evidence by object and field, without network access, recollection, or writes."
CLI_READ_EPILOG = """Example:
  %(prog)s --evidence run.json --field pull_requests --fields id,title

Read data, then pass next_offset to --offset until it is null.
Keep the same evidence, object, field, and --fields when continuing.
If an item is too large, narrow --fields, select its text with --field,
or increase --max-chars.
"""
CLI_HELP_READ_EVIDENCE = "Evidence JSON or a receipt saved with --json"
CLI_HELP_READ_PR = "PR identifier (owner/repo#number) or full URL"
CLI_HELP_READ_SESSION = "Full session ID or an unambiguous short ID"
CLI_HELP_READ_FIELD = "Field path, e.g. events.0.text (zero-based); omit to inspect metadata and field names"
CLI_HELP_READ_FIELDS = "Fields to return from each list object, e.g. id,title (requires --field)"
CLI_HELP_READ_OFFSET = "Starting position for --field, counted like --limit (default: %(default)s)"
CLI_HELP_READ_LIMIT = "Maximum characters, array items, or object fields per page (default: {page_items} items or {page_chars} characters)"
CLI_HELP_READ_MAX_CHARS = "Character budget for the entire JSON output (default: %(default)s; minimum: {min_chars})"


# =============================================================================
# Coverage status strings (render.coverage_status)
# =============================================================================
COV_DISABLED = "未启用"
COV_UNAVAILABLE = "来源不可用（目录或依赖缺失）"
COV_PARTIAL_SOME_OK = "部分失败"
COV_FAILED = "读取或解析失败"
COV_FETCH_GAP = "远程同步不完整，使用可读的本地记录"
COV_OK = "正常"
COV_EMPTY = "所选范围无记录"


# =============================================================================
# Markdown — collection report (render.py)
# =============================================================================
R_PERIOD_DAILY = "日报"
R_PERIOD_WEEKLY = "周报"
R_PERIOD_GENERIC = "{days} 天汇总"

R_H_EVIDENCE = "# 工作证据 {span}"
R_GENERATED = "_生成于 {time}（UTC{offset}）_"

# Scope section
R_H_SCOPE = "## 采集范围"
R_SCOPE_UNCOLLECTED = "未采集"
R_SCOPE_GIT_EMPTY = "已启用，但未发现仓库"
R_SCOPE_GIT_LINE = "- Git：{detail}"
R_SCOPE_SESSIONS_LINE = "- 会话：{detail}"
R_SCOPE_DIRTY_LINE = "- 当前工作区状态：{detail}"
R_SCOPE_DIRTY_YES = "已扫描（运行时快照）"
R_SCOPE_ONLY_REPOS_LINE = "- 仅关注项目：{detail}"

# Diagnostics section
R_H_DIAG = "## 采集诊断"
R_DIAG_LINE = ("- {name}：{status}；扫描 {scanned}，"
              "无已知解析错误 {ok_files}，失败/部分解析 {failed_files}，坏记录 {invalid_records}")
R_DIAG_GIT_SUFFIX = "；提交 {commits}"
R_DIAG_SESSION_SUFFIX = ("；会话 {sessions}；详细摘要 {detailed}，摘要省略 {omitted}；"
                       "范围外会话 {scope_excluded}，项目无法确认 {scope_unknown}；"
                       "提取用户消息 {user_messages}，助手文字 {assistant_messages}，"
                       "文字消息解析不完整 {unparsed_messages}")
R_DIAG_ISSUES_HEAD = "  异常定位（最多 20 条，计数不受此限制；行号 0 表示文件级问题）："
R_DIAG_ISSUE_LINE = "  - {source}:{line} · {kind}{record_type}"
R_DIAG_ISSUE_RECORD_TYPE = " · {record_type}"
R_DIAG_GIT_EXTRA = ("- git 补充：本地扫描到本人提交 {local_commits}，"
                   "其中未在本次 PR 检索结果中找到关联 {local_unassociated}（不在证据内，"
                   "需要时用会话记录或用户说明补充）；本期 PR 提交在本地找不到 "
                   "{commits_without_local_copy} 条（只有平台元数据）")
R_DIAG_FOOTER = ("计数单位：Git 为仓库，会话来源为文件；成功和失败文件分别计数。"
                "摘要省略的会话仍在完整索引及结构化证据中。")

# Git fetch section
R_H_FETCH = "### Git 远程同步"
R_FETCH_LINE = "- `{repo_path}`：{detail}；检查时间 {checked_at}"
R_FETCH_FOOTER = ("同步失败或跳过时，提交仅反映当前本地引用，远程覆盖未确认。"
                 "fetch 成功也不证明提交已合并、已验证或已部署。")

# PR section
R_H_PR = "### 本期 PR（第一层：involves 本人，按最近更新）"
R_PR_SUMMARY = ("状态：{status}；搜到 PR {prs_found}，含本期本人提交 {prs_with_commits}，"
               "本期无本人提交 {prs_without_commits}；"
               "查询 {queries}，失败 {failures}，未查询 {unqueried}。")
R_PR_OUT_OF_SCOPE = "范围外 PR {count} 个（仅关注项目限定），未纳入。"
R_PR_UNSUPPORTED = "本地没有对应仓库的 PR：{repos}；这些提交只有平台元数据，无法在本地核对改动。"
R_PR_INVOLVES_NOTE = ("`involves:` 只匹配 author/assignee/mentions/commenter，不含提交作者："
                    "他人创建、含本人提交但本人从未留言的 PR 不会出现在这里。"
                    "此处不含 Description；选定工作事项后再用 pr_details.py 获取详情。")
R_PR_LINE = ("- [{id}]({url})：{title}；{state}；"
            "读取时状态 {state_at_discovery}；更新于 {updated_at}；获取时间 {retrieved_at}")
R_PR_COMMITS_LINE = "  参与线索：{shas}"
R_PR_STATE_COMMITS = "本期本人提交 {n} 条"
R_PR_STATE_NO_COMMITS = "本期无本人提交（可能是评审或讨论，需初筛确认）"
R_PR_STATE_UNQUERIED = "未查询提交（超出查询额度）"
R_PR_STATE_FAILED = "提交查询失败，覆盖未确认"
R_PR_STATE_UNKNOWN = "未知参与形式"

# Topic section
R_H_TOPIC = "## 主题候选（按 commit scope 聚合，仅供参考）"
R_TOPIC_LINE = "- `{scope}`：{total} 条 — {types}"

# Commits section
R_H_COMMITS_BARE = "## A. 本期提交（{count} 条，来自上列 PR）"
R_H_COMMITS_WITH_EXTRAS = "## A. 本期提交（{count} 条，来自上列 PR，已折叠 {extras} 个 rebase 副本）"
R_COMMITS_EMPTY = "_无提交_"
R_COMMIT_REPO_HEADER = "### {repo}"
R_COMMIT_SIZE_UNKNOWN = "(改动规模未知：本地没有该提交)"
R_COMMIT_SIZE = "(+{added}/-{deleted}, {files} files)"
R_COMMIT_LINE = "- [{date}]{authored_note} {subject}{flags}  {size}"
R_COMMIT_AUTHORED = "（作者 {authored_date}）"
R_COMMIT_SOURCE_NONE = "无关联 PR"
R_COMMIT_EVIDENCE = ("  证据：commit `{full_sha}` · {at} · 仓库 `{repo_path}` · 来自 {source}"
                   "（不证明已合并或已验证）")
R_COMMIT_FLAG_NO_LOCAL = "本地未找到该提交，仅平台元数据"
R_COMMIT_FLAG_REVERT = "⚠️REVERT"
R_COMMIT_FLAG_REVERTED_BY = "已被 {sha} 回退"
R_COMMIT_FLAG_REVERTS = "回退 {sha}"
R_COMMIT_FLAG_COPIES = "另有 {count} 个副本"

# Sessions section
R_H_SESSIONS_BARE = "## B. Agent 会话（{count} 段）"
R_H_SESSIONS_TRUNCATED = "## B. Agent 会话（{count} 段，部分详细摘要省略，索引完整）"
R_SESSIONS_EMPTY = "_无会话记录_"
R_H_SESSION_INDEX = "### 完整会话索引"
R_SESSION_INDEX_FOOTER = ("以下是证据留存索引，不是待选清单。缺少文字线索的项先由 agent 初筛；"
                        "无法辨认具体工作时不向用户提问。助手线索仅为自述，仍需核验。")
R_SESSION_DETAIL_EXPANDED = "已展开"
R_SESSION_DETAIL_OMITTED = "摘要省略，可回查"
R_SESSION_SIGNAL_USER = "请求线索"
R_SESSION_SIGNAL_ASSISTANT = "助手线索，未核验"
R_SESSION_SIGNAL_SCREEN = "需初筛，暂不列候选"
R_SESSION_INDEX_LINE = ("- [{detail}；{signal}] {agent} · {project} · "
                      "{start:%m-%d %H:%M}–{end:%m-%d %H:%M} · id `{id}`{hint}")
R_SESSION_INDEX_HINT = " · {hint}"
R_SESSION_INDEX_SOURCE = "  来源：`{source}`；完整事件及分片定位见结构化证据。"
R_SESSION_HEADER = "### [{agent}] {project} · {span} · {steps} 步 · id {short_id}{dup}"
R_SESSION_HEADER_DUP = " · ↻ 相同有效请求 {count} 段（可能相关）"
R_SESSION_USER_LINE = "- 用户：{detail}"
R_SESSION_USER_OMITTED = "…（省略 {count} 条）"
R_SESSION_ASSISTANT_LINE = "- 助手陈述（未独立核验）：{text}"
R_SESSION_TOOLS_LINE = "- 动手：{tools}"
R_SESSION_TOOLS_PAIR = "{name}×{count}"
R_SESSION_TOOLS_FILES = "；涉及 {files}"
R_SESSION_RELATED = "- 可能相关提交：{links}"
R_SESSION_RELATED_PAIR = "{sha} {subject}"

R_H_EVENTS_USER_OMITTED = "- 用户请求省略 {count} 条，见结构化证据。"
R_EVENT_USER = "- 用户请求：{excerpt}"
R_EVENT_ASSISTANT = "- 助手陈述（未独立核验）：{excerpt}"
R_EVENT_TOOL_CALL = "- 对应工具调用：{excerpt}"
R_EVENT_TOOL_RESULT = "- 工具结果（输出摘录，需核对范围）：{excerpt}"
R_EVENT_TOTAL = "- 共 {count} 条原始事件；此处为摘录，中间决策及完整输出见结构化证据。"
R_EVENT_LOCATOR = "{text} （{locator}{call}）"
R_EVENT_CALL_SUFFIX = " · call {call_id}"

# Dirty section
R_H_DIRTY = "## C. 当前未提交改动（运行时快照）"
R_DIRTY_HINT = "最近提交距今 ≥{stale_days} 天只表示仓库长期无提交；改动存在时长未知。"
R_DIRTY_AGE_UNKNOWN = "未知"
R_DIRTY_AGE_DAYS = "{days} 天前"
R_DIRTY_STALE_MARK = "  长期无提交的仓库存在当前改动"
R_DIRTY_LINE = "- [{repo}] {files} 个文件改动；最近一次提交 {age}{mark}"

# Writing hint
R_H_WRITING = "## D. 写稿提示"
R_WRITING_BODY = ("本包是自动采集结果，不代表用户已确认范围。"
                "下一步按 SKILL.md 的阶段表和 references/interaction.md 初筛、确认工作事项，再分析。"
                "写{period}前按 references/writing.md 归纳与审稿，证据核对见 references/collection.md；"
                "只写已选对象中由本证据包、关联原始记录或用户补充材料支持的事实。"
                "先检查采集诊断和完整会话索引；助手陈述不能单独证明已验证或已合并。")


# =============================================================================
# Markdown — pr-details report (pr_report.render_markdown)
# =============================================================================
D_H_EVIDENCE = "# 已选 PR 的分析证据"
D_SUMMARY_LINE = ("完整性：{status}；PR 详情 {loaded_prs}/{requested_prs}，失败 {failed_prs}；"
                 + _SUMMARY_TAIL + "。")
D_COMPLETENESS_NOTE = "complete 只表示所选详情和已知提交信息读取完整，不代表工作覆盖完整或成果已验证。"
D_RANGE_LINE = "原始范围：{start} → {end}"
D_RANGE_FOOTER = ("Description 为读取时版本，提交按原快照的本人和时间范围筛选。"
                 "完整正文与 commit message 见本次 JSON；改动本身不在本包内，按需自行 review。")
D_PR_HEADER = "## [{id}]({url}) {title}"
D_PR_META = "获取时间：{retrieved_at}；读取时状态：{state_now}；合并时间：{merged_at}"
D_COMMIT_LINE = "- `{full_sha}` · {at} · {subject}"
D_COMMIT_MESSAGE = "  message：{excerpt}"
D_COMMIT_MESSAGE_SOURCE = "  message 来源：{message_source}；获取时间：{message_retrieved_at}"
D_COMMIT_LOCAL_ERROR = "  本地读取情况：{error}"
D_COMMIT_MESSAGE_GAP = "  message 缺口：{error}"
D_COMMIT_REVIEW_HINT = "  需要确认改动时自行 review：`git -C {repo_path} show {full_sha}`"
D_FAILURE_LINE = "- 缺口 {id}：{error}"

# PR detail limitations (github_details._DETAIL_LIMITATIONS)
D_LIM_DESCRIPTION = "Description 和状态为读取时的版本，不是历史快照。"
D_LIM_COMMITTER_DATE = "区间按提交的 committer date 筛选；rebase/cherry-pick 不等于新增工作。"
D_LIM_PLATFORM_COMMITS = ("下列是平台关联的本人提交及其 commit message，不是整个 PR 的成果，"
                        "也不证明本期首次加入 PR。")
D_LIM_SELF_REPORTED = "commit message 是作者自述；不足以说明改动时按 SHA 自行 review 本地 diff。"
D_LIM_DEDUP = "同一提交可能关联多个 PR；汇报时去重，不将关联视为独占归属。"
D_LIM_NO_PERIOD_COMMITS = ("原快照未发现本人区间内提交；Description 仅用于理解背景。"
                         "是否在本期参与评审或讨论，需用带时间的评审/评论记录或用户说明确认，"
                         "不能把整个 PR 的实现算作本人成果。")
D_LIM_INCOMPLETE_QUERY = "原快照的提交查询未完成，空提交列表不代表本期没有本人提交。"
D_LIM_COMMIT_LIST_CAPPED = "原快照的提交列表达到平台上限，可能还有未返回的提交。"
