# Assistant, retrieval timing and theme verification — 2026-11-01

Branch: `codex/astraroll-v2-final-product`. The current application remains
http://127.0.0.1:8000/. No tenant orders, rollups, outbox records, migrations or
Developer B aggregation modules were changed by this task.

The existing Groq key was valid: an authenticated provider request returned HTTP
200 using `openai/gpt-oss-120b`. The assistant previously used `os.environ`, while
the local key was stored in `.env` and never exported by the launcher. It silently
fell back to a deterministic answer. Provider credentials/model now use the same
Pydantic settings path as the rest of the backend; the key is a redacted SecretStr.
The key stays server-side and is not copied to browser code, documentation or Git.

Retrieval is required before synthesis, following Groq's
[local tool-calling contract](https://console.groq.com/docs/tool-use/local-tool-calling).
The server pins tenant, selected currency and supplied date window. Tool results
include actual persisted freshness metadata. Database reads run off the async
event loop. Provider authentication, rate-limit, connection and empty/ungrounded
response failures produce a clearly labeled verified fallback, not a disguised
LLM success. Provider response bodies and credentials are not logged.
An additional live check reproduced Groq's `tool_use_failed` error when the model
requested status breakdown after summary but the backend prematurely forced a
final answer. The assistant now permits follow-up allowlisted lookups, bounded
to five model turns and eight total tool calls. A regression test covers this
exact summary → status → answer sequence and another covers repeated-tool limits.
The chat now scrolls only its message pane; new answers no longer scroll the whole
dashboard past the assistant. Page switches return to the top, and the chat input
fits within the available viewport.

Three real authenticated requests through port 8000 returned Groq answers:

| Question | Provider/model | Observed complete response time |
| --- | --- | ---: |
| Total shipped revenue and order volume (summary + status lookups) | Groq / openai/gpt-oss-120b | 2881.01 ms |
| Order status breakdown | Groq / openai/gpt-oss-120b | 1706.55 ms |
| Aggregate freshness | Groq / openai/gpt-oss-120b | 1666.04 ms |

These are three observed requests, not a percentile benchmark. Revenue and order
count in the retrieved assistant data exactly matched the summary API. Answers
carried the real tenant revision and materialization timestamp. Local detailed
evidence is `.runtime/provider-live-proof.json`; it contains no provider key.
The tenant baseline can change through owner-authorized live imports. No new
global order-count audit is claimed by this UI task.

Command Center now displays **Last retrieval: N ms** beside **Shipped Sales & Volume
Trends**. It measures the actual trends HTTP round trip through response decoding
with `performance.now()`. It is explicitly not SQL execution time. A failed
refresh does not replace the last successful result with a fake new measurement;
tenant/filter changes clear the previous scope through the existing polling hook.

Advanced Analytics had conflicting `bg-white` and forest background utilities.
The white alternatives were removed from its panels, controls/tables use the
forest palette, and chart summary cards use matching forest surfaces. The chart's
all-order ratio is labeled **Sales / Total Orders**, distinct from Command Center's
shipped-order AOV.

**Tenant Ops & Freshness** is retained as **Data Health**. It is useful for diagnosing
missing analytics: it shows database connectivity, worker heartbeat, unapplied
events, canonical projection counts and verified UTC/currency coverage. Its refresh
button now says **Refresh selected tenant**; it never claims to synchronize all
100 databases. The page explains that old source timestamps alone do not imply lag.

Validation: **386 backend tests passed** on isolated PostgreSQL 55434 / Redis 56381
in 106.72 seconds, with one existing Starlette/httpx deprecation warning. Provider
tests mock external calls and explicitly suppress local secrets. **12 frontend
tests**, TypeScript production build, ESLint, Ruff check/format and dependency
audit passed. Visual browser checks cover the forest analytics page, the retrieval
badge, Data Health and an actual assistant response.

Reproduce from the worktree using the isolated test profile documented in
[100-tenant restoration](100-tenant-restoration.md), then run:

```powershell
& D:/cyberforge/.venv/Scripts/python.exe -m pytest -ra
$pythonFiles = @(git ls-files '*.py')
& D:/cyberforge/.venv/Scripts/ruff.exe check @pythonFiles
& D:/cyberforge/.venv/Scripts/ruff.exe format --check @pythonFiles
# In web/:
npm test
npm run lint
npm run build
npm audit --audit-level=low
```

To configure an assistant in another environment, set server-only `GROQ_API_KEY`
and `GROQ_MODEL` in its `.env` or process environment and restart the API process.
Never restart or reset the protected PostgreSQL cluster for this change.

## Greeting correction from the follow-up screenshot

The screenshot showed `hi` falling through to a financial summary after a provider
400. Social/help turns had been forced into analytics tool selection. Greetings,
acknowledgements and help requests now return a clearly labeled local conversational
reply, without an LLM call, analytics read, metric values or fabricated citations.
Unclear fallback requests ask for clarification. A full-match greeting check leaves
`Hi, show me total revenue` on the real analytics path. Provider failure notices now
appear above a fallback reply rather than below a long summary.

The exact screenshot context was checked in the browser and over authenticated HTTP:
Tenant #99 (`0f3acb72-4566-5bfe-8568-0df70c33a4d0`), USD, UTC
[2026-10-30, 2026-11-02). `hi` returned a greeting with no data/citations; `thanks`
and `what can you do?` returned conversational responses. A subsequent revenue
question returned a Groq answer whose retrieved metrics matched the summary API.
Greeting requests still returned 401 without authentication and 403 outside tenant
scope. Local evidence: `.runtime/conversation-live-proof.json`.

Twelve added regressions cover greeting/help variants, no provider/database access,
unknown intent clarification, and a greeting prefix followed by an actual metrics
question. The final full suite passed 377 tests; frontend 12 tests, build and lint
also passed. No tenant data, source PostgreSQL process or worker was reset.

## Current-day HTTP 400 correction

The owner identified the exact failing question: `what day is it today?`. It was
being sent to Groq with required financial tools, despite needing no financial
data. Calendar questions now read the server clock in `ASSISTANT_TIMEZONE`, with
explicit zone labeling, before any analytics/provider call. The local port-8000
launcher sets `Asia/Kolkata`; the general deployment default is `UTC`. The setting
validates an IANA zone. The existing locked tzdata package is now also an explicit
project dependency for Windows and minimal runtime reproducibility. Analytics
windows remain UTC and do not determine the current calendar day.

The provider and fallback now share one analytics-intent boundary. Unrelated
questions ask for clarification without forcing a financial tool. Provider failures
continue to be honest; safe status/error-code/turn diagnostics are logged without
provider bodies, prompts or credentials. This is not a claim that every possible
provider outage has been eliminated.

Authenticated HTTP checks against Tenant #99 and the original three-day window
returned the actual current date, `Sunday, 1 November 2026 (Asia/Kolkata)`, and the
current local time with no provider error and no analytics citations. A revenue
question still returned a genuine Groq response matching the summary API. Calendar
requests retained 401 unauthenticated / 403 cross-tenant denial. Detailed local
evidence: `.runtime/clock-live-proof.json`.

Nine additional regressions cover current date/time under UTC and Asia/Kolkata
across a UTC-midnight boundary, independence from historical analytics filters,
unrelated questions, real sales questions containing `today`, and invalid zones.
The full backend run passed **386 tests** in 106.72 seconds; the final focused
assistant/product run passed **41 tests**. Ruff check/format and pip dependency
validation passed. Frontend assets are unchanged from the preceding verified build.
