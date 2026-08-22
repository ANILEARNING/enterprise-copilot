# Security Rules

- Never commit or expose secrets.
- Validate all inputs with Pydantic where applicable.
- Sanitize filenames and uploaded files.
- Restrict local code execution to a temporary workspace.
- Apply timeouts and output limits.
- Treat local subprocess execution as development-only, not a production sandbox.
- Require HITL for configured risky actions.
