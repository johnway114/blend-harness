from __future__ import annotations

import bpy

from blend_runtime import ProjectContext


def build_scene(context: ProjectContext) -> None:
    """Create the intentionally empty template's explicit scene marker."""
    bpy.context.scene["blend_template"] = "empty"


if __name__ == "__main__":
    context = ProjectContext.from_cli()
    context.reset_scene()
    build_scene(context)
    context.execute_requested_operation()
