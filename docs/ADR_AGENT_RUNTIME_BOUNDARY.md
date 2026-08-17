# Agent Runtime Boundary

Status: accepted

Date: 2026-08-18

## Decision

ProductionRun is the deterministic control plane and the only cross-stage
authority. Its event ledger, artifact graph, approvals, grants, operation
records, and revision contracts cannot be replaced by an Agent checkpoint.

LangGraph is the single Agent runtime for bounded creative subsystems. A
LangGraph checkpoint records recoverable child-run progress only. A stage can
affect ProductionRun only through a verified `StageResult` and immutable
artifact references accepted by its adapter.

Deterministic transformations remain ordinary Python stages. Agents may select
only declared tools and cannot approve cost, issue grants, submit a paid call,
or change a completed predecessor. Cross-stage communication uses versioned
artifacts rather than free-form Agent conversation state.

## Consequences

- Do not add CrewAI, AutoGen, Microsoft Agent Framework, or another Agent
  runtime alongside LangGraph without a new ADR and a demonstrated capability
  gap.
- Keep Provider side effects behind approval-bound, idempotent adapters.
- Treat every Agent manifest, trace, and checkpoint as stage-private evidence,
  not project truth.
- Future CLI, HTTP, WebSocket, and desktop clients use `ProductionService`
  commands and path-free DTOs.
- A distributed workflow engine such as Temporal may be evaluated only when
  multi-worker deployment becomes a real requirement. It would host the
  deterministic control plane rather than replace its domain contracts.

## M4 application

M4.0 voice-script generation is deterministic and therefore does not use
LangGraph, an LLM, TTS, or a Provider. A later voice-director Agent may use
LangGraph for bounded creative decisions. Paid TTS remains a separate milestone
under the existing approval, grant, operation, settlement, and audit boundary.
