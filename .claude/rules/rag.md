# RAG Rules

The Knowledge/RAG UI must support the complete document lifecycle:

- Add document
- List documents
- View/edit document
- Update document
- Delete document
- Re-index after update

All application operations use POST APIs.

Document changes must update the in-memory index/state immediately in v1.
