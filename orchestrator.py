#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib import error, parse, request


OPENAI_URL = "https://api.openai.com/v1/responses"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
XAI_URL = "https://api.x.ai/v1/chat/completions"
GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

DEFAULT_OUTPUT_DIR = "runs"
DEFAULT_ROUNDS = 4
PROMPT_TRANSCRIPT_CHAR_BUDGET = 18000
MAX_PARTICIPANTS = 5

PROVIDER_LABELS = {
    "openai": "ChatGPT",
    "anthropic": "Claude",
    "gemini": "Gemini",
    "xai": "Grok",
}

MODEL_SUGGESTIONS = {
    "openai": ["gpt-5.4", "gpt-5-mini"],
    "anthropic": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash"],
    "xai": ["grok-4", "grok-3-mini"],
}

DEFAULT_MAX_OUTPUT_TOKENS = {
    "openai": 4000,
    "anthropic": 1600,
    "gemini": 1600,
    "xai": 1600,
}


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
class ParticipantConfig:
    participant_id: str
    label: str
    provider: str
    model: str
    max_output_tokens: int
    reasoning: str = "none"


@dataclass
class ConversationTurn:
    participant_id: str
    speaker_label: str
    provider: str
    model: str
    round_number: int
    turn_index: int
    prompt: str
    response_text: str
    citations: List[Citation] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    raw_response: Dict[str, Any] = field(default_factory=dict)
    is_summary: bool = False


