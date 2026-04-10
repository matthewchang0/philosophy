# Duet Lab

Duet Lab is a multi-model collaboration tool that pairs one OpenAI model with one Anthropic model, lets them work through a request in turns, and saves everything to disk.

It supports both:

- a CLI orchestrator
- a lightweight web app with a playful chat-style UI

You can use it for philosophy, coding, research, planning, writing, or any other task where two strong models comparing and refining each other's ideas is useful.

## What it does

- Alternates OpenAI and Anthropic turns for a configurable number of rounds
- Lets you choose different models for each provider per run
- Uses web search when enabled by provider settings
- Renders Markdown formatting in the browser
- Shows each round in two side-by-side columns
- Saves transcripts, summaries, and structured JSON metadata
- Supports resuming interrupted runs
- Supports dry-run mode so you can test without spending credits

## Environment

Put your keys in `.env` using either naming style:

```bash
OPENAI_API=your-openai-key
ANTHROPIC_API=your-anthropic-key
```

or:

```bash
OPENAI_API_KEY=your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key
```

## Website

Start the web app:

```bash
python3 webapp.py
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

The web app gives you:

- a sidebar of saved requests
- a composer for new requests
- model pickers for OpenAI and Anthropic
- configurable rounds
- Anthropic web search mode
- a dry-run toggle for testing the UI and orchestration flow without API calls
- side-by-side model responses with Markdown rendering
- resume support for interrupted or failed runs

## CLI Usage

Basic run:

```bash
python3 orchestrator.py "Design a robust background job system for a SaaS app."
```

Current defaults:

- OpenAI: `--openai-model gpt-5.4 --openai-reasoning none --openai-max-output-tokens 4000`
- Anthropic: `--anthropic-model claude-sonnet-4-6 --anthropic-max-output-tokens 1400`

Longer run:

```bash
python3 orchestrator.py "What is the meaning of life?" --rounds 8
```

Resume an interrupted run:

```bash
python3 orchestrator.py --resume runs/20260410-170041-what-is-the-meaning-of-life
```

Dry validation without spending API credits:

```bash
python3 orchestrator.py "What is justice?" --dry-run
```

If you add `--dry-run` to a resume, the script writes a non-destructive preview under `.dry-run-preview/` inside that run folder.

If Anthropic web search is unavailable in your account and you want the run to continue anyway:

```bash
python3 orchestrator.py "What is justice?" --fallback-without-web-search
```

## Output

Each run creates a timestamped folder under `runs/`:

- `transcript.md` for the full collaboration log
- `summary.md` for the final Anthropic synthesis
- `run.json` for structured metadata and raw turn data

If a run is interrupted, rerun the CLI with `--resume` pointed at the run directory or its `run.json` file and it will continue from the next missing step.

## Notes

- Anthropic web search must be enabled for your account to use search-backed turns.
- Anthropic's dynamic web search mode may require additional account capabilities, so the default remains `basic`.
- Extended runs can become expensive quickly because both models may search the web repeatedly.
- OpenAI visible-text failures are handled with an automatic retry using a larger output budget.
- Retryable rate limits now wait and retry using provider guidance instead of failing immediately.
