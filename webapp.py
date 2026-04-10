#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import orchestrator as orch


APP_ROOT = Path(__file__).resolve().parent
WEB_ROOT = APP_ROOT / "web"
RUNS_ROOT = APP_ROOT / orch.DEFAULT_OUTPUT_DIR
WEB_STATE_FILENAME = "web_state.json"

OPENAI_MODEL_OPTIONS = [
    "gpt-5.4",
    "gpt-5-mini",
]

ANTHROPIC_MODEL_OPTIONS = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
]

ACTIVE_RUNS: Dict[str, threading.Thread] = {}
ACTIVE_RUNS_LOCK = threading.Lock()


def iso_now() -> str:
    return datetime.now().isoformat()


def make_runtime_args(question: str, payload: Dict[str, Any]) -> SimpleNamespace:
    args = SimpleNamespace(
        question=question,
        rounds=int(payload.get("rounds", orch.DEFAULT_ROUNDS)),
        dry_run=bool(payload.get("dry_run", False)),
        openai_model=str(payload.get("openai_model", orch.DEFAULT_OPENAI_MODEL)),
        anthropic_model=str(payload.get("anthropic_model", orch.DEFAULT_ANTHROPIC_MODEL)),
        openai_reasoning=str(payload.get("openai_reasoning", orch.DEFAULT_OPENAI_REASONING)),
        max_output_tokens=None,
        openai_max_output_tokens=int(
            payload.get("openai_max_output_tokens", orch.DEFAULT_OPENAI_MAX_OUTPUT_TOKENS)
        ),
        anthropic_max_output_tokens=int(
            payload.get("anthropic_max_output_tokens", orch.DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS)
        ),
        anthropic_web_search=str(payload.get("anthropic_web_search", orch.DEFAULT_ANTHROPIC_WEB_SEARCH)),
        anthropic_max_searches=int(payload.get("anthropic_max_searches", 5)),
        output_dir=orch.DEFAULT_OUTPUT_DIR,
        resume=None,
        fallback_without_web_search=bool(payload.get("fallback_without_web_search", True)),
    )
    orch.resolve_token_budgets(args)
    return args


def default_resume_args() -> SimpleNamespace:
    args = SimpleNamespace(
        question=None,
        rounds=orch.DEFAULT_ROUNDS,
        openai_model=orch.DEFAULT_OPENAI_MODEL,
        anthropic_model=orch.DEFAULT_ANTHROPIC_MODEL,
        openai_reasoning=orch.DEFAULT_OPENAI_REASONING,
        max_output_tokens=None,
        openai_max_output_tokens=orch.DEFAULT_OPENAI_MAX_OUTPUT_TOKENS,
        anthropic_max_output_tokens=orch.DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS,
        anthropic_web_search=orch.DEFAULT_ANTHROPIC_WEB_SEARCH,
        anthropic_max_searches=5,
        output_dir=orch.DEFAULT_OUTPUT_DIR,
        resume=None,
        fallback_without_web_search=True,
        dry_run=False,
    )
    orch.resolve_token_budgets(args)
    return args


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def web_state_path(run_dir: Path) -> Path:
    return run_dir / WEB_STATE_FILENAME


def read_web_state(run_dir: Path) -> Dict[str, Any]:
    return read_json(web_state_path(run_dir), {})


def write_web_state(run_dir: Path, patch: Dict[str, Any]) -> Dict[str, Any]:
    current = read_web_state(run_dir)
    current.update(patch)
    current["updated_at"] = iso_now()
    web_state_path(run_dir).write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return current


def is_run_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".") and (path / "run.json").exists()


def is_active_run(run_id: str) -> bool:
    with ACTIVE_RUNS_LOCK:
        thread = ACTIVE_RUNS.get(run_id)
        if thread and thread.is_alive():
            return True
        if thread and not thread.is_alive():
            ACTIVE_RUNS.pop(run_id, None)
    return False


def register_active_run(run_id: str, thread: threading.Thread) -> None:
    with ACTIVE_RUNS_LOCK:
        ACTIVE_RUNS[run_id] = thread


def clear_active_run(run_id: str) -> None:
    with ACTIVE_RUNS_LOCK:
        ACTIVE_RUNS.pop(run_id, None)


def turn_to_api(turn: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "speakerLabel": turn.get("speaker_label", ""),
        "model": turn.get("model", ""),
        "provider": turn.get("provider", ""),
        "roundNumber": turn.get("round_number", 0),
        "responseText": turn.get("response_text", ""),
        "citations": turn.get("citations", []),
        "usage": turn.get("usage", {}),
    }


