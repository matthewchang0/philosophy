# Pantheon

Pantheon is a multi-model conversation studio for OpenAI, Anthropic, Gemini, and xAI.

Users choose which models participate, paste their own API keys in the web app, select which participant writes the final synthesis, and watch the full conversation unfold round by round.

## What Changed

- The app no longer assumes only OpenAI plus Anthropic.
- Users can choose 1 to 5 participants.
- Each participant can use a different provider and model.
- API keys come from the user at runtime and are not written into `run.json`.
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
- paste an API key per participant
- choose the final synthesizer
- run a dry run without hitting any provider API

The conversation page shows:

- the selected participant roster
- the transcript grouped by round
- a single final synthesis section at the bottom

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

`run.json` stores participant configuration and turns, but not API keys.

## Notes

- The web app expects user-supplied API keys at request time.
- If a run is resumed from the web UI, the app asks for keys again if they are not still in browser session storage.
- Dry runs work without any API key.
