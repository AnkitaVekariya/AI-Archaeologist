---
type: llm
weight: 1
---

The trace reaches AuthService.login, the user lookup with prisma.user.findUnique, the password check, and the JWT signed with JWT_SECRET.
