"""
SSB-klient for tabell 11656 (lønnstakere — lønn etter næring/kjønn/alder/kvartal).

Forenklet versjon:
    - Henter metadata + hele datasettet UTEN filtrering.
    - Konverterer JSON-Stat 2.0 til wide-nested format med klartekst-labels.
    - Cacher i minne (TTL 1 time) OG skriver til disk for inspeksjon.

Public API:
    get_all_salary_data()           -> wide-nested records
    list_industries()               -> alle næringer i klartekst
    list_age_groups()               -> alle aldersgrupper i klartekst
    list_genders()                  -> alle kjønn-kategorier i klartekst
    list_periods()                  -> alle kvartaler, nyeste først
    fetch_and_cache_salary_data()   -> tving ny nedlasting fra SSB

Cache-filer:
    MCP_server_SSB/cache/salary_data.json   (wide-nested data)
    MCP_server_SSB/cache/metadata.json      (rå metadata fra SSB)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from itertools import product
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ============================================================================
# === KONFIG
# ============================================================================

# Hardkodede SSB-endepunkter.
#   - codelist[NACE2007]=agg_NACE2007arb22 → 22-grupperingen i stedet for hundrevis av koder.
#   - valueCodes[<dim>]=* → returner ALLE verdier for dimensjonen.
#     Uten denne returnerer SSB kun default-utvalg (typisk én verdi per dimensjon),
#     som kollapser Alder og Tid bort fra responsen.
SSB_DATA_URL = (
    "https://data.ssb.no/api/pxwebapi/v2/tables/11656/data"
    "?lang=no"
    "&codelist[NACE2007]=agg_NACE2007arb22"
    "&valueCodes[NACE2007]=*"
    "&valueCodes[Kjonn]=*"
    "&valueCodes[Alder]=*"
    "&valueCodes[ContentsCode]=*"
    "&valueCodes[Tid]=*"
)
SSB_METADATA_URL = (
    "https://data.ssb.no/api/pxwebapi/v2/tables/11656/metadata"
    "?lang=no"
    "&codelist[NACE2007]=agg_NACE2007arb22"
)

# Disk-cache: sammen med kildekoden i MCP_server_SSB/cache/.
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_FILE = CACHE_DIR / "salary_data.json"
METADATA_FILE = CACHE_DIR / "metadata.json"

CACHE_TTL_SECONDS = 3600         # 1 time i minnet før vi henter på nytt.
HTTP_TIMEOUT_SECONDS = 60        # SSB svarer normalt < 5 sek; trygt slingringsmonn.


# ============================================================================
# === STATUS-KODER
# === SSB sender ":", "..", "." osv. for celler uten verdi. Oversett til
# === klartekst slik at LLM forstår hvorfor en verdi mangler.
# ============================================================================

STATUS_LABELS: dict[str, str] = {
    ":":  "konfidensielt",
    "..": "ikke_tilgjengelig",
    ".":  "ikke_aktuelt",
    "-":  "null",
}


def _translate_status(raw: Any) -> Any:
    if raw is None:
        return None
    return STATUS_LABELS.get(raw, raw)


# ============================================================================
# === HTTP
# ============================================================================

async def _fetch_json(url: str) -> dict[str, Any]:
    """GET en URL og returner JSON-body."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.json()


# ============================================================================
# === CACHE-STATE
# ============================================================================

_cache_lock = asyncio.Lock()
_cached_data: list[dict[str, Any]] | None = None
_cached_metadata: dict[str, Any] | None = None
_cache_timestamp: float = 0.0


# ============================================================================
# === METADATA
# ============================================================================

async def _load_metadata() -> dict[str, Any]:
    """Hent (og cache) tabellens metadata. Skriver også til disk."""
    global _cached_metadata
    if _cached_metadata is not None:
        return _cached_metadata

    logger.info("Henter SSB-metadata fra %s", SSB_METADATA_URL)
    raw = await _fetch_json(SSB_METADATA_URL)
    _cached_metadata = raw

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    METADATA_FILE.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Metadata lagret til %s", METADATA_FILE)
    return raw


def _dim_labels(metadata: dict[str, Any], dim_id: str) -> dict[str, str]:
    """Returner {kode: klartekst} for en dimensjon."""
    return metadata["dimension"][dim_id]["category"]["label"]