def build_conversation_payload(run_dir: Path, include_turns: bool = True) -> Dict[str, Any]:
    metadata = read_json(run_dir / "run.json", {})
    state = read_web_state(run_dir)
    turns = metadata.get("turns", [])
    dry_run = bool(metadata.get("dry_run")) or any(bool((turn.get("usage") or {}).get("dry_run")) for turn in turns)
    summary_from_turns = ""
    for turn in reversed(turns):
        speaker_label = str(turn.get("speaker_label", "")).lower()
        if "summary" in speaker_label:
            summary_from_turns = str(turn.get("response_text", "")).strip()
            break

    summary_path = run_dir / "summary.md"
    summary_markdown = summary_from_turns
    if not summary_markdown and summary_path.exists():
        summary_markdown = summary_path.read_text(encoding="utf-8").strip()

    status = state.get("status", "idle")
    completed = bool(summary_markdown and turns)
    if completed:
        status = "completed"
    elif status == "running" and not is_active_run(run_dir.name):
        status = "interrupted"

    question = str(metadata.get("question") or state.get("question") or run_dir.name).strip()
    title = question if len(question) <= 72 else question[:69] + "..."

    payload = {
        "id": run_dir.name,
        "title": title,
        "question": question,
        "status": status,
        "error": state.get("error", ""),
        "createdAt": state.get("created_at") or metadata.get("generated_at") or "",
        "updatedAt": state.get("updated_at") or metadata.get("generated_at") or "",
        "config": {
            "rounds": int(metadata.get("rounds", 0) or 0),
            "openaiModel": metadata.get("openai_model", ""),
            "openaiReasoning": metadata.get("openai_reasoning", ""),
            "openaiMaxOutputTokens": metadata.get("openai_max_output_tokens", ""),
            "anthropicModel": metadata.get("anthropic_model", ""),
            "anthropicMaxOutputTokens": metadata.get("anthropic_max_output_tokens", ""),
            "anthropicWebSearch": metadata.get("anthropic_web_search", ""),
            "dryRun": dry_run,
        },
        "turnCount": len([turn for turn in turns if "summary" not in str(turn.get("speaker_label", "")).lower()]),
        "hasSummary": bool(summary_markdown),
        "isActive": is_active_run(run_dir.name),
    }
    if include_turns:
        payload["turns"] = [
            turn_to_api(turn) for turn in turns if "summary" not in str(turn.get("speaker_label", "")).lower()
        ]
        payload["summaryMarkdown"] = summary_markdown
    return payload


def list_conversations() -> List[Dict[str, Any]]:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    conversations = [build_conversation_payload(path, include_turns=False) for path in RUNS_ROOT.iterdir() if is_run_dir(path)]
    conversations.sort(key=lambda item: item.get("updatedAt", ""), reverse=True)
    return conversations


def persist_progress(
    run_dir: Path,
    args: SimpleNamespace,
    turns: List[orch.DebateTurn],
    summary_markdown: str,
) -> None:
    orch.write_markdown_logs(run_dir, args.question, turns, summary_markdown, args)
    orch.write_run_metadata(run_dir, args.question, turns, args)
    write_web_state(
        run_dir,
        {
            "question": args.question,
            "status": "running",
            "error": "",
            "last_turns": len(turns),
        },
    )


