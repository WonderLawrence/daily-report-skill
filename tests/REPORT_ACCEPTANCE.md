# 写稿行为验收

修改 SKILL.md 或 references/ 中的采集、交互、归纳和审稿规则后使用。固定证据位于 [fixtures/report_cases.json](fixtures/report_cases.json)，项目与编号均为虚构。
这组验收评估对象选择、事实、主题、状态和可读性，不要求逐字匹配参考文案，也不把采集器单元测试通过视为写作能力通过。

## 操作方法

1. 每次只给写稿 agent 一个 case 的 `request`、`evidence`、可选的 `context`、日期，以及 SKILL.md 的路径，允许按入口链接读取 references/ 中的对应文档。这些是合成输入，不访问真实工作数据或联网；文件修改案例按 `context` 在临时目录准备指定文件。不要额外加入“必须询问”“必须采集”等会替 skill 作决定的指令。
2. 保存交互记录；应先选择对象的案例，在尚未收到答复时结束该次评估，检查是否停留在候选选择，不能替用户自动回复。已选对象的续写由独立 case 提供上下文。
   产出报告时，把报告和“报告句子 → 证据 ID”核对表保存到临时目录。核对表是评审材料，不写入正式报告。
3. 生成后再读取该 case 的 `criteria`，逐项评审事实支持、主题关联、回退处理和验证范围。记录所用模型、skill 版本和失败项，不能提前给出参考答案。
4. 若发现失败，修正规则后用相同原始输入重跑；独立 agent 可用且已获授权时，可由独立 agent 生成并由另一方评审。

通过条件：全部 case 的 criteria 满足；没有无来源的项目、合并/上线状态、测试结果、性能收益或计划，也没有超出请求的采集、落盘或重复确认。

## 可读性验收

[report_readability.json](fixtures/report_readability.json) 从实际反馈抽取了两类合成场景：多个 PR、实现细节和不同状态混在一起的完整材料；缺少动机且用户要求简短的记录。沿用上面的隔离执行方法，写稿 agent 只收到请求、上下文和证据，不提前看到 criteria 或改写答案。

评审者先只读生成的正文，不打开证据链接，尝试复述“各项工作完成了什么、重点在哪里”。再对照原始材料核对以下内容：

- 首句能交代工作对象和问题或变化；缺少背景证据时没有自行补写动机。
- 主要成果突出，辅助修改简述；句子简单连贯，必要术语与实际行为连起来，英文专有名词沿用项目组惯例。
- 进度与验证仅在影响理解时补充；提及时归属于对应事项，不混用状态或扩大验证范围。
- 简化后仍保留影响结论或使用的兼容性、生效条件和风险；用户要求的技术名称与格式仍完整。

记录支持每项判断的原文片段及未通过之处。字数、条数、术语数量和关键词匹配不能代替这项评审。作为回归，另跑原有 `provided_evidence_custom_format` 与 `failed_then_scoped_pass`，检查简化是否误删用户指定名称或扩大验证范围。

固定事实与状态出错，或读者仍需从证据中补齐核心关系，均应修改后重跑原始输入。通过只代表本轮案例的实际输出，不保证今后的任意报告都容易理解。

2026-11-15 的[可读性执行记录](results/report_readability_2026-11-15.json)保留了两轮各四个独立案例的实际输入、回复、逐项评审和规则哈希。首次短条目省略了材料明确给出的未知合并状态；收紧该条规则后，用新的独立 agent 重跑相同输入，四项均通过。记录中的工具摘要由写稿 agent 自报，不是平台原始日志；本轮未重跑其他独立案例或连续多轮对话。

随后按用户反馈，将完成内容、主次和易读性设为重点，进度与验证改为按需补充，并明确保留英文专有名词。上述记录对应旧规则，不代表本次调整已通过写稿行为验收。

## 连续多轮验收

[fixtures/report_conversation.json](fixtures/report_conversation.json) 定义一段连续对话。与上面的独立 case 分开运行：

