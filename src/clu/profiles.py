"""profile 机制：学校/集群定制（公共资源、重操作名单、抢占分区），代码零硬编码。

M1 承载模型与存储；heavy_patterns 由 M2 安全门消费，preempt_partition 由 M3 alloc 消费。
"""

from pydantic import BaseModel, Field


class Profile(BaseModel):
    """挂在连接 profile 字段上的定制配置。"""

    label: str
    public_dirs: list[str] = Field(default_factory=list)
    heavy_guard: bool = True
    heavy_patterns: list[str] = Field(default_factory=list)
    preempt_partition: str | None = None
