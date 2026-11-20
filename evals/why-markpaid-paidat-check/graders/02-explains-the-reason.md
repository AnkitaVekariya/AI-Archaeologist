---
type: llm
weight: 1
---

The answer explains that Stripe retries webhook deliveries, so reconciliation could run twice for the same payment and create duplicates, and that the paidAt check prevents this.
