"""全局常量与枚举。

这些值是跨模块的**唯一事实来源**：意图分类、路由、评测、前端都从这里取，
禁止在别处再写一遍字面量。
"""

from __future__ import annotations

from enum import Enum


class Intent(str, Enum):
    """意图类别（PRD §7.1，刻意只保留 5 类 + 兜底，不穷举）。"""

    SPEC = "spec"                 # 尺寸 / 厚度 / 异形 / 最大加工范围
    CONFIG = "config"             # 中空 / 三玻 / Low-E / 特殊编码
    SELECTION = "selection"       # 泳池围栏 / 门 / 天窗该用什么
    AFTER_SALES = "after_sales"   # 裂纹 / 起雾 / 划痕 / 理赔
    LOGISTICS = "logistics"       # 交期 / 运费 / 加急 / 分批
    OTHER = "other"               # 闲聊、打招呼、与业务无关


class Authority(str, Enum):
    """来源权威级别（PRD §8）。"""

    STANDARD = "standard"         # 澳洲标准
    MANUAL = "manual"             # 设备手册 / 能力矩阵 / 报价表
    EXPERIENCE = "experience"     # 客服战地手册 / 历史工单 / 工程师口述

    @property
    def is_authoritative(self) -> bool:
        """权威来源可以直接对客户说；经验来源必须带"通常 / 我们一般"话术。"""
        return self in (Authority.STANDARD, Authority.MANUAL)


class AnswerState(str, Enum):
    """一次问答的终态（PRD §7.3）。"""

    OK = "ok"
    NEED_SLOT = "need_slot"                 # 槽位缺失，需追问
    LOW_CONFIDENCE = "low_confidence"       # 有答案但置信度低
    ESCALATE = "escalate"                   # 转人工
    SERVICE_ERROR = "service_error"         # 依赖不可用，禁止静默返回空答案


class OutputMode(str, Enum):
    """输出形态由**渠道**决定，不由角色决定（PRD §2）。"""

    PHONE = "phone"     # 口语、可念、≤3 行、禁表格
    EMAIL = "email"     # 结构化、可整段复制


class Channel(str, Enum):
    """对话渠道。"""

    PHONE = "phone"
    EMAIL = "email"

    @property
    def output_mode(self) -> OutputMode:
        return OutputMode.PHONE if self is Channel.PHONE else OutputMode.EMAIL


INTENT_LABELS_ZH: dict[Intent, str] = {
    Intent.SPEC: "尺寸与能力",
    Intent.CONFIG: "配置与编码",
    Intent.SELECTION: "场景选型",
    Intent.AFTER_SALES: "售后",
    Intent.LOGISTICS: "交期物流",
    Intent.OTHER: "其他",
}

AUTHORITY_LABELS_ZH: dict[Authority, str] = {
    Authority.STANDARD: "标准",
    Authority.MANUAL: "手册/表",
    Authority.EXPERIENCE: "经验",
}

# 经验类答案必须带的话术前缀（PRD §8 分级规则）
EXPERIENCE_PREFIX = "Usually / In our experience"