1. 创建独立临时目录，只向测试 agent 提供第一轮 `request`、日期和 `initial_evidence`，标明证据是采集器产物；不提供 criteria、未来轮次或期望答案。
2. 保持同一个 agent / 对话上下文，每次完成回复后再交付下一轮 request 和 evidence_additions。初次采集已有预置产物，不访问真实用户数据或网络。
3. 完整保存每轮输入、输出及工具行为，再由评审者对照该轮 criteria 检查。不将手工注入“已确认状态”的独立 case 冒充连续对话测试。
4. 检查首次选择与文档补问、只回答对象时的独立状态、文档合并、重采新增候选及排除状态继承。复跑时保留失败记录，修正后从第一轮重新开始。

模型行为验收和脚本单元测试分别报告结果，不以脚本的测试数量代替多轮验收。PR API、严格项目范围和离线回退由 test_prs.py、test_scope.py 的本地/模拟测试验证。

重构前的[四轮实际执行记录](results/report_conversation.json)保存了输入、回复、工具记录、被测文件哈希和逐轮评审，四轮均满足对应判据。
历史记录保留当时的文件名和哈希，不代表当前版本已通过；重构后需另存执行记录。每次合成对话验证不代表 20 个独立 case 全部执行，也不验证真实 GitHub 账号连通性。

结构重构后的[四轮执行记录](results/report_conversation_refactor.json)使用同一组原始输入和新的独立 agent，从入口自行读取参考文档；四轮均满足对应判据。

2026-11-15 修复后的[四轮执行记录](results/report_conversation_2026-11-15.json)使用新的独立 agent 连续执行，四轮均通过。记录包含实际输入、回复、工具行为摘要、来源核对和当前规则哈希；另记载 149 项脚本测试通过，覆盖分页与截断、无提交 PR 详情、显式作者筛选、脱敏及安装同步。两类验证的范围分别记录，不验证真实 GitHub 账号连通性。

随后修复 Codex `response_item/message` 文字漏采及诊断缺口，新增[脚本回归记录](results/codex_message_parsing_2026-11-15.json)，155 项测试通过。上面的四轮记录和代码哈希仍保留其执行时的版本；本次解析器修复未重新执行写稿行为验收。

按重构阶段的约定，现已移除 `event_msg` 文字兼容，统一从 `response_item/message` 提取文字。[本次脚本回归](results/codex_response_items_only_2026-11-15.json)共 157 项通过；旧格式只诊断、不提取，正常采集用例已切换为当前格式。此前记录中的兼容行为仅代表当时版本。

实际运行反馈的[修复与回放记录](results/feedback_replay_2026-11-15.json)记录了 169 项脚本测试和真实来源的只读回放：剔除 24 条注入请求、消除 8 段会话的错误关联、修正 Claude 请求副本导致的 628 条误报，并用已保存的响应验证 6 条 commit message 回退。另 2 条缺少回放响应，保留为 `partial`，不推断线上不可用。未重跑写稿行为验收，历史报告与快照未修改。

随后新增 JSON 回执与分页读取入口，[读取接口验证记录](results/evidence_reader_2026-11-15.json)包含 180 项脚本测试及真实快照回放：10 个 PR 正文、12 段会话的 750 条事件定位、最长 55,843 字符事件均可完整读取或无损拼回。字段拼写错误、对象不存在和单项超预算会明确报错。本轮验证读取与交付接口，未重新执行写稿行为验收。

引用某次结果前，先在工作区核对它的规则哈希；不一致表示记录对应旧版，需要复跑，不能沿用通过结论：

```bash
python3 - <<'PY'
import hashlib, json
from pathlib import Path
record = json.loads(Path('tests/results/report_conversation_2026-11-15.json').read_text())
stale = [name for name, expected in record['tested_sha256'].items()
         if not Path(name).is_file() or hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected]
print('需重新验收：' + ', '.join(stale) if stale else '规则与本次验收记录一致')
raise SystemExit(bool(stale))
PY
```

## 参考表述与核对示例

以下只供生成后的人工评审，不作为输入。

### revert

尝试增加低费率交易拒绝规则，随后撤回。会话分析认为该链 base fee 固定，不适合使用动态拒绝阈值；最终保留最低协议费用配置默认值对齐。

核对：尝试与撤回 C1/C2；分析理由 S1；保留方案 C3。不能写“拒绝规则已上线”。

