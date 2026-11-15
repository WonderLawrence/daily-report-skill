"""Render the collection result into the Markdown evidence pack an agent reads.

One function per section, each returning lines. The wording states the evidence and the
known gaps only; it never decides for the user what counts as an achievement.
Output strings stay in Chinese: they are the deliverable, and the skill documents refer to
these exact section titles.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .analysis import scope_summary
from .diagnostics import (GitDiagnostics, PullRequestDiagnostics, SessionDiagnostics,
                          SourceDiagnostics)
from .evidence import format_counts
from .models import (Commit, DirtyState, Event, FetchResult, Involvement, PullRequest,
                     Report, Scope, Session)
from .text import is_injected, one_line, short_id

ACKNOWLEDGMENTS = {"好", "好的", "收到", "明白", "可以", "继续", "继续吧", "请继续",
                   "完成", "完成了", "谢谢", "ok", "okay", "done", "continue", "thanks"}

# One place for the four Involvement values and their rendered descriptions; keeps the
# contract between discovery (writer) and this section (reader) in a single table.
_PR_STATE: dict[str, str] = {
    Involvement.COMMITS: "本期本人提交 {n} 条",
    Involvement.NO_PERIOD_COMMITS: "本期无本人提交（可能是评审或讨论，需初筛确认）",
    Involvement.UNQUERIED: "未查询提交（超出查询额度）",
    Involvement.FAILED: "提交查询失败，覆盖未确认",
}


def _rel(paths: set[str], cwd: str, limit: int = 4) -> list[str]:
    out = []
    for p in sorted(paths):
        try:
            p = str(Path(p).relative_to(cwd))
        except (ValueError, TypeError):
            pass
        if len(p) < 90:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def _period_word(days: int) -> str:
    return "日报" if days == 1 else "周报" if days == 7 else f"{days} 天汇总"


def coverage_status(diag: SourceDiagnostics) -> str:
    """Turn one source's counts into a sentence; a missing source and "no records" differ."""
    if diag.disabled:
        return "未启用"
    if diag.unavailable:
        return "来源不可用（目录或依赖缺失）"
    if diag.failed_files:
        return "部分失败" if diag.ok_files else "读取或解析失败"
    if diag.has_fetch_gap:
        return "远程同步不完整，使用可读的本地记录"
    return "正常" if diag.has_records else "所选范围无记录"


def event_excerpt(event: Event, limit: int = 260) -> str:
    locator = f"{event.at:%m-%d %H:%M:%S} · {event.source}:{event.line}"
    call = f" · call {event.call_id}" if event.call_id else ""
    return f"{one_line(event.text, limit)} （{locator}{call}）"


def session_index_hint(session: Session) -> tuple[str, str | None]:
    """Find a usable text clue, without deciding work value or dropping evidence."""

    def usable(text: str) -> bool:
        text = text.strip()
        return (bool(text) and not is_injected(text)
                and text.casefold().strip("。.!！?？,，;；:： ") not in ACKNOWLEDGMENTS)

    requests = [text for text in session.users if usable(text)]
    if requests:
        chosen = requests[:1] + requests[-1:] if len(requests) > 1 else requests
        return one_line(" / ".join(chosen), 160), "user_request"
    # A continuation may have no in-window request, or end with an acknowledgment.
    # Use the last substantive statement in the retained in-window events.
    claims = [event.text for event in session.events
              if event.kind == "assistant_statement"] or session.concl
    for text in reversed(claims):
        if usable(text):
            return one_line(text, 160), "assistant_statement"
    return "", None


# ---------------- Sections ----------------
def _header(report: Report) -> list[str]:
    period = report.period
    span = (period.start.strftime("%Y-%m-%d") if period.single_day
            else f"{period.start:%Y-%m-%d} → {period.last_day:%Y-%m-%d}")
    lines = [f"# 工作证据 {span}", ""]
    if report.generated:
        # The pack is a point in time snapshot: later commits and sessions are not in it,
        # and re-running needs --force
        lines += [f"_生成于 {report.generated:%Y-%m-%d %H:%M}（UTC{report.generated:%z}）_", ""]
    return lines


def _scope_section(scope: Scope | None) -> list[str]:
    if scope is None:
        return []
    git_scope = (", ".join(scope.repos) if scope.git and scope.repos
                 else "未采集" if not scope.git else "已启用，但未发现仓库")
    lines = ["## 采集范围",
             "- Git：" + git_scope,
             "- 会话：" + (", ".join(scope.sessions) if scope.sessions else "未采集"),
             "- 当前工作区状态：" + ("已扫描（运行时快照）" if scope.dirty else "未采集")]
    if scope.only_repos:
        lines.append("- 仅关注项目：" + ", ".join(scope.only_repos))
    return lines + [""]


