"""Tool suite assembly."""
from __future__ import annotations

from typing import Any, Dict

from .registry import Tool, ToolContext, ToolRegistry, ToolResult, fail, ok

__all__ = ["Tool", "ToolContext", "ToolRegistry", "ToolResult", "ok", "fail", "build_registry"]


def build_registry(services: Dict[str, Any]) -> ToolRegistry:
    from . import (
        autonomy_tools,
        business_tools,
        document_tools,
        email_tools,
        file_tools,
        integration_tools,
        intelligence_tools,
        os_tools,
        personal_tools,
        selfedit_tools,
        skill_tools,
        system_tools,
        web_tools,
        workspace_tools,
        work_tools,
    )

    registry = ToolRegistry(ToolContext(**services))
    for module in (system_tools, file_tools, web_tools, work_tools, personal_tools,
                   integration_tools, intelligence_tools, os_tools, selfedit_tools,
                   workspace_tools, business_tools, autonomy_tools, skill_tools,
                   document_tools, email_tools):
        module.register(registry)
    return registry
