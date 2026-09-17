# 常用模型覆盖与定时测试

## 当前任务
在测试平台公共渠道展示统一常用模型、人工获取清单、定时测试接入与实测状态，支持批量加入现有计划及 GPT-6 Responses。项目根 /Users/lmurder/Desktop/api中转站，子项目工作区 模型测试工作台-常用模型，模块 features/model_coverage。分支 feature/channel-model-coverage；基线 3fee8e0ac1aed5d7af0b9c2c6b5d29544ed18a38；正式规则 v1.0。

## 进度
已实现目录、人工获取、映射、派生状态、原子批量排期、单次验证、Responses 与页面。开发端完整 12 组已通过，Python 检查 339 项，覆盖新旧页面浏览器及端到端；结果见外置 full-checks/verification.json。模型列表没有定时拉取或时间过期规则。隔离环境与证据：/Users/lmurder/Desktop/api中转站/中转站极限测试数据/常用模型/20260917-development。

## 下一步
固定候选提交，按正式规则完成独立审查及干净提交隔离验收；必要修复重新验证后提交合并审阅。

## 未定事项
未授权合并、push、发布或真实上游测试。GPT-6 使用现有 gpt-6-astra 预设；GPT-5.6 为 luna、terra、sol。主工作区原有三个未跟踪文档保留。
