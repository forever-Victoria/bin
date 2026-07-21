from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from config import settings


@dataclass(frozen=True)
class VoiceRole:
    id: str
    display_name: str
    speaker: str        # 豆包 voice_type（预置 BVxxx_streaming / 复刻 S_xxx / 设计音色）
    instructions: str   # 角色 system prompt


class RoleRegistry:
    def __init__(self, path: str) -> None:
        self._by_id: dict[str, VoiceRole] = {}
        self._load(path)

    def _load(self, path: str) -> None:
        p = Path(path)
        if not p.is_absolute():
            # 相对路径基于进程工作目录（即 bin/ 仓库根）
            p = Path.cwd() / path
        if not p.exists():
            raise FileNotFoundError(f"角色配置文件不存在: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        for item in data.get("roles", []):
            role = VoiceRole(
                id=item["id"],
                display_name=item.get("displayName", item["id"]),
                speaker=item.get("speaker") or settings.tts_voice_default,
                instructions=item.get("instructions", ""),
            )
            self._by_id[role.id] = role

    def all(self) -> list[VoiceRole]:
        return list(self._by_id.values())

    def get(self, role_id: str) -> VoiceRole | None:
        return self._by_id.get(role_id)

    def require(self, role_id: str) -> VoiceRole:
        role = self.get(role_id)
        if role is None:
            raise KeyError(f"未知角色: {role_id}")
        return role

    def resolve(self, role_id: str | None) -> VoiceRole:
        if role_id:
            role = self.get(role_id)
            if role is not None:
                return role
        role = self.get(settings.default_role_id)
        if role is not None:
            return role
        # 兜底：第一个角色
        if self._by_id:
            return next(iter(self._by_id.values()))
        raise RuntimeError("roles.json 中没有任何角色")


try:
    registry = RoleRegistry(settings.roles_path)
except FileNotFoundError:
    # 允许在角色文件缺失时仍能导入（启动时再报错）
    registry = RoleRegistry.__new__(RoleRegistry)
    registry._by_id = {}


def resolve_role(role_id: str | None) -> VoiceRole:
    return registry.resolve(role_id)


def require_role(role_id: str) -> VoiceRole:
    return registry.require(role_id)