# ============================================================================
# === JSON-STAT 2.0 → NESTED-BY-INDUSTRY
# === Resultat-format (én entry per næring, alt øvrig nestet under):
# === [
# ===   {
# ===     "næring": "Reklamevirksomhet og markedsundersøkelser",
# ===     "kvartaler": {
# ===       "2025K4": {
# ===         "Begge kjønn": {
# ===           "Alle aldre": {
# ===             "statistikk": {
# ===               "Gjennomsnittlig månedslønn": 55000,
# ===               "Median månedslønn": 52000,
# ===               ...
# ===             }
# ===           },
# ===           "25-39 år": { "statistikk": { ... } }
# ===         },
# ===         "Menn":    { ... },
# ===         "Kvinner": { ... }
# ===       },
# ===       "2025K3": { ... }
# ===     }
# ===   }
# === ]
# === Fordel: næringsnavnet skrives ÉN gang per næring i stedet for ~600 ganger
# === som i flat liste. JSON-en blir mindre og lettere å skanne for LLM.
# ============================================================================

# Mapping fra SSB-dimensjons-ID til menneskevennlig nøkkel.
_DIM_NAME_MAP = {
    "NACE2007": "næring",
    "Tid": "kvartal",
    "Kjonn": "kjønn",
    "Alder": "aldersgruppe",
    "ContentsCode": "statistikkvariabel",
}


