# API

Application behavior uses POST APIs.

## Core
- POST /api/health
- POST /api/chat
- POST /api/guardrails/status
- POST /api/session/start
- POST /api/tools/mcp/status — MCP tool server config + last load result (see docs/tools.md)
- POST /api/observability/status — whether Langfuse tracing is enabled (see docs/observability.md)

## RAG
- POST /api/rag/document/add
- POST /api/rag/document/list
- POST /api/rag/document/get
- POST /api/rag/document/update
- POST /api/rag/document/delete

## Agent Layer
- POST /api/agents/list — registered agents and their trigger keywords/skills
- POST /api/skills/list — registered skills

`POST /api/chat` accepts `agent_mode`; when true the request goes through `AgentOrchestrator` and
the response includes `agent`, `skills`, `provider`, `used_fallback`, `hitl_pending`, `sources`.
When false it's a direct provider call with no agent/skill selection.

## Tools / HITL
- POST /api/tools/code/submit — submit a code snippet; creates a HITL request in `WAITING_FOR_APPROVAL`, does not execute.
- POST /api/hitl/list
- POST /api/hitl/get
- POST /api/hitl/decide — `{request_id, approved}`; approving executes the associated tool and moves the request to `COMPLETED`, rejecting moves it to `REJECTED`.
