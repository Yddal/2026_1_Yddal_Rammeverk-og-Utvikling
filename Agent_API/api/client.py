"""
Enkel REPL-klient mot ADK API-server (port 8001).

Flyt:
    1. Opprett en sesjon (POST /apps/<app>/users/<user>/sessions).
    2. Be brukeren skrive et spørsmål, send det via POST /run.
    3. Hent ut tekst fra siste model-event og vis det.
    4. Lagre hele event-loggen til responses/<session-id>.json.

ADK /run blokkerer til hele pipelinen er ferdig (planner + opptil 3
loop-iterasjoner). På lokal Ollama med qwen2.5:7b kan det ta flere minutter,
derfor er CLIENT_TIMEOUT_SECONDS satt høyt og kan overstyres via env-var.
"""

import json
import os
import uuid
from pathlib import Path

import requests


# === Konfig ===
BASE_URL = os.environ.get("ADK_API_BASE_URL", "http://127.0.0.1:8001")
APP_NAME = "SSB_agent_api"
USER_ID = "demo_user"
RESPONSES_DIR = Path(__file__).resolve().parent / "responses"

# /run blokkerer til hele agent-pipelinen er ferdig. På lokal Ollama er 60 sek
# for lavt — vi har sett kjøringer på 5-10 minutter. 600 sek (10 min) er en
# trygg default; overstyr med env-var hvis du vil ha kortere/lengre.
CLIENT_TIMEOUT_SECONDS = int(os.environ.get("CLIENT_TIMEOUT_SECONDS", "600"))

# Sesjonsoppretting er rask — egen, lavere timeout.
SESSION_TIMEOUT_SECONDS = 30


def create_session(session_id: str | None = None) -> str:
    """Opprett en ny ADK-sesjon. Returnerer sesjons-ID."""
    payload: dict = {}
    if session_id:
        payload["sessionId"] = session_id

    response = requests.post(
        f"{BASE_URL}/apps/{APP_NAME}/users/{USER_ID}/sessions",
        json=payload,
        timeout=SESSION_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["id"]


def run_agent(session_id: str, prompt: str) -> list[dict]:
    """Send et spørsmål til agent-pipelinen. Returnerer event-listen."""
    payload = {
        "appName": APP_NAME,
        "userId": USER_ID,
        "sessionId": session_id,
        "newMessage": {
            "role": "user",
            "parts": [{"text": prompt}],
        },
    }

    response = requests.post(
        f"{BASE_URL}/run",
        json=payload,
        timeout=CLIENT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def extract_agent_text(events: list[dict]) -> str:
    """Plukk ut tekst fra siste model-event i event-listen."""
    for event in reversed(events):
        content = event.get("content", {})
        if content.get("role") != "model":
            continue

        parts = content.get("parts", [])
        text_parts = [part.get("text", "") for part in parts if part.get("text")]
        if text_parts:
            return "\n".join(text_parts)

    return ""


def save_response(session_id: str, prompt: str, events: list[dict]) -> Path:
    """Append en samtale-tur til sesjonsfilen (lager den om nødvendig)."""
    RESPONSES_DIR.mkdir(exist_ok=True)
    response_file = RESPONSES_DIR / f"{session_id}.json"

    if response_file.exists():
        with response_file.open("r", encoding="utf-8") as file:
            session_log = json.load(file)
    else:
        session_log = {
            "session_id": session_id,
            "app_name": APP_NAME,
            "user_id": USER_ID,
            "turns": [],
        }

    session_log["turns"].append(
        {
            "user_message": prompt,
            "events": events,
        }
    )

    with response_file.open("w", encoding="utf-8") as file:
        json.dump(session_log, file, indent=2, ensure_ascii=False)

    return response_file


def _print_friendly_error(prefix: str, exc: Exception) -> None:
    """Vis en lesbar feilmelding uten full stack-trace."""
    print(f"[{prefix}] {type(exc).__name__}: {exc}")


def main() -> None:
    """REPL-løkke. Forblir i live ved nettverksfeil eller server-restart."""
    # Opprett sesjon én gang ved oppstart. Hvis dette feiler, gir vi opp —
    # poenget med klienten er å holde én sesjons-ID konstant gjennom løpet.
    try:
        session_id = create_session(f"demo-session-{uuid.uuid4().hex[:8]}")
    except requests.exceptions.RequestException as exc:
        _print_friendly_error("Kunne ikke opprette sesjon", exc)
        print("Sjekk at ADK API-serveren kjører på", BASE_URL)
        return

    print(f"Sesjon opprettet: {session_id}")
    print("Skriv 'exit' eller 'quit' for å avslutte.\n")

    while True:
        try:
            user_input = input("Enter a message for the agent: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAvslutter.")
            break

        if user_input.lower() in {"exit", "quit"}:
            break
        if not user_input.strip():
            continue

        # Wrap nettverkskallet — ConnectionError/Timeout skal IKKE krasje REPL-en.
        # Brukeren må kunne prøve igjen (f.eks. etter at de starter MCP-server).
        try:
            result = run_agent(session_id, user_input)
        except requests.exceptions.Timeout:
            print(
                f"[Timeout] Agenten brukte mer enn {CLIENT_TIMEOUT_SECONDS}s. "
                "Prøv et enklere spørsmål eller hev CLIENT_TIMEOUT_SECONDS."
            )
            continue
        except requests.exceptions.ConnectionError as exc:
            _print_friendly_error("Tilkobling avbrutt", exc)
            print("Sjekk at ADK API-serveren fortsatt kjører på", BASE_URL)
            continue
        except requests.exceptions.HTTPError as exc:
            _print_friendly_error("Server-feil", exc)
            continue

        agent_text = extract_agent_text(result)
        response_file = save_response(session_id, user_input, result)

        print(f"Session ID: {session_id}")
        print(f"Saved response to: {response_file}")
        if agent_text:
            print("Agent response:")
            print(agent_text)
        else:
            print("No response from agent.")


if __name__ == "__main__":
    main()
