# Pantheon

Pantheon is a multi-model conversation studio for OpenAI, Anthropic, Gemini, and xAI.

Users choose which models participate, select which participant writes the final synthesis, and watch the full conversation unfold round by round. All provider calls run through platform-owned server-side API keys.

## What Changed

- The app no longer assumes only OpenAI plus Anthropic.
- Users can choose 1 to 5 participants.
- Each participant can use a different provider and model.
- Provider API keys stay on the server and are never exposed to the browser.
- Every run is prepaid through credits before the first provider call is made.
- The conversation framing is open-ended and collaborative.
- The final synthesis is now one compact section with:
  - `Snapshot`
  - `Where They Agreed`
  - `Where They Disagreed`
  - `Best Answer Right Now`

## Providers

Pantheon currently supports:

- OpenAI
- Anthropic
- Gemini
- xAI

Suggested models are exposed directly in the UI.

## Website

Start the app:

```bash
python3 webapp.py
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

The home page lets users:

- enter a prompt
- choose 1 to 5 participants
- choose provider and model per participant
- choose the final synthesizer
- see the estimated credit cost before running

## Billing

Pantheon now uses a prepaid billing model with server-owned provider keys:

- no free tier
- no user-provided provider API keys
- active subscription required before any paid run
- optional prepaid credit packs for overage
- Stripe Checkout for plan purchases and credit packs
- verified Stripe webhooks before granting credits
- append-only credit ledger for every grant, reservation, and refund

Stripe webhook events handled:

- `checkout.session.completed`
- `invoice.paid`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`

Before Pantheon executes a run:

1. It validates the account has an active paid subscription.
2. It estimates the maximum bounded provider cost.
3. It converts that maximum cost into credits with a safety margin.
4. It reserves those credits up front.
5. It blocks the run if the balance is insufficient.
6. It settles actual usage after execution and refunds unused credits.

The conversation page shows:

- the selected participant roster
- the transcript grouped by round
- a single final synthesis section at the bottom

## Production Backend

Pantheon can run with a durable shared backend by setting `DATABASE_URL`.

When `DATABASE_URL` is present:

- accounts are stored in the database
- sessions are stored in the database
- conversation ownership is stored in the database
- transcripts, summaries, run metadata, and status are stored in the database
- local and Vercel environments can share the same customer accounts and saved runs

Recommended production env vars:

```bash
DATABASE_URL=postgresql://...
POSTGRES_URL=postgresql://...
PANTHEON_BASE_URL=https://your-project.vercel.app
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
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
PANTHEON_MODEL_COSTS_JSON={...}
```

For Vercel, use the same `DATABASE_URL` in every environment where you want the same customer data to appear.

Pantheon also exposes a health endpoint:

```text
/api/health
```

If you already have local SQLite users and file-based runs, migrate them into the production database with:

```bash
python3 scripts/migrate_local_to_database.py
```

Smoke test the production backend with:

```bash
python3 scripts/verify_database_backend.py
```

That verification now checks both the account/auth schema and the billing tables.

### Google Sign-In

Pantheon supports `Continue with Google` on the login and signup pages.

Set these environment variables:

```bash
GOOGLE_CLIENT_ID=your-google-oauth-client-id
GOOGLE_CLIENT_SECRET=your-google-oauth-client-secret
PANTHEON_BASE_URL=http://127.0.0.1:8000
```

In Google Cloud Console, create a Web application OAuth client and add this redirect URI:

```text
http://127.0.0.1:8000/auth/google/callback
```

For Vercel, set `PANTHEON_BASE_URL` to your deployed site URL and add:

```text
https://your-project.vercel.app/auth/google/callback
```

## CLI

There is still a terminal entry point:

```bash
python3 orchestrator.py "Compare two launch strategies for a new SaaS product." --dry-run
```

For richer CLI usage, pass participants as JSON:

```bash
python3 orchestrator.py "Design a lightweight API architecture." \
  --participants-json '[{"participant_id":"openai-1","label":"Athena","provider":"openai","model":"gpt-5.4","max_output_tokens":4000},{"participant_id":"gemini-1","label":"Hermes","provider":"gemini","model":"gemini-2.5-pro","max_output_tokens":1600}]' \
  --summarizer-id gemini-1
```

## Output

Each run writes a folder under `runs/` with:

- `transcript.md`
- `summary.md`
- `run.json`
- `web_state.json`

`run.json` stores participant configuration and turns, but never provider API keys.

## Notes

- The web app uses only server-side provider API keys.
- Model availability is gated by both server configuration and the account's paid plan.
- If a model does not have a configured bounded cost profile, Pantheon will refuse to run it.
