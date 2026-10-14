# 飞书渠道手动同步

在“公共渠道 → 从飞书同步渠道 → 飞书连接设置”填写应用 App ID、App Secret 和包含 `table=` 的表格链接。支持飞书 `base` 与 `wiki` 链接。应用须拥有多维表格读取权限，并取得该表格及所需字段的读取授权；知识库链接还需要读取知识库节点。App Secret 和导入的 API Key 使用公共渠道库的配套密钥加密，保存后不回显。更换应用须重新输入 App Secret；同应用留空保留。

点击“手动同步”才读取飞书。保存设置、打开页面和刷新状态均不联网。同步使用飞书官方应用鉴权、知识库节点解析、字段列表及记录查询接口，不写表格，不调用 New API 管理接口，不请求模型、不创建测试目标或计划。

| 表格列 | Eval 字段与规则 |
| --- | --- |
| 是否在线 | 单选，仅“已上线”且 Key 非空进入候选；“已下线”“待确认”及上线但 Key 清空的已绑定记录会停用 |
| apikey | 文本，去除首尾空白；不能为空且须符合公共渠道密钥格式 |
| baseurl | 超链接或文本，使用链接地址；须是无凭据、查询与片段的完整 HTTP(S) URL |
| 渠道名称 | 文本，对应渠道名称；空值按现有公共库规则使用主机名 |
| 模型家族 | 文本，对应分类，不作为具体模型 ID |
| 倍率 | 数字，直接使用采购倍率，必须大于 0 且不超过 1000 |

“渠道网址”和隐藏的后台令牌列不读取。同步不受视图筛选影响：读取同一数据表的上述六列，再在 Eval 筛选。倍率不推算、不默认填 1；缺项或无效记录列为待补，已绑定的无效记录保留上次资料。

同步由表格身份与记录引用定位 Eval 自身的稳定渠道 ID；不凭显示名称合并，不生成 New API 渠道身份。换 Key、名称、家族、地址、倍率时更新原渠道，并保留本地备注、协议资料、目标引用和历史。相同连接在表内重复或公共库已有其他记录时，提示冲突且不接管既有渠道。表格负责同步字段，本地编辑这些字段会在下次有效同步时被表格值覆盖。

最多读取 20 页、10000 条记录，单页上限 4 MiB，总读取时限 90 秒。所有页及字段类型验证通过后才以一个本地事务应用；凭据失败、分页循环、重复记录 ID、中途超时或设置变更都不会提交半份结果。同步租约防止重复提交；旧任务不能覆盖新设置。失败保留上次成功时间和结果。表格删除记录、过滤权限变化导致未读到记录时保留原渠道，并提示数量；下线请使用“是否在线”。换表只管理新表，不批量删除旧来源渠道。

接口：`GET/PUT /api/registry/feishu/settings` 读取或版本化保存设置，`POST /api/registry/feishu/sync` 接收 `{"confirm_read":true}`。只有脱敏统计、固定错误消息和记录引用进入同步结果；不保存原始飞书响应、API Key 或应用访问 Token 的明文。

只读检查：`python -B scripts/inspect_feishu_channels.py`，可选 `--data-dir` 指定已授权的渠道数据目录。检查不解密、不初始化、不联网。完整 Mock 验证复用 `scripts/verify_admission.py --output <仓库外新证据目录>`；新 Python 回归由 `test_all.py` 发现，新浏览器组及检查组在 `scripts/test_manifest.py` 登记。真实飞书授权和读取成功必须单独核验，Mock 通过不代表已经同步生产表格。

官方契约：[记录查询](https://open.feishu.cn/document/docs/bitable-v1/app-table-record/search)、[知识库节点](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/get_node)、[应用访问凭证](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)。
