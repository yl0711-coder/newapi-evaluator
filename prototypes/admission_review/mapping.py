"""The complete application-owned Feishu field contract."""
from __future__ import annotations


DEFAULT_CHANNEL_FIELD = "渠道"
DEFAULT_GROUP_FIELD = "测试分组"


def build_feishu_fields(
    channel: str,
    test_group: str,
    *,
    channel_field: str = DEFAULT_CHANNEL_FIELD,
    group_field: str = DEFAULT_GROUP_FIELD,
) -> dict[str, str]:
    """Build exactly the two fields owned by the admission application.

    The human evaluation column is intentionally absent: it belongs entirely
    to the Bitable operator and must never be read or overwritten here.
    """
    if channel_field == group_field:
        raise ValueError("渠道字段和测试分组字段不能同名")
    return {channel_field: channel, group_field: test_group}
