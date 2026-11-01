"""Local control plane for opencode-voice: web panel, CLI, and OS service.

The package deliberately never imports ``config`` at module scope: ``config``
parses ``.env`` at import time and raises when mandatory keys are missing, so
a first run with no settings file must still be able to open the panel.
"""

from __future__ import annotations

__version__ = "0.1.1"
