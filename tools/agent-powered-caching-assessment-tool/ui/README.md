# Console

A React, TypeScript and Cloudscape single-page app for the Agent Powered Caching Assessment Tool API.
It is built around saved databases. With none saved, it opens on Get started, which
lists the PostgreSQL databases discovery found and connects one with a wizard (choose,
sign in, check readiness, save). Otherwise it opens on the databases list. A database
page runs assessments and lists them, and each report opens under its database. The
assistant is a drawer on every page, about the database or assessment on screen, and
also a full page (`#/chat`). The one-off submit form (`#/console/new`) and the list of
every assessment (`#/assessments`) are still there. It talks to the backend only over
HTTP (`/api/v1`).

## Run it

1. Start the API (loopback only, no authentication), from the repo root:

   ```
   ARTIFACT_ROOT=./artifacts AWS_REGION=us-east-1 .venv/bin/python -m src.api.main
   ```

   It listens on 127.0.0.1:8000. Set `API_PORT` for another port. The assistant calls
   Amazon Bedrock with the server's AWS credentials, so the shell that starts the API
   needs credentials with Bedrock access (`PGCA_CHAT_MODEL_ID` picks the model). Without
   them the console still works, and the assistant answers from the report directly.

2. Install, generate the API client, and start the console:

   ```
   cd ui
   npm ci
   npm run gen:api
   npm run dev
   ```

   Open http://127.0.0.1:3000. The dev server forwards `/api`, `/health` and
   `/openapi.json` to the API, so the browser sees one origin. If the API is not on
   port 8000, set `APCAT_API` for both commands, for example
   `APCAT_API=http://127.0.0.1:8001 npm run dev`.

3. Build and test:

   ```
   npm run build
   npm test
   ```

   `npm run preview` serves `dist/` on 127.0.0.1:4173 with the same forwarding.

## The API client

`npm run gen:api` reads the running API's `/openapi.json` and writes
`src/api/schema.d.ts`. `src/api/types.ts` and `src/chat/protocol.ts` only name those
generated types, and the routes are called through `openapi-fetch`, so request and
response shapes are never typed by hand. Fields with a server default are generated as
optional (`--default-non-nullable false`). Regenerate after any backend contract change.

Every form option, default, bound, region rule and limit comes from `GET /capabilities`
at runtime, in the console and in the assistant's confirmation cards.

## The assistant

Transport: `HttpChatTransport` (`src/chat/transport.ts`) over the additive
`POST /api/v1/agent/chat`, which streams server-sent events. The model loop runs on the
server and calls Bedrock there. The UI depends only on the `ChatTransport` interface.

Which tool runs where:

| Runs | Tools |
|---|---|
| Server, read-only, through the route functions | `get_capabilities`, `list_assessments`, `get_status`, `get_summary`, `get_report`, `list_report_items`, `ask_report`, `get_sizing`; render: `present_answer`, `open_report` |
| Browser, after an in-chat confirmation of the exact request | `run_preflight`, `start_assessment`, `cancel_assessment`, `rerun_assessment`, `delete_assessment` (type the id to confirm), `whatif_sizing` |
| Browser, as a form card | `request_connection_details`, `request_collection_file` |

A confirmed action is called through the generated client, so its result and
`X-Request-ID` are the API's own. Connection details and collection files stay in the
browser tab's memory; the model only gets an opaque id. Under each answer, the trace
shows which tool ran, where, with what parameters and what came back.

Report answers: the model reads the item through `ask_report` and answers through
`present_answer` with citations. `src/chat/citations.ts` checks every citation against
the item itself and marks one that is not in the report, or that quotes a missing or
withheld value as 0.

The assistant can run several assessments at once (one connection form per database).
Start, rerun and what-if cards have editable options (AI review, model, require the
model, size, price, region, label, sizing settings) and send what the user confirms; the
model is told the values sent. Report tools answer "not ready" with the job's status
while the report is being made, and the chips under the prompt follow the job's live
status and what its report holds. The model picker under the prompt chooses the model
for chat turns (default Sonnet 5.5, from `GET /capabilities`).

Conversations are saved in this browser's local storage (the newest 30) and listed under
Conversations in the side menu. They hold what was shown, including query text, so treat
the browser profile as sensitive. Connection details and collection files are never
saved: a reopened conversation declines what it was waiting on and asks again.

When the model is unavailable, the chat routes a fixed set of requests to the same tools
in the browser (`src/chat/localAgent.ts`) and answers "why" questions with the
deterministic grounding module (`src/agent/grounding.ts`), and says so.

Chat and console share one react-query cache: a job started in either shows in both.

## Security

- The API has no authentication and answers loopback callers only. The dev and preview
  servers bind to 127.0.0.1; do not widen them.
- Every chat turn calls Bedrock with the API server's own credentials. Whoever runs the
  API owns that access and its cost.
- `src/api/auth.ts` is the one place an identity provider (OIDC, SAML or SigV4) plugs in.
  There is no login screen until that is decided. Limits are per server, not per user.
- Query text can hold literal values. The console shows it but never logs it, and the
  server's audit log records a chat turn's message count only.

## Component inventory

| Surface | Files |
|---|---|
| Shell | `App.tsx` (TopNavigation, AppLayout, SideNavigation, BreadcrumbGroup, Flashbar), `theme.ts`, `router.ts` |
| Chat workspace | `chat/ChatPage.tsx` (ChatBubble, Avatar, SupportPromptGroup, PromptInput), `chat/cards.tsx` (status stepper, preflight checklist, report card, answer with checked citations, connection and collection forms, confirmation and outcome cards, tool trace), `chat/databaseCards.tsx` (saved databases, one database, discovery finds, a check's outcome; all redrawn from the API, because the model gets no connection details), `chat/ChatContext.tsx` |
| Chat logic (pure, tested in `chat/chat.test.ts`) | `chat/conversation.ts` (state, save and reopen), `chat/history.ts` (saved conversations), `chat/clientTools.ts` (argument checks and the confirmation plan), `chat/citations.ts`, `chat/sse.ts`, `chat/followups.ts`, `chat/localAgent.ts` |
| Chat plumbing | `chat/transport.ts`, `chat/execute.ts`, `chat/stores.ts`, `chat/protocol.ts` |
| Assessment console | `pages/HistoryPage.tsx`, `pages/NewAssessmentPage.tsx`, `pages/JobOptionsForm.tsx`, `pages/AssessmentPage.tsx`, `monitor/JobMonitor.tsx` |
| Report | `report/ReportView.tsx`, `report/Sections.tsx`, `report/ItemTables.tsx`, `report/ItemDetail.tsx`, `report/ExpandableList.tsx`, `report/Sizing.tsx`, `report/common.tsx` |
| No-model answers | `agent/grounding.ts` (tested in `agent/grounding.test.ts`), `agent/transport.ts`, `agent/reportSource.ts`, `agent/AgentContext.tsx` (report cards open the chat on an item) |
| Shared | `api/*` (generated client, errors with code, detail and request id), `components/*` |
