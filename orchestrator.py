#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib import error, request


OPENAI_URL = "https://api.openai.com/v1/responses"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-6"
DEFAULT_ROUNDS = 6
DEFAULT_MAX_OUTPUT_TOKENS = 1400
DEFAULT_OPENAI_REASONING = "medium"
DEFAULT_ANTHROPIC_WEB_SEARCH = "basic"
DEFAULT_OUTPUT_DIR = "runs"
PROMPT_TRANSCRIPT_CHAR_BUDGET = 18000


ACADEMIC_SOURCE_HINTS = [
    "plato.stanford.edu",
    "iep.utm.edu",
    "philpapers.org",
    "jstor.org",
    "cambridge.org",
    "oup.com",
    "academic.oup.com",
    "press.princeton.edu",
]


class ApiError(RuntimeError):
    def __init__(self, provider: str, status: Optional[int], message: str, body: Any = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.body = body


@dataclass
class Citation:
    title: str
    url: str
    note: str = ""


@dataclass
class DebateTurn:
    speaker_label: str
    model: str
    provider: str
    round_number: int
    prompt: str
    response_text: str
    citations: List[Citation] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    raw_response: Dict[str, Any] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an extended philosophy debate between GPT-5.4 and Claude Opus 4.6."
    )
    parser.add_argument("question", help="The question or prompt to debate.")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="How many GPT/Claude exchange rounds to run.")
    parser.add_argument(
        "--openai-model",
        default=DEFAULT_OPENAI_MODEL,
        help=f"OpenAI model to use. Default: {DEFAULT_OPENAI_MODEL}",
    )
    parser.add_argument(
        "--anthropic-model",
        default=DEFAULT_ANTHROPIC_MODEL,
        help=f"Anthropic model to use. Default: {DEFAULT_ANTHROPIC_MODEL}",
    )
    parser.add_argument(
        "--openai-reasoning",
        default=DEFAULT_OPENAI_REASONING,
        choices=["minimal", "low", "medium", "high", "xhigh"],
        help=f"Reasoning effort for the OpenAI call. Default: {DEFAULT_OPENAI_REASONING}",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        help=f"Maximum response tokens per model turn. Default: {DEFAULT_MAX_OUTPUT_TOKENS}",
    )
    parser.add_argument(
        "--anthropic-web-search",
        choices=["off", "basic", "dynamic"],
        default=DEFAULT_ANTHROPIC_WEB_SEARCH,
        help=(
            "Anthropic web search mode. "
            "'dynamic' uses the newer web search tool that may require code execution to be enabled."
        ),
    )
    parser.add_argument(
        "--anthropic-max-searches",
        type=int,
        default=5,
        help="Maximum web searches Anthropic can perform per turn when web search is enabled.",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for generated Markdown logs. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--fallback-without-web-search",
        action="store_true",
        help="If Anthropic web search is unavailable, retry without the search tool instead of stopping.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate prompts, file generation, and CLI flow without calling either API.",
    )
    return parser.parse_args()


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def get_env_value(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def require_api_keys() -> Dict[str, str]:
    openai_api_key = get_env_value("OPENAI_API_KEY", "OPENAI_API")
    anthropic_api_key = get_env_value("ANTHROPIC_API_KEY", "ANTHROPIC_API")

    missing = []
    if not openai_api_key:
        missing.append("OPENAI_API_KEY or OPENAI_API")
    if not anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY or ANTHROPIC_API")

    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Missing API keys. Please set {joined} in your environment or .env file.")

    return {
        "openai": openai_api_key,
        "anthropic": anthropic_api_key,
    }


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:60] or "debate"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def dedupe_citations(citations: Iterable[Citation]) -> List[Citation]:
    seen = set()
    result: List[Citation] = []
    for citation in citations:
        key = (citation.title.strip(), citation.url.strip(), citation.note.strip())
        if not citation.url or key in seen:
            continue
        seen.add(key)
        result.append(citation)
    return result


def extract_openai_citations(raw_response: Dict[str, Any]) -> List[Citation]:
    citations: List[Citation] = []
    for item in raw_response.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                for annotation in content.get("annotations", []):
                    url = annotation.get("url") or annotation.get("source", {}).get("url") or ""
                    title = annotation.get("title") or annotation.get("source", {}).get("title") or url
                    note = (
                        annotation.get("text")
                        or annotation.get("cited_text")
                        or annotation.get("source", {}).get("text")
                        or ""
                    )
                    if url:
                        citations.append(Citation(title=title or url, url=url, note=note))

        if item.get("type") == "web_search_call":
            action = item.get("action", {})
            for source in action.get("sources", []):
                url = source.get("url") or ""
                title = source.get("title") or url
                note = source.get("snippet") or source.get("text") or ""
                if url:
                    citations.append(Citation(title=title, url=url, note=note))
    return dedupe_citations(citations)


