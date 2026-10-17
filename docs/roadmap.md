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

*A mockup with sample data: the change report P2 builds. Nothing on this page is built yet.*

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
set or left empty, compared with its last passing run. Next: checking new writes for a column
left at its default, which the booking-app replay showed matters more than the diff.

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
  logins, database writes and outside calls first.
- Deploy and dependency changes no CI job checked: a requirements, lock or ignore file, a
  workflow or a version pin changed, and nothing in the pull request built, installed or
  deployed from it. In P0 these were 4 of 9 bugs, which coverage can't see.
- Flaky or real for every test, with the existing repeat statistics.
- Mark a change intended once.
- The same report as the PR comment, and as JSON a coding agent can act on.

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
- Claim check: when the coding agent says "all tests pass" or "fixed", check it against what
  actually ran. This extends the existing claim contracts.

Done when: at least 3 of 4 proposed rules are confirmed, with no more than 1 false alarm per 10
pull requests.

User stories: [US-08](user-stories.md#us-08), [US-09](user-stories.md#us-09),
[US-10](user-stories.md#us-10).

![A mockup of rules an app follows, each with its evidence ("6 of 6 delete flows"), three checked on every pull request and two waiting for a yes or no](assets/roadmap/rules.png)

*Mockup, sample data.*

### P4. Cost and performance

- Database queries and outside calls per test, with growth flagged ("1 query became 21").
- What a change cost to build (coding sessions on the branch) and what it adds to run (AI
  calls per request).
- A bug found in production becomes a test, extending what [Learning from
  production](learning.md) does for AI failures to all code.

Done when: cost per change matches the provider's bill within 5%.

User stories: [US-12](user-stories.md#us-12), [US-13](user-stories.md#us-13),
[US-18](user-stories.md#us-18).

### P5. A dashboard organized by value

- Overview, Changes, Quality, Reliability, Cost and Timelines, each opening with what needs
  attention; today's pages stay, under Investigate. Opt-in until it's proven.
- A monthly page with the four numbers, each linked to its evidence, exported as PDF and CSV.

User stories: [US-15](user-stories.md#us-15).

![A mockup of the overview a lead sees: quality, reliability, cost and delivery time this month, each compared with last month](assets/roadmap/overview.png)

*Mockup, sample data.*

### P6. Teams and companies

- Roll-up across repositories and teams.
- Roles on top of the existing SSO and audit log; retention and redaction policies.
- The enterprise items below: SAML and SCIM, alert routing, aggregation in SQL.

User stories: [US-16](user-stories.md#us-16), [US-17](user-stories.md#us-17).

## Not planned

- **An AI code-review bot.** Several good ones exist.
- **Coding-session analytics** (tool calls and tokens per session). That's activity, not value;
  cost per change stays, as one line.
- **Production tracing.** Tracing tools do it well, and it isn't what Assay is for.
- **"Hours saved" estimates.** Numbers you can't check lose trust.

## Not built yet

- **SDKs for other languages:** there are SDKs for Python and for TypeScript and JavaScript.
  Other languages write the events `assay test` reads ([any language](any-language.md)), send
  them to `POST /v1/ingest` directly, or use OpenTelemetry.
- **Agent security: scope tests and security alerts.** An agent given credentials and a task can
  still reach what it wasn't meant to, and the organizations it happened to often didn't notice.
  Today a `never` contract, `must_not_call`, `must_get_approval_before`, the injection check and
  the personal-data check catch it in tests, and recorded HTTP calls show a request made around
  the tools. Production alerts watch rates over runs, which suits quality and is too slow for this.
  - **A scope per case:** the tools an agent may call and the argument values they may take (this
    customer's account, not any account), checked against the actual operation and parameters.
    Anything else fails, including an HTTP call to a protected host made another way.
  - **Fails closed:** `faults()` on the permission check (error, timeout) and a check that the
    protected action didn't run anyway.
  - **Probes:** cases that ask, or inject an instruction, for an out-of-scope action, so the
    boundary is tested on every pull request rather than assumed.
  - **Security flags on what agents access and run, in production:** people now approve almost
    everything an agent asks, so a click can't be the safeguard. Assay already sees each tool
    call, its arguments and the HTTP requests a run makes. It should flag the security-relevant
    ones by itself, with no rules to write first, approved or not:
    - **Secrets and credentials:** reading `.env`, keys, tokens, `~/.ssh`, cloud credentials, or a
      secret appearing in a tool's arguments, result or the answer.
    - **Destructive or irreversible actions:** deletes, drops, `rm -rf`, force pushes, payments,
      refunds, emails to many people.
    - **Privilege and reach:** `sudo`, permission changes, new users or keys, access to another
      customer's records, a host outside the usual list, a download piped into a shell.
    - **Data leaving:** personal data sent to a tool or host that doesn't need it.
    Each flag names the run, the step, what it touched and why it matters, with its severity.
    High severity alerts at once to Slack or a webhook (not after a rate moves, as quality alerts
    do), grouped so one bad deploy is one alert. Flags a team marks as expected become the
    allowed list; the rest become tests, so the same action is caught in CI before the next
    deploy. The same flags apply to the coding agents that build the app, through their hooks.
  - **Approvals that are rubber stamps:** people approve almost everything an agent asks, so an
    approval step can look safe while checking nothing. `run.approval()` records the action, the
    decision and who, but not what the person was shown or how long they took. It should record
    both (the arguments on screen, asked and decided times) and report, per approver and per
    action: the approval rate, the median seconds to decide, risky actions approved in seconds,
    an approval that doesn't match what then ran (approved a $40 refund, $400 went out), and
    approved runs that broke a security rule anyway. In tests, a case where the person says no
    checks the agent stops. The number for a team: the share of approvals that were real reviews.
- **Saying "the sources don't say":** a RAG answer to a question its documents don't answer
  should say so, not fill the gap with something plausible. Faithfulness flags a fabricated
  claim, but nothing marks a case as unanswerable and fails an answer that answers it anyway. A
  case should be able to expect an abstention, and the report should count abstentions apart:
  right to abstain, abstained when the answer was there, answered when it wasn't.
- **Citations the answer gives, checked:** faithfulness checks the judge's evidence against the
  fragments, not the answer's own citations. A citation (a document, a page, a section) should
  be checked against the source it names: that source exists, was retrieved, and says what the
  sentence citing it says. A right claim with the wrong page fails as a wrong citation, not as
  a wrong answer.
- **Paid tool calls (x402):** an agent that pays for an API gets a `402` challenge, pays, and
  retries. A check should read the challenge (price, network, the payment flow it declares) and
  compare it with what the test expects, then check the paid response against its schema and
  the settlement receipt. It uses a mocked facilitator or test payments, never real money by
  default. Price, status, schema and settlement then show in the case-level diff like any other
  change, and `at_most_once` already catches a payment repeated after a lost reply.
- **The same request sent twice:** `faults(tool="lost")` and `expect(run).at_most_once()` catch an
  agent that pays again after a lost reply. A client that sends the whole request again (a
  double click, a webhook delivered twice) isn't simulated yet.
- **One release decision across both gates:** the slice gate (`/v1/gates/evaluate`) and the
  stability gate (`/v1/evals/runs/{run}/stability`) answer different questions and are called
  separately. A release should get one decision table: every gate as a row (quality per slice,
  latency, timeouts, fallbacks, stability), each with its value, limit and verdict, then one
  call: advance, advance with conditions ("p95 up 8%, under the ceiling: watch it in canary"),
  hold or roll back. Quality and operations stay separate rows and are never blended into one
  score.
- **Small high-risk slices:** a slice under `min_n` (30 by default) always holds, so a
  20-case high-risk slice can never advance on the slice gate. For those slices the gate should
  fall back to the stability gate's per-case rules: no case that passed reliably may now fail,
  and any collapse blocks.
- **Accuracy from eval runs:** evaluation results are grouped into causes, but the accuracy
  measures don't read them yet.
- **Naming causes:** cause names come from templates. An LLM could write better one-line names
  (only names, never kinds).
- **SAML and SCIM:** SSO is OpenID Connect, which every major provider offers. There's no SAML,
  and no SCIM provisioning: people appear when they first sign in, and leave through their groups.
- **Shared rate limits:** limits are per instance, in memory. Use a shared store (such as
  Redis) when running several replicas.
- **Alert routing:** there is no per-team routing, silencing, acknowledgement or on-call
  paging (PagerDuty).
- **Scale:** measures compute in Python over fetched rows. That is fine for tens of thousands
  of calls per window. Push aggregation into SQL before running at production volume.
- **Timing noise:** p90 completion time on small slices is the noisiest alert. It needs a
  bootstrap error estimate.
- **Migrations beyond adding:** on start, Assay adds new tables and columns to an existing
  database. Renaming or removing columns would need a real migration tool (Alembic).
- **Secrets at rest:** Slack, Jira and Linear tokens are masked in the API but stored as
  plain text in Assay's database. Protect the database, or add encryption with a server key.

Ideas and disagreements are welcome: open an issue or a discussion.
