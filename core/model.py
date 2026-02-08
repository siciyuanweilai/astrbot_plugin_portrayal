from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict


@dataclass(slots=True)
class UserProfile:
    """
    用户画像（领域模型）
    - 存数据库
    - 做二次分析
    - 做画像更新 / 版本化
    """

    user_id: str
    nickname: str
    gender: str

    # ---------- 语义属性 ----------

    @property
    def pronoun(self) -> str:
        """性别代词（用于 prompt）"""
        return "他" if self.gender == "male" else "她"

    # ---------- 持久化友好 ----------

    def to_dict(self) -> Dict[str, Any]:
        """用于 ORM / JSON / KV 存储"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserProfile":
        """从数据库记录恢复"""
        return cls(**data)
