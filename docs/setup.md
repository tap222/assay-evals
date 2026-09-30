[Assay](../README.md) › [Documentation](README.md)

# Setup: install, connect and deploy

## Setup guide

Setting up Assay has two stages. Installing it is done once by someone technical and takes
30–60 minutes. Integrating it happens in the dashboard and needs no code.

### Part 1: install (someone technical, once)

**1. Pick where it runs.**

| Option | Steps | Good for |
|---|---|---|
| **Docker** (recommended) | `git clone https://github.com/tap222/assay-evals && cd assay-evals`<br>`docker build -t assay .`<br>`docker run -d -p 8400:8400 -v assay-data:/data assay` | a company server or VM |
| **Vercel** | Import the repo at vercel.com/new (no build settings). Add a Postgres database, e.g. Neon from the Vercel marketplace: its `DATABASE_URL` is used as the store | a quick hosted setup |
| **Laptop trial** | `pip install assay-server`<br>`assay demo`<br>`assay serve` → http://127.0.0.1:8400 | trying it with demo data |

On Vercel without a database, data is lost whenever an instance restarts. Use that for
demos only.

**2. Use a real database for anything beyond a trial.** SQLite is fine on one server. For
production, set `ASSAY_STORE_URL=postgresql+psycopg://user:pass@host:5432/assay`. Tables are
created automatically, and a newer version adds its columns when it starts, so there are no
migrations to run.

**3. Switch on access control before sharing the link.** A new server has no login. Creating
the first admin key switches login on:

```bash
python -m assay keys create --tenant acme --scopes admin --name "acme admin"
# with Docker: docker exec <container> assay keys create --tenant acme --scopes admin --name "acme admin"
```

Save the printed key. It isn't shown again.

