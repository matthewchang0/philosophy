# Pantheon

Pantheon is a multi-model conversation studio. It lets you send one prompt to several frontier models, have them respond round by round, and then choose one model to write the final synthesis.

Today the app is wired for:

- OpenAI / ChatGPT
- Anthropic / Claude
- Google / Gemini
- xAI / Grok

## What You Can Do

- Pick 1 to 5 model participants.
- Choose a different provider and model for each participant.
- Set the number of rounds.
- Choose which participant writes the final synthesis.
- Save conversations and reopen them later in the web app.
- Resume interrupted runs.
- Run the same workflow from the CLI and keep markdown logs on disk.

## The Easiest Way To Try It

If you just want to see how Pantheon works, start with the CLI dry run:

```bash
python3 orchestrator.py "Compare two launch strategies for a new SaaS product." --dry-run
```

This does not call any model APIs. It writes a run folder under `runs/` with:

- `transcript.md`
- `summary.md`
- `run.json`

## Real CLI Runs

Pantheon can also call live provider APIs directly from the CLI.

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Create a `.env` file or export the provider keys you want to use:

```bash
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
XAI_API_KEY=...
```

Notes:

- Gemini also accepts `GOOGLE_API_KEY`.
- You only need keys for the providers you actually plan to use.

Example live run:

```bash
python3 orchestrator.py "Design a lightweight API architecture." \
  --participants-json '[{"participant_id":"openai-1","label":"ChatGPT","provider":"openai","model":"gpt-5.4","max_output_tokens":4000},{"participant_id":"gemini-1","label":"Gemini","provider":"gemini","model":"gemini-2.5-pro","max_output_tokens":1600}]' \
  --summarizer-id gemini-1
```

If a run stops partway through, you can resume it:

```bash
python3 orchestrator.py --resume runs/<run-folder>
```

## Web App

Start the server:

```bash
python3 webapp.py
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

The web UI lets you:

- sign up and log in
- browse saved conversations in the sidebar
- configure participants, rounds, reasoning, and the final synthesizer
- see an upfront credit estimate before starting a run
- review the full transcript and final synthesis in the browser

## Important Setup Reality For The Web App

The browser app is not a simple local demo. It is designed around authenticated, prepaid usage.

To actually start conversations from the web UI, you need:

- `DATABASE_URL` for the shared Postgres-backed app state and billing tables
- at least one server-side provider API key
- a user account with an active paid subscription and enough credits

Important behavior:

- Without `DATABASE_URL`, the site can still load, but browser conversations cannot run.
- The web app does not accept end-user provider keys in the browser. Provider keys live on the server.
- Model availability in the UI depends on both server configuration and the user's active plan.
- Web `Dry run` is optional, disabled by default, and still reserves credits when enabled.

If you want the easiest local experience, use the CLI first. If you want the full browser product, plan on configuring Postgres plus billing.

## Environment Variables

### Minimal For CLI Live Runs

Set one or more of:

```bash
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
GOOGLE_API_KEY=...
XAI_API_KEY=...
```

### Needed For The Full Web Product

```bash
DATABASE_URL=postgresql://...
PANTHEON_BASE_URL=http://127.0.0.1:8000
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
XAI_API_KEY=...
STRIPE_SECRET_KEY=...
STRIPE_WEBHOOK_SECRET=...
STRIPE_PRICE_STARTER_MONTHLY=...
STRIPE_PRICE_PRO_MONTHLY=...
STRIPE_PRICE_SCALE_MONTHLY=...
STRIPE_PRICE_CREDITS_2500=...
STRIPE_PRICE_CREDITS_8000=...
```

Useful optional settings:

```bash
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
PANTHEON_ENABLE_BILLED_DRY_RUN=true
PANTHEON_DRY_RUN_CREDITS=100
PANTHEON_MODEL_COSTS_JSON={...}
PANTHEON_DATA_DIR=/path/to/pantheon-data
```

Notes:

- `PANTHEON_MODEL_COSTS_JSON` overrides or extends the built-in bounded cost table used for quoting and credit reservation.
- `PANTHEON_DATA_DIR` controls where local run folders and the local auth database are stored. By default that is the repo directory locally and `/tmp/pantheon` on Vercel.

## Google Sign-In

Google sign-in is optional. Local email/password auth works without it.

To enable Google sign-in, set:

```bash
GOOGLE_CLIENT_ID=your-google-oauth-client-id
GOOGLE_CLIENT_SECRET=your-google-oauth-client-secret
PANTHEON_BASE_URL=http://127.0.0.1:8000
```

In Google Cloud Console, add this redirect URI:

```text
http://127.0.0.1:8000/auth/google/callback
```

For a deployed app, use your real site URL instead:

```text
https://your-project.vercel.app/auth/google/callback
```

## Health Check

Pantheon exposes:

```text
/api/health
```

## Deployment Notes

- `api/index.py` is the Vercel entrypoint.
- When `DATABASE_URL` is set, Pantheon stores users, sessions, conversations, and billing data in Postgres.
- Conversation artifacts are still written to the Pantheon data directory and synced into durable storage for shared access.

## Migration And Verification Scripts

These are mainly for existing installs, not first-time setup:

```bash
python3 scripts/migrate_local_to_database.py
python3 scripts/verify_database_backend.py
```
