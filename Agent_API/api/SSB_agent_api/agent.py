"""
SSB lønnsdata-agent — Google ADK pipeline med 4 spesialiserte agenter.

Pipeline:
    SequentialAgent: ssb_pipeline (root_agent)
    ├── planner_agent              -> klassifiserer spørsmål, lager plan eller avslår
    └── retrieval_loop (LoopAgent, max 3 iter, hopper over hvis IRRELEVANT)
        ├── retriever_agent        -> kaller MCP-verktøy mot SSB-cache
        ├── presenter_agent        -> formaterer rådata til norsk svar
        ├── verifier_agent         -> sjekker om svaret dekker spørsmålet
        └── escalation_checker     -> stopper loopen ved godkjenning eller stagnasjon
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import AsyncGenerator

from dotenv import load_dotenv
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.loop_agent import LoopAgent
from google.adk.agents.sequential_agent import SequentialAgent
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_toolset import StreamableHTTPConnectionParams
from google.genai import types


# ============================================================================
# === KONFIG: env-variabler, modell, MCP-tilkobling
# ============================================================================

load_dotenv()
logger = logging.getLogger(__name__)

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
MODEL_NAME = os.environ.get("AGENT_MODEL", "ollama_chat/qwen2.5")


# ----------------------------------------------------------------------------
# CUDA / GPU-AKTIVERING
#
# USE_CUDA-env-var:
#   "auto" (default) — detekter via nvidia-smi
#   "1"/"true"       — tving GPU
#   "0"/"false"      — tving CPU
# ----------------------------------------------------------------------------

def _detect_cuda_available() -> bool:
    """Returner True hvis nvidia-smi finnes og kjører uten feil."""
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        subprocess.run(
            ["nvidia-smi"],
            check=True,
            capture_output=True,
            timeout=5,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


_use_cuda_setting = os.environ.get("USE_CUDA", "auto").lower()
if _use_cuda_setting in {"1", "true", "yes", "on"}:
    USE_CUDA = True
elif _use_cuda_setting in {"0", "false", "no", "off"}:
    USE_CUDA = False
else:
    USE_CUDA = _detect_cuda_available()

NUM_GPU_LAYERS = -1 if USE_CUDA else 0
logger.info(
    "GPU/CUDA-modus: %s (USE_CUDA=%s, num_gpu=%d)",
    "AKTIVERT" if USE_CUDA else "DEAKTIVERT (CPU)",
    _use_cuda_setting,
    NUM_GPU_LAYERS,
)


# ----------------------------------------------------------------------------
# Per-agent LLM-instanser.
#
# Designprinsipper:
#   - Lav temperatur (0.0-0.2) for roller som KREVER format-presisjon:
#     planner (RELEVANT/IRRELEVANT-tag), retriever (tool-kall), verifier (OK/RETRY).
#   - Litt høyere temperatur for presenter — naturlig norsk-formulering tjener på det.
#   - Stor num_ctx på retriever fordi get_all_salary_data returnerer hele tabellen.
#   - num_gpu sendes som signal til Ollama for GPU-bruk.
# ----------------------------------------------------------------------------

planner_llm = LiteLlm(
    model=MODEL_NAME,
    temperature=0.1,
    num_gpu=NUM_GPU_LAYERS,
)

retriever_llm = LiteLlm(
    model=MODEL_NAME,
    temperature=0.0,
    num_gpu=NUM_GPU_LAYERS,
)

presenter_llm = LiteLlm(
    model=MODEL_NAME,
    temperature=0.4,
    num_gpu=NUM_GPU_LAYERS,
)

verifier_llm = LiteLlm(
    model=MODEL_NAME,
    temperature=0.0,
    num_gpu=NUM_GPU_LAYERS,
)


mcp_toolset = McpToolset(
    connection_params=StreamableHTTPConnectionParams(url=MCP_SERVER_URL),
)


# ============================================================================
# === STATE-INITIALISERING
# === ADK-prompts refererer til state-felter via {nøkkel}. Hvis nøkkelen
# === mangler ved første iterasjon, krasjer prompt-render. Vi seeder feltene.
# ============================================================================

def seed_loop_state(callback_context: CallbackContext):
    """Forhåndsfyll state-nøkler retrieval-loopens prompts refererer til."""
    state = callback_context.state
    state.setdefault("verification", "")
    state.setdefault("retrieved_data", "")
    state.setdefault("presentation", "")
    return None


# ============================================================================
# === ESCALATE-TAG-PARSING (planner)
# === Planner merker svaret med RELEVANT eller IRRELEVANT: på første linje.
# === Vi fjerner taggen før den vises bruker, og lagrer relevans-flagget.
# ============================================================================

_PLANNER_TAG_PATTERN = re.compile(
    r"^\s*\**\s*(RELEVANT|IRRELEVANT)\s*:?\s*\**\s*",
    flags=re.IGNORECASE,
)

# Heuristikk for tag-løse svar: planner glemmer ofte RELEVANT-prefikset på
# oppfølgings-spørsmål i samme session. Hvis svaret inneholder "næring:" er
# det en plan (RELEVANT), uavhengig av tag.
_PLAN_HINT_PATTERN = re.compile(r"\bnæring\s*:", re.IGNORECASE)


def _strip_escalate_tag(text: str) -> tuple[str, str | None]:
    """Returnerer (renset_tekst, normalisert_tag) — eller (originaltekst, None)."""
    match = _PLANNER_TAG_PATTERN.match(text)
    if match is None:
        return text, None
    return text[match.end():], match.group(1).upper()


def strip_planner_escalate_tag(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """Fjern RELEVANT/IRRELEVANT-tag fra planner-svar, lagre flagg i state."""
    if not llm_response.content or not llm_response.content.parts:
        return None

    is_relevant: bool | None = None
    changed = False
    full_text_parts: list[str] = []
    for part in llm_response.content.parts:
        text = getattr(part, "text", None)
        if not text:
            continue
        full_text_parts.append(text)
        new_text, tag = _strip_escalate_tag(text)
        if tag is None:
            continue
        part.text = new_text
        changed = True
        if is_relevant is None:
            is_relevant = (tag == "RELEVANT")

    if is_relevant is None:
        joined = "\n".join(full_text_parts)
        if _PLAN_HINT_PATTERN.search(joined):
            is_relevant = True
            logger.info(
                "Planner-svar manglet tag, men inneholder 'næring:' — "
                "tolker som RELEVANT (typisk oppfølgings-spørsmål)."
            )
        else:
            logger.warning(
                "Planner-svar manglet RELEVANT/IRRELEVANT-tag og ser ikke ut "
                "som en plan — defaulter til irrelevant."
            )

    callback_context.state["plan_is_relevant"] = bool(is_relevant)
    return llm_response if changed else None


def skip_loop_if_irrelevant(callback_context: CallbackContext):
    """Hopp over retrieval-loop hvis planner markerte spørsmålet som irrelevant."""
    if not callback_context.state.get("plan_is_relevant"):
        return types.Content(role="model", parts=[])
    return None


# ============================================================================
# === VERIFIER OK-PARSING
# === Verifier prefikser godkjente svar med "OK:". Vi fjerner prefikset og
# === setter flagg i state. Regex matcher OK som EGET ord (ikke "Også"/"Okay").
# ============================================================================

_VERIFIER_OK_PATTERN = re.compile(r"^\s*OK\b\s*:?\s*", flags=re.IGNORECASE)


def strip_verifier_ok_prefix(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """Fjern OK-prefiks og sett verifier_approved-flagg basert på match."""
    if not llm_response.content or not llm_response.content.parts:
        return None

    approved = False
    changed = False
    for part in llm_response.content.parts:
        text = getattr(part, "text", None)
        if not text:
            continue
        match = _VERIFIER_OK_PATTERN.match(text)
        if match is None:
            continue
        part.text = text[match.end():]
        approved = True
        changed = True

    callback_context.state["verifier_approved"] = approved
    return llm_response if changed else None


# ============================================================================
# === ESCALATION CHECKER
# === Stopper LoopAgent når:
# ===   - verifier_approved = True, eller
# ===   - retrieved_data er identisk med forrige iterasjon (stagnasjon).
# ============================================================================

class EscalationChecker(BaseAgent):
    """Stopper retrieval_loop ved godkjenning eller stagnasjon."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        approved = bool(state.get("verifier_approved"))

        current_data = state.get("retrieved_data", "")
        previous_data = state.get("_previous_retrieved_data", "")
        stalled = bool(current_data) and current_data == previous_data
        state["_previous_retrieved_data"] = current_data

        should_stop = approved or stalled
        if stalled and not approved:
            logger.info(
                "Retrieval-loop stoppes — retriever leverte samme data to ganger."
            )

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            actions=EventActions(escalate=should_stop),
        )


