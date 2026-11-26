# Job Apply MCP
## 1 · The problem
A candidate has to revisit career sites, spot new vacancies, choose evidence from a CV, re-enter the same information and track unfinished applications. The worker moves that recurring work onto a schedule. The candidate owns the final Workday submission.

## 2 · One project, two application routes
The RSS worker reads configured recruitment feeds. For eligible vacancies with a supported recipient, it can email the CV to the recruiter under the configured daily limit. Workday has a separate workflow. It prepares a draft inside the employer portal and emails the candidate. A review notification and a recruiter application have different recipients and different meanings.

## 3 · Entry points
Windows Task Scheduler launches the enabled workers every four hours while the signed-in computer can run. PowerShell can start the same workers on demand. An MCP client can launch the Workday stdio server and call prepare_workday_drafts or workday_history. All three entry points use local settings and the same Workday database.

## 4 · Discovery and matching
The worker pages through public listings on configured employer boards. It checks title phrases and exclusions, reads the posting details and filters by location and posting date. Optional Brave Search discovers additional indexed Workday boards. This is a shortlist based on configured criteria. A match does not establish eligibility for every mandatory requirement.

## 5 · Preparing a vacancy-specific CV
The PDF builder extracts text blocks from the master CV and ranks excerpts by vocabulary shared with the title and description. It places up to four excerpts on a new first page, preserves the original pages, and archives the result with a content hash. Missing evidence, unreadable input, or an oversized result requires attention.

## 6 · Navigating an application
Playwright opens the posting using a persistent local browser profile. The worker checks for an existing application, uploads the archived PDF, applies supported experience corrections and fills recognized questions with configured answers. It checks the active wizard step after each transition. A sign-in prompt, unsupported control or missing answer can interrupt the draft.

## 7 · Knowing when to act
At Review, the worker records the draft state and sends a candidate email containing the employer, role, reference, posting link and verified PDF. Open the posting using the same account, resume the draft and review it before clicking Submit. A Needs attention email identifies a blockage and links to the posting. Some employers can require additional manual steps.

## 8 · Persistence and failure recovery
The SQLite draft key combines employer board and requisition ID. The record keeps status, last check, pending notification and CV identity. A failed mail attempt leaves a pending notification for retry. As with SMTP generally, interruption between server acceptance and the local success record can produce a duplicate email. The independent email worker uses its own application history.

## 9 · Testing and operating
The offline tests exercise filters, duplicate suppression, mail retry and a simulated Workday form in Chromium. The browser fixture captures the uploaded bytes and compares them with the notification attachment. Live acceptance requires a saved employer session, a matching open vacancy, receipt of a real candidate email and successful reopening of that specific draft. The Windows installation steps are in the project README.

## 10 · Scope
FirstRand, Absa and Betway Africa are seed boards. Additional compatible Workday boards can be configured. Employer-specific sign-in, language, wizard changes and custom questions affect how far automation can advance. PNet, LinkedIn and Indeed would each require separate discovery and application integrations.
