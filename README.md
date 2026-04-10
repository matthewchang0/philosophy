# Philosophy Debate Orchestrator

This repo contains a small Python CLI that makes OpenAI GPT-5.4 and Anthropic Claude Opus 4.6 debate a question for multiple rounds, then asks Claude Opus to summarize where they agree and disagree.

It is designed for terminal use and writes Markdown logs for every run.

## What it does

- Alternates GPT-5.4 and Claude Opus 4.6 for a configurable number of rounds
- Enables OpenAI web search each GPT turn
- Enables Anthropic web search each Claude turn
- Saves the full transcript to Markdown
- Saves the final Claude summary to Markdown
- Keeps a JSON artifact too, which helps with debugging if an API response shape changes

## Environment

The script supports either the standard SDK-style names or the names already present in your `.env`:

```bash
OPENAI_API=your-openai-key
ANTHROPIC_API=your-anthropic-key
```

It also accepts:

```bash
OPENAI_API_KEY=your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key
```

## Usage

Basic run:

```bash
python3 orchestrator.py "What is the meaning of life, and what form of government best supports human flourishing?"
```

Longer debate:

```bash
python3 orchestrator.py "What is the meaning of life?" --rounds 8
```

Dry validation without spending API credits:

```bash
python3 orchestrator.py "What is justice?" --dry-run
```

If Anthropic web search is unavailable in your account and you want the run to continue anyway:

```bash
python3 orchestrator.py "What is justice?" --fallback-without-web-search
```

## Output

Each run creates a timestamped folder under `runs/`:

- `transcript.md` for the full debate log
- `summary.md` for the final Opus synthesis
- `run.json` for raw structured metadata

## Notes

- Anthropic's docs say web search must be enabled in the Claude Console for your organization.
- Anthropic's newer dynamic web search mode may require code execution to be enabled too. The script defaults to the more compatible basic web search mode.
- Extended debates can become expensive quickly because both models may search the web repeatedly.
- The default prompt explicitly prefers academic and high-authority sources, but the APIs still depend on what the search tools can access.
