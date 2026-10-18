import os
from pathlib import Path

# Load a local .env (KEY=value lines) without adding a dependency; real env vars win.
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

FALKORDB_URL = os.environ.get("FALKORDB_URL", "falkor://localhost:6380")
GRAPH_NAME = os.environ.get("SEISMO_GRAPH", "seismo")
USER_AGENT = os.environ.get("SEISMO_UA", "Seismo/0.1 (Graph Hacks 2026 hackathon project; contact via github)")
STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"
# Only article-namespace human edits on Wikipedia projects.
KEEP_TYPES = {"edit", "new"}
RETAIN_SECONDS = int(os.environ.get("SEISMO_RETAIN", 6 * 3600))  # how much edit history the graph keeps
WINDOW_SECONDS = int(os.environ.get("SEISMO_WINDOW", 20 * 60))   # tremor detection window
MODEL_ANALYST = os.environ.get("SEISMO_MODEL_ANALYST", "claude-opus-5-5")
MODEL_SCOUT = os.environ.get("SEISMO_MODEL_SCOUT", "claude-sonnet-5-5")