def _source_counts(diag: SourceDiagnostics) -> str:
    """Only the counters that exist for this kind of source; no cross-source zeros."""
    if isinstance(diag, GitDiagnostics):
        return f"；提交 {diag.commits}"
    if isinstance(diag, SessionDiagnostics):
        return (f"；会话 {diag.sessions}；详细摘要 {diag.detailed}，"
                f"摘要省略 {diag.omitted}；范围外会话 {diag.scope_excluded}，"
                f"项目无法确认 {diag.scope_unknown}；提取用户消息 {diag.user_messages}，"
                f"助手文字 {diag.assistant_messages}，文字消息解析不完整 {diag.unparsed_messages}")
    return ""


def _diagnostics_section(diagnostics: dict[str, SourceDiagnostics]) -> list[str]:
    if not diagnostics:
        return []
    lines = ["## 采集诊断", ""]
    for name, diag in diagnostics.items():
        lines.append(f"- {name}：{coverage_status(diag)}；扫描 {diag.scanned}，"
                     f"无已知解析错误 {diag.ok_files}，失败/部分解析 {diag.failed_files}，"
                     f"坏记录 {diag.invalid_records}" + _source_counts(diag))
    git = diagnostics.get("git")
    if git is not None and (git.local_commits or git.commits_without_local_copy):
        lines.append(f"- git 补充：本地扫描到本人提交 {git.local_commits}，"
                     f"其中未在本次 PR 检索结果中找到关联 {git.local_unassociated}（不在证据内，"
                     f"需要时用会话记录或用户说明补充）；本期 PR 提交在本地找不到 "
                     f"{git.commits_without_local_copy} 条（只有平台元数据）")
    return lines + ["计数单位：Git 为仓库，会话来源为文件；成功和失败文件分别计数。"
                    "摘要省略的会话仍在完整索引及结构化证据中。", ""]


def _fetch_section(results: list[FetchResult]) -> list[str]:
    if not results:
        return []
    lines = ["### Git 远程同步", ""]
    for result in results:
        lines.append(f"- `{result.repo_path}`：{result.detail}；检查时间 {result.checked_at}")
    return lines + ["同步失败或跳过时，提交仅反映当前本地引用，远程覆盖未确认。"
                    "fetch 成功也不证明提交已合并、已验证或已部署。", ""]


def _pr_section(diag: PullRequestDiagnostics | None,
                pull_requests: list[PullRequest]) -> list[str]:
    """First level index: the PRs involved this period and how many own commits each yielded."""
    if diag is None:
        return []
    lines = ["### 本期 PR（第一层：involves 本人，按最近更新）", "",
             f"状态：{diag.status}；搜到 PR {diag.prs_found}，"
             f"含本期本人提交 {diag.prs_with_commits}，"
             f"本期无本人提交 {diag.prs_without_commits}；"
             f"查询 {diag.queries}，失败 {diag.failures}，未查询 {diag.unqueried}。"]
    if diag.detail:
        lines.append(diag.detail)
    if diag.out_of_scope:
        lines.append(f"范围外 PR {diag.out_of_scope} 个（仅关注项目限定），未纳入。")
    if diag.unsupported_repos:
        lines.append("本地没有对应仓库的 PR：" + ", ".join(sorted(set(diag.unsupported_repos)))
                     + "；这些提交只有平台元数据，无法在本地核对改动。")
    lines += ["`involves:` 只匹配 author/assignee/mentions/commenter，不含提交作者："
              "他人创建、含本人提交但本人从未留言的 PR 不会出现在这里。"
              "此处不含 Description；选定工作事项后再用 pr_details.py 获取详情。", ""]
    for pr in pull_requests:
        state = _PR_STATE.get(pr.involvement, "未知参与形式").format(n=len(pr.associated_commits))
        lines.append(f"- [{pr.id}]({pr.url})：{one_line(pr.title, 180)}；{state}；"
                     f"读取时状态 {pr.state_at_discovery}；"
                     f"更新于 {pr.updated_at}；获取时间 {pr.retrieved_at}")
        if pr.associated_commits:
            lines.append("  参与线索：" + ", ".join(c.full_sha for c in pr.associated_commits))
    return lines + [""]