def _to_nested_by_industry(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """Konverter JSON-Stat 2.0 til næring-nestet klartekst-format."""
    dim_ids: list[str] = dataset["id"]
    dimension = dataset["dimension"]
    values: list[Any] = dataset.get("value", [])
    status = dataset.get("status", {})

    # Bygg ordnede (kode, label)-par per dimensjon i samme rekkefølge som value-listen.
    per_dim: list[list[tuple[str, str]]] = []
    for dim_id in dim_ids:
        cat = dimension[dim_id]["category"]
        index_map: dict[str, int] = cat["index"]
        label_map: dict[str, str] = cat["label"]
        ordered = sorted(index_map.items(), key=lambda kv: kv[1])
        per_dim.append([(code, label_map.get(code, code)) for code, _ in ordered])

    expected = 1
    for d in per_dim:
        expected *= len(d)
    if expected != len(values):
        logger.warning(
            "JSON-Stat-mismatch: forventet %d celler, fikk %d.",
            expected,
            len(values),
        )

    # Bygg nestet struktur direkte: næring → kvartal → kjønn → aldersgruppe → statistikk.
    by_industry: dict[str, dict[str, Any]] = {}

    for flat_index, combo in enumerate(product(*per_dim)):
        labels: dict[str, str] = {}
        for dim_id, (_code, label) in zip(dim_ids, combo):
            key = _DIM_NAME_MAP.get(dim_id, dim_id)
            labels[key] = label

        value = values[flat_index] if flat_index < len(values) else None

        if isinstance(status, dict):
            raw_status = status.get(str(flat_index))
        elif isinstance(status, list):
            raw_status = status[flat_index] if flat_index < len(status) else None
        elif isinstance(status, str):
            raw_status = status
        else:
            raw_status = None
        translated_status = _translate_status(raw_status)

        # Plukk ut dimensjonsverdier (de står i klartekst i `labels`).
        ind = labels.get("næring", "?")
        kv = labels.get("kvartal", "?")
        kj = labels.get("kjønn", "?")
        al = labels.get("aldersgruppe", "?")
        metric = labels.get("statistikkvariabel", "?")

        # Naviger ned i strukturen og legg verdien på rett plass.
        ind_obj = by_industry.setdefault(ind, {"næring": ind, "kvartaler": {}})
        kv_obj = ind_obj["kvartaler"].setdefault(kv, {})
        kj_obj = kv_obj.setdefault(kj, {})
        al_obj = kj_obj.setdefault(al, {"statistikk": {}, "status": {}})
        al_obj["statistikk"][metric] = value
        if translated_status is not None:
            al_obj["status"][metric] = translated_status

    # Rydd: fjern tom status-dict på det innerste nivået.
    for ind_obj in by_industry.values():
        for kv_obj in ind_obj["kvartaler"].values():
            for kj_obj in kv_obj.values():
                for al_obj in kj_obj.values():
                    if not al_obj["status"]:
                        del al_obj["status"]

    # Sorter næringer alfabetisk for stabil output.
    return [by_industry[name] for name in sorted(by_industry.keys())]


# ============================================================================
# === HOVEDFUNKSJON: hent og cache
# ============================================================================

async def fetch_and_cache_salary_data() -> list[dict[str, Any]]:
    """
    Last ned hele SSB-tabell 11656, konverter til næring-nestet format, og cache.
    Skriver også til disk for inspeksjon i MCP_server_SSB/cache/.
    """
    global _cached_data, _cache_timestamp

    async with _cache_lock:
        now = time.time()
        if _cached_data is not None and (now - _cache_timestamp) < CACHE_TTL_SECONDS:
            return _cached_data

        await _load_metadata()

        logger.info("Henter SSB-data fra %s", SSB_DATA_URL)
        raw = await _fetch_json(SSB_DATA_URL)
        dataset = raw.get("dataset", raw)

        industries = _to_nested_by_industry(dataset)
        logger.info("Konvertert til %d næring-nestede objekter.", len(industries))

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps(industries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Lønnsdata lagret til %s", CACHE_FILE)

        _cached_data = industries
        _cache_timestamp = now
        return industries


# ============================================================================
# === FILTRERINGSHJELPERE
# ============================================================================

_PERIOD_PATTERN = re.compile(r"^\s*(\d{4})K(\d+)\s*$")


def _period_sort_key(code: str) -> tuple[int, int]:
    """Konverter '2025K4' → (2025, 4) for stabil sortering på (år, kvartal)."""
    match = _PERIOD_PATTERN.match(code)
    if match is None:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def _industry_matches(industry_name: str, query: str) -> bool:
    """Case-insensitiv substring-match mellom næringsnavn og søkestreng."""
    return query.lower().strip() in industry_name.lower()


def _latest_period(industries: list[dict[str, Any]]) -> str | None:
    """Finn nyeste kvartal på tvers av alle næring-objekter."""
    periods: set[str] = set()
    for ind_obj in industries:
        periods.update(ind_obj.get("kvartaler", {}).keys())
    if not periods:
        return None
    return max(periods, key=_period_sort_key)


# ============================================================================
# === PUBLIC API
# ============================================================================

async def get_all_salary_data() -> list[dict[str, Any]]:
    """Returner hele lønnsdatasettet (næring-nestet klartekst)."""
    return await fetch_and_cache_salary_data()


async def get_salary_data(
    industry: str | None = None,
    period: str | None = None,
) -> list[dict[str, Any]]:
    """
    Returner filtrerte lønnsdata fra cachet datasett.

    Args:
        industry: Næringsnavn eller del av navn (case-insensitiv substring-match).
            None = alle næringer.
        period: Kvartal som "2025K4". None = nyeste tilgjengelige kvartal.

    Returns:
        Liste av næring-objekter, men kun med valgt kvartal beholdt under
        "kvartaler". Strukturen ellers er identisk med get_all_salary_data().
    """
    all_industries = await fetch_and_cache_salary_data()

    # Filtrer på næring først (eller behold alle).
    if industry:
        candidates = [
            ind_obj for ind_obj in all_industries
            if _industry_matches(ind_obj.get("næring", ""), industry)
        ]
    else:
        candidates = list(all_industries)

    target_period = period or _latest_period(candidates)
    if target_period is None:
        return []

    # Bygg redusert kopi: hvert næring-objekt med kun valgt kvartal.
    pruned: list[dict[str, Any]] = []
    for ind_obj in candidates:
        kvartaler = ind_obj.get("kvartaler", {})
        if target_period in kvartaler:
            pruned.append({
                "næring": ind_obj["næring"],
                "kvartaler": {target_period: kvartaler[target_period]},
            })
    return pruned


async def get_salary_data_by_industry(industry: str) -> list[dict[str, Any]]:
    """
    Returner næring-objekt(er) som matcher, med ALLE kvartaler bevart
    (alle kjønn × alle aldre × alle kvartaler).

    Args:
        industry: Næringsnavn eller del av navn (case-insensitiv substring-match).

    Returns:
        Liste av næring-objekter. Hvert objekt har full historikk.
    """
    if not industry or not industry.strip():
        raise ValueError("industry er påkrevd og kan ikke være tom.")

    all_industries = await fetch_and_cache_salary_data()
    return [
        ind_obj for ind_obj in all_industries
        if _industry_matches(ind_obj.get("næring", ""), industry)
    ]


async def list_industries() -> list[str]:
    """Alle næringer i klartekst, sortert alfabetisk."""
    metadata = await _load_metadata()
    labels = _dim_labels(metadata, "NACE2007")
    return sorted(labels.values())


async def list_age_groups() -> list[str]:
    """Alle aldersgrupper i klartekst, i SSBs egen rekkefølge."""
    metadata = await _load_metadata()
    labels = _dim_labels(metadata, "Alder")
    return list(labels.values())


async def list_genders() -> list[str]:
    """Alle kjønn-kategorier i klartekst."""
    metadata = await _load_metadata()
    labels = _dim_labels(metadata, "Kjonn")
    return list(labels.values())


async def list_periods() -> list[str]:
    """Alle tilgjengelige kvartaler, nyeste først."""
    metadata = await _load_metadata()
    labels = _dim_labels(metadata, "Tid")
    # Strengsortering fungerer på "2025K4"-format så lenge kvartalsnummeret er ett siffer.
    return sorted(labels.values(), reverse=True)
