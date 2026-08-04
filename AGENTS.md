# RAG Project Agent Workflow

Use the 10-agent engineering loop for every code change in this project.

Core rules:
- Keep changes small, readable, secure, and maintainable.
- Preserve existing working behavior unless the requirement explicitly changes it.
- Inspect the current worktree before editing and never overwrite unrelated changes.
- Only the Implementation Agent and Root Cause Patch Agent may edit code.
- Testing, security, documentation, API docs, DB docs, and frontend design docs are validation or documentation roles.
- Failed tests or security findings must go back to the Root Cause Patch Agent for a permanent minimal fix.
- Add useful comments for functions, validations, security controls, edge cases, and non-obvious logic.
- Do not add comments that merely repeat obvious code.

Activation prompt:

```text
Use the RAG Project Agent Workflow from AGENTS.md and docs/ENGINEERING_LOOP.md.
Run the work through the full loop:
1. Implementation Agent
2. Test Validation Agent
3. Root Cause Patch Agent if needed
4. Retest Verification Agent
5. Security Review Agent
6. Final Documentation Agent
7. Commit Message Agent
8. API Documentation Agent
9. Database Documentation Agent
10. Frontend Design Documentation Agent

Only Agent 1 and Agent 3 may change code. All fixes must be permanent root-cause fixes, not temporary workarounds. Preserve old correct behavior and document the final result.
```

See `docs/ENGINEERING_LOOP.md` for the full workflow.