def _topic_section(commits: list[Commit]) -> list[str]:
    rows = scope_summary(commits)
    if not rows:
        return []
    lines = ["## 主题候选（按 commit scope 聚合，仅供参考）"]
    for name, total, types in rows:
        lines.append(f"- `{name}`：{total} 条 — " + " / ".join(f"{t} {n}" for t, n in types))
    return lines + [""]


def _commit_flags(commit: Commit) -> str:
    flags = []
    if commit.files is None:
        flags.append("本地未找到该提交，仅平台元数据")
    if commit.revert:
        flags.append("⚠️REVERT")
    if commit.reverted_by:
        flags.append(f"已被 {commit.reverted_by} 回退")
    if commit.reverts_sha:
        flags.append(f"回退 {commit.reverts_sha}")
    if commit.copies:
        flags.append(f"另有 {commit.copies} 个副本")
    return ("  " + "；".join(flags)) if flags else ""


def _authored_note(commit: Commit) -> str:
    """Flag an author/committer gap over a day: rebase or cherry-pick is not new work."""
    if not commit.authored or commit.authored[:10] == commit.at[:10]:
        return ""
    try:
        gap = abs((datetime.fromisoformat(commit.at) -
                   datetime.fromisoformat(commit.authored)).days)
    except ValueError:
        gap = 0
    return f"（作者 {commit.authored[5:10]}）" if gap > 1 else ""


def _commits_section(commits: list[Commit], extras: int) -> list[str]:
    head = f"## A. 本期提交（{len(commits)} 条，来自上列 PR"
    head += f"，已折叠 {extras} 个 rebase 副本）" if extras else "）"
    lines = [head]
    if not commits:
        lines.append("_无提交_")
    by_repo: dict[str, list[Commit]] = {}
    for commit in commits:
        by_repo.setdefault(commit.repo, []).append(commit)
    for repo, items in by_repo.items():
        lines.append(f"### {repo}")
        for commit in items:
            size = ("(改动规模未知：本地没有该提交)" if commit.files is None else
                    f"(+{commit.added}/-{commit.deleted}, {len(commit.files)} files)")
            lines.append(f"- [{commit.at[5:10]}]{_authored_note(commit)} {commit.subject}"
                         f"{_commit_flags(commit)}  {size}")
            source = ", ".join(commit.pr_ids) if commit.pr_ids else "无关联 PR"
            lines.append(f"  证据：commit `{commit.full_sha}` · {commit.at}"
                         f" · 仓库 `{commit.repo_path or repo}` · 来自 {source}"
                         f"（不证明已合并或已验证）")
            if commit.files:
                lines.append("  " + ", ".join(commit.files[:5]))
    return lines + [""]


def _session_index(sessions: list[Session]) -> list[str]:
    lines = ["### 完整会话索引",
             "以下是证据留存索引，不是待选清单。缺少文字线索的项先由 agent 初筛；"
             "无法辨认具体工作时不向用户提问。助手线索仅为自述，仍需核验。"]
    for session in sessions:
        detail = "已展开" if session.detailed else "摘要省略，可回查"
        hint, basis = session_index_hint(session)
        signal = ("请求线索" if basis == "user_request" else "助手线索，未核验"
                  if basis == "assistant_statement" else "需初筛，暂不列候选")
        lines.append(f"- [{detail}；{signal}] {session.agent} · {Path(session.project).name} · "
                     f"{session.start:%m-%d %H:%M}–{session.end:%m-%d %H:%M} · "
                     f"id `{session.id}`" + (f" · {hint}" if hint else ""))
        if session.source:
            lines.append(f"  来源：`{session.source}`；完整事件及分片定位见结构化证据。")
    return lines + [""]


def _session_events(session: Session) -> list[str]:
    """Excerpt the window's events.

    Requests keep head and tail, assistant statements and tool results keep the last few;
    everything else is looked up in the structured evidence.
    """
    lines = []
    requests = [e for e in session.events if e.kind == "user_request"]
    if len(requests) > 4:
        lines.append(f"- 用户请求省略 {len(requests) - 4} 条，见结构化证据。")
        requests = requests[:2] + requests[-2:]
    for event in requests:
        lines.append("- 用户请求：" + event_excerpt(event, 220))
    for event in [e for e in session.events if e.kind == "assistant_statement"][-2:]:
        lines.append("- 助手陈述（未独立核验）：" + event_excerpt(event))
    calls = {e.call_id: e for e in session.events if e.kind == "tool_call" and e.call_id}
    for event in [e for e in session.events if e.kind == "tool_result"][-3:]:
        if event.call_id in calls:
            lines.append("- 对应工具调用：" + event_excerpt(calls[event.call_id]))
        lines.append("- 工具结果（输出摘录，需核对范围）：" + event_excerpt(event))
    return lines + [f"- 共 {len(session.events)} 条原始事件；此处为摘录，"
                    "中间决策及完整输出见结构化证据。"]


