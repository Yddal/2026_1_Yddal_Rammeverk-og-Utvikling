import json
from datetime import datetime, timezone
from pathlib import Path


RESPONSES_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = RESPONSES_DIR / "session_stats.md"


def find_model_event(events: list[dict]) -> dict:
    for event in reversed(events):
        content = event.get("content", {})
        if content.get("role") == "model":
            return event
    return {}


def format_timestamp(timestamp: float | None) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def summarize_session(session_file: Path) -> dict:
    with session_file.open("r", encoding="utf-8") as file:
        session_data = json.load(file)

    turns = session_data.get("turns", [])
    prompt_tokens = 0
    candidate_tokens = 0
    thoughts_tokens = 0
    total_tokens = 0
    last_timestamp = None
    model_version = "-"

    for turn in turns:
        event = find_model_event(turn.get("events", []))
        usage = event.get("usageMetadata", {})

        prompt_tokens += usage.get("promptTokenCount", 0)
        candidate_tokens += usage.get("candidatesTokenCount", 0)
        thoughts_tokens += usage.get("thoughtsTokenCount", 0)
        total_tokens += usage.get("totalTokenCount", 0)

        if event.get("timestamp"):
            last_timestamp = event["timestamp"]
        if event.get("modelVersion"):
            model_version = event["modelVersion"]

    return {
        "session_id": session_data.get("session_id", session_file.stem),
        "turns": len(turns),
        "model_version": model_version,
        "prompt_tokens": prompt_tokens,
        "candidate_tokens": candidate_tokens,
        "thoughts_tokens": thoughts_tokens,
        "total_tokens": total_tokens,
        "last_updated": format_timestamp(last_timestamp),
    }


def build_markdown_table(session_summaries: list[dict]) -> str:
    lines = [
        "# Session Stats",
        "",
        "## Forklaring Av Tokens",
        "",
        "- `Prompt Tokens`: Tokens i inputen som sendes til modellen. Dette inkluderer både den nåværende brukerbeskjeden og samtalekonteksten som ADK sender med fra sesjonen.",
        "- `Candidate Tokens`: Tokens i det synlige svaret modellen returnerer til brukeren.",
        "- `Thoughts Tokens`: Interne resonneringstokens modellen bruker før den lager det synlige svaret. De telles i bruken, men selve resonneringen vises ikke til brukeren.",
        "- `Total Tokens`: Totalt antall tokens brukt i det modellkallet.",
        "",
        "| Session ID | Turns | Model | Prompt Tokens | Candidate Tokens | Thoughts Tokens | Total Tokens | Last Updated |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
    ]

    for summary in sorted(session_summaries, key=lambda item: item["session_id"]):
        lines.append(
            "| {session_id} | {turns} | {model_version} | {prompt_tokens} | "
            "{candidate_tokens} | {thoughts_tokens} | {total_tokens} | "
            "{last_updated} |".format(**summary)
        )

    if not session_summaries:
        lines.append("| No session files found | 0 | - | 0 | 0 | 0 | 0 | - |")

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    session_files = sorted(
        file_path
        for file_path in RESPONSES_DIR.glob("*.json")
        if file_path.name != OUTPUT_FILE.name
    )

    session_summaries = [summarize_session(session_file) for session_file in session_files]
    markdown = build_markdown_table(session_summaries)
    OUTPUT_FILE.write_text(markdown, encoding="utf-8")

    print(f"Updated report: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
