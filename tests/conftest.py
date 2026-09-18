from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(REPO_ROOT / ".env")

os.environ.setdefault("ADMIN_USER_ID", "123456789")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:TEST-TOKEN")
os.environ.setdefault("STT_API_KEY", "test-openrouter-key")
os.environ.setdefault("STT_MODEL", "test/stt-model")
os.environ.setdefault("OPENCODE_BASE_URL", "http://localhost:4096")

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