# ============================================================================
# === PROMPTS
# ============================================================================

PLANNER_INSTRUCTION = """
Du er Planner-agenten. Brukerens spørsmål er den siste meldingen i samtalen.

Avgjør om spørsmålet handler om norsk lønnsstatistikk fra SSB.

Svaret MÅ starte med ett av to ord på første linje:

"IRRELEVANT:" hvis spørsmålet IKKE handler om lønn/yrke/næring, ELLER
hvis du mangler info (næring/yrke) som er nødvendig for å hente data.
   Etter "IRRELEVANT:" skriver du en kort, høflig forklaring eller oppfølgings-
   spørsmål til brukeren — på NORSK.

"RELEVANT" hvis spørsmålet handler om lønn og du har nok info til å fortsette.
   Etter "RELEVANT" skriver du en kort plan i dette formatet:
       næring: <næringsord brukeren brukte, eller "alle">
       kjønn: <én av labelene nedenfor>
       alder: <én av labelene nedenfor>
       periode: <kvartal som "2025K4", eller "siste">

GYLDIGE SSB-LABELS — bruk dem EKSAKT (bruker skriver uformelt; du mapper til disse):
   kjønn:  "Begge kjønn" | "Menn" | "Kvinner"
   alder:  "Alle aldre" | "Under 25 år" | "25-39 år" | "40-54 år" | "55-66 år" | "67 år eller eldre"

MAPPING-EKSEMPLER:
   "28-åring", "ung" (under 40)   → "25-39 år"
   "nyutdannet" eller "under 25"  → "Under 25 år"
   "midt i karrieren", "voksen"   → "40-54 år"
   "før pensjon"                  → "55-66 år"
   "pensjonist", "eldre"          → "67 år eller eldre"
   ingen alder nevnt              → "Alle aldre"
   "dame"/"kvinne"/"jente"        → "Kvinner"
   "mann"/"gutt"                  → "Menn"
   begge eller ikke nevnt         → "Begge kjønn"

Hold svaret kort. Du resonnerer ikke i prosa — bare tag + plan eller tag + avslag.
"""


