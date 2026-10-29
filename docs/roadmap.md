[Assay](../README.md) › [Documentation](README.md)

# Roadmap

Assay is regression testing for AI apps: the AI inside them, and the AI that builds them.

More and more code is written by coding agents, and so are its tests. Those tests often check
little, so they pass even when the app's behavior has changed, and the pull requests are too
big to read line by line. The question a developer needs answered is: **what did this change
actually do, and is it safe to merge?**

Assay already answers it for the AI parts of an app: each test is compared with its last
passing run, a change that skips a step or escalates differently fails even when the asserts
hold, and `assay diff` shows the flow before and after. The roadmap extends that to the whole
app, from the tests you already have, and keeps the report short: one verdict and at most three
things to look at.

Who it's for, and what each person needs: [user stories](user-stories.md).

![A mockup of the change report for one pull request: a verdict, "Check 3 things before merging", over a weakened test, a changed function no test runs, and a checkout that now runs 21 database queries instead of 3](assets/roadmap/change-report.png)

*A mockup with sample data: the change report P2 builds. Its first version is built, as `assay changes`; the query counts are P4.*

## Where Assay fits

| Tools that | What they do | What Assay does |
|---|---|---|
| Review code with AI | Read the diff and comment | Show what the code did when it ran |
| Record and replay traffic | Replay captured requests or user sessions | Use the tests you already have, and understand AI behavior: flaky or real, agents, judges |
| Evaluate AI outputs | Score answers | Cover the whole change, AI and non-AI, ending in one verdict |
| Give coding agents rules and memory | Tell the agent what to do | Check that the app actually kept the rule |

## Principles

- **Value at a glance, numbers that count.** Every feature has to be understood by a developer
  in seconds, and move a number a team cares about: quality, reliability, cost or delivery time.
  Reports show measured counts and times, never estimated savings.
- **The AI side improves with every phase.** A feature that only helps non-AI code doesn't ship.
- **Nothing breaks for today's users.** New features are opt-in until they're proven; versions
  follow semantic versioning, and every change is in the release notes.
- **Works without a server.** Each feature runs in the CLI and the GitHub Action first; the
  dashboard is optional.
- **Short reports.** One verdict and at most three items per pull request; everything else is
  grouped into causes and collapsed. Values that change on every run are left out.

## Phases

Each phase starts when the one before has met its "done when".

### P0. Prove it on a real app (first replay done)

Before building, replay the history of a real production app offline and see whether a
per-PR report would be short and right.

- Replay 10 to 20 past pull requests and count what each report would show.
- Check what it flags against the bugs that were later fixed by hand.
- Look for tests that were weakened to make them pass, and for corrections repeated to the
  coding agent.

Done when: most reports have 3 items or fewer, at least half the bugs later fixed by hand would
have been flagged, and fewer than 1 in 5 flagged items is noise. The findings are published here.

First replay, on Assay's own history ([findings](p0-findings.md)): reports were short, but
changed functions no test ran would have flagged 1 of 9 later-fixed bugs. Changed lines plus
deploy and dependency changes would flag 6 of 9. P2 below is changed to match. Next: repeat it
on a larger app, with JavaScript and TypeScript measured.