@dataclass
class ResumeState:
    run_dir: Path
    metadata: Dict[str, Any]
    question: str
    rounds: int
    participants: List[ParticipantConfig]
    summarizer_id: str
    turns: List[ConversationTurn]
    summary_markdown: str


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:60] or "conversation"


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def get_env_value(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def provider_env_names(provider: str) -> tuple[str, ...]:
    return {
        "openai": ("OPENAI_API_KEY", "OPENAI_API"),
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_API"),
        "gemini": ("GEMINI_API_KEY", "GEMINI_API", "GOOGLE_API_KEY"),
        "xai": ("XAI_API_KEY", "XAI_API"),
    }.get(provider, ())


def default_tokens_for_provider(provider: str) -> int:
    return DEFAULT_MAX_OUTPUT_TOKENS.get(provider, 1600)


def normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    aliases = {
        "google": "gemini",
        "grok": "xai",
    }
    return aliases.get(value, value)


def validate_provider(provider: str) -> str:
    normalized = normalize_provider(provider)
    if normalized not in PROVIDER_LABELS:
        raise ValueError(f"Unsupported provider: {provider}")
    return normalized


def citation_from_dict(payload: Dict[str, Any]) -> Citation:
    return Citation(
        title=str(payload.get("title", "")),
        url=str(payload.get("url", "")),
        note=str(payload.get("note", "")),
    )


def participant_from_dict(payload: Dict[str, Any]) -> ParticipantConfig:
    provider = validate_provider(str(payload.get("provider", "")))
    participant_id = str(payload.get("participant_id") or payload.get("id") or "").strip()
    model = str(payload.get("model", "")).strip()
    if not participant_id:
        raise ValueError("Each participant needs an id.")
    if not model:
        raise ValueError(f"Participant {participant_id} is missing a model.")
    label = str(payload.get("label") or f"{PROVIDER_LABELS[provider]} {model}").strip()
    return ParticipantConfig(
        participant_id=participant_id,
        label=label,
        provider=provider,
        model=model,
        max_output_tokens=int(payload.get("max_output_tokens") or default_tokens_for_provider(provider)),
        reasoning=str(payload.get("reasoning") or "none"),
    )


def participant_to_metadata(participant: ParticipantConfig) -> Dict[str, Any]:
    return {
        "participant_id": participant.participant_id,
        "label": participant.label,
        "provider": participant.provider,
        "model": participant.model,
        "max_output_tokens": participant.max_output_tokens,
        "reasoning": participant.reasoning,
    }


def turn_from_dict(payload: Dict[str, Any]) -> ConversationTurn:
    return ConversationTurn(
        participant_id=str(payload.get("participant_id", "")),
        speaker_label=str(payload.get("speaker_label", "")),
        provider=str(payload.get("provider", "")),
        model=str(payload.get("model", "")),
        round_number=int(payload.get("round_number", 0)),
        turn_index=int(payload.get("turn_index", 0)),
        prompt=str(payload.get("prompt", "")),
        response_text=str(payload.get("response_text", "")),
        citations=[citation_from_dict(item) for item in payload.get("citations", [])],
        usage=payload.get("usage", {}) or {},
        raw_response=payload.get("raw_response", {}) or {},
        is_summary=bool(payload.get("is_summary", False)),
    )


def parse_retry_after_header(value: str) -> Optional[float]:
    raw = value.strip()
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        pass
    try:
        reset_time = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if reset_time.tzinfo is None:
        reset_time = reset_time.replace(tzinfo=timezone.utc)
    return max((reset_time - datetime.now(timezone.utc)).total_seconds(), 0.0)


def parse_iso_reset_header(value: str) -> Optional[float]:
    raw = value.strip()
    if not raw:
        return None
    try:
        reset_time = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if reset_time.tzinfo is None:
        reset_time = reset_time.replace(tzinfo=timezone.utc)
    return max((reset_time - datetime.now(timezone.utc)).total_seconds(), 0.0)


def compute_retry_delay_seconds(http_error: error.HTTPError, provider: str, attempt: int) -> float:
    headers = http_error.headers
    retry_after = headers.get("retry-after")
    if retry_after:
        parsed = parse_retry_after_header(retry_after)
        if parsed is not None:
            return parsed

    if provider == "anthropic":
        for name in (
            "anthropic-ratelimit-requests-reset",
            "anthropic-ratelimit-input-tokens-reset",
            "anthropic-ratelimit-output-tokens-reset",
        ):
            header_value = headers.get(name)
            if not header_value:
                continue
            parsed = parse_iso_reset_header(header_value)
            if parsed is not None:
                return parsed

    return float(attempt * 2)


def extract_error_message(parsed_body: Any) -> str:
    if isinstance(parsed_body, dict):
        error_block = parsed_body.get("error")
        if isinstance(error_block, dict):
            if error_block.get("message"):
                return str(error_block["message"])
            if error_block.get("code"):
                return str(error_block["code"])
        if parsed_body.get("message"):
            return str(parsed_body["message"])
        if parsed_body.get("detail"):
            return str(parsed_body["detail"])
    return ""


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
                delay = compute_retry_delay_seconds(exc, provider, attempt)
                print(
                    f"{provider} returned HTTP {exc.code}. Retrying in {delay:.1f} seconds "
                    f"(attempt {attempt + 1}/{attempts})."
                )
                time.sleep(delay)
                continue
            raise last_error
        except error.URLError as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 2)
                continue
            raise ApiError(provider=provider, status=None, message=str(exc)) from exc

    raise ApiError(provider=provider, status=None, message=str(last_error))


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


def extract_openai_text(raw_response: Dict[str, Any]) -> str:
    pieces: List[str] = []
    for item in raw_response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                text = str(content.get("text", "")).strip()
                if text:
                    pieces.append(text)
    if pieces:
        return "\n\n".join(pieces).strip()
    output_text = raw_response.get("output_text")
    return output_text.strip() if isinstance(output_text, str) else ""


def extract_openai_citations(raw_response: Dict[str, Any]) -> List[Citation]:
    citations: List[Citation] = []
    for item in raw_response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            for annotation in content.get("annotations", []):
                url = annotation.get("url") or annotation.get("source", {}).get("url") or ""
                title = annotation.get("title") or annotation.get("source", {}).get("title") or url
                note = annotation.get("text") or annotation.get("cited_text") or ""
                if url:
                    citations.append(Citation(title=title or url, url=url, note=note))
    return dedupe_citations(citations)


def extract_anthropic_text(raw_response: Dict[str, Any]) -> str:
    pieces: List[str] = []
    for block in raw_response.get("content", []):
        if block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                pieces.append(text)
    return "\n\n".join(pieces).strip()


