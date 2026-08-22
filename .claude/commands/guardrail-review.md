# /guardrail-review

Review the current implementation for Phase 0 guardrails.

Verify:
- input checks run before AI execution
- output checks run before response
- blocked requests do not reach the model
- UI exposes guardrail status
- no secrets/internal prompts are exposed

Fix clear issues and run smoke tests.
