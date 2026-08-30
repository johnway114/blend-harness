"""Blender --python target that executes the authoritative project entry point."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from blend_runtime.context import ProjectContext, _arguments  # noqa: E402


def main() -> None:
    arguments = _arguments()
    runtime = json.loads(Path(arguments.blend_runtime_config).read_text(encoding="utf-8"))
    project_root = Path(runtime["projectRoot"])
    for source_path in [project_root, project_root / "lib"]:
        if source_path.is_dir() and str(source_path) not in sys.path:
            sys.path.insert(0, str(source_path))
    for library in runtime.get("libraries", []):
        library_path = Path(library["path"])
        for source_path in [library_path, library_path / "python"]:
            if source_path.is_dir() and str(source_path) not in sys.path:
                sys.path.insert(0, str(source_path))
    entrypoint = project_root / runtime["config"]["entrypoint"]
    namespace = runpy.run_path(str(entrypoint), run_name="__main__")
    context = ProjectContext._current
    if context is None:
        build_scene = namespace.get("build_scene")
        if not callable(build_scene):
            raise RuntimeError("scene.py did not create ProjectContext or define callable build_scene(context)")
        context = ProjectContext.from_cli()
        context.reset_scene()
        build_scene(context)
    if not context._executed:
        context.execute_requested_operation()


if __name__ == "__main__":
    main()