**4. Optional settings.** All of them are listed in `.env.example`.
- `ASSAY_PUBLIC_URL`: the dashboard's address, so alerts and tickets link back to it.
- `ASSAY_SCHEDULE_MINUTES=60` and `ASSAY_SCHEDULE_SOURCES=events:acme`: recompute every hour.
  On Vercel, use the cron setup under [Deploy](#deploy) instead.
- `ASSAY_ABANDON_MINUTES=30`: an agent run with no events for this long is marked abandoned
  and evaluated. `ASSAY_EVALUATE_SECONDS=60`: how often that's checked (0 turns it off).

**5. Hand over the dashboard address and the admin key** to whoever will set up the
integrations.

### Part 2: integrate (in the dashboard, no code)

Open the dashboard, paste the key when asked, and go to **Connect**. The details are in
[Getting started without code](#getting-started-without-code).

**6. Pick how your data gets in.** Use one choice or several:

| Choice | Who does it | Effort |
|---|---|---|
| **Upload a spreadsheet** | you | minutes |
| **We use OpenTelemetry** | whoever runs the collector | about an hour, no code changes |
| **A developer can add a few lines** (Python, `pip install assay-evals`) | a developer | about an hour |
| **Another system can send web requests** (Zapier, n8n, a script) | whoever owns it | 1–3 hours |
| **Our data is in a database** | whoever runs the Assay server | about a day |

Each choice has a **Create a key for this** button, a snippet to copy, and a ready-to-send
message for the person who needs to act on it.

**7. Watch the checklist.** It shows "N of 8 features ready" and gives the next step for
anything missing:
- items and steps switch on health, cost and alerts;
- corrections show where wrong answers start;
- test results switch on release checks;
- agent runs switch on step-by-step agent checks;
- feedback and inputs let Assay find problems nobody reported and turn them into tests.

**8. Build history (optional).** Under **Connect → Advanced**, **Backfill 30 days** replays
the past month so alerts work from day one.

**9. Send results where your team works.** Under **Send results where your team works**:
- **Slack:** paste an incoming-webhook URL and save. Assay sends a test message.
- **Jira or Linear:** paste the site and token and save. Failure patterns under **Learn** then
  get an **Open a ticket** button.
- **Block bad releases:** give the generated GitHub Actions or GitLab file to whoever runs
  your builds, and add a key with the `manage` scope as the `ASSAY_KEY` secret.

### Part 3: day to day

| Where | What you do |
|---|---|
| **Overview** and **Alerts** | see what's broken right now |
| **Failures** | see a test run's failures grouped into causes, and accept intended changes |
| **Learn** | review draft test cases built from production failures, and approve them into a suite |
| **Agents** and **Trace** | open any run step by step |

### Go-live checklist

- [ ] Postgres, not SQLite in `/tmp`
- [ ] An admin key created and stored safely, and a separate `ingest` key for each system
  that sends data
- [ ] `ASSAY_PUBLIC_URL` set
- [ ] At least one source showing **Receiving data** under Connect
- [ ] Slack connected, plus Jira or Linear if you want tickets
- [ ] Regular recomputing: the built-in schedule, cron, or Vercel cron
- [ ] Assay's database protected: Slack, Jira and Linear tokens are stored in it unencrypted

**Shortest path to a first result:** Docker, create a key, then **Connect → Upload a
spreadsheet** of corrections or test results. You'll see results within 15 minutes, with no
developer involved after the install.

## Getting started without code

Open the dashboard and go to **Connect**. It works as a checklist:

1. **Pick how your data can reach Assay.** There are five choices, in plain words:
   - **Upload a spreadsheet.** No code. Corrections, test results, user feedback, or a list
     of items, as a CSV file or cells copied from Excel or Google Sheets. Assay works out what
     the sheet holds and which column is which, and shows rows it can't read and why. You can
     correct its guesses before importing. Test sheets just need a run name.
   - **We use OpenTelemetry.** One exporter added to the collector's config.
   - **A developer can add a few lines.** `pip install assay-evals`, with no dependencies.
   - **Another system can send web requests.** Zapier, n8n, or any script.
   - **Our data is in a database.** Read-only access and a mapping file.

   Each choice gives you something to copy, with the server's address filled in. It also
   writes a ready-to-send message for whoever looks after that system. If you manage keys,
   one button creates a sending key and puts it in both.
2. **Watch the checklist.** It shows which features your data switches on and the one next
   step for each that's missing, and it refreshes itself as data arrives.
3. **Send results where your team works.** Paste a Slack webhook URL, a Jira site and token,
   or a Linear key, and Assay checks it works. Alerts then appear in Slack, and any failure
   pattern under **Learn** gets an "Open a ticket" button. Saved tokens are never shown
   again. **Block bad releases** gives you a ready-made GitHub Actions, GitLab or plain-script
   job for whoever looks after your builds: it stops a release unless Assay says "advance".

Upgrading Assay is safe. On start it adds any new tables and columns to your existing
database and never removes data.

## Connect your pipeline

### The quick way: `assay connect`

Run it in your project. It looks at what's there and gives the way in that needs the least
work, in this order: your database (no code changes), tracing you already have, then a few
lines of code.

```bash
$ assay connect                 # what's here, and the ways in
$ assay connect db              # reads your database's schema, writes the mapping, tests it
$ assay connect code            # the code change, as a diff (nothing is written)
$ assay connect code --apply    # write it
$ assay connect verify          # the pipeline Assay found, and the steps it hasn't seen yet
$ assay connect evals           # a proposed test for every model call (--apply writes them)
```

- **`db`** reads table and column names (with a read-only login: `DATABASE_URL`,
  `ASSAY_SOURCE_URL` or the URL you give). It picks the likeliest table for documents, steps and
  model calls, maps each field, and lists every guess for you to check ("stage = step_name").
  It tests the mapping against the database before writing `mappings/<database>.json`, and
  leaves out anything that doesn't work. It never overwrites a mapping without `--force`.
- **`code`** reads your Python and proposes the smallest change: `assay.init()` and
  `assay.instrument()` once, `@assay.step` on each LangGraph node or function that calls a
  model, `@assay.tool` on your tools, and `@assay.pipeline` (or `@assay.agent`) on the
  function where one run begins, such as whatever calls `graph.invoke()`. It shows the diff and
  saves it to `.assay/connect.patch` (`git apply` works on it). Files change only with
  `--apply`.
- **`verify`** reads what was recorded (`.assay/events.jsonl`, or the server at `ASSAY_URL`) and
  prints the pipeline as Assay sees it, such as `Received → classify → extract → validate →
  Done`, with the steps your code has that no run has reached yet.

- **`evals`** finds every public function that calls a model and proposes a pytest file for it
  under `tests/ai/`. The file holds the call's facts as context (model, system prompt, tools,
  structured output), a spec to write, cases to fill in, and the checks that need no judge (output
  present, the fields a structured output requires, the tools it may use). A judge is left
  commented, for what no rule can check, with the reminder to calibrate it. Every file is
  **proposed, not trusted**: it's skipped until a person writes its spec and cases and removes the
  mark, because evals the same AI wrote with the code, unreviewed, share its blind spots. A
  function that already has a test is left alone, and nothing is written without `--apply`.

Then open the Workflow page: your pipeline, and where Assay's checks attach to it.

### No database password? Use the login you have

Most companies give you a login, not a database password: `aws sso login`, `az login`,
`gcloud auth login`, or Okta in front of one of them. The database accepts a short-lived token
that the cloud's CLI makes. Assay runs that CLI when it opens a connection, keeps the token in
memory only, and runs it again before the token expires. Nothing is stored, and the token is
never printed.

```bash
# the preset is suggested when the host and your CLIs make it clear
assay connect db postgresql+psycopg://you@prod.abc.eu-west-1.rds.amazonaws.com:5432/pipeline
assay connect db … --preset okta --profile data-ro        # or name it
assay connect db … --password-command "your-cli print-db-token"   # any command that prints one
```

| Preset | For | The command it runs |
|---|---|---|
| `aws-rds` | AWS RDS or Aurora with IAM auth | `aws rds generate-db-auth-token` (region from the host; `--profile`) |
| `okta` | Okta in front of AWS | the same, through `saml2aws exec` or `aws-okta exec`, or the AWS profile `gimme-aws-creds` or `aws sso login` gives you |
| `azure` | Azure Database with Microsoft Entra auth (Okta federated into Entra too) | `az account get-access-token --resource-type oss-rdbms` |
| `gcloud` | Cloud SQL with IAM auth | `gcloud sql generate-login-token` (or the Cloud SQL Auth Proxy with `--auto-iam-authn`, and no password at all) |
| `snowflake-sso` | Snowflake behind Okta or any SSO | none: `authenticator=externalbrowser` opens the browser to sign you in |

Token logins need TLS, so `sslmode=require` is added to a Postgres URL. The database user must
be set up for the login (for RDS, `GRANT rds_iam TO you`), and the preset reminds you what it
needs. For the server, set the same command once:

```bash
export ASSAY_SOURCE_URL=postgresql+psycopg://you@prod…:5432/pipeline?sslmode=require
export ASSAY_SOURCE_PASSWORD_COMMAND='aws rds generate-db-auth-token --hostname prod… --port 5432 --username you'
assay serve --source sql --every 60
```

If the login has expired, Assay says so ("Are you logged in (aws sso login, az login, gcloud
auth login, or your Okta tool)?") rather than failing with a database error.

There are two ways to connect by hand. Both feed the same measures.

### 1. Point Assay at your database (read-only)

Assay needs four kinds of records, plus a fifth for people cost. Most pipelines already have them:

| Record | What it is | Key fields |
|---|---|---|
| **documents** | one row per document or file | `document_id`, `received_at`, `completed_at`, `segment`, `document_type`, `processing_mode`, `file_hash`, `page_count`, `facets` (what the document is like: source, language, template, ... see [Document extraction](documents.md#robustness-slices-where-regressions-hide)) |
| **stage_runs** | one row per pipeline stage per document | `document_id`, `stage`, `status`, `started_at`, `finished_at`, `did_work` |
| **calls** | one row per model call | `call_id`, `stage`, `ts`, `model_declared`, `model_served`, `latency_ms`, `cost_usd`, `status`, `resolving_layer`, `gate_reason`, `code_revision` |
| **indexed** | one row per extracted value | `document_id`, `has_positions` |
| **reviews** *(optional)* | time a person spent on a document | `review_id`, `document_id`, `ts`, `kind` (review / rework), `minutes` or `cost_usd`, `reviewer`, `stage` |

`segment` is whatever you want failures broken out by, such as customer, region, business
unit or jurisdiction. `document_type` is your own taxonomy.

Write a mapping that says where each field lives in your schema. The mapping has a `FROM`
clause per record type and a SQL expression per field. See `mappings/example.json`. Fields
you don't record can be set to `"NULL"`. The measures that need them then say *unmeasured*
instead of guessing.

```bash
pip install -e ".[postgres]"
export ASSAY_SOURCE_URL=postgresql+psycopg://readonly:***@your-db:5432/pipeline
export ASSAY_SOURCE_MAPPING=./mappings/mine.json
python -m assay check-source                  # tests every mapped field against the live DB
python -m assay serve --every 60 --source sql # hourly runs over a 1-day window
```

If your tables already follow the reference schema in `assay/sources/sql.py`
(`documents`, `stage_runs`, `model_calls`, `extractions`), you don't need a mapping.
Any database SQLAlchemy supports works. Use a read-only role; Postgres sessions are also
opened `READ ONLY`.

### 2. Push events

If you can't expose a database, send events from your code with the
[`assay-evals`](../sdk/python/README.md) SDK (`pip install assay-evals`, standard library only):

```python
import assay_sdk as assay
assay.init("https://assay.example.com", key="ak_...")   # an ingest key; it sets the tenant

with assay.run("invoice", kind="pipeline", input_ref="s3://inbox/inv-9.pdf", segment=customer) as run:
    with run.stage("field_extraction", prompt="extract_fields@v13") as s:   # timing, status, failures
        run.llm(model="claude-sonnet-5", tokens_in=2400, tokens_out=300, cost_usd=0.011)
        s.outputs.update(extract(doc))                                     # so errors can be traced

assay.correction(run.id, "total", expected="1240.00", observed="1204.00")   # a reviewer's fix
```

Events stream in the background. The SDK retries, and never raises into your pipeline
unless `strict=True`. Agents use the same `run` with `run.llm`, `run.call` (tool calls),
`run.state` and `run.answer`; see [Agents](agents.md#agents-evaluating-the-trajectory-not-just-the-answer).

Other ways in:
- **Any language:** `POST /v1/ingest` with `Authorization: Bearer <ingest key>`, using the
  [event schema](event-schema.md).
- **OpenTelemetry:** add an exporter and change no code. See
  [API and authentication](api.md#api-and-authentication).
- **Older integrations:** `assay/client.py` (per-record `POST /v1/events`) still works.

### 3. See what you get, and backfill

- The **Connect** tab (or `python -m assay coverage --source …`) checks the last 7 days of your
  data. For every measure it says whether it's *live*, *partial* (works, but a field would make
  it more useful) or *blocked*, and names the exact field that would unlock it.
- **Backfill** (`python -m assay backfill --source … --days 30`, or the button) replays past
  days so every slice has a baseline and anything already wrong is flagged on day one. It
  notifies nobody about history, skips days that already have a run, and never disturbs
  current alerts.

## Deploy

**Docker:** `docker build -t assay . && docker run -p 8400:8400 -v assay-data:/data assay`

**Vercel:** import the repo at vercel.com/new. There are no build settings to change,
because Vercel detects the FastAPI `app` in the root `app.py`.
- **With no environment variables**, results go to SQLite in `/tmp`. Each fresh instance
  loads the demo on its first request, and the data is lost when the instance is recycled.
  That's fine for a showcase.
- **With Postgres**, the data persists and a cold start is fast: add Neon from the Vercel
  marketplace, and the `DATABASE_URL` it sets is the store (`ASSAY_STORE_URL`, if set, wins;
  `postgres://` URLs get the psycopg driver). The demo is loaded once, when the database is
  empty; `ASSAY_AUTO_DEMO=0` turns that off. For real use, also set `ASSAY_SOURCE_URL` and
  `ASSAY_SOURCE_MAPPING`, or use events.
- **Scheduled runs:** serverless has no background process. Set `CRON_SECRET` (and
  `ASSAY_SCHEDULE_SOURCES` for scheduled measures), then add a `vercel.json` with
  `"crons": [{"path": "/v1/cron", "schedule": "0 6 * * *"}]`. Each call also marks quiet
  agent runs abandoned and evaluates them. Runs that end are evaluated when they end,
  without cron.
- Create keys (`python -m assay keys create …`) or set `ASSAY_ADMIN_KEY` before sharing the URL.
  Until then the server is in open mode.

All settings are listed in `.env.example`.
