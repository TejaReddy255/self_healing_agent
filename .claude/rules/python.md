---
paths:
  - "**/*.py"
---

# Python rules
- Target the project's declared Python version.
- Use type hints for public interfaces and meaningful internal boundaries.
- Prefer `pathlib`, context managers, explicit exceptions, and structured logging.
- Async code must not call blocking I/O on the event loop without isolation.
- External calls require timeouts.
- Pydantic/dataclasses/TypedDict should be used where they materially improve contracts.
- Keep import-time side effects minimal.
