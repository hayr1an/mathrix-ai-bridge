"""Entry point: `python -m bridge.server` runs the API and the worker."""
from .service import main

if __name__ == "__main__":
    raise SystemExit(main())
