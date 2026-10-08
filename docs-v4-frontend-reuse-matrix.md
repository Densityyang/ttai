# V4 frontend reuse matrix

This is an integration map for the existing `tt-intelligent-main/tt-admin`
Vue product. It is not a replacement frontend and does not modify the ignored
reference snapshot.

| Old path | Old responsibility | V4 target responsibility | Decision | API/state change | Visual reuse | Migration risk |
| --- | --- | --- | --- | --- | --- | --- |
| `src/views/AiAssistant/components/AiAssistant.vue` | Assistant drawer/pinned chat, prompts, streaming states | QUERY/ANALYZE/BUILD entry, run metadata and mode-switch actions | KEEP + ADAPT | Thread remains stable while each mode switch creates a new server run | Reuse assistant shell, messages, drawers, tags and loading states | Medium: old session model assumes one undifferentiated run |
| `src/views/AiAssistant/api/sse.ts` | OpenAI-style `choices[].delta.content` parser | V4 `metadata -> block -> done` parser | ADAPT | Store server `thread_id`, `run_id`, modes and provenance from metadata | Reuse stream lifecycle, stop/error handling and auth refresh | High: old payload shape must not silently fall back |
| `src/views/AiAssistant/api/types.ts` | Legacy chat request/event types | V4 QueryRequest/QueryResponse, RunEnvelope metadata and canonical blocks | ADAPT | Add requested/effective mode, switched lineage and authority provenance | Keep existing TypeScript contract style | Medium |
| `src/views/AiAssistant/hooks/useChatSession.ts` | Persistent chat sessions keyed by chat/thread | Thread plus multiple run records | ADAPT | Add run history: run id, requested mode, effective mode, switch lineage, provenance | Keep local session list and message persistence | High: do not equate session id with run id |
| `src/views/AiAssistant/components/blocks/BlockRenderer.vue` | Routes text/metric/chart/table/image, unknown fallback | Routes all V4 canonical public blocks | EXTEND | Consume server capabilities manifest and fallback envelope | Keep existing renderer and block spacing | Medium |
| `src/views/AiAssistant/components/blocks/*BlockView.vue` | Existing public block views | Retain existing views and add clarification, plan card, mode suggestion, conflict, provenance and definition views | KEEP + EXTEND | Render strict V4 fields; never infer authority | Reuse Element Plus cards/tags/dialogs | Medium |
| `src/config/axios/*` and auth store | Axios base URL, auth token and refresh | Same-origin/proxy V4 calls and BUILD mutation headers | KEEP + ADAPT | Preserve auth/token flow; send only server-returned BUILD references | Reuse existing request/error conventions | Low/medium |
| `vite.config.ts` | Dev proxy for `/ai` and `/api` | Proxy `/api/v2/nl2sql/**` to FastAPI | ADAPT | Keep old proxy only during migration; V4 is explicit | Reuse Vite development workflow | Low |
| `src/router/index.ts` and existing admin shell | Product navigation/layout | Add Definition/Library/Conflict workbench routes | EXTEND | Workbench uses current server APIs and exact BUILD headers | Reuse Layout, menu, tables, forms, dialogs and theme | Medium |

## Required V4 workbench seams

- Definition workbench: typed form for the frozen repair-service calculation,
  separate lifecycle axes, exact-version execute, revision and publish actions.
- Catalogue/library workbench: exact install, star, explicit upgrade,
  certification, withdrawal warning/ack and lineage/fork views.
- Conflict workbench: comparison plus explicit selection; no star/cert/newest
  automatic winner.
- Assistant/workbench bridge: BUILD metadata from the assistant is the only
  source of `X-TT-Build-Thread-ID` and `X-TT-Build-Run-ID`.

## Current delivery boundary

The backend contract and demo profiles are implemented in this repository. The
actual Vue integration remains pending a writable `tt-admin` repository; the
present `tt-intelligent-main/tt-admin` tree is reference-only and has no Git
metadata or installed dependencies. No greenfield UI was created here.
