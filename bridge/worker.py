"""Entry point: `python -m bridge.worker` runs the queue worker on its own."""
from .service import run_worker

if __name__ == "__main__":
    raise SystemExit(run_worker())
