"""Runtime injected into Blender's isolated Python path by the Blend CLI."""

from .context import ProjectContext

__all__ = ["ProjectContext"]
__version__ = "1.0.0"