def extract_anthropic_citations(raw_response: Dict[str, Any]) -> List[Citation]:
    citations: List[Citation] = []
    for block in raw_response.get("content", []):
        if block.get("type") != "text":
            continue
        for citation in block.get("citations", []):
            url = citation.get("url") or ""
            title = citation.get("title") or url
            note = citation.get("cited_text") or ""
            if url:
                citations.append(Citation(title=title, url=url, note=note))
    return dedupe_citations(citations)


def extract_gemini_text(raw_response: Dict[str, Any]) -> str:
    pieces: List[str] = []
    for candidate in raw_response.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            text = str(part.get("text", "")).strip()
            if text:
                pieces.append(text)
    return "\n\n".join(pieces).strip()


def extract_xai_text(raw_response: Dict[str, Any]) -> str:
    choices = raw_response.get("choices", [])
    if not choices:
        return ""
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        return "\n\n".join(str(item.get("text", "")).strip() for item in content if item.get("text")).strip()
    return str(content).strip()


def extract_usage_metrics(participant: ParticipantConfig, raw_response: Dict[str, Any]) -> Dict[str, int]:
    usage = raw_response.get("usage") or {}
    if participant.provider == "openai":
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
    if participant.provider == "anthropic":
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
    if participant.provider == "gemini":
        usage_metadata = raw_response.get("usageMetadata") or {}
        input_tokens = int(usage_metadata.get("promptTokenCount") or usage.get("input_tokens") or 0)
        output_tokens = int(usage_metadata.get("candidatesTokenCount") or usage.get("output_tokens") or 0)
        total_tokens = int(usage_metadata.get("totalTokenCount") or usage.get("total_tokens") or (input_tokens + output_tokens))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
    if participant.provider == "xai":
        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def call_openai(api_key: str, model: str, prompt: str, max_output_tokens: int, reasoning: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "input": prompt,
        "max_output_tokens": max_output_tokens,
    }
    if reasoning and reasoning != "none":
        payload["reasoning"] = {"effort": reasoning}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return request_json(OPENAI_URL, headers=headers, payload=payload, provider="openai")


def call_anthropic(api_key: str, model: str, prompt: str, max_output_tokens: int) -> Dict[str, Any]:
    payload = {
        "model": model,
        "max_tokens": max_output_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    return request_json(ANTHROPIC_URL, headers=headers, payload=payload, provider="anthropic")


def call_gemini(api_key: str, model: str, prompt: str, max_output_tokens: int) -> Dict[str, Any]:
    url = GEMINI_URL_TEMPLATE.format(model=parse.quote(model, safe=""))
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_output_tokens},
    }
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    return request_json(url, headers=headers, payload=payload, provider="gemini")


def call_xai(api_key: str, model: str, prompt: str, max_output_tokens: int) -> Dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_output_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return request_json(XAI_URL, headers=headers, payload=payload, provider="xai")


def call_provider(participant: ParticipantConfig, api_key: str, prompt: str) -> Dict[str, Any]:
    if participant.provider == "openai":
        return call_openai(api_key, participant.model, prompt, participant.max_output_tokens, participant.reasoning)
    if participant.provider == "anthropic":
        return call_anthropic(api_key, participant.model, prompt, participant.max_output_tokens)
    if participant.provider == "gemini":
        return call_gemini(api_key, participant.model, prompt, participant.max_output_tokens)
    if participant.provider == "xai":
        return call_xai(api_key, participant.model, prompt, participant.max_output_tokens)
    raise ValueError(f"Unsupported provider: {participant.provider}")


def extract_response_text(participant: ParticipantConfig, raw_response: Dict[str, Any]) -> str:
    if participant.provider == "openai":
        return extract_openai_text(raw_response)
    if participant.provider == "anthropic":
        return extract_anthropic_text(raw_response)
    if participant.provider == "gemini":
        return extract_gemini_text(raw_response)
    if participant.provider == "xai":
        return extract_xai_text(raw_response)
    return ""