Second replay, on a booking app's 41 fixes, asked only about database writes
([findings](p0-findings.md#second-replay-a-booking-app-and-database-writes)): 6 saved wrong
data, all wrong from the start rather than regressions. Checks on new writes would have
caught 4; a diff against the base branch alone, none. P1 below is changed to match.

### P1. Record every test, and guard the tests

- Every existing test is recorded, AI or not, with no `assayCase()` or `assay_case` needed: a
  Jest and Vitest reporter, and the pytest plugin.
- What the code did in each test: outside calls (HTTP), database writes, AI calls, responses
  and time. HTTP and one database driver first (node-postgres); more only where they catch
  real bugs.
- Database writes as a summary, not raw SQL: table, operation and the columns set. Reads are
  only counted. New writes are checked for a request value that never reaches the row and a
  column left at its default, ranked money first, then times and timezones, then ids.
- **Weakened-test guard.** Today a pull request can't delete, skip or filter out the test its
  change breaks. The guard adds the quieter ways a test stops checking anything: a loosened
  assertion (`toBe(1200)` becoming `toBeGreaterThan(0)`), an expected value rewritten in the
  same pull request as the code it checks, a snapshot updated without review. (Built: an
  expected value that only gained entries is no longer listed as rewritten.)

Started before P0's targets were met: the first replay found the report short but the signal
weak, and recording more of what the code did is what the signal needs. First step: every pytest
test with its outbound HTTP calls (`--assay-all`, opt-in), who ran it, and the weakened tests a
run found, in the CLI, the PR comment and the dashboard. Second step: every Jest test with what it
writes to Postgres through `pg` (`assay-evals/jest`, opt-in), as table, operation and the columns
set or left empty, compared with its last passing run. Third step: new writes checked on their own,
which the booking-app replay showed matters more than the diff. A placeholder saved, money left
empty, a reference left empty next to its twin, and a value the test sent (a JSON request, or
`assay.sent()`) that never reached a row are listed under "New writes to check". Writes the test
makes itself are left out.

Met on the booking app (October 2026): its whole Jest suite, 1,671 tests, recorded with no test
changes, about 5% slower (25.9 s to 27.2 s). The first version of the check listed 157 findings.
After grouping a finding across tests, leaving out the tests' own fixtures, and dropping optional
dates and references, it lists 5. Two are real bugs: a location's `clinic_id` never set, so its
relation is always empty, and queue entries saved with an empty-string user. The other three are
empty strings saved where null was meant. The guard lists 4a4d5a0 and not #32. Next: P2.

Done when: a real app's whole suite is recorded with no test changes, recording adds under 10%
to test time, and the guard flags the rewritten expected value found in P0 (4a4d5a0) and not
the one that only gained an entry (#32).

User stories: [US-02](user-stories.md#us-02), [US-11](user-stories.md#us-11).

### P2. The change report

- A behavior diff for all code against the base branch: one change seen in many tests is one
  item, grouped into causes.
- The likely cause of each change: the file, commit, package or config next to it, or "nothing
  on your side changed" when an outside service did.
- Changed lines that no test ran, grouped by function and ranked by risk: money, messages,
  logins, database writes and outside calls first. A changed helper takes the risk of what calls it: an untested
  change to `format_amount` used only by `refund()` ranks with money, not with the rest. The
  callers come from the code itself (the parser, or the language's own tooling), never a model.
- Deploy and dependency changes no CI job checked: a requirements, lock or ignore file, a
  workflow or a version pin changed, and nothing in the pull request built, installed or
  deployed from it. In P0 these were 4 of 9 bugs, which coverage can't see.
- Flaky or real for every test, with the existing repeat statistics.
- Mark a change intended once.
- The same report as the PR comment, and as JSON a coding agent can act on. Also as an MCP server,
  so Claude Code, Cursor or any other agent can ask mid-session what its change did, and fix it
  before a pull request exists.
- Fragile code ranked higher: lines and functions that later fixes kept changing, from the
  repository's own history (a fix commit's blame, as the P0 replay did). An untested change in a
  function fixed four times this quarter outranks one in code nobody has touched in a year. No
  model is involved, and the history is read once and kept.
- Failure paths no test ran: changed lines in an error handler, a retry, a fallback, a timeout or
  an empty-result branch, listed as their own kind ("failure handling") and ranked just after money
  and the rest. Tests usually prove the happy path; a pull request where only the happy path ran is
  implemented, not ready.
- The deployed app against what was tested: settings the changed code reads that every test
  leaves unset (an environment variable, a feature flag), which is how an upload that never
  uploaded passed every test in the P0 replay. After a deploy, a few recorded cases run against the
  deployed app, and what differs from the test run is listed: a status, a write that didn't happen,
  an outside call that went elsewhere.
- A fix proven by a test that failed before it: in a pull request that fixes a bug, the tests it
  adds or changes are also run against the base branch's code. A test that passes there too doesn't
  reproduce the bug, and the fix is unproven, however green the run. A fix whose test passes only
  for the one reported case, while related cases in the suite get worse, is flagged too.
- Mechanical edits apart from behavior changes: a rename, a moved function or a reformat that
  leaves the parsed code the same is grouped as mechanical and left out of untested lines, so the
  short list of behavior changes is what gets read. A behavior change hidden in a mechanical
  commit is still listed.
- A pass that only holds in this workspace: a test run with untracked or uncommitted files the
  pull request doesn't include, or setup a test didn't make itself (a directory, a local
  database), is marked. `assay test --clean` runs the changed tests from a fresh checkout of the
  commit, so "passed" means it passes for everyone.
- A score that improved while its evaluation changed: when a pull request changes the test
  cases, their inputs, expected values, evaluation data or the eval script, an improvement in
  that same pull request is reported as not comparable, with what changed, until it's run on
  the old evaluation too. An agent that reports better accuracy has to show it wasn't the test
  that got easier.

Built, opt-in: `assay changes`, the change report. It gives one verdict (not safe to merge, check
N things, or nothing to check) and at most three items, ranked from every signal: regressions with
the change next to them, weakened tests, new writes that look wrong, changed lines no test ran,
and deploy and dependency changes no CI job checked. Then come behavior changed but still passing,
and rewritten expected values. Flaky tests are a count. It's the top of the PR comment with the
Action's `changes: true`, and JSON for a coding agent ([docs](changes.md)).

Changed lines no test ran come from the coverage a run leaves (coverage.py, istanbul, lcov or Go's
coverage profile), are
grouped by function, and are ranked by risk. Replayed over this repository's history, the deploy rule
lists all four such bugs, but since pull requests began it lists 7 changes, only 2 of them bugs:
it still needs fewer false alarms. One change seen in many cases is one item. Any item other than a
regression or a weakened test can be marked intended once, with a reason, in
`assay.intended.toml`, and it comes back if what was flagged changes. Next: live pull requests, to
measure P0's targets and reading time.

Done when: P0's targets hold on live pull requests, and developers read a report in under a
minute.

User stories: [US-01](user-stories.md#us-01), [US-03](user-stories.md#us-03),
[US-04](user-stories.md#us-04), [US-05](user-stories.md#us-05), [US-06](user-stories.md#us-06),
[US-07](user-stories.md#us-07), [US-14](user-stories.md#us-14).

### P3. Rules and claims

- Rules found from behavior: what the app has always done ("every delete asks for confirmation
  first", "every message passes the consent check"), so the first pull request that breaks one
  is flagged, before anyone wrote the rule down.
- Corrections a developer repeated to the coding agent, and fix pull requests, count as
  evidence for a rule; they don't create one.
- Nothing becomes a rule without a yes. A confirmed rule is written to `CLAUDE.md` (or
  `AGENTS.md`) and checked on every pull request; at most one suggestion per report.
- One copy of the rules, and the version each run used: the same rules often live in several files
  that drift apart (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules`, a copy in a subfolder or the home
  directory), and an agent follows whichever it loads, stale or not, with nothing failing. Copies
  that disagree are listed, with the lines that differ and which one is newer. A rule Assay writes
  goes to the file the agent actually loads. Each test run records a digest of the instruction files
  in effect, as it already does for prompt versions, so when a rule is corrected the runs and pull
  requests made under the old one can be listed.
- Claim check: when the coding agent says "all tests pass" or "fixed", check it against what
  actually ran. Each run gives a receipt (the command, its exit code, the commit, how many tests
  ran and passed), and a claim without a matching receipt is flagged before anyone reads the diff. This extends the existing claim contracts.

Done when: at least 3 of 4 proposed rules are confirmed, with no more than 1 false alarm per 10
pull requests.

User stories: [US-08](user-stories.md#us-08), [US-09](user-stories.md#us-09),
[US-10](user-stories.md#us-10).

![A mockup of rules an app follows, each with its evidence ("6 of 6 delete flows"), three checked on every pull request and two waiting for a yes or no](assets/roadmap/rules.png)

*Mockup, sample data.*

### P4. Cost and performance

- Database queries and outside calls per test, with growth flagged ("1 query became 21").
- Growth that hides in small steps: each number (cost, input tokens, fixed context, calls) is
  compared with the last passing run, which moves forward with every pass. A prompt that grows 20%
  per pull request passes every time and is 7 times larger after nine. Numbers are also compared
  with an anchor that doesn't move (the last release, or a month ago), and the report names the
  changes that added the most: "fixed context 1,200 → 9,000 tokens since v1.4, over 9 pull
  requests, most from summarise@7 and summarise@9".
- What a change cost to build (coding sessions on the branch) and what it adds to run (AI
  calls per request).
- A bug found in production becomes a test, extending what [Learning from
  production](learning.md) does for AI failures to all code. The case keeps the state that chose
  the path (feature flags, the account's permissions, the response the customer got), since the
  same code takes different paths for two customers, and a case without it may not reproduce. A
  person overriding the agent's action counts too: an approval rejected, or a different action
  taken than the one it recommended, with the reason given, becomes a candidate case that expects
  the person's choice. How often people override the agent, per action, is the number, and only
  cases that ended well are kept.

Done when: cost per change matches the provider's bill within 5%.

User stories: [US-12](user-stories.md#us-12), [US-13](user-stories.md#us-13),
[US-18](user-stories.md#us-18).

### P5. A dashboard organized by value

- Overview, Changes, Quality, Reliability, Cost and Timelines, each opening with what needs
  attention; today's pages stay, under Investigate. Opt-in until it's proven.
- A monthly page with the four numbers, each linked to its evidence, exported as PDF and CSV.
- **Changes a coding agent built, against the rest:** the same numbers split by who built the
  change, which Assay already records with each run (a coding agent's session, CI or a person). Per
  group: the change failure rate (a later fix changed its lines, or a regression reached the default
  branch), regressions and weakened tests caught before merge, items the change report raised, and
  the time from a pull request's first commit to its merge. That's what AI changed in outcomes,
  measured, not how much it was used. Per repository and team, never per person.

User stories: [US-15](user-stories.md#us-15).

![A mockup of the overview a lead sees: quality, reliability, cost and delivery time this month, each compared with last month](assets/roadmap/overview.png)

*Mockup, sample data.*

### P6. Teams and companies

- Roll-up across repositories and teams.
- Roles on top of the existing SSO and audit log; retention and redaction policies.
- Enterprise SSO: SAML 2.0 and SCIM 2.0 directory provisioning (shipped).
- Alert routing by source/slice/team, maintenance silencing, and PagerDuty v2 on-call integration (shipped).
- High-scale SQL aggregation and distributed Redis rate limiting (shipped).
- Server-key encryption for secrets at rest (AES-256-GCM) and schema migrations (shipped).

User stories: [US-16](user-stories.md#us-16), [US-17](user-stories.md#us-17).

## Built recently

Built in October 2026, beyond what the phases above say. Each has its docs; items that change a
phase are marked there too.

- **Coding agents run the tests themselves:** `assay init --claude-code` and `assay init
  --antigravity` add a skill, so Claude Code or Antigravity runs the tests after it changes a
  prompt or the agent, and never accepts a new baseline on its own. `--global` installs it for
  every project. Each run records which agent ran it.
- **Flaky LLM tests, settled with more attempts only where needed:** `assay test --rerun-unsure`
  reruns only the cases that could be worse, a batch at a time, until each is clearly chance or
  clearly worse (sequential testing); an unclear case stays inconclusive, never passes.
- **LLM-as-a-judge, checked:** `assay rejudge` asks a judge about a saved run's answers again,
  with no calls to the AI: agreement with the saved verdicts (Cohen's kappa), flips, and
  `--min-agreement` for CI. The report gives code checks and judged checks a line each, and counts
  cases that went pass to fail and fail to pass with an exact McNemar test.
- **A payment or booking repeated after a lost reply:** `assay.faults(tool="lost")` runs a tool
  for real and then times out, and `expect(run).at_most_once(tool, key=...)` fails an agent that
  does it again.
- **Where agent runs broke:** one line per place runs broke ("gen_sql worked, then exec_sql went
  wrong"), what got worse against the baseline first, and every run behind a count (`assay matrix`).
- **Release gates:** a percentile, not only the mean; an absolute limit the candidate must keep
  whatever the baseline did; and a margin per slice.
- **The change report reads Go:** untested changed lines come from Go's own coverage profile
  (`go test -coverprofile`) too, next to coverage.py, istanbul and lcov.
- **A call with no answer isn't $0:** a call that timed out or lost its connection is reported as
  not priced, since the provider may have billed it.
- **Runaway runs, caught in the first minute:** an agent retrying a failing tool
  overnight or caught in an error loop is detected in the first minute of execution via
  `assay.runaway.RunawayDetector`. Alerts fire at once, per run, for:
  - **The same failure again and again:** the same tool returning the same error (with IDs,
    hex addresses, and numbers stripped) five times in one run, whatever the model changes in its arguments.
  - **Spend velocity cap:** dollar and call velocity per minute against agent baselines.
  - **The slow leak:** runs that ended having done nothing (no-op burn with high step count or cost).
- **One release decision across both gates:** `POST /v1/gates/release-decision` provides
  a single decision table: every gate as a row (quality per slice, latency, timeouts, fallbacks,
  stability), each with its value, limit and verdict, yielding one call: `advance`, `advance_with_conditions`,
  `hold` or `rollback`. Quality and operations stay separate rows and are never blended into one score.
- **Small high-risk slices:** high-severity slices under `min_n` (30 by default) no longer
  block release gates automatically. Instead, they fall back to per-case stability checks: no case that
  passed reliably may now fail, and any collapse blocks.
- **Accuracy from eval runs:** `EvalRunAccuracy` and `FieldAccuracy` compute precision, recall,
  F1 and exact match directly from evaluation check runs joined with golden datasets, breaking down results
  by responsible role and root cause.
- **Naming causes:** `name_cause_with_llm` uses LLM summarization over failure diffs to generate
  clean, concise one-line cause descriptions, falling back to structural pattern signatures.
- **SAML and SCIM:** Enterprise identity provider support with SCIM 2.0 directory provisioning
  (`/scim/v2/Users`, `/scim/v2/ServiceProviderConfig`) and SAML 2.0 SP metadata and assertion consumer service
  (`/v1/auth/saml/metadata`, `/v1/auth/saml/acs`).
- **Shared rate limits:** `RateLimiter` supports distributed atomic counters via Redis
  (`ASSAY_REDIS_URL`) across multiple container replicas, falling back to local memory.
- **Alert routing:** Multi-team routing by source, measure, dimension or kind (`/v1/alerts/routes`),
  maintenance silencing windows (`/v1/alerts/silences`), acknowledgement tracking (`/v1/alerts/{id}/ack`),
  and PagerDuty Events API v2 on-call integration (`pagerduty_send`).
- **Scale: push aggregation into SQL:** `document_volume_sql` and `stage_failure_rate_sql` push
  `COUNT()`, conditional `SUM(CASE WHEN ...)`, and `GROUP BY` directly into database queries for millions of records.
- **Timing noise:** Bootstrap quantile error estimation (`bootstrap_quantile_stderr`) computes
  tail latency standard errors (p90, p95, p99), and alert bands scale with sample size to prevent false alarms on jitter.
- **Migrations beyond adding:** Incremental database schema migration runner (`assay/migrations.py`)
  with persistent version tracking table `_assay_schema_versions` executed safely on startup.
- **Secrets in what's recorded:** Credential redaction in `assay/redaction.py` and `assay learn` scans
  for Gitleaks-style API keys (OpenAI, Anthropic, Google, AWS, GitHub, Slack), JWTs, and private keys, replacing them
  with placeholders before traces are saved or uploaded.
- **Secrets at rest:** Sensitive integration credentials (webhook targets, Slack hooks, Linear tokens,
  and PagerDuty keys) are encrypted with AES-256-GCM server-side encryption using `ASSAY_SECRET_KEY`.
- **Long passages the judge reads in full (RAG):** faithfulness used to show the judge the first
  4,000 characters of each fragment, and keep 2,000 of the fragments passed in, so a claim
  supported further down a long statute section counted as fabricated. A long fragment is now
  split into parts and the judge reads them all, up to 120,000 characters. When it couldn't see
  all of one (a fragment recorded on a run keeps 2,000 characters), a claim it calls fabricated or
  inferred is reported as "couldn't judge", never as fabricated.
- **Saying "the sources don't say" (RAG):** `expect(run).must_abstain()` marks a case whose
  documents don't answer its question, and fails an answer that answers it anyway;
  `.must_not_abstain()` fails "I couldn't find it" when the documents do answer. `assay test` and
  the pull request summary count them apart: right to abstain, answered anyway, abstained when the
  answer was there.
- **Retrieval that failed quietly (RAG):** `faults()` breaks a retriever like a tool
  (`run.search(name, fn, query)`, or a LangChain or LlamaIndex retriever by its name), and
  `handles_failure()` fails an answer that goes on as if a failed or empty search had found the
  evidence. `expect(run).must_retrieve(name, source=...)` fails a case that never searched where its
  evidence is.
- **The same request sent twice (webhooks, double clicks):** `assay.duplicate(fn, request, run=...)`
  (and `await assay.aduplicate(...)` for coroutines) simulates client double clicks and webhook
  redeliveries. `expect(run).duplicate_safe()` verifies the second delivery changed nothing (no
  repeated side-effecting tool calls, charges, or writes), answered success to the sender (not an
  HTTP `409 Conflict`, which causes the sender to retry repeatedly and retries to pile up), and was
  recorded as a duplicate with its original run id.
- **The same tool name on two MCP servers (server-scoped contracts and tool collisions):** with
  multiple MCP servers behind an agent, two servers can both offer `search` and the client picks
  one arbitrarily. `must_call("db.search")`, `must_not_call("web.search")`, and `at_most_once("payments.charge")`
  allow contracts and assertions to name tools by server. `expect(run).no_tool_collisions()` detects
  and flags when two MCP servers offer the same tool name in the same model call.
- **Citations the answer gives, checked:** `expect(run).citations_valid()` checks each citation
  (a document, a page, a section) against the sources retrieved in the run. It verifies that the cited
  source exists and was retrieved, matches the cited page and section metadata, and actually says what
  the sentence citing it says. A right claim with the wrong page fails as a wrong citation, not as a
  wrong answer.
- **The files that decide how tests run, guarded like the tests:** changes to test runner configuration
  files (`conftest.py`, `pytest.ini`, `setup.cfg`, `tox.ini`, Jest and Vitest configs, `pyproject.toml` pytest
  options, `package.json` test scripts, and CI workflow test steps) are guarded alongside weakened
  assertions with exact changed lines. `assay guard` lists them for human confirmation and halts unsafe merges
  until marked intended. `assay changes --intended-stats` tracks which flags get marked intended and flags
  kinds with high ignore rates before the rest stop being read.
- **Errors a test logged and still passed:** errors and warnings logged during each test (Python `logging`
  and `warnings`, `console.error` and `console.warn`) are captured and parameterized into templates with IDs,
  numbers, and PII removed ("user 1234 not found" and "user 5678 not found" are one template). Compared with
  the base branch, templates no test logged before are listed with the tests that logged them and the changed
  function they came from; familiar errors occurring significantly more often (>= 3x) count as regressions.
  Templates logged at the same rate on the base branch are left out to keep suites quiet. `expect(run).no_logged_errors()`
  guards tests in the suite.
- **Tests that reached something real:** monitors commands started (`scp`, `rsync`, `psql`), non-local socket
  connections, and file writes outside the repository and temporary folders. Any real-world access not touched by
  the base branch is caught and listed in the change report (`touched_real`). An opt-in offline mode
  (`assay test --offline`, `pytest --assay --offline`, `assay.offline()`) refuses non-local sockets and blocks
  outside writes loudly so forgotten fakes never reach real systems. `expect(run).no_external_touches()` guards
  agent test cases.
- **Agent security: scope tests and security alerts:** per-case scope tests, fails-closed permission
  checks, security probes, provenance taint tracking, markdown data exfiltration detection, policy gate
  evaluation, autonomous security scanning, gateway governance, and agent reach profiling:
  - **A scope per case:** `Scope(tools=..., arg_constraints=..., allowed_hosts=..., protected_hosts=..., base_path=...)`
    and `expect(run).within_scope(scope)` enforce allowed tools, parameter values, canonical path resolution
    (`PathWithin` preventing `../` traversal or symlink escapes), and protected/allowed network endpoints.
  - **Fails closed:** `expect(run).fails_closed(permission_tool, protected_action)` verifies that when permission
    checks fail or fault (error, timeout, lost), protected operations never execute.
  - **Probes:** `probe(agent_fn, prompt, forbidden_actions=[...])` verifies agent refusal and boundary safety on
    adversarial prompts.
  - **Planted values, taint tracking, and exfiltration:** catches prompt injection planted in retrieved document
    fragments (`kind == "retrieval"`), flags untrusted values passed to outbound tools (`expect(run).no_untrusted_taint()`),
    and detects markdown image/link exfiltration in answers (`expect(run).no_data_exfiltration()`).
  - **Policy gate evaluation:** `PolicyGate` and `evaluate_policy_gate(decisions)` track allow/block/needs_approval
    decisions across policy versions, reporting normal blocked rates and attacks allowed rates.
  - **Autonomous security scanner:** `scan_security_flags(run)` inspects runs without pre-written rules, flagging
    secrets (.env, keys, tokens), destructive actions (`rm -rf`, DROP TABLE, refunds), privilege escalation (`sudo`,
    chmod 777), and data leaving (`expect(run).no_security_flags()`).
  - **Agent reach profiling & widening:** `profile_agent_reach()` profiles capabilities (read, write, delete, spend money,
    send messages, outside hosts), and flags capability widening in `assay changes`.
  - **Gateway governance:** `check_gateway_bypass(run, gateway_host)` and `expect(run).used_gateway(gateway_host)`
    fail tests whose code calls model providers or governed services directly instead of through the gateway.
- **Cross-cutting agent reliability scenarios:** reusable end-to-end cases that run before a prompt,
  model, tool-router or orchestration change, and on a schedule. Each handoff has an explicit contract,
  permission scope and owner (`HandoffContract` for transfer vs advice); each run is checked for the right
  terminal state with external read-back from the system of record (`read_back`), not just the agent's claim
  that it succeeded. Injects timeouts, lost replies, crashes (`crash_at`), and duplicate deliveries, then verifies
  external state and side effects: no lost task, no duplicate charge or write (`at_most_once`), no action after
  a handoff (`expect(run).valid_handoffs()`), and no uncompensated actions left standing (`expect(run).no_orphaned_effects()`).
  When a run fails, pinpoints the first broken transition and its downstream cascade effects
  (`find_first_broken_transition()`) and reports it in `assay changes`.

## Not planned

- **An AI code-review bot.** Several good ones exist.
- **Coding-session analytics** (tool calls and tokens per session). That's activity, not value;
  cost per change stays, as one line.
- **Production tracing.** Tracing tools do it well, and it isn't what Assay is for.
- **"Hours saved" estimates.** Numbers you can't check lose trust.
- **AI usage scores per person, or an HR org tree.** How much someone used an AI tool says nothing
  about what it changed, and a ranking of people by it measures the wrong thing. Outcomes are
  reported per repository and team.

## Not built yet

- **SDKs for other languages:** there are SDKs for Python and for TypeScript and JavaScript.
  Other languages write the events `assay test` reads ([any language](any-language.md)), send
  them to `POST /v1/ingest` directly, or use OpenTelemetry.
- **Agent security: identity, drift, and approvals in production:**
  - **Whose access an agent uses, and when it drifts:** agents often run on the credentials of
    the person who set them up, keep them across sessions, and have no identity or baseline of
    their own, so identity and security tools see nothing unusual. Assay sees what each run did,
    but not whose access it used. It should:
    - **Record the identity behind each action:** the agent, its owner, the person or system that
      asked, and the credential each tool call and HTTP request used. A run that acts with a
      person's credentials on someone else's request, or with no owner on record, is flagged.
    - **Keep a baseline per agent:** the tools, hosts, tables and kinds of records it normally
      reaches, learned from its runs. The first time it reaches something new (a new host,
      another customer's records, a write where it only read), that's an alert at once, not after
      a rate moves. Volume counts too: an agent allowed to read the CRM that reads a thousand
      customers for a one-customer task is draining it one permitted query at a time. Drift is measured against the agent's own history, so no rules come first.
    - **Access held but not used:** the tools and scopes an agent is given against the ones it used
      in the last 30 days, so access can be trimmed to what it does. An agent with no runs for a
      while that still holds access, or whose owner no longer signs in to Assay, is listed for
      review.
    The number for a team: the share of agent actions with a known owner and an identity of their
    own, and how many agents hold access they didn't use.
  - **"What did this agent do, and who approved it?" in a minute:** each run is recorded, but
    finding every action of one kind means opening runs one by one. Assay's own audit log covers
    the people using Assay, not what agents did. It should answer by action: every refund over $100
    this month, with the agent, who asked, who approved and how fast, the rule or contract that
    allowed it, the evidence it rested on, and what happened after. The same view exports for
    security, legal or finance as CSV and PDF, signed so an edit shows. The number for a team:
    the time to answer that question, and the share of actions with a full record.
  - **An approval the agent claimed, not one it was given:** `must_get_approval_before` accepts any
    approval recorded in the run, whoever recorded it. An agent that writes its own approval, or
    passes "trusted" or "approved_by: finance" in a tool's arguments, gets through. An approval
    should record where it came from (the approval system, a person's decision, a signed record),
    and only those count. A claim in the agent's own output or arguments never does, and probe cases
    check it: an invoice that says "pre-approved" in its text must not get paid. An approval used
    long after it was given, past the window the case allows, counts as none.
  - **Approvals that are rubber stamps:** people approve almost everything an agent asks, so an
    approval step can look safe while checking nothing. `run.approval()` records the action, the
    decision and who, but not what the person was shown or how long they took. It should record
    both (the arguments on screen, asked and decided times) and report, per approver and per
    action: the approval rate, the median seconds to decide, risky actions approved in seconds,
    an approval that doesn't match what then ran (approved a $40 refund, $400 went out), and
    approved runs that broke a security rule anyway. In tests, a case where the person says no
    checks the agent stops. The number for a team: the share of approvals that were real reviews.
- **Voice and phone agents: the call, not just the booking:** most phone agents book the slot
  when the caller plays along. They differ on what happens around it: hold, silence, being
  interrupted, a caller who changes an answer. Simulated users (`simulate()`) test a conversation,
  but only as text taking turns. They should be able to act like callers:
  - **Caller moves:** go silent or put the agent on hold for a while, interrupt it mid-sentence,
    correct an earlier answer ("actually, Thursday"), give two answers at once, ask for a person.
  - **Checks on the outcome, not the transcript:** the booking holds the final, corrected
    details, not the first ones. The agent read the time back. "Next Tuesday" resolved to the
    right date against a fixed clock. Nothing from its instructions was spoken that the caller
    shouldn't hear ("3:30 Pacific"). A handoff to a person carried the summary.
  - **Timing, from the platform's own call record:** time to first response, gaps after a hold,
    how often it talked over the caller, read from the transcript timings a voice platform gives
    (Retell, Vapi and others send them), so the same cases compare two agents or two versions of
    one.
  Each failed call is a case that can be run again, and the diff says what changed between
  versions: "stopped reading the time back", "now answers during hold".
- **Handoffs between agents: advice or a transfer:** one agent asks another either for a second
  opinion or to take over the next task. These are different promises, and a run records only
  that a sub-agent ran (`parent_run_id`), not which was asked or what it was allowed to do. A
  handoff should say which it is, and tests should check that each kept its promise:
  - **Advice:** the agent asked for an opinion only reads. A write, a message sent or any other
    action with an effect, in its run, fails the case, and its findings come back to the agent
    that asked.
  - **Take over:** the receiving agent says whether it accepted, and the task has one owner after:
    the agent that handed it off doesn't keep acting on it, and a task handed off and never
    accepted or finished is flagged, not lost.
  - **No permission by message:** a request from another agent grants nothing a person didn't.
    The receiving agent's actions are held to the scope and approvals the person gave the first
    agent, narrowed to the task it was handed, never wider, and an approval doesn't carry over to a
    task it wasn't given for.
  - **What was handed over:** the facts the task needs (the customer, the constraint, the deadline)
    are in what the first agent passed on, so a handoff that drops one is a finding, not a guess.
  The diff shows a run with its sub-agents as one tree, so a change in who did what is visible.
- **Replay recorded runs, decision by decision:** before switching a model, a prompt or a router
  that picks tools, a team wants to know what it would have done on real traffic. Recorded runs
  hold each step's input and each tool's result. Replaying one means giving the new setup the same
  history up to a step and comparing its next decision (the tool, its arguments, or stopping) with
  the recorded one. Tools aren't run again: the recorded results are fed back, so nothing is
  charged, sent or deleted. The report counts where decisions diverge, grouped by kind (another
  tool, other arguments, stopped early, went further), with each divergence scored against the
  run's outcome and the case's checks: better, worse or unclear. A divergence on a tool that writes
  or spends weighs more than one on a read. Runs whose tool definitions changed since they were
  recorded are set apart, since their recorded results may no longer hold.
- **The same model, served differently (self-hosted and open-weight models):** a team keeps the
  model and changes how it runs: int8 or 4-bit instead of 16-bit weights, a new vLLM or llama.cpp
  version, another GPU, a bigger batch. The weights' name is the same, so nothing in the run says
  the model changed, yet outputs can, even at temperature 0. `settings` records temperature and
  similar, not how the model was served. It should:
  - **Record the serving setup with each call:** the quantization, the inference engine and its
    version, the hardware, so a change to any of them shows as a cause like a new prompt or model.
  - **Run the same cases on both setups:** the usual diff, case by case, with the cost and latency
    of each next to it, so "4-bit saves 60% and changes these 3 cases" is one report. Cases that
    gave the same answer every time on the old setup and now differ are listed first.
  - **Point to the first step that differs:** in an agent run, the first call whose output changed
    between the two setups, rather than every step after it.
- **Evidence diffs: the same answer, from different evidence.** Two runs can both say "approved"
  while the new one read an older policy, or the wrong customer's record. Each retrieved fragment
  is recorded (id, source, score, whether it went into the prompt) and each tool call with its
  result, but the diff compares the flow and the counts, not the evidence. It should say, per
  case, which documents and records an answer rested on, and what changed against the baseline:
  evidence dropped, added or swapped, a record of another entity. A changed basis under an
  unchanged answer is a finding, not a pass. Fragments take a version and a date, so evidence that
  was superseded shows as such. This is also the provenance for a decision: which policy version
  and which records supported it.
- **Retrieval on its own: two retrievers or rerankers, compared query by query:** swapping an
  embedding model, a chunk size or a reranker changes what's retrieved before any answer changes.
  Assay records each fragment a run retrieved but doesn't score retrieval against relevance
  labels. It should:
  - **Score each query** with recall@k, MRR and nDCG against labels (which documents answer which
    query), read from a qrels or CSV file or given per case, and split by slice.
  - **Compare two configurations on the same queries:** regressions (found before, missed now),
    recoveries, and shared failures (missed by both), the way `assay diff` does for answers. A
    paired test says whether the overall change is real, and the report names the queries behind it.
  - **Check the labels before the scores:** a retrieved document with no label (a missing judgment
    counted as wrong drags a better retriever down), the same document labelled two ways for one
    query, and labels on different scales. Each says how many results it affects, so a team fixes
    the labels instead of reading noise.
  - **Time and quality on the same queries:** `run.retrieve()` records one start and end per
    retrieval. Each stage should get its own time (embedding the query, each search, fusing,
    reranking), so a search engine's speed isn't confused with the embedding model's. The
    comparison shows p50, p95 and p99 next to recall for each configuration, with the filters the
    app really uses (permissions, metadata) and at the concurrency it's run at, since a fast
    median on an unfiltered index hides the slow tail.
  - **Keep the evidence:** each finding links the query, both result lists and the labels, so
    someone else can rerun it and get the same numbers.
  - **Where the document dropped out:** a router, a filter or a cluster choice before the search
    loses a document that nothing after it can bring back. Each stage's candidates are recorded,
    and a missed document is reported at the stage that dropped it: routed to the wrong cluster,
    filtered out, never in the candidates, or ranked too low.
  - **All the evidence, not some:** a question that needs two passages, say the general rule and
    an exception filed somewhere else, passes only when both are retrieved. Recall@k averages
    over documents and hides it, so these cases are reported on their own.
  - **Make the losses into tests:** each regression becomes a case with its success criterion
    ("kb-12 in the top 5"), so the next change is held to it.
  - **A handoff page:** the comparison, label problems and open regressions as one page for a
    client, or a reviewer who approves the change. Rerun after a fix, it shows what moved.
  It works from saved results, so a team can compare runs it already has without sharing its
  index or rerunning anything.
- **Embedding changes treated as data migrations (RAG):** if ingestion and queries quietly move to
  different embedding models, retrieval still returns neighbors, and the scores look like ordinary
  similarity. Assay records each fragment's id, source and score, but not what made its vector. It
  should:
  - **Record an embedding config id:** the model and everything before it (preprocessing,
    chunking, tokenizer), with each fragment and with each query. A run whose query config doesn't
    match the config of the fragments it got back fails, and so does an index holding more than one.
    Index settings (k, `ef_search`, hybrid weights) go in the same config, so a change to them shows
    as a cause like a new prompt or model.
  - **Check candidates before the reranked list:** for the known query-document pairs, first that
    the document reaches the candidate set, then where the reranker puts it. A reranker can't
    rescue a document the new embedding never retrieved, and comparing only the final lists credits
    it for one it was never given.
  - **A rebuild, tested before the cutover:** a case starts from a recorded snapshot of the
    sources, then edits, deletes and adds documents while the new index is built. Before queries
    switch, it checks those come back under the new config id, the pre-edit chunk doesn't, and the
    rebuild didn't write an older version over a newer edit. The old and new index are compared
    against the same source state, so a newer document in one isn't mistaken for a model regression.
  - **Drift with no deploy:** a model alias that now points at new weights, a replaced checkpoint,
    a dependency bump that changed the tokenizer. A small set of known pairs runs on a schedule
    against the live index, and the distribution of their similarity scores is tracked over time,
    not just pass or fail, so a shift alerts before a pair fails.
- **Agent memory across sessions:** agents that remember users (Mem0, Zep, a vector store, a graph)
  fail where a single conversation can't show it. A user moves house in one session, and the next
  still ships to the old address. A user asks to be forgotten, and the fact comes back a week later.
  One user's detail turns up in another user's answer. `simulate()` runs one conversation as one
  run, so none of this is tested. A case should be able to span sessions (tell, update, delete,
  then ask) and check that the latest value is used, a deleted fact never comes back in an answer or
  a tool call, and nothing crosses from one user to another. Switching or upgrading the memory
  store then shows, case by case, what it now remembers differently.
- **Outdated and conflicting sources (RAG):** a policy has a 2024 and a 2025 version, both are
  retrieved, and the answer follows the old one. Or two sources disagree and the answer blends
  them. Teams handle it with version metadata, a filter and a prompt rule ("use the latest
  version"), or a separate layer that ranks what to trust. Whichever they use, nothing tests that
  it worked. Faithfulness checks that a claim is in the fragments, and an outdated fragment passes
  that check. Recording a version and a date with each fragment makes these checkable:
  - **Answered from a superseded version:** the claims the answer rests on come from a fragment
    a newer version of the same document replaces, while the current one was retrieved or exists.
    An exception that only the old version has counts too.
  - **Conflicting sources:** retrieved fragments that disagree on the claim the answer makes. A
    case says what the answer should do: follow the rule (latest, or the more authoritative
    source), or name the conflict. Blending the two, or picking one without saying so, fails.
  - **Cases for it:** a few built from the documents themselves: each pair of versions that
    changed a rule becomes a question whose answer differs between them, so a test exists before
    a user hits it.
  - **The documents changed, not the code:** a re-index or a new version of a document is a
    cause, like a new prompt or model. "These 6 answers changed after policy.pdf v3 was indexed"
    goes next to the regression, and a newer version left out of the index is flagged.
  - **Which cases a new version affects, passage by passage:** a typo fix or a reformatted table is
    a new version too, and rerunning or flagging every case that cited the document fills a queue
    nobody reads. The two versions are compared passage by passage, changes to whitespace and
    layout are dropped, and only the cases whose answers rested on a passage whose wording changed
    are rerun. Each says which passage changed, old and new text side by side.
  - **A rule added somewhere the answer never looked:** an agent decides a late refund needs no
    approval because the refund policy doesn't say it does; then a separate approvals document
    adds the rule. Nothing the answer cited changed, so a check on citations misses it. When a
    document is added or a passage is new, the cases whose questions now retrieve it are rerun
    too, and an answer that changes is listed with the new passage as its cause.
  Counted per case, the number for a team: answers given from current sources, of all answers
  whose sources have versions.
- **Conditions dropped, and questions half answered (RAG):** faithfulness counts a claim as
  supported when its quote is in a fragment word for word. "Deployments are permitted" quotes the
  source exactly, though the source says "only after approval", or says it in the next sentence. And
  nothing checks that every part of a question was answered. It should:
  - **Keep the conditions:** the evidence check reads the sentences around a quote, not only the
    quote. A claim whose source limits it (only, unless, except, provided, after) and that leaves the
    limit out fails as a dropped condition, and so does one an exception in another document limits.
    Both kinds come with built-in cases (a condition in the same sentence, in the next one, in
    another document), so the check is tested on what it's meant to catch.
  - **Answer each part:** a question is split into the parts a complete answer has to establish.
    Each is backed by a passage or stated as missing, and a part with no passage never counts as
    answered. When the missing part is the one the verdict rests on (yes or no, compliant, on
    time), an answer that gives the verdict anyway fails.
  - **Where each part was lost:** never retrieved, retrieved but left out of the answer, or written
    and then removed by a later check. Each needs a different fix, and one accuracy score hides
    which one a team has.
- **"None", "all of them", and a bigger library (RAG):** the misses that look like answers.
  - **False reassurance:** "no interaction", "no such clause", "not required", when a source says
    there is one. A negative claim quotes nothing, so a claim-by-claim check has little to hold it
    to. A case can expect the caveat, and a "no" answered while the source says yes is counted on
    its own, ahead of every other failure, since it reads as confident and cited.
  - **"Which of our documents...":** a list answer is scored by how many of the labelled documents
    it found, not by whether the ones it names are right. A list that misses some and doesn't say
    it may be incomplete fails; one that says it's a first pass is counted as partial.
  - **The same questions on a bigger library:** a passage found among 1,000 documents can be pushed
    out at 30,000 by near-duplicates (other versions, other strengths, copies of the same text)
    filling the top results. The cases run against the index at more than one size, and a passage
    lost as the library grew is reported with what took its place.
- **Every sentence supported, the answer still wrong:** "No, a Prometheus counter can't go down",
  then, quoted from the docs, "it can only increase or be reset to zero on restart". Each claim
  passes on its own and the answer contradicts itself. Faithfulness checks claims one by one. It
  should also read the claims together, against all the passages they rest on: claims that
  contradict each other, and a conclusion (yes, no, allowed, compliant) that the supported claims
  don't lead to, fail.
- **An app's own check-and-retry loop, tested:** some apps check each sentence or answer with a
  second model as they write it, and retry what fails. Assay can record that, but doesn't read it.
  Each rejected draft and retry should be a step, and the report should give, against labelled
  cases: wrong sentences the loop let through, right ones it rejected, how often a retry was
  wrong again, and the time and cost added per error avoided. A loop that adds time and avoids
  nothing (a strong model with the source already in the prompt) shows as such.
- **A judge tested by taking the evidence away:** a judge given the question, a reference answer
  and the answer, but not what was retrieved, scores agreement with the reference and calls it
  faithfulness. Each judge-based check should be run once on a copy of a passing case with the
  only passage supporting one claim removed. A judge whose verdict doesn't change isn't reading
  the evidence, and is flagged like a weakened test, as success checks will be.
- **Paid tool calls (x402):** an agent that pays for an API gets a `402` challenge, pays, and
  retries. A check should read the challenge (price, network, the payment flow it declares) and
  compare it with what the test expects, then check the paid response against its schema and
  the settlement receipt. It uses a mocked facilitator or test payments, never real money by
  default. Price, status, schema and settlement then show in the case-level diff like any other
  change, and `at_most_once` already catches a payment repeated after a lost reply.
- **Done, proven from outside the agent:** browser and tool agents report success because the
  page changed, while the form was silently rejected or the session had expired. A success check
  today (`success=` in `simulate()`, or `run.state_of()`) reads what the run recorded, which is the
  agent's own view agreeing with itself. It should:
  - **Read back from somewhere the agent didn't write:** a case names the expected end state and a
    check that reads it from the system of record (an API, the database, the admin view, the inbox),
    by the id the action returned. It compares the fields that matter, not just that a row exists,
    waits for a final status rather than "pending", and expects it exactly once.
  - **Allow "unverified":** when the read-back can't decide, the run ends unverified, a status of
    its own, never counted as done. An agent that claims done there is flagged, and so is one that
    retries after an unknown outcome instead of checking first.
  - **Per call, not only at the end:** a tool reports that it ran, not that it worked. An `UPDATE`
    that matched no rows returns the same "success" as one that changed the row. Database writes
    Assay records (P1) should keep how many rows each changed, a count and never values. A write the
    tool called a success that changed nothing is flagged, and so is a file write that left the file
    as it was. A step with no effect to show for its success is "unknown", not done.
  - **Test the check itself:** each success check is run once on a deliberately failed case:
    `faults()` makes the action report success without doing anything. A check that still passes
    checks nothing, and is flagged like a weakened test.
- **Half-finished work: a failure or a crash in the middle:** an agent books the slot, charges
  the card, then fails to send the confirmation, or its process dies after the charge. The first
  steps took effect and nothing undid them. `faults()` fails one call, and `at_most_once()` catches
  a repeat, but nothing checks what a failed or interrupted run leaves behind. It should:
  - **Fail a later step and check the earlier ones were undone:** a case says which action
    compensates which (a refund for a charge, a release for a hold), and a run that failed after an
    effect either compensated it or says plainly what's still in place. A charge left standing for
    a booking that failed is a finding.
  - **Stop the run after any step and start it again:** `faults()` gets a crash point, and the case
    is rerun from the agent's saved state. The resumed run doesn't repeat what took effect, and
    finishes or escalates instead of leaving a job half done. The crash points that matter most are
    just before a call and just after the remote call returns but before the run records it.
  - **The same idempotency key on the retry:** `at_most_once(tool, key=...)` counts calls per key,
    so a retry that makes a new key looks like a different call and passes. The key should be made
    before the call and saved with the intent to call, and a retry after a crash or a lost reply
    should reuse it. A repeated call with a new key for the same action fails.
  - **Count it in production:** runs that ended after an effect without completing or compensating
    it, per agent, so orphaned work is found before a customer finds it.
- **A run that's waiting when something changes: a cancel, a deploy, a late approval:** an agent
  updates a record, calls another system and waits for a person. A good answer and a task done
  right are separate outcomes, and these cases test the second. It should:
  - **Cancel during an external call:** the call has already left, so a cancel can't pull it back.
    The run should record that the cancel arrived mid-call, then check the other system once the
    call ends. A run that just flips to cancelled, with nobody knowing whether the record changed,
    fails.
  - **Deploy while a run waits:** the case pauses at a wait (an approval, a callback), the app's
    code or prompt changes, and the run resumes on the new version. It should finish or ask again,
    never carry on with a step that no longer exists in the shape it had.
  - **An approval bound to what was shown:** `run.approval()` records an action name, so an
    approval of "step 3" carries over to a different step 3 after a deploy. An approval should
    cover a hash of the exact arguments and the version of the record they act on. If either
    changed, it no longer counts and the agent asks again. An approval that arrives after the
    action was cancelled must not make it run.
  - **"Unknown" with an owner and a clock:** a call whose outcome isn't known is a state of its own
    (see "unverified" above), not a log line. In tests, the case checks something resolves it; in
    production, unknown outcomes older than a set time are counted per agent, so none sits forever.
Ideas and disagreements are welcome: open an issue or a discussion.
