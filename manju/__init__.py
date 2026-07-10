"""manju-tool: AI 漫剧生成流水线"""

import sys


def _make_console_output_resilient() -> None:
    """Keep decorative Unicode from crashing legacy Windows consoles."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_make_console_output_resilient()

__version__ = "0.6.0"
