# Acceptance Tests

## Core
- [ ] FastAPI starts
- [ ] SPA loads
- [ ] POST /api/health returns ok
- [ ] POST /api/chat returns a response
- [ ] Mock mode works without credentials

## Phase 0 Guardrails
- [ ] Guardrails status is visible in UI
- [ ] Input guardrail runs before model/agent
- [ ] Blocked input does not execute
- [ ] Output guardrail runs before response
- [ ] No secrets/system prompts are exposed

## RAG / Knowledge
- [ ] Add document from UI
- [ ] List documents
- [ ] Open/edit document
- [ ] Update document
- [ ] Delete document
- [ ] Updated document is immediately marked indexed
- [ ] All RAG APIs use POST

## Agent Architecture
- [ ] AgentOrchestrator remains framework-independent
- [ ] AutoGen-specific code remains behind the orchestrator boundary
- [ ] No unnecessary agents are added

## Session
- [ ] POST /api/session/start returns a session_id
- [ ] POST /api/chat returns and reuses a session_id

## Agent Layer
- [ ] Agent registry selects coding-agent for code-fenced/debug tasks
- [ ] Agent registry selects research-agent for knowledge/document questions
- [ ] Agent registry defaults to general for simple tasks
- [ ] knowledge-rag skill retrieves and cites sources from RAGStore
- [ ] Coding agent queues code execution via HITL instead of running it inline
- [ ] Session history is windowed (relevant context), not unbounded
- [ ] Mock mode responds without credentials
- [ ] Configured mode with missing/unreachable provider falls back to mock without crashing

## HITL / Tools
- [ ] Submitting code creates a WAITING_FOR_APPROVAL request and does not execute it
- [ ] Rejecting a request never executes code
- [ ] Approving a request executes it once and attaches a result
- [ ] Code execution is time-limited and output-limited
- [ ] HITL queue is visible in the UI