def extract_response_citations(participant: ParticipantConfig, raw_response: Dict[str, Any]) -> List[Citation]:
    if participant.provider == "openai":
        return extract_openai_citations(raw_response)
    if participant.provider == "anthropic":
        return extract_anthropic_citations(raw_response)
    return []


def normalize_response_text(text: str) -> str:
    stripped = text.strip()
    if stripped:
        return stripped
    return "[No text returned by the provider. See run.json for the raw response.]"


def participant_roster_text(participants: List[ParticipantConfig], summarizer: ParticipantConfig) -> str:
    lines = []
    for participant in participants:
        summary_note = " (final synthesizer)" if participant.participant_id == summarizer.participant_id else ""
        lines.append(f"- {participant.label}: provider={participant.provider}, model={participant.model}{summary_note}")
    return "\n".join(lines)


def render_transcript_excerpt(turns: List[ConversationTurn], char_budget: int = PROMPT_TRANSCRIPT_CHAR_BUDGET) -> str:
    if not turns:
        return "No prior turns yet."
    rendered = []
    for turn in turns:
        if turn.is_summary:
            continue
        rendered.append(f"[Round {turn.round_number} | {turn.speaker_label}]\n{turn.response_text.strip()}")
    joined = "\n\n".join(rendered)
    if len(joined) <= char_budget:
        return joined
    kept: List[str] = []
    total = 0
    for entry in reversed(rendered):
        if total + len(entry) > char_budget and kept:
            break
        kept.append(entry)
        total += len(entry) + 2
    kept.reverse()
    return "[Earlier turns omitted for length. The full transcript is on disk.]\n\n" + "\n\n".join(kept)


def compose_turn_prompt(
    participant: ParticipantConfig,
    question: str,
    round_number: int,
    participants: List[ParticipantConfig],
    summarizer: ParticipantConfig,
    prior_turns: List[ConversationTurn],
) -> str:
    transcript_excerpt = render_transcript_excerpt(prior_turns)
    roster = participant_roster_text(participants, summarizer)
    peer_names = ", ".join(item.label for item in participants if item.participant_id != participant.participant_id) or "no other models"

    return "\n".join(
        [
            f"You are {participant.label}, one participant in a multi-model conversation.",
            "",
            "The goal is to pressure-test ideas, challenge weak assumptions, compare perspectives, and move the group toward what is most true, useful, and well-supported.",
            "",
            "User request:",
            question,
            "",
            "Participants:",
            roster,
            "",
            f"The other participants in this conversation are: {peer_names}.",
            "",
            "What to do in this turn:",
            "1. Engage directly with the strongest ideas or mistakes already on the table.",
            "2. Add a distinct perspective, correction, or refinement.",
            "3. If you disagree, say exactly why.",
            "4. If there is emerging agreement, sharpen it into something clearer and more actionable.",
            "5. For factual or technical claims, be careful and concrete.",
            "6. You may change your mind if another participant has the better point.",
            "",
            "Style:",
            "- Speak to the other models, not directly to the end user.",
            "- Use Markdown when useful.",
            "- Be concise but substantive.",
            "- Keep the answer roughly 250-700 words.",
            "- End with two short bullets exactly in this form:",
            "  Agreement signal: ...",
            "  New pressure test: ...",
            "",
            f"This is round {round_number}.",
            "",
            "Conversation so far:",
            transcript_excerpt,
        ]
    ).strip()


def compose_summary_prompt(
    question: str,
    participants: List[ParticipantConfig],
    summarizer: ParticipantConfig,
    turns: List[ConversationTurn],
) -> str:
    transcript_excerpt = render_transcript_excerpt(turns, char_budget=50000)
    roster = participant_roster_text(participants, summarizer)
    return "\n".join(
        [
            f"You are {summarizer.label}. Write the final synthesis for the multi-model conversation below.",
            "",
            "User request:",
            question,
            "",
            "Participants:",
            roster,
            "",
            "Return Markdown with exactly these sections in this order:",
            "## Snapshot",
            "## Where They Agreed",
            "## Where They Disagreed",
            "## Best Answer Right Now",
            "",
            "Requirements:",
            "- Snapshot must be about two sentences total.",
            "- Focus on what the participants actually said, not generic filler.",
            "- In Best Answer Right Now, give the clearest conclusion or recommendation the conversation supports.",
            "- Mention uncertainty only where it genuinely matters.",
            "- Keep the whole synthesis compact and readable.",
            "",
            "Conversation transcript:",
            transcript_excerpt,
        ]
    ).strip()


