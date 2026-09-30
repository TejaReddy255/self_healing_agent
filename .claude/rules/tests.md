---
paths:
  - "tests/**"
  - "**/*test*.py"
  - "**/*.spec.ts"
  - "**/*.test.ts"
---

# Test rules
- Tests should be deterministic and independent.
- Use explicit fixtures instead of production dependencies.
- Assert externally meaningful behavior.
- Include failure paths, not only happy paths.
- Do not weaken assertions just to make a failing test pass.
- Agent tests should assert tool/action boundaries and terminal state.
