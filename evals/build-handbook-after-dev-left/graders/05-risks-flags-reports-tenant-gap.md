---
type: llm
focus:
  source: file
  path: docs/project-brain/risks.md
weight: 1
---

The risks page flags GET /api/reports/revenue (ReportsController) as not protected by TenantGuard even though it reads invoice data that the guarded invoice and payment routes protect.
