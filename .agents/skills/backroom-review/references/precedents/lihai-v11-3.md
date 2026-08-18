---
id: lihai-v11-3
agent_id: b84720a3-da60-4d10-97ca-3fc10866cb61
agent_name: lihai_v11_3
agent_version: 1
resolution: reject
tags: [family-compiler, program, production-engine]
holding: Closed three-family Program compiler serving computed amounts as evidence.
---

lihai_v11_3 is a closed three-family `Program` tree. It classifies the
request into a bench family, computes the amount, and serves that value as
if it were retrieved evidence. That cannot ship against arbitrary production
records.

Contrast [lihai-v11-2.md](lihai-v11-2.md): the prior revision was left up
because the derive path was still prompt grounding. A version bump is not a
new family; read the served `Program` / `try_solve` path.
