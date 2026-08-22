# AutoGen / MAF Boundary

AutoGen is the v1 implementation.

Required boundary:

Application → AgentOrchestrator → AutoGenOrchestrator

Do not scatter AutoGen-specific types or calls across routes/services.

Future MAF migration should replace the orchestrator implementation while preserving application contracts.
