# Ask AI (`ask`, g k)

The AI analyst. The owner types a question (command line `ASK`, or `<SECURITY> ASK` to ask about a security); the answer streams in, labelled **AI**, with the model, effort, tokens and exact cost, and every statement cites the tool results it used. A citation chip (`R1`, `R2`, ...) opens that tool result in the side panel exactly as the model saw it (inside its untrusted-data markers). Statements without a source are greyed and flagged "no source"; numbers that do not appear in the cited results are flagged "not in source". Missing values are an em dash with their reason (`ui/unavailable.tsx`).

Routes: `#/ask` (new conversation), `#/ask/<thread_id>` (a conversation; follow-up questions carry its earlier questions and answers).

Backend (`backend/src/unknown/api/routes/ai.py`, owner session; POSTs need the CSRF token):

| Call | Use |
|---|---|
| `GET /api/ai/status` | AI on or off and why, the model and effort from configuration, caps, spend today |
| `GET /api/ai/threads`, `GET /api/ai/threads/<id>` | Conversations and their questions, answers and job states |
| `POST /api/ai/ask` | `{question, thread_id?, security?}`; 409 when paid calls are off or the thread's last question is still running |
| `GET /api/ai/stream?job=<id>` | SSE: `job`, `status`, `turn`, `thinking`, `text`, `segment_end`, `reset`, `tool`, `tool_result`, `usage`, `answer`, `done`; resumes after `Last-Event-ID` |
| `POST /api/ai/jobs/<id>/cancel` | The Cancel button: queued questions stop at once, running ones at the next stream event |
| `GET /api/ai/results/<job>/<ref>` | The side panel |

Answers are advisory. Nothing in this workspace, or in the analyst's tools, can place, change or cancel an order, start a session or move money. The workspace is lazily loaded; only `index.ts` (the definition and the `ASK` function) is in the initial bundle.