def execute_conversation(run_dir: Path, args: SimpleNamespace, resume_state: Optional[orch.ResumeState] = None) -> None:
    run_id = run_dir.name
    turns = list(resume_state.turns) if resume_state else []
    summary_markdown = resume_state.summary_markdown if resume_state else ""
    orch.load_dotenv(APP_ROOT / ".env")
    openai_label = f"OpenAI {args.openai_model}"
    anthropic_label = f"Anthropic {args.anthropic_model}"

    write_web_state(
        run_dir,
        {
            "question": args.question,
            "status": "running",
            "error": "",
            "created_at": read_web_state(run_dir).get("created_at", iso_now()),
            "started_at": iso_now(),
        },
    )

    try:
        if args.dry_run:
            while True:
                step, round_number = orch.determine_next_step(turns, args.rounds)
                completed_debate_turns = [turn for turn in turns if turn.round_number <= args.rounds]
                if summary_markdown.strip() and len(completed_debate_turns) >= args.rounds * 2:
                    step = "done"

                if step == "done":
                    write_web_state(
                        run_dir,
                        {
                            "status": "completed",
                            "completed_at": iso_now(),
                            "error": "",
                        },
                    )
                    orch.write_markdown_logs(run_dir, args.question, turns, summary_markdown, args)
                    orch.write_run_metadata(run_dir, args.question, turns, args)
                    return

                if step == "openai":
                    openai_prompt = orch.compose_debate_prompt(
                        speaker_name=openai_label,
                        counterpart_name=anthropic_label,
                        question=args.question,
                        round_number=round_number,
                        prior_turns=turns,
                    )
                    turns.append(
                        orch.build_stub_turn(
                            speaker_label=openai_label,
                            model=args.openai_model,
                            provider="openai",
                            round_number=round_number,
                            prompt=openai_prompt,
                        )
                    )
                    persist_progress(run_dir, args, turns, summary_markdown)
                    continue

                if step == "anthropic":
                    anthropic_prompt = orch.compose_debate_prompt(
                        speaker_name=anthropic_label,
                        counterpart_name=openai_label,
                        question=args.question,
                        round_number=round_number,
                        prior_turns=turns,
                    )
                    turns.append(
                        orch.build_stub_turn(
                            speaker_label=anthropic_label,
                            model=args.anthropic_model,
                            provider="anthropic",
                            round_number=round_number,
                            prompt=anthropic_prompt,
                        )
                    )
                    persist_progress(run_dir, args, turns, summary_markdown)
                    continue

                if step == "summary":
                    summary_markdown = "\n".join(
                        [
                            "# Claude's Final Wrap-Up",
                            "",
                            "## Final Verdict",
                            "",
                            f"[dry-run] {anthropic_label} would give the direct answer here.",
                            "",
                            "## Agreements",
                            "",
                            f"- [dry-run] Shared ground from {openai_label} and {anthropic_label}.",
                            "",
                            "## Disagreements",
                            "",
                            f"- [dry-run] Remaining differences between {openai_label} and {anthropic_label}.",
                            "",
                            f"## Best Insight from {openai_label}",
                            "",
                            f"[dry-run] Strongest contribution from {openai_label}.",
                            "",
                            f"## Best Insight from {anthropic_label}",
                            "",
                            f"[dry-run] Strongest contribution from {anthropic_label}.",
                            "",
                            "## Conclusion",
                            "",
                            f"[dry-run] {anthropic_label} would close with a concise conclusion here.",
                            "",
                            "## Sources Mentioned",
                            "",
                            "- [dry-run] No live sources fetched.",
                        ]
                    )
                    orch.write_markdown_logs(run_dir, args.question, turns, summary_markdown, args)
                    orch.write_run_metadata(run_dir, args.question, turns, args)
                    write_web_state(
                        run_dir,
                        {
                            "status": "completed",
                            "completed_at": iso_now(),
                            "error": "",
                        },
                    )
                    return

                raise RuntimeError(f"Unknown step type {step!r}")

        api_keys = orch.require_api_keys()
        while True:
            step, round_number = orch.determine_next_step(turns, args.rounds)
            completed_debate_turns = [turn for turn in turns if turn.round_number <= args.rounds]
            if summary_markdown.strip() and len(completed_debate_turns) >= args.rounds * 2:
                step = "done"

            if step == "done":
                write_web_state(
                    run_dir,
                    {
                        "status": "completed",
                        "completed_at": iso_now(),
                        "error": "",
                    },
                )
                orch.write_markdown_logs(run_dir, args.question, turns, summary_markdown, args)
                orch.write_run_metadata(run_dir, args.question, turns, args)
                return

            if step == "openai":
                openai_prompt = orch.compose_debate_prompt(
                    speaker_name=openai_label,
                    counterpart_name=anthropic_label,
                    question=args.question,
                    round_number=round_number,
                    prior_turns=turns,
                )
                openai_raw = orch.call_openai(
                    api_key=api_keys["openai"],
                    model=args.openai_model,
                    prompt=openai_prompt,
                    reasoning=args.openai_reasoning,
                    max_output_tokens=args.openai_max_output_tokens,
                )
                openai_response_text = orch.extract_openai_text(openai_raw)
                openai_prompt_for_log = openai_prompt
                if orch.should_retry_openai_for_visible_text(openai_raw, openai_response_text):
                    retry_raw = orch.retry_openai_with_more_headroom(
                        api_key=api_keys["openai"],
                        model=args.openai_model,
                        prompt=openai_prompt,
                        reasoning=args.openai_reasoning,
                        max_output_tokens=args.openai_max_output_tokens,
                    )
                    if retry_raw is not None:
                        openai_raw = retry_raw
                        openai_response_text = orch.extract_openai_text(openai_raw)
                        openai_prompt_for_log = (
                            openai_prompt
                            + "\n\n[auto-retry] The first OpenAI attempt exhausted max_output_tokens "
                            + "before producing visible text, so the orchestrator retried with a larger budget."
                        )
                turns.append(
                    orch.DebateTurn(
                        speaker_label=openai_label,
                        model=openai_raw.get("model", args.openai_model),
                        provider="openai",
                        round_number=round_number,
                        prompt=openai_prompt_for_log,
                        response_text=orch.normalize_response_text(openai_response_text),
                        citations=orch.extract_openai_citations(openai_raw),
                        usage=openai_raw.get("usage", {}),
                        raw_response=openai_raw,
                    )
                )
                persist_progress(run_dir, args, turns, summary_markdown)
                continue

            if step == "anthropic":
                anthropic_prompt = orch.compose_debate_prompt(
                    speaker_name=anthropic_label,
                    counterpart_name=openai_label,
                    question=args.question,
                    round_number=round_number,
                    prior_turns=turns,
                )
                anthropic_raw = orch.call_anthropic(
                    api_key=api_keys["anthropic"],
                    model=args.anthropic_model,
                    prompt=anthropic_prompt,
                    max_output_tokens=args.anthropic_max_output_tokens,
                    web_search_mode=args.anthropic_web_search,
                    max_searches=args.anthropic_max_searches,
                    fallback_without_web_search=args.fallback_without_web_search,
                )
                turns.append(
                    orch.DebateTurn(
                        speaker_label=anthropic_label,
                        model=anthropic_raw.get("model", args.anthropic_model),
                        provider="anthropic",
                        round_number=round_number,
                        prompt=anthropic_prompt,
                        response_text=orch.normalize_response_text(orch.extract_anthropic_text(anthropic_raw)),
                        citations=orch.extract_anthropic_citations(anthropic_raw),
                        usage=anthropic_raw.get("usage", {}),
                        raw_response=anthropic_raw,
                    )
                )
                persist_progress(run_dir, args, turns, summary_markdown)
                continue

            if step == "summary":
                summary_prompt = orch.compose_summary_prompt(
                    args.question,
                    turns,
                    openai_label=openai_label,
                    anthropic_label=anthropic_label,
                )
                summary_raw = orch.call_anthropic(
                    api_key=api_keys["anthropic"],
                    model=args.anthropic_model,
                    prompt=summary_prompt,
                    max_output_tokens=max(args.anthropic_max_output_tokens, 1600),
                    web_search_mode=args.anthropic_web_search,
                    max_searches=max(3, min(args.anthropic_max_searches, 5)),
                    fallback_without_web_search=args.fallback_without_web_search,
                )
                summary_markdown = orch.normalize_response_text(orch.extract_anthropic_text(summary_raw))
                turns.append(
                    orch.DebateTurn(
                        speaker_label=f"{anthropic_label} Summary",
                        model=summary_raw.get("model", args.anthropic_model),
                        provider="anthropic",
                        round_number=args.rounds + 1,
                        prompt=summary_prompt,
                        response_text=summary_markdown,
                        citations=orch.extract_anthropic_citations(summary_raw),
                        usage=summary_raw.get("usage", {}),
                        raw_response=summary_raw,
                    )
                )
                orch.write_markdown_logs(run_dir, args.question, turns[:-1], summary_markdown, args)
                orch.write_run_metadata(run_dir, args.question, turns, args)
                write_web_state(
                    run_dir,
                    {
                        "status": "completed",
                        "completed_at": iso_now(),
                        "error": "",
                    },
                )
                return

            raise RuntimeError(f"Unknown step type {step!r}")
    except orch.ApiError as exc:
        orch.write_markdown_logs(run_dir, args.question, turns, summary_markdown, args)
        orch.write_run_metadata(run_dir, args.question, turns, args)
        write_web_state(
            run_dir,
            {
                "status": "failed",
                "error": f"{exc.provider}: {exc}",
                "failed_at": iso_now(),
            },
        )
    except Exception as exc:  # pragma: no cover - defensive path
        orch.write_markdown_logs(run_dir, args.question, turns, summary_markdown, args)
        orch.write_run_metadata(run_dir, args.question, turns, args)
        write_web_state(
            run_dir,
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "failed_at": iso_now(),
            },
        )
    finally:
        clear_active_run(run_id)