def _session_detail(session: Session, single_day: bool) -> list[str]:
    project = Path(session.project).name if session.project not in (".", "/") else "?"
    span = (f"{session.start:%H:%M}–{session.end:%H:%M}"
            if single_day and session.start.date() == session.end.date()
            else f"{session.start:%m-%d %H:%M}–{session.end:%m-%d %H:%M}")
    dup = f" · ↻ 同开场白 {session.dup_n} 段" if session.dup_n else ""
    lines = [f"### [{session.agent}] {project} · {span} · {session.steps} 步 · "
             f"id {short_id(session.id)}{dup}"]
    if session.events:
        lines += _session_events(session)
    elif session.users:
        users = session.users
        if len(users) > 4:  # Long sessions often pivot late: keep head and tail
            users = users[:2] + [f"…（省略 {len(users) - 4} 条）"] + users[-2:]
        lines.append("- 用户：" + " / ".join(users))
    if session.concl and not session.events:
        for text in session.concl[-2:]:
            lines.append(f"- 助手陈述（未独立核验）：{text}")
    if session.tools:
        line = "- 动手：" + ", ".join(f"{n}×{v}" for n, v in session.tools.most_common(5))
        rel = _rel(session.files, session.project)
        if rel:
            line += "；涉及 " + ", ".join(rel)
        lines.append(line)
    if session.related:
        links = "; ".join(f"{c.sha} {c.subject[:60]}" for c in session.related)
        lines.append(f"- 可能相关提交：{links}")
    return lines + [""]


def _sessions_section(sessions: list[Session], truncated: bool, single_day: bool) -> list[str]:
    lines = [f"## B. Agent 会话（{len(sessions)} 段"
             + ("，部分详细摘要省略，索引完整" if truncated else "") + "）"]
    if not sessions:
        return lines + ["_无会话记录_"]
    lines += _session_index(sessions)
    for session in sorted(sessions, key=lambda s: s.start):
        if session.detailed:
            lines += _session_detail(session, single_day)
    return lines


def _dirty_section(dirty: list[DirtyState], stale_days: int) -> list[str]:
    if not dirty:
        return []
    lines = ["## C. 当前未提交改动（运行时快照）",
             f"最近提交距今 ≥{stale_days} 天只表示仓库长期无提交；改动存在时长未知。"]
    for state in dirty:
        if state.age_days is None:
            age, mark = "未知", ""
        else:
            age = f"{state.age_days} 天前"
            mark = "  长期无提交的仓库存在当前改动" if state.age_days >= stale_days else ""
        lines.append(f"- [{state.repo}] {state.files} 个文件改动；最近一次提交 {age}{mark}")
    return lines + [""]


def _writing_hint(period: str) -> list[str]:
    return ["## D. 写稿提示",
            "本包是自动采集结果，不代表用户已确认范围。"
            "下一步按 SKILL.md 的阶段表和 references/interaction.md 初筛、确认工作事项，再分析。"
            f"写{period}前按 references/writing.md 归纳与审稿，证据核对见 references/collection.md；"
            "只写已选对象中由本证据包、关联原始记录或用户补充材料支持的事实。"
            "先检查采集诊断和完整会话索引；助手陈述不能单独证明已验证或已合并。"]


def render(report: Report) -> str:
    single_day = report.period.single_day
    lines = _header(report)
    lines += _scope_section(report.scope)
    lines += _diagnostics_section(report.diagnostics)
    lines += _fetch_section(report.fetch_results)
    lines += _pr_section(report.pr_diagnostics, report.pull_requests)
    lines += _topic_section(report.commits)
    lines += _commits_section(report.commits, report.extras)
    lines += _sessions_section(report.sessions, report.truncated, single_day)
    lines += _dirty_section(report.dirty, report.stale_days)
    lines += _writing_hint(report.period_word or _period_word(report.period.days))
    # Lets a later run detect evidence that disappeared between collections
    lines += ["", format_counts(len(report.commits), len(report.sessions))]
    return "\n".join(lines)
