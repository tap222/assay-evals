# assay-server

The [Assay](https://github.com/tap222/assay-evals) server: evaluation and observability for AI
systems, meaning document-intelligence pipelines (OCR, classification, splitting, field
extraction) and AI agents (reasoning, tool calls, state changes). This package includes
the API, the dashboard, and a demo tenant. Python 3.10+.

```bash
pip install assay-server
assay demo      # synthetic tenant, 7 weeks of daily runs, staged incidents
assay serve     # http://127.0.0.1:8400  (API docs at /docs)
```

To test an AI feature on your own machine, as pytest tests (no server, no account):

```bash
pip install assay-server pytest
assay init                  # assay.toml, and tests/ai/test_support.py with an example agent
pytest --assay tests/ai     # checks every run, compares each test with its last passing run
```

By default, results go to SQLite in the current directory. For production, add the Postgres
driver and point Assay at your database:

```bash
pip install "assay-server[postgres]"
export ASSAY_STORE_URL=postgresql+psycopg://user:pass@host:5432/assay
```

A new server has no login. Creating the first admin key switches login on:

```bash
assay keys create --tenant acme --scopes admin --name "acme admin"
```

To send data to the server from your own code, use the SDK,
[`assay-evals`](https://pypi.org/project/assay-evals/), which has no dependencies.

The [setup guide](https://github.com/tap222/assay-evals/blob/main/docs/setup.md) also covers Docker,
Vercel, connecting your pipeline's database, and release gates.