### investigation_only

评估了全局锁与分片锁的取舍：前者实现简单但存在串行化限制，后者增加一致性处理。讨论建议先收集竞争数据，当前尚未实施。明日计划：暂无明确计划。

核对：评估范围 S2；取舍与状态 S3。建议收集数据不等于用户已安排明日执行。

### cross_repo

围绕 RPC v2 升级，服务端增加响应字段，SDK 补齐解析，文档同步更新调用示例。三处已本地提交，等待联调，尚未合并。

核对：共同目标 S4；三处改动 C4/C5/C6；联调与合并状态 S5。以同一主题描述配套关系。

### failed_then_scoped_pass

修正邮箱匹配后，相关 3 项测试通过。此前全量测试有 2 项失败，尚无修复后全量重跑结果。

核对：修复 C7；局部命令与结果 T3/T4；此前全量失败 T1/T2。S6 的“都通过”不能扩展 T4 的验证范围。

## 范围、交互与格式案例

这些案例同时检查执行行为和报告内容；不能仅凭最终文字正确判定通过。

| Case | 需要观察的行为 |
|---|---|
| provided_evidence_custom_format | 使用完整材料直接回复两列表格，完整保留长技术名称和状态限定；不采集或落盘。 |
| polish_only | 只润色所贴文本，不追问日期或配置，不扩大验证范围，不附无关采集统计。 |
| partial_source_known_identity | 沿用已确认身份，写有据的修复并说明会话来源缺口；不因配置缺失重复确认。 |
| revert_without_reason | 说明已回退，允许省略未知原因和替代方案，不为叙述完整而编造。 |
| cross_day_without_activity | 说明所给材料缺少当日活动记录，不跨日期归入事件，也不补写“继续”。 |
| authorized_report_update | 在临时目录更新指定文件，保留其他内容，不再次询问已经授予的覆盖授权。 |
| discovered_targets_need_selection | 自动发现的 PR 和会话先批量列候选，同次补问范围外文档产出；无答复时不深入分析待选项或生成报告。 |
| resume_with_selected_targets | 沿用所选编号，只总结选中 PR 的区间增量和会话，不重复确认，不纳入排除项。 |
| selected_session_fragment | 明确指定的片段直接分析，不扩展到同一会话的其他话题或时段。 |
| new_candidate_after_selection | 重写时保留既有选择；新增候选未选前不分析，不阻塞已选事项，也不重问已排除项。 |
| empty_sources_ask_documents | 未采到记录时主动询问范围外文档产出，不断言没有工作，不展示空选择题。 |
| supplement_external_document | 纳入用户明确补充的文档，只归纳本人本期贡献，不将历史正文或方案写成已实现成果。 |
| no_extra_documents_already_confirmed | 沿用没有补充文档的答复，继续写已选有据内容，不因来源缺口重复追问。 |
| auto_evidence_not_user_selection | 自动获得完整证据仍先确认对象并补问文档；“直接回复”和项目名不代表全选。 |
| explicit_automatic_selection | 遵循用户明确的自动筛选、无需询问要求，直接写有据内容。 |
| screen_uninformative_session_index | 初筛后只列可辨认工作的会话；不询问空索引，也不漏掉仅有分析或工具产出的续接会话。 |

## 评审记录模板

| Case | 事实与状态 | 主题与格式 | 范围与交互 | 失败说明 |
|---|---|---|---|---|
| revert | | | | |
| investigation_only | | | | |
| cross_repo | | | | |
| failed_then_scoped_pass | | | | |
| provided_evidence_custom_format | | | | |
| polish_only | | | | |
| partial_source_known_identity | | | | |
| revert_without_reason | | | | |
| cross_day_without_activity | | | | |
| authorized_report_update | | | | |
| discovered_targets_need_selection | | | | |
| resume_with_selected_targets | | | | |
| selected_session_fragment | | | | |
| new_candidate_after_selection | | | | |
| empty_sources_ask_documents | | | | |
| supplement_external_document | | | | |
| no_extra_documents_already_confirmed | | | | |
| auto_evidence_not_user_selection | | | | |
| explicit_automatic_selection | | | | |
| screen_uninformative_session_index | | | | |
