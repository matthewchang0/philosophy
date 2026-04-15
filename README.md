# Pantheon

Pantheon is a multi-model conversation studio. Send one prompt to several frontier models, let them debate round by round, and then choose one to write the final synthesis.

Supported providers: **OpenAI (ChatGPT)** · **Anthropic (Claude)** · **Google (Gemini)** · **xAI (Grok)**

---

## Quick Start

### 1. CLI Dry Run (no API keys needed)

The fastest way to see Pantheon in action:

```bash
python3 orchestrator.py "Compare two launch strategies for a new SaaS product." --dry-run
```

This creates a run folder under `runs/` with `transcript.md`, `summary.md`, and `run.json` — no model APIs are called.

### 2. CLI with Live Models

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Create a `.env` file (or export environment variables) with the API keys for the providers you plan to use:

```bash
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...          # also accepts GOOGLE_API_KEY
XAI_API_KEY=...
```

You only need keys for the providers you actually select.

Run a live conversation:

```bash
python3 orchestrator.py "Design a lightweight API architecture." \
  --participants-json '[
    {"participant_id":"openai-1","label":"ChatGPT","provider":"openai","model":"gpt-5.4","max_output_tokens":4000},
    {"participant_id":"gemini-1","label":"Gemini","provider":"gemini","model":"gemini-2.5-pro","max_output_tokens":1600}
  ]' \
  --summarizer-id gemini-1
```

If no `--participants-json` is provided, the default lineup is OpenAI GPT-5.4 and Anthropic Claude Sonnet 4.6.

### 3. Resume an Interrupted Run

```bash
python3 orchestrator.py --resume runs/<run-folder>
```

---

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `question` | *(required unless `--resume`)* | The prompt or task for the models |
| `--rounds` | `4` | Number of discussion rounds |
| `--participants-json` | 2 default models | JSON array or path to a JSON file describing participants |
| `--summarizer-id` | Last participant | Which participant writes the final synthesis |
| `--output-dir` | `runs` | Where run folders are saved |
| `--dry-run` | off | Skip API calls; generate placeholder responses |
| `--resume` | — | Path to an existing run directory or `run.json` to continue |

### Participant JSON Format

Each participant object accepts:

| Field | Required | Description |
|-------|----------|-------------|
| `participant_id` | yes | Unique identifier (e.g. `openai-1`) |
| `label` | no | Display name; auto-generated from provider and model if omitted |
| `provider` | yes | `openai`, `anthropic`, `gemini`, or `xai` |
| `model` | yes | Model string (e.g. `gpt-5.4`, `claude-sonnet-4-6`) |
| `max_output_tokens` | no | Token limit per response; defaults vary by provider |
| `reasoning` | no | Reasoning effort level (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`); currently applies to OpenAI only |

You can include up to **5 participants**, including duplicates from the same provider.

### Available Models

| Provider | Suggested Models |
|----------|-----------------|
| OpenAI | `gpt-5.4`, `gpt-5-mini` |
| Anthropic | `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5` |
| Gemini | `gemini-2.5-pro`, `gemini-2.5-flash` |
| xAI | `grok-4`, `grok-3-mini` |

---

## Web App

### Starting the Server

```bash
python3 webapp.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

The web UI supports signing up, logging in, browsing saved conversations, configuring participants and rounds, reviewing transcripts, and reading the final synthesis.

### Web App Prerequisites

The web app is designed around authenticated, prepaid usage. To run conversations from the browser, you need:

- **`DATABASE_URL`** — a PostgreSQL connection string for app state and billing
- **Server-side provider API keys** — at least one of the provider keys listed above
- **A user account with credits** — the app requires an active subscription and sufficient prepaid credits

Without `DATABASE_URL`, the site loads but conversations cannot run. The web app does not accept user-supplied API keys in the browser; all provider keys live on the server. Model availability depends on server configuration and the user's plan.

If you want the simplest local experience, start with the CLI.

---

## Environment Variables

### CLI Only

Set one or more provider keys:

```bash
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
GOOGLE_API_KEY=...       # alternative for Gemini
XAI_API_KEY=...
```

### Full Web App

```bash
# Database
DATABASE_URL=postgresql://...

# Base URL (used for OAuth redirects and links)
PANTHEON_BASE_URL=http://127.0.0.1:8000

# Provider keys
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
XAI_API_KEY=...

# Stripe billing
STRIPE_SECRET_KEY=...
STRIPE_WEBHOOK_SECRET=...
STRIPE_PRICE_STARTER_MONTHLY=...
STRIPE_PRICE_PRO_MONTHLY=...
STRIPE_PRICE_SCALE_MONTHLY=...
STRIPE_PRICE_CREDITS_2500=...
STRIPE_PRICE_CREDITS_8000=...
```

### Optional

| Variable | Description |
|----------|-------------|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Enable Google sign-in (see below) |
| `PANTHEON_ENABLE_BILLED_DRY_RUN` | Set to `true` to allow dry runs in the web UI (disabled by default; still reserves credits) |
| `PANTHEON_DRY_RUN_CREDITS` | Credit cost for a billed dry run (default: `100`) |
| `PANTHEON_MODEL_COSTS_JSON` | JSON object to override or extend the built-in model cost table used for quoting and credit reservation |
| `PANTHEON_DATA_DIR` | Directory for local run folders and the local auth database; defaults to the repo directory locally and `/tmp/pantheon` on Vercel |

---

## Google Sign-In (Optional)

Local email/password authentication works without any additional setup. To add Google sign-in:

1. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `PANTHEON_BASE_URL` in your environment.
2. In Google Cloud Console, add the redirect URI:
   - Local: `http://127.0.0.1:8000/auth/google/callback`
   - Production: `https://your-domain.com/auth/google/callback`

---

## Deployment

### Vercel

- `api/index.py` is the Vercel entrypoint; `vercel.json` routes all requests through it.
- Set `DATABASE_URL` and all required environment variables in Vercel's project settings.
- When `DATABASE_URL` is configured, users, sessions, conversations, and billing data are stored in PostgreSQL.
- Conversation artifacts are also written to the Pantheon data directory and synced to durable storage.

### Health Check

```
GET /api/health
```

---

## Migration and Verification

For existing installs that need to move local data into a database backend:

```bash
# Migrate local SQLite users and run folders into PostgreSQL
python3 scripts/migrate_local_to_database.py

# Verify the database backend, tables, and auth flow
python3 scripts/verify_database_backend.py
```

Both scripts require `DATABASE_URL` to be set.

---

## Project Structure

```
orchestrator.py          CLI orchestrator and provider API clients
webapp.py                Web application server
pantheon_storage.py      PostgreSQL storage layer (users, sessions, conversations)
pantheon_billing.py      Stripe billing integration
api/index.py             Vercel serverless entrypoint
web/                     Frontend HTML, CSS, and JavaScript
scripts/                 Migration and verification utilities
runs/                    Default output directory for CLI runs
```

---

## License

See the repository for license terms.