def build_stub_turn(participant: ParticipantConfig, round_number: int, turn_index: int, prompt: str, is_summary: bool = False) -> ConversationTurn:
    if is_summary:
        response_text = "\n".join(
            [
                "## Snapshot",
                "",
                f"[dry-run] {participant.label} would summarize the conversation in two sentences here.",
                "",
                "## Where They Agreed",
                "",
                "- [dry-run] Shared ground between the participants.",
                "",
                "## Where They Disagreed",
                "",
                "- [dry-run] Main tensions or unresolved disagreements.",
                "",
                "## Best Answer Right Now",
                "",
                f"[dry-run] {participant.label} would present the clearest current answer here.",
            ]
        )
    else:
        response_text = f"[dry-run] {participant.label} would contribute here after challenging the other participants."

    return ConversationTurn(
        participant_id=participant.participant_id,
        speaker_label=participant.label,
        provider=participant.provider,
        model=participant.model,
        round_number=round_number,
        turn_index=turn_index,
        prompt=prompt,
        response_text=response_text,
        usage={"dry_run": True},
        raw_response={"dry_run": True},
        is_summary=is_summary,
    )


def determine_next_step(
    turns: List[ConversationTurn],
    participants: List[ParticipantConfig],
    total_rounds: int,
) -> tuple[str, int, Optional[ParticipantConfig]]:
    for turn in turns:
        if turn.is_summary:
            return ("done", total_rounds + 1, None)

    conversation_turns = [turn for turn in turns if not turn.is_summary]
    expected_turns = total_rounds * len(participants)
    if len(conversation_turns) < expected_turns:
        participant_index = len(conversation_turns) % len(participants)
        round_number = len(conversation_turns) // len(participants) + 1
        return ("participant", round_number, participants[participant_index])
    return ("summary", total_rounds + 1, None)


def participants_from_payload(payload: List[Dict[str, Any]]) -> List[ParticipantConfig]:
    if not payload:
        raise ValueError("At least one participant is required.")
    participants = [participant_from_dict(item) for item in payload]
    if len(participants) > MAX_PARTICIPANTS:
        raise ValueError(f"You can choose at most {MAX_PARTICIPANTS} participants.")
    ids = [item.participant_id for item in participants]
    if len(ids) != len(set(ids)):
        raise ValueError("Participant ids must be unique.")
    return participants


def extract_runtime_keys(
    participants: List[ParticipantConfig],
    participant_payloads: List[Dict[str, Any]],
    use_env_fallback: bool,
) -> Dict[str, str]:
    resolved: Dict[str, str] = {}
    missing: List[str] = []
    for participant in participants:
        api_key = get_env_value(*provider_env_names(participant.provider)) or ""
        if not api_key:
            missing.append(participant.label)
            continue
        resolved[participant.participant_id] = api_key
    if missing:
        if use_env_fallback:
            raise ValueError("Pantheon is not configured for: " + ", ".join(missing))
        raise ValueError("Pantheon is not configured for: " + ", ".join(missing))
    return resolved


def participant_by_id(participants: List[ParticipantConfig], participant_id: str) -> ParticipantConfig:
    for participant in participants:
        if participant.participant_id == participant_id:
            return participant
    raise ValueError(f"Unknown participant id: {participant_id}")