def start_new_conversation(payload: Dict[str, Any]) -> Dict[str, Any]:
    question = str(payload.get("question", "")).strip()
    if not question:
        raise ValueError("Question is required.")

    args = make_runtime_args(question, payload)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = RUNS_ROOT / f"{orch.now_stamp()}-{orch.slugify(question)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    write_web_state(
        run_dir,
        {
            "question": question,
            "status": "queued",
            "created_at": iso_now(),
            "updated_at": iso_now(),
            "error": "",
        },
    )
    orch.write_markdown_logs(run_dir, question, [], "", args)
    orch.write_run_metadata(run_dir, question, [], args)

    thread = threading.Thread(target=execute_conversation, args=(run_dir, args), daemon=True)
    register_active_run(run_dir.name, thread)
    thread.start()
    return build_conversation_payload(run_dir)


def resume_conversation(run_id: str) -> Dict[str, Any]:
    run_dir = RUNS_ROOT / run_id
    if not is_run_dir(run_dir):
        raise FileNotFoundError(f"No conversation found for {run_id}")
    if is_active_run(run_id):
        return build_conversation_payload(run_dir)

    resume_state = orch.load_resume_state(str(run_dir))
    args = default_resume_args()
    orch.apply_resume_config(args, resume_state)
    thread = threading.Thread(target=execute_conversation, args=(run_dir, args, resume_state), daemon=True)
    register_active_run(run_id, thread)
    thread.start()
    write_web_state(run_dir, {"status": "running", "error": ""})
    return build_conversation_payload(run_dir)