RETRIEVER_INSTRUCTION = """
Du er Retriever-agenten. Du har MCP-verktøy mot et cachet SSB-datasett (tabell 11656).

Plan fra Planner:
{plan}

DATAFORMAT (næring-nestet):
Verktøyene returnerer en liste av næring-objekter. Hvert objekt:

    {{
      "næring": "Reklamevirksomhet og markedsundersøkelser",
      "kvartaler": {{
        "2025K4": {{
          "Begge kjønn": {{
            "Alle aldre": {{ "statistikk": {{ "Gjennomsnittlig månedslønn": 55000, ... }} }},
            "25-39 år":   {{ "statistikk": {{ ... }} }}
          }},
          "Menn":    {{ ... }},
          "Kvinner": {{ ... }}
        }},
        "2025K3": {{ ... }}
      }}
    }}

VERKTØY:

1. get_salary_data(industry, period)  — HOVEDVERKTØY. Bruk dette FØRST.
   Returnerer næring-objekter med KUN ett kvartal.
   - industry: næringsnavn eller del av navn (case-insensitiv substring-match,
     f.eks. "reklame" matcher "Reklamevirksomhet og markedsundersøkelser").
     Send et SHORT, KARAKTERISTISK ord fra planens "næring:".
   - period: "2025K4". None = NYESTE TILGJENGELIGE (default).

2. get_salary_data_by_industry(industry)
   For historisk utvikling — returnerer ALLE kvartaler for én næring.

3. list_industries()  — FALLBACK. KALL UTEN ARGUMENTER.
   ⚠️ Dette verktøyet tar INGEN parametere. Skriv {} (tom dict) som arguments.
   IKKE send {industry: "..."} — det blir IGNORERT og du får hele lista uansett.
   Bruk KUN hvis get_salary_data returnerer 0 records, for å finne riktig
   stavemåte på næringen. Etterpå kaller du get_salary_data på nytt med
   eksakt navn fra lista.

4. fetch_salary_data()  — sjelden, kun for tvungen oppfrisking av cache.

ARBEIDSMETODE (følg denne rekkefølgen):

Steg 1: Les "næring:" og "periode:" fra planen.
Steg 2: Plukk et kort, karakteristisk ord fra næringen (f.eks. "reklame",
        "frisør", "bygg"). Bytt ut typos hvis du gjenkjenner dem
        ("reclame" → "reklame", "marketing" → "reklame").
Steg 3: Kall get_salary_data(industry="<ord>", period="<periode eller utelat>").
Steg 4a: Hvis du fikk records (antall > 0) → returner JSON-strengen. FERDIG.
Steg 4b: Hvis antall == 0 → kall list_industries() (INGEN args!) for å se
         alle 22 næringer. Velg riktig navn og GÅ TILBAKE til Steg 3 med
         det nye navnet. Ikke kall list_industries flere ganger i samme runde.

REGLER:
- Send alltid næringsnavn som streng, ikke liste.
- LES "periode:" fra planen. Hvis den er et konkret kvartal som "2024K2",
  send det som period-argument. Hvis den er "siste" eller mangler, la period
  stå tom — get_salary_data defaulter til nyeste tilgjengelige kvartal.
- IKKE forsøk å filtrere på "kjønn:" eller "alder:" — toolene støtter ikke
  det. Returner alltid hele næring-objektet; alle kjønn × aldersgrupper er
  allerede nestet under hvert kvartal. Presenter velger ut riktig undertre.
- Maks 3 tool-kall per runde. Hvis du ikke har data etter 3 kall, returner
  en JSON-streng med {"feil": "fant ikke næring X"} og stopp.

Verifier-tilbakemelding fra forrige iterasjon (tom = første runde):
{verification}

Returner næring-objektene som JSON-streng. IKKE skriv tekst til bruker.
"""


