# 写稿行为验收

修改 SKILL.md 的归纳、审稿规则后使用。固定证据位于 [fixtures/report_cases.json](fixtures/report_cases.json)，项目与编号均为虚构。
这组验收评估事实、主题和状态，不要求逐字匹配参考文案，也不把采集器单元测试通过视为写作能力通过。

## 操作方法

1. 每次只给写稿 agent 一个 case 的 `request`、`evidence`、可选的 `context`、日期，以及 SKILL.md、REFERENCE.md。这些是合成输入，不访问真实工作数据或联网；文件修改案例按 `context` 在临时目录准备指定文件。不要额外加入“必须询问”“必须采集”等会替 skill 作决定的指令。
2. 把产出的报告和“报告句子 → 证据 ID”核对表保存到临时目录。核对表是评审材料，不写入正式报告。
3. 生成后再读取该 case 的 `criteria`，逐项评审事实支持、主题关联、回退处理和验证范围。记录所用模型、skill 版本和失败项，不能提前给出参考答案。
4. 若发现失败，修正规则后用相同原始输入重跑；独立 agent 可用且已获授权时，可由独立 agent 生成并由另一方评审。

通过条件：全部 case 的 criteria 满足；没有无来源的项目、合并/上线状态、测试结果、性能收益或计划，也没有超出请求的采集、落盘或重复确认。

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