def extract_openai_text(raw_response: Dict[str, Any]) -> str:
    pieces: List[str] = []
    for item in raw_response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text", "").strip()
                if text:
                    pieces.append(text)
    if pieces:
        return "\n\n".join(pieces).strip()
    output_text = raw_response.get("output_text")
    return output_text.strip() if isinstance(output_text, str) else ""


def extract_anthropic_citations(raw_response: Dict[str, Any]) -> List[Citation]:
    citations: List[Citation] = []
    for block in raw_response.get("content", []):
        if block.get("type") == "text":
            for citation in block.get("citations", []):
                url = citation.get("url") or ""
                title = citation.get("title") or url
                note = citation.get("cited_text") or ""
                if url:
                    citations.append(Citation(title=title, url=url, note=note))
        elif block.get("type") == "web_search_tool_result":
            content = block.get("content", [])
            if isinstance(content, list):
                for result in content:
                    url = result.get("url") or ""
                    title = result.get("title") or url
                    note = result.get("page_age") or ""
                    if url:
                        citations.append(Citation(title=title, url=url, note=note))
    return dedupe_citations(citations)


def extract_anthropic_text(raw_response: Dict[str, Any]) -> str:
    pieces: List[str] = []
    for block in raw_response.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            if text:
                pieces.append(text)
    return "\n\n".join(pieces).strip()


def request_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_seconds: int = 240,
    attempts: int = 3,
    provider: str = "api",
) -> Dict[str, Any]:
    encoded = json.dumps(payload).encode("utf-8")
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        req = request.Request(url=url, data=encoded, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            parsed_body: Any = raw_body
            try:
                parsed_body = json.loads(raw_body)
            except json.JSONDecodeError:
                pass

            message = extract_error_message(parsed_body) or raw_body or str(exc)
            retryable = exc.code in {408, 409, 429, 500, 502, 503, 504}
            last_error = ApiError(provider=provider, status=exc.code, message=message, body=parsed_body)
            if retryable and attempt < attempts:
                time.sleep(attempt * 2)
                continue
            raise last_error
        except error.URLError as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 2)
                continue
            raise ApiError(provider=provider, status=None, message=str(exc)) from exc

    raise ApiError(provider=provider, status=None, message=str(last_error))


def extract_error_message(parsed_body: Any) -> str:
    if isinstance(parsed_body, dict):
        if isinstance(parsed_body.get("error"), dict):
            return str(parsed_body["error"].get("message", ""))
        if "message" in parsed_body:
            return str(parsed_body["message"])
        if "detail" in parsed_body:
            return str(parsed_body["detail"])
    return ""


def render_transcript_excerpt(turns: List[DebateTurn], char_budget: int = PROMPT_TRANSCRIPT_CHAR_BUDGET) -> str:
    if not turns:
        return "No prior turns yet."

    rendered_turns = []
    for turn in turns:
        rendered_turns.append(
            f"[Round {turn.round_number} | {turn.speaker_label} | {turn.model}]\n{turn.response_text.strip()}"
        )

    joined = "\n\n".join(rendered_turns)
    if len(joined) <= char_budget:
        return joined

    kept: List[str] = []
    total = 0
    for entry in reversed(rendered_turns):
        if total + len(entry) > char_budget and kept:
            break
        kept.append(entry)
        total += len(entry) + 2
    kept.reverse()
    return "[Earlier turns omitted from the prompt for length. Full transcript is on disk.]\n\n" + "\n\n".join(kept)