PRESENTER_INSTRUCTION = """
Du er Presenter-agenten. Skriv et klart, vennlig svar PÅ NORSK basert på
rådataene fra Retriever.

Brukerens spørsmål (fra Planner-planen):
{plan}

Rådata (næring-nestet JSON):
{retrieved_data}

NAVIGERING I JSON-STRUKTUREN (VIKTIG):
Rådataene inneholder ALLE kjønn og ALLE aldersgrupper nestet under hver
næring. DU må plukke ut riktig kombinasjon basert på planen:

    data[næring].kvartaler[<periode>][<kjønn>][<alder>].statistikk

Bruk EKSAKT verdiene fra planens "kjønn:" og "alder:"-felter som JSON-nøkler:
    - kjønn: "Begge kjønn" | "Menn" | "Kvinner"
    - alder: "Alle aldre" | "Under 25 år" | "25-39 år" | "40-54 år"
             | "55-66 år" | "67 år eller eldre"

EKSEMPEL:
Plan sier «kjønn: Kvinner, alder: 25-39 år, periode: 2026K1». Da skal du
rapportere tallene under nøkkelen ["2026K1"]["Kvinner"]["25-39 år"], IKKE
["Begge kjønn"]["Alle aldre"]. Hvis planen ber om en spesifikk demografi,
ikke rapporter "Alle aldre" eller "Begge kjønn" — det er feil målgruppe.

KRAV:
- Skriv på norsk, forståelig og konsist.
- Inkluder periode (kvartal), næring, kjønn og aldersgruppe eksplisitt i svaret.
- Hvis dataene har "konfidensielt" eller manglende verdier for den valgte
  demografien — si det. IKKE fall tilbake til "Alle aldre" uten å forklare.
- Avslutt med: Kilde: SSB tabell 11656.
- Ikke finn på tall som ikke står i {retrieved_data}.
- Bruk tabell-format hvis det er flere næringer/grupper å sammenligne i dataene.
"""


