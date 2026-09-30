---
paths:
  - "**/prompts/**"
  - "**/*prompt*.py"
  - "**/*prompt*.ts"
---

# Prompt rules
- Give prompts stable identifiers/versions.
- Separate system policy from user data.
- Clearly delimit untrusted retrieved/tool content.
- Request structured output for machine consumption.
- Avoid asking for hidden chain-of-thought; request concise rationale/evidence fields when useful.
- Prompt changes that affect behavior require eval/regression updates.