def resolve_resume_run_dir(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    if candidate.is_file():
        if candidate.name != "run.json":
            raise SystemExit(f"Resume path must be a run directory or run.json file, got: {candidate}")
        run_dir = candidate.parent
    else:
        run_dir = candidate
    if not (run_dir / "run.json").exists():
        raise SystemExit(f"Could not find run.json in {run_dir}")
    return run_dir


def load_resume_state(raw_path: str) -> ResumeState:
    run_dir = resolve_resume_run_dir(raw_path)
    metadata = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    turns = [turn_from_dict(item) for item in metadata.get("turns", [])]
    summary_markdown = ""
    for turn in reversed(turns):
        if turn.is_summary:
            summary_markdown = turn.response_text.strip()
            break
    if not summary_markdown and (run_dir / "summary.md").exists():
        summary_markdown = (run_dir / "summary.md").read_text(encoding="utf-8").strip()

    question = str(metadata.get("question", "")).strip()
    if not question:
        raise SystemExit(f"Saved run metadata in {run_dir / 'run.json'} does not include a question.")

    participants = participants_from_payload(metadata.get("participants", []))
    summarizer_id = str(metadata.get("summarizer_id", ""))
    if not summarizer_id and participants:
        summarizer_id = participants[-1].participant_id

    return ResumeState(
        run_dir=run_dir,
        metadata=metadata,
        question=question,
        rounds=int(metadata.get("rounds", DEFAULT_ROUNDS)),
        participants=participants,
        summarizer_id=summarizer_id,
        turns=turns,
        summary_markdown=summary_markdown,
    )


def write_markdown_logs(
    run_dir: Path,
    question: str,
    participants: List[ParticipantConfig],
    summarizer_id: str,
    turns: List[ConversationTurn],
    summary_markdown: str,
) -> None:
    transcript_lines = [
        "# Pantheon Transcript",
        "",
        f"- Started: `{run_dir.name}`",
        f"- Question: {question}",
        f"- Rounds configured: `{len([turn for turn in turns if not turn.is_summary]) // max(len(participants), 1) if participants else 0}`",
        "- Participants:",
    ]
    for participant in participants:
        suffix = " (final synthesizer)" if participant.participant_id == summarizer_id else ""
        transcript_lines.append(
            f"  - `{participant.label}` · provider=`{participant.provider}` · model=`{participant.model}`{suffix}"
        )
    transcript_lines.extend(["", "## Conversation", ""])

    for turn in turns:
        section_label = "Final Synthesis" if turn.is_summary else f"Round {turn.round_number}"
        transcript_lines.extend(
            [
                f"### {section_label} - {turn.speaker_label}",
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
        transcript_lines.extend(["## Final Summary", "", summary_markdown.strip(), ""])

    (run_dir / "transcript.md").write_text("\n".join(transcript_lines).strip() + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(summary_markdown.strip() + "\n", encoding="utf-8")


def write_run_metadata(
    run_dir: Path,
    question: str,
    rounds: int,
    participants: List[ParticipantConfig],
    summarizer_id: str,
    turns: List[ConversationTurn],
    dry_run: bool,
) -> None:
    payload = {
        "question": question,
        "rounds": rounds,
        "dry_run": dry_run,
        "participants": [participant_to_metadata(item) for item in participants],
        "summarizer_id": summarizer_id,
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a multi-model Pantheon conversation from the terminal.")
    parser.add_argument("question", nargs="?", help="The user question or task.")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--participants-json", help="JSON array or path to a JSON file describing participants.")
    parser.add_argument("--summarizer-id", help="Participant id to use for the final synthesis.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", help="Resume an existing run from a run directory or run.json file.")
    args = parser.parse_args()
    if not args.question and not args.resume:
        parser.error("question is required unless --resume is provided")
    return args


def load_participants_from_cli(raw_value: Optional[str]) -> List[ParticipantConfig]:
    if not raw_value:
        return [
            ParticipantConfig(
                participant_id="openai-1",
                label="OpenAI GPT-5.4",
                provider="openai",
                model="gpt-5.4",
                max_output_tokens=default_tokens_for_provider("openai"),
            ),
            ParticipantConfig(
                participant_id="anthropic-1",
                label="Anthropic Claude Sonnet",
                provider="anthropic",
                model="claude-sonnet-4-6",
                max_output_tokens=default_tokens_for_provider("anthropic"),
            ),
        ]

    candidate = Path(raw_value).expanduser()
    if candidate.exists():
        parsed = json.loads(candidate.read_text(encoding="utf-8"))
    else:
        parsed = json.loads(raw_value)
    return participants_from_payload(parsed)


def print_turn(turn: ConversationTurn) -> None:
    line = "=" * 88
    print(line)
    print(f"Round {turn.round_number} | {turn.speaker_label} | {turn.model}")
    print(line)
    print(turn.response_text.strip())
    print("")


def print_summary(summary_markdown: str) -> None:
    line = "=" * 88
    print(line)
    print("Final Synthesis")
    print(line)
    print(summary_markdown.strip())
    print("")


def main() -> int:
    args = parse_args()
    load_dotenv(Path(".env"))

    if args.resume:
        resume_state = load_resume_state(args.resume)
        question = resume_state.question
        rounds = resume_state.rounds
        participants = resume_state.participants
        summarizer_id = resume_state.summarizer_id
        turns = list(resume_state.turns)
        summary_markdown = resume_state.summary_markdown
        run_dir = resume_state.run_dir
    else:
        question = str(args.question).strip()
        rounds = int(args.rounds)
        participants = load_participants_from_cli(args.participants_json)
        summarizer_id = str(args.summarizer_id or participants[-1].participant_id)
        turns: List[ConversationTurn] = []
        summary_markdown = ""
        run_dir = Path(args.output_dir) / f"{now_stamp()}-{slugify(question)}"
        run_dir.mkdir(parents=True, exist_ok=True)

    summarizer = participant_by_id(participants, summarizer_id)
    provider_payloads = [participant_to_metadata(item) for item in participants]
    runtime_keys = extract_runtime_keys(participants, provider_payloads, use_env_fallback=True)

    while True:
        step, round_number, participant = determine_next_step(turns, participants, rounds)
        if step == "done":
            write_markdown_logs(run_dir, question, participants, summarizer_id, turns, summary_markdown)
            write_run_metadata(run_dir, question, rounds, participants, summarizer_id, turns, args.dry_run)
            if summary_markdown.strip():
                print_summary(summary_markdown)
            print(f"Saved run to {run_dir}")
            return 0

        if step == "participant" and participant is not None:
            prompt = compose_turn_prompt(participant, question, round_number, participants, summarizer, turns)
            if args.dry_run:
                turn = build_stub_turn(participant, round_number, len([item for item in turns if not item.is_summary]) + 1, prompt)
            else:
                raw_response = call_provider(participant, runtime_keys[participant.participant_id], prompt)
                turn = ConversationTurn(
                    participant_id=participant.participant_id,
                    speaker_label=participant.label,
                    provider=participant.provider,
                    model=raw_response.get("model", participant.model),
                    round_number=round_number,
                    turn_index=len([item for item in turns if not item.is_summary]) + 1,
                    prompt=prompt,
                    response_text=normalize_response_text(extract_response_text(participant, raw_response)),
                    citations=extract_response_citations(participant, raw_response),
                    usage=raw_response.get("usage", {}),
                    raw_response=raw_response,
                )
            turns.append(turn)
            print_turn(turn)
            continue

        summary_prompt = compose_summary_prompt(question, participants, summarizer, turns)
        if args.dry_run:
            summary_turn = build_stub_turn(summarizer, rounds + 1, len(turns) + 1, summary_prompt, is_summary=True)
        else:
            raw_response = call_provider(summarizer, runtime_keys[summarizer.participant_id], summary_prompt)
            summary_turn = ConversationTurn(
                participant_id=summarizer.participant_id,
                speaker_label=f"{summarizer.label} Summary",
                provider=summarizer.provider,
                model=raw_response.get("model", summarizer.model),
                round_number=rounds + 1,
                turn_index=len(turns) + 1,
                prompt=summary_prompt,
                response_text=normalize_response_text(extract_response_text(summarizer, raw_response)),
                citations=extract_response_citations(summarizer, raw_response),
                usage=raw_response.get("usage", {}),
                raw_response=raw_response,
                is_summary=True,
            )
        turns.append(summary_turn)
        summary_markdown = summary_turn.response_text


if __name__ == "__main__":
    raise SystemExit(main())