VERIFIER_INSTRUCTION = """
Du er Verifier-agenten. Vurder om presentasjonen besvarer spørsmålet.

Brukerens spørsmål (fra Planner-planen):
{plan}

Presentasjon til bruker:
{presentation}

Sjekk EKSPLISITT:
1. Næring: stemmer næringen i presentasjonen med planens "næring:"?
2. Periode: stemmer kvartalet med planens "periode:"?
3. Kjønn: stemmer kjønnet med planens "kjønn:"? Hvis planen sier "Kvinner"
   og presentasjonen rapporterer "Begge kjønn", er det FEIL → RETRY.
4. Alder: stemmer aldersgruppen med planens "alder:"? Hvis planen sier
   "25-39 år" og presentasjonen rapporterer "Alle aldre", er det FEIL → RETRY.
5. Hallusinerte tall: står tallene i rådataene, eller er noe oppfunnet?

Svaret MÅ starte med ett av to ord:

Hvis presentasjonen er god (alle 5 punkter OK):
    "OK:" etterfulgt av presentasjonen ord-for-ord uendret.
    Eksempel: "OK: <presentasjonen>"

Hvis noe mangler eller er feil:
    "RETRY:" etterfulgt av en kort, konkret instruks til Retriever ELLER
    Presenter. Vær spesifikk om hva som er feil.
    Eksempler:
    - "RETRY: Presenter rapporterte 'Alle aldre', men planen ber om '25-39 år'."
    - "RETRY: Mangler kvartalet 2026K1 i rådataene — hent på nytt."
"""


# ============================================================================
# === AGENT-OPPSETT
# ============================================================================

planner_agent = LlmAgent(
    model=planner_llm,
    name="planner_agent",
    description="Klassifiserer brukerens spørsmål og lager kort plan.",
    instruction=PLANNER_INSTRUCTION,
    output_key="plan",
    before_agent_callback=seed_loop_state,
    after_model_callback=strip_planner_escalate_tag,
)

retriever_agent = LlmAgent(
    model=retriever_llm,
    name="retriever_agent",
    description="Henter relevante records fra cachet SSB-datasett via MCP.",
    instruction=RETRIEVER_INSTRUCTION,
    tools=[mcp_toolset],
    output_key="retrieved_data",
)

presenter_agent = LlmAgent(
    model=presenter_llm,
    name="presenter_agent",
    description="Formaterer SSB-data til norsk svar.",
    instruction=PRESENTER_INSTRUCTION,
    output_key="presentation",
)

verifier_agent = LlmAgent(
    model=verifier_llm,
    name="verifier_agent",
    description="Verifiserer at presentasjonen besvarer spørsmålet.",
    instruction=VERIFIER_INSTRUCTION,
    output_key="verification",
    after_model_callback=strip_verifier_ok_prefix,
)

escalation_checker = EscalationChecker(
    name="escalation_checker",
    description="Stopper retrieval_loop ved godkjenning eller stagnasjon.",
)

retrieval_loop = LoopAgent(
    name="retrieval_loop",
    sub_agents=[retriever_agent, presenter_agent, verifier_agent, escalation_checker],
    max_iterations=3,
    before_agent_callback=skip_loop_if_irrelevant,
    description="Henter, presenterer og verifiserer — looper opptil 3 ganger.",
)

root_agent = SequentialAgent(
    name="ssb_pipeline",
    sub_agents=[planner_agent, retrieval_loop],
    description="Planner → (Retriever → Presenter → Verifier → escalation_checker)*",
)

app = App(
    name="SSB_agent_api",
    root_agent=root_agent,
)