def file_response_path(path: str) -> Path:
    if path == "/favicon.ico":
        relative = "favicon.svg"
    else:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
    candidate = (WEB_ROOT / relative).resolve()
    if WEB_ROOT.resolve() not in candidate.parents and candidate != WEB_ROOT.resolve():
        raise FileNotFoundError("Invalid path")
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(relative)
    return candidate


class AppHandler(BaseHTTPRequestHandler):
    server_version = "DuetLab/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.serve_static_file("index.html")
            return

        if parsed.path.startswith("/conversations/"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 2:
                self.serve_static_file("conversation.html")
                return

        if parsed.path == "/api/models":
            self.send_json(
                {
                    "openai": OPENAI_MODEL_OPTIONS,
                    "anthropic": ANTHROPIC_MODEL_OPTIONS,
                    "defaults": {
                        "openai": orch.DEFAULT_OPENAI_MODEL,
                        "anthropic": orch.DEFAULT_ANTHROPIC_MODEL,
                    },
                }
            )
            return

        if parsed.path == "/api/conversations":
            self.send_json({"conversations": list_conversations()})
            return

        if parsed.path.startswith("/api/conversations/"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 3:
                run_id = parts[2]
                run_dir = RUNS_ROOT / run_id
                if not is_run_dir(run_dir):
                    self.send_json({"error": "Conversation not found."}, status=HTTPStatus.NOT_FOUND)
                    return
                self.send_json(build_conversation_payload(run_dir))
                return

        try:
            file_path = file_response_path(parsed.path)
        except FileNotFoundError:
            self.send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
            return
        self.send_static_file(file_path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/conversations":
            try:
                payload = self.read_json_body()
            except json.JSONDecodeError:
                self.send_json({"error": "Invalid JSON body."}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                conversation = start_new_conversation(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json(conversation, status=HTTPStatus.CREATED)
            return

        if parsed.path.startswith("/api/conversations/") and parsed.path.endswith("/resume"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 4:
                run_id = parts[2]
                try:
                    self.read_json_body()
                except json.JSONDecodeError:
                    self.send_json({"error": "Invalid JSON body."}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    conversation = resume_conversation(run_id)
                except FileNotFoundError:
                    self.send_json({"error": "Conversation not found."}, status=HTTPStatus.NOT_FOUND)
                    return
                self.send_json(conversation)
                return

        self.send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def send_json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_static_file(self, relative_path: str) -> None:
        self.send_static_file(file_response_path(relative_path))

    def send_static_file(self, file_path: Path) -> None:
        mime, _ = mimetypes.guess_type(str(file_path))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.end_headers()
        self.wfile.write(file_path.read_bytes())

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def parse_server_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Duet Lab web interface.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def main() -> int:
    args = parse_server_args()
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Duet Lab running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
