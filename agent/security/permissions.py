"""
ToolPermission - Per-user tool permission management.

Controls which tools each user can access based on:
  - Permission levels (admin, user, guest)
  - Tool categories (read, write, execute, network, system)
  - Per-user tool allowlists/denylists
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("agent.security.permissions")


class PermissionLevel(Enum):
    """User permission levels."""

    ADMIN = "admin"    # Full access to all tools
    USER = "user"      # Standard user access
    GUEST = "guest"    # Minimal access (read-only tools)


class ToolCategory(Enum):
    """Tool categories for permission grouping."""

    READ = "read"          # Read-only operations (memory_query, list_files)
    WRITE = "write"        # Write operations (memory_store, write_file)
    EXECUTE = "execute"    # Code execution (execute_code)
    NETWORK = "network"    # Network access (web_search)
    SYSTEM = "system"      # System operations (chat_completion, sub_agent)


# Tool-to-category mapping
TOOL_CATEGORIES: dict[str, ToolCategory] = {
    "memory_query": ToolCategory.READ,
    "memory_list": ToolCategory.READ,
    "memory_forget": ToolCategory.WRITE,
    "list_files": ToolCategory.READ,
    "read_file": ToolCategory.READ,
    "read_files": ToolCategory.READ,
    "get_weather": ToolCategory.READ,
    "memory_store": ToolCategory.WRITE,
    "write_file": ToolCategory.WRITE,
    "execute_code": ToolCategory.EXECUTE,
    "web_search": ToolCategory.NETWORK,
    "chat_completion": ToolCategory.SYSTEM,
    "delegate": ToolCategory.SYSTEM,  # sub-agent delegation (formerly spawn_agent placeholder)
}

# Default permissions per level
DEFAULT_PERMISSIONS: dict[PermissionLevel, set[ToolCategory]] = {
    PermissionLevel.ADMIN: set(ToolCategory),  # All categories
    PermissionLevel.USER: {
        ToolCategory.READ,
        ToolCategory.WRITE,
        ToolCategory.EXECUTE,
        ToolCategory.NETWORK,
        ToolCategory.SYSTEM,
    },
    PermissionLevel.GUEST: {
        ToolCategory.READ,
    },
}


@dataclass
class UserPermissions:
    """Permission configuration for a specific user."""

    username: str
    level: PermissionLevel = PermissionLevel.USER
    # Extra tools allowed beyond the level default (additive)
    extra_allowed: set[str] = field(default_factory=set)
    # Tools explicitly denied (takes priority over everything)
    denied: set[str] = field(default_factory=set)


class ToolPermission:
    """
    Manages tool permissions for users.

    Checks whether a user is allowed to call a specific tool.
    """

    def __init__(self):
        self._users: dict[str, UserPermissions] = {}
        # Global tool denylist (no one can use these)
        self._global_deny: set[str] = set()

    def register_user(self, username: str, level: PermissionLevel = PermissionLevel.USER):
        """Register a user with a permission level."""
        self._users[username] = UserPermissions(username=username, level=level)
        logger.info("Registered user permissions: %s (level=%s)", username, level.value)

    def set_level(self, username: str, level: PermissionLevel):
        """Update a user's permission level."""
        if username in self._users:
            self._users[username].level = level
        else:
            self.register_user(username, level)

    def allow_tool(self, username: str, tool_name: str):
        """Explicitly allow a tool for a user (additive)."""
        if username not in self._users:
            self.register_user(username)
        self._users[username].extra_allowed.add(tool_name)

    def deny_tool(self, username: str, tool_name: str):
        """Explicitly deny a tool for a user (takes priority)."""
        if username not in self._users:
            self.register_user(username)
        self._users[username].denied.add(tool_name)

    def deny_tool_globally(self, tool_name: str):
        """Deny a tool for all users."""
        self._global_deny.add(tool_name)
        logger.info("Globally denied tool: %s", tool_name)

    def add_category_override(self, tool_name: str, category: ToolCategory):
        """Register a tool with an explicit category (overrides TOOL_CATEGORIES).

        Used by MCP integration to grant read-only servers only READ access.
        """
        TOOL_CATEGORIES[tool_name] = category
        logger.info("Category override: %s -> %s", tool_name, category.value)

    def can_use_tool(self, username: str, tool_name: str) -> tuple[bool, str]:
        """
        Check if a user can use a specific tool.

        Returns:
            (allowed, reason)
        """
        # Global deny takes priority
        if tool_name in self._global_deny:
            return False, f"Tool '{tool_name}' is globally disabled"

        # Get or create user permissions
        user_perm = self._users.get(username)
        if user_perm is None:
            # Unknown user — treat as guest
            user_perm = UserPermissions(username=username, level=PermissionLevel.GUEST)

        # Explicit deny takes priority
        if tool_name in user_perm.denied:
            return False, f"Tool '{tool_name}' is denied for user '{username}'"

        # Check category-based permissions
        category = TOOL_CATEGORIES.get(tool_name)
        if category:
            allowed_categories = DEFAULT_PERMISSIONS.get(user_perm.level, set())
            if category in allowed_categories:
                return True, "OK (category allowed)"

        # Check extra allowed list
        if tool_name in user_perm.extra_allowed:
            return True, "OK (explicitly allowed)"

        # Default: deny
        return False, (
            f"Tool '{tool_name}' not permitted for user '{username}' "
            f"(level={user_perm.level.value})"
        )

    def get_user_tools(self, username: str, all_tools: list[str]) -> list[str]:
        """
        Filter a list of tools to only those the user can access.
        """
        allowed = []
        for tool_name in all_tools:
            can_use, _ = self.can_use_tool(username, tool_name)
            if can_use:
                allowed.append(tool_name)
        return allowed

    def get_user_info(self, username: str) -> dict:
        """Get permission info for a user."""
        user_perm = self._users.get(username)
        if not user_perm:
            return {
                "username": username,
                "level": "guest",
                "extra_allowed": [],
                "denied": [],
            }
        return {
            "username": username,
            "level": user_perm.level.value,
            "extra_allowed": sorted(user_perm.extra_allowed),
            "denied": sorted(user_perm.denied),
        }