def compose_debate_prompt(
    speaker_name: str,
    counterpart_name: str,
    question: str,
    round_number: int,
    prior_turns: List[DebateTurn],
) -> str:
    transcript_excerpt = render_transcript_excerpt(prior_turns)
    source_domains = ", ".join(ACADEMIC_SOURCE_HINTS)

    return "\n".join(
        [
            f"You are {speaker_name} in a long-form philosophical debate with {counterpart_name}.",
            "",
            "Debate topic:",
            question,
            "",
            "Your job in this turn:",
            f"1. Engage directly with the latest reasoning from {counterpart_name} and the broader transcript.",
            "2. Use web research when it helps, with special attention to academic or high-authority sources.",
            "3. Think carefully about philosophy, the meaning of life, and political organization where relevant.",
            "4. Push the conversation forward rather than merely summarizing.",
            "5. Explicitly note at least one point of agreement and one live disagreement if possible.",
            "6. Ground claims in evidence when making factual assertions.",
            "",
            "Research preferences:",
            "- Prefer reputable academic and reference sources when available.",
            f"- Especially useful domains include: {source_domains}",
            "- You may also use other credible internet sources when helpful.",
            "",
            "Style:",
            "- Write as if you are speaking to another brilliant philosopher, not to an end user.",
            "- Be substantive, skeptical, and charitable.",
            "- Keep the response to roughly 500-900 words.",
            "- End with 2 short bullets:",
            "  Agreement: ...",
            "  Disagreement: ...",
            "",
            f"This is round {round_number}.",
            "",
            "Conversation so far:",
            transcript_excerpt,
        ]
    ).strip()


def compose_summary_prompt(question: str, turns: List[DebateTurn]) -> str:
    transcript_excerpt = render_transcript_excerpt(turns, char_budget=50000)
    return "\n".join(
        [
            "You are Claude Opus 4.6. Summarize and synthesize the debate transcript below.",
            "",
            "Original question:",
            question,
            "",
            "Please return Markdown with these exact sections:",
            "# Final Synthesis",
            "## Direct Answer",
            "## Agreements",
            "## Disagreements",
            "## Strongest Points from GPT-5.4",
            "## Strongest Points from Claude Opus 4.6",
            "## Unresolved Questions",
            "## Tentative Conclusion",
            "## Sources Mentioned",
            "",
            "Requirements:",
            "- Focus on what the two models actually argued.",
            "- Be specific about where they converged and where they parted ways.",
            "- If either model relied on stronger evidence, say so.",
            "- Use concise bullets inside sections when helpful.",
            "- Keep the final synthesis readable and not overly long.",
            "",
            "Debate transcript:",
            transcript_excerpt,
        ]
    ).strip()


