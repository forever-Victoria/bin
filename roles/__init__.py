"""角色注册表：加载 roles.json，按 role_id 解析角色。"""
from .registry import VoiceRole, RoleRegistry, registry, resolve_role, require_role

__all__ = ["VoiceRole", "RoleRegistry", "registry", "resolve_role", "require_role"]
