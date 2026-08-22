# Enterprise Copilot — Claude Code Instructions

## Mission
Build and maintain a minimal, reliable Enterprise Copilot SaaS that produce $100 revenue.

## Architecture
FastAPI + Pydantic backend; HTML/CSS/JS/Bootstrap SPA frontend.
Keep files and abstractions minimal.

## Agent Framework
v1 uses AutoGen only through `AgentOrchestrator`.
Future MAF integration must replace the implementation, not the application contract.

## Workflow
1. Read relevant project docs, rules, agents and skills before changing code.
2. Make the smallest coherent change.
3. Validate imports, APIs and UI.
4. Run tests/smoke checks before completion.
5. Never claim incomplete work is complete.

## Safety
Local code execution is development-only, not a secure sandbox.
Never expose secrets, `.env`, credentials or host-sensitive files to generated code.