def call_openai(
    api_key: str,
    model: str,
    prompt: str,
    reasoning: str,
    max_output_tokens: int,
) -> Dict[str, Any]:
    payload = {
        "model": model,
        "input": prompt,
        "reasoning": {"effort": reasoning},
        "max_output_tokens": max_output_tokens,
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
        "include": ["web_search_call.action.sources"],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return request_json(OPENAI_URL, headers=headers, payload=payload, provider="openai")


def anthropic_tools(web_search_mode: str, max_searches: int) -> List[Dict[str, Any]]:
    if web_search_mode == "off":
        return []

    tool_type = "web_search_20250305"
    if web_search_mode == "dynamic":
        tool_type = "web_search_20260209"

    return [
        {
            "type": tool_type,
            "name": "web_search",
            "max_uses": max_searches,
        }
    ]


def call_anthropic(
    api_key: str,
    model: str,
    prompt: str,
    max_output_tokens: int,
    web_search_mode: str,
    max_searches: int,
    fallback_without_web_search: bool,
) -> Dict[str, Any]:
    payload = {
        "model": model,
        "max_tokens": max_output_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }

    tools = anthropic_tools(web_search_mode, max_searches)
    if tools:
        payload["tools"] = tools

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        return request_json(ANTHROPIC_URL, headers=headers, payload=payload, provider="anthropic")
    except ApiError as exc:
        if (
            fallback_without_web_search
            and tools
            and "web search" in str(exc).lower()
        ):
            payload.pop("tools", None)
            return request_json(ANTHROPIC_URL, headers=headers, payload=payload, provider="anthropic")
        raise


def write_markdown_logs(
    run_dir: Path,
    question: str,
    turns: List[DebateTurn],
    summary_markdown: str,
    args: argparse.Namespace,
) -> None:
    transcript_path = run_dir / "transcript.md"
    summary_path = run_dir / "summary.md"

    transcript_lines = [
        "# Philosophy Debate Transcript",
        "",
        f"- Started: `{run_dir.name}`",
        f"- Question: {question}",
        f"- Rounds configured: `{args.rounds}`",
        f"- OpenAI model: `{args.openai_model}`",
        f"- Anthropic model: `{args.anthropic_model}`",
        f"- Anthropic web search mode: `{args.anthropic_web_search}`",
        "",
        "## Turns",
        "",
    ]

    for turn in turns:
        transcript_lines.extend(
            [
                f"### Round {turn.round_number} - {turn.speaker_label}",
                "",
                f"- Provider: `{turn.provider}`",
                f"- Model: `{turn.model}`",
            ]
        )
        if turn.usage:
            transcript_lines.append(f"- Usage: `{json.dumps(turn.usage, ensure_ascii=True, sort_keys=True)}`")
        transcript_lines.extend(
            [
                "",
                "#### Prompt Sent",
                "",
                "```text",
                turn.prompt.strip(),
                "```",
                "",
                "#### Response",
                "",
                turn.response_text.strip(),
                "",
            ]
        )
        if turn.citations:
            transcript_lines.extend(["#### Citations", ""])
            for index, citation in enumerate(turn.citations, start=1):
                note_suffix = f" - {citation.note}" if citation.note else ""
                transcript_lines.append(f"{index}. [{citation.title}]({citation.url}){note_suffix}")
            transcript_lines.append("")

    if summary_markdown.strip():
        transcript_lines.extend(["## Final Opus Summary", "", summary_markdown.strip(), ""])

    transcript_path.write_text("\n".join(transcript_lines).strip() + "\n", encoding="utf-8")
    summary_path.write_text(summary_markdown.strip() + "\n", encoding="utf-8")


def write_run_metadata(run_dir: Path, question: str, turns: List[DebateTurn], args: argparse.Namespace) -> None:
    payload = {
        "question": question,
        "rounds": args.rounds,
        "openai_model": args.openai_model,
        "anthropic_model": args.anthropic_model,
        "anthropic_web_search": args.anthropic_web_search,
        "generated_at": datetime.now().isoformat(),
        "turns": [
            {
                **asdict(turn),
                "citations": [asdict(citation) for citation in turn.citations],
            }
            for turn in turns
        ],
    }
    (run_dir / "run.json").write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def print_turn(turn: DebateTurn) -> None:
    line = "=" * 88
    print(line)
    print(f"Round {turn.round_number} | {turn.speaker_label} | {turn.model}")
    print(line)
    print(turn.response_text.strip())
    if turn.citations:
        print("\nSources:")
        for citation in turn.citations[:8]:
            suffix = f" | {citation.note}" if citation.note else ""
            print(f"- {citation.title}: {citation.url}{suffix}")
        if len(turn.citations) > 8:
            print(f"- ... and {len(turn.citations) - 8} more")
    print("")


def print_summary(summary_markdown: str) -> None:
    line = "=" * 88
    print(line)
    print("Final Claude Opus Summary")
    print(line)
    print(summary_markdown.strip())
    print("")


def build_stub_turn(
    speaker_label: str,
    model: str,
    provider: str,
    round_number: int,
    prompt: str,
) -> DebateTurn:
    response_text = (
        f"[dry-run] {speaker_label} would answer here after researching the web and academic sources."
    )
    return DebateTurn(
        speaker_label=speaker_label,
        model=model,
        provider=provider,
        round_number=round_number,
        prompt=prompt,
        response_text=response_text,
        usage={"dry_run": True},
        raw_response={"dry_run": True},
    )


def normalize_response_text(text: str) -> str:
    stripped = text.strip()
    if stripped:
        return stripped
    return "[No text returned by the provider. See run.json for the raw response.]"


def main() -> int:
    args = parse_args()
    load_dotenv(Path(".env"))

    run_dir = Path(args.output_dir) / f"{now_stamp()}-{slugify(args.question)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    turns: List[DebateTurn] = []
    summary_markdown = ""

    print(f"Writing logs to {run_dir}")
    print(f"Question: {args.question}\n")

    if args.dry_run:
        for round_number in range(1, args.rounds + 1):
            openai_prompt = compose_debate_prompt(
                speaker_name="OpenAI GPT-5.4",
                counterpart_name="Claude Opus 4.6",
                question=args.question,
                round_number=round_number,
                prior_turns=turns,
            )
            turns.append(
                build_stub_turn(
                    speaker_label="GPT-5.4",
                    model=args.openai_model,
                    provider="openai",
                    round_number=round_number,
                    prompt=openai_prompt,
                )
            )

            anthropic_prompt = compose_debate_prompt(
                speaker_name="Claude Opus 4.6",
                counterpart_name="OpenAI GPT-5.4",
                question=args.question,
                round_number=round_number,
                prior_turns=turns,
            )
            turns.append(
                build_stub_turn(
                    speaker_label="Claude Opus 4.6",
                    model=args.anthropic_model,
                    provider="anthropic",
                    round_number=round_number,
                    prompt=anthropic_prompt,
                )
            )

        summary_markdown = "# Final Synthesis\n\n[dry-run] Claude Opus 4.6 would synthesize the debate here."
        write_markdown_logs(run_dir, args.question, turns, summary_markdown, args)
        write_run_metadata(run_dir, args.question, turns, args)
        print_summary(summary_markdown)
        return 0

    api_keys = require_api_keys()

    try:
        for round_number in range(1, args.rounds + 1):
            openai_prompt = compose_debate_prompt(
                speaker_name="OpenAI GPT-5.4",
                counterpart_name="Claude Opus 4.6",
                question=args.question,
                round_number=round_number,
                prior_turns=turns,
            )
            openai_raw = call_openai(
                api_key=api_keys["openai"],
                model=args.openai_model,
                prompt=openai_prompt,
                reasoning=args.openai_reasoning,
                max_output_tokens=args.max_output_tokens,
            )
            openai_turn = DebateTurn(
                speaker_label="GPT-5.4",
                model=openai_raw.get("model", args.openai_model),
                provider="openai",
                round_number=round_number,
                prompt=openai_prompt,
                response_text=normalize_response_text(extract_openai_text(openai_raw)),
                citations=extract_openai_citations(openai_raw),
                usage=openai_raw.get("usage", {}),
                raw_response=openai_raw,
            )
            turns.append(openai_turn)
            write_markdown_logs(run_dir, args.question, turns, summary_markdown, args)
            write_run_metadata(run_dir, args.question, turns, args)
            print_turn(openai_turn)

            anthropic_prompt = compose_debate_prompt(
                speaker_name="Claude Opus 4.6",
                counterpart_name="OpenAI GPT-5.4",
                question=args.question,
                round_number=round_number,
                prior_turns=turns,
            )
            anthropic_raw = call_anthropic(
                api_key=api_keys["anthropic"],
                model=args.anthropic_model,
                prompt=anthropic_prompt,
                max_output_tokens=args.max_output_tokens,
                web_search_mode=args.anthropic_web_search,
                max_searches=args.anthropic_max_searches,
                fallback_without_web_search=args.fallback_without_web_search,
            )
            anthropic_turn = DebateTurn(
                speaker_label="Claude Opus 4.6",
                model=anthropic_raw.get("model", args.anthropic_model),
                provider="anthropic",
                round_number=round_number,
                prompt=anthropic_prompt,
                response_text=normalize_response_text(extract_anthropic_text(anthropic_raw)),
                citations=extract_anthropic_citations(anthropic_raw),
                usage=anthropic_raw.get("usage", {}),
                raw_response=anthropic_raw,
            )
            turns.append(anthropic_turn)
            write_markdown_logs(run_dir, args.question, turns, summary_markdown, args)
            write_run_metadata(run_dir, args.question, turns, args)
            print_turn(anthropic_turn)

        summary_prompt = compose_summary_prompt(args.question, turns)
        summary_raw = call_anthropic(
            api_key=api_keys["anthropic"],
            model=args.anthropic_model,
            prompt=summary_prompt,
            max_output_tokens=max(args.max_output_tokens, 1600),
            web_search_mode=args.anthropic_web_search,
            max_searches=max(3, min(args.anthropic_max_searches, 5)),
            fallback_without_web_search=args.fallback_without_web_search,
        )
        summary_markdown = normalize_response_text(extract_anthropic_text(summary_raw))

        summary_turn = DebateTurn(
            speaker_label="Claude Opus 4.6 Summary",
            model=summary_raw.get("model", args.anthropic_model),
            provider="anthropic",
            round_number=args.rounds + 1,
            prompt=summary_prompt,
            response_text=summary_markdown,
            citations=extract_anthropic_citations(summary_raw),
            usage=summary_raw.get("usage", {}),
            raw_response=summary_raw,
        )
        turns.append(summary_turn)
        write_markdown_logs(run_dir, args.question, turns[:-1], summary_markdown, args)
        write_run_metadata(run_dir, args.question, turns, args)
        print_summary(summary_markdown)
        print(f"Saved transcript to {run_dir / 'transcript.md'}")
        print(f"Saved summary to {run_dir / 'summary.md'}")
        return 0
    except ApiError as exc:
        write_markdown_logs(run_dir, args.question, turns, summary_markdown, args)
        write_run_metadata(run_dir, args.question, turns, args)
        print(f"{exc.provider} API error", file=sys.stderr)
        if exc.status is not None:
            print(f"Status: {exc.status}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print(f"Partial logs were saved to {run_dir}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
