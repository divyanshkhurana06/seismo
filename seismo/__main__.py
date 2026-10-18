"""CLI: python -m seismo ingest [live|replay:path] | detect"""
import sys
from .store import GraphWriter, ensure_schema
from .stream import live_events, replay

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ingest"
    if cmd == "ingest":
        src = sys.argv[2] if len(sys.argv) > 2 else "live"
        ensure_schema(reset="--reset" in sys.argv)
        events = live_events() if src == "live" else replay(src.split(":", 1)[1], speed=float(sys.argv[3]) if len(sys.argv) > 3 else 20)
        w = GraphWriter()
        w.listeners.append(lambda rows: print(f"[ingest] +{len(rows)} (total {w.total})", file=sys.stderr))
        w.run(events)
