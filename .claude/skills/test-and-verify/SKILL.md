---
description: Verify a change with layered tests, static checks, failure-path testing, and runtime verification; report exact evidence and remaining gaps.
---

# Test and verify

Determine the project's actual commands from config/README/build files.

Run, as applicable:
1. formatter check
2. lint
3. type check
4. focused unit tests
5. integration/contract tests
6. build/package
7. migration validation
8. runtime smoke test
9. critical failure-path test
10. relevant AI eval subset

Report a matrix:
`check | command | result | evidence/notes`.

Do not substitute one layer for another. A passing unit suite does not prove deployment works.
If external credentials/services prevent a check, state `NOT VERIFIED` and provide the exact
remaining verification.
