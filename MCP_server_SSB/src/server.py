"""
MCP-server for SSB lønnstatistikk (tabell 11656) — forenklet versjon med
server-side filtrering på næring og kvartal.

Eksponerer 6 tools:
    - fetch_salary_data            : trigger nedlasting/oppfrisking av cache
    - list_industries              : alle næringer i klartekst
    - list_age_groups              : alle aldersgrupper i klartekst
    - get_salary_data              : filtrert uttrekk (næring + kvartal, default nyeste kvartal)
    - get_salary_data_by_industry  : all historikk for én næring (alle kjønn × alle aldre × alle kvartaler)
    - get_all_salary_data          : hele datasettet wide-nested (kun for inspeksjon — stor!)

Server kjøres via __init__.py med transport "http" eller "stdio".
"""

import json

from mcp.server.fastmcp import FastMCP

from ssb_client import (
    fetch_and_cache_salary_data,
    get_all_salary_data,
    get_salary_data,
    get_salary_data_by_industry,
    list_age_groups,
    list_industries,
)


server = FastMCP("MCP_server_SSB")


@server.tool(
    name="fetch_salary_data",
    title="Fetch SSB salary data",
    description=(
        "Trigger nedlasting av SSB-tabell 11656 og caching til disk. Bruk hvis "
        "du vil tvinge oppfrisking — ellers henter de andre toolene fra cache."
    ),
)
async def fetch_salary_data_tool() -> str:
    records = await fetch_and_cache_salary_data()
    return json.dumps(
        {"status": "ok", "antall_records": len(records)},
        ensure_ascii=False,
    )


@server.tool(
    name="list_industries",
    title="List industries (klartekst)",
    description=(
        "Returner alle næringer (NACE2007 22-grupperingen) i klartekst, sortert "
        "alfabetisk. Bruk for å finne eksakt næringsnavn å filtrere på."
    ),
)
async def list_industries_tool() -> str:
    return json.dumps(
        {"naeringer": await list_industries()},
        ensure_ascii=False,
    )


@server.tool(
    name="list_age_groups",
    title="List age groups (klartekst)",
    description=(
        "Returner alle aldersgrupper i klartekst (f.eks. 'Alle aldre', "
        "'25-39 år', '40-54 år')."
    ),
)
async def list_age_groups_tool() -> str:
    return json.dumps(
        {"aldersgrupper": await list_age_groups()},
        ensure_ascii=False,
    )


@server.tool(
    name="get_salary_data",
    title="Get salary data (filtered)",
    description=(
        "Hovedverktøyet for lønnsuttrekk. Returnerer næring-nestet struktur "
        "filtrert på næring og kvartal.\n\n"
        "PARAMETERE:\n"
        "  - industry (valgfri): næringsnavn eller del av navn (case-insensitiv "
        "    substring-match). F.eks. 'reklame' matcher 'Reklamevirksomhet ...'.\n"
        "    None = alle næringer.\n"
        "  - period (valgfri): kvartal som '2025K4'. None = NYESTE TILGJENGELIGE.\n\n"
        "RETUR: liste av næring-objekter. Hvert objekt:\n"
        "  { \"næring\": \"...\",\n"
        "    \"kvartaler\": { \"2025K4\": { \"Begge kjønn\": { \"Alle aldre\": "
        "{ \"statistikk\": {...} } } } } }"
    ),
)
async def get_salary_data_tool(
    industry: str | None = None,
    period: str | None = None,
) -> str:
    records = await get_salary_data(industry=industry, period=period)
    return json.dumps(
        {"antall": len(records), "data": records},
        ensure_ascii=False,
    )


@server.tool(
    name="get_salary_data_by_industry",
    title="Get salary data for one industry (all history)",
    description=(
        "Returner ALL data for én næring: alle kvartaler × alle kjønn × alle "
        "aldersgrupper. Bruk dette for historisk utvikling eller demografisk "
        "sammenligning innen samme næring.\n\n"
        "PARAMETERE:\n"
        "  - industry (påkrevd): næringsnavn eller del av navn (case-insensitiv).\n\n"
        "RETUR: liste av næring-objekter med full historikk under 'kvartaler'."
    ),
)
async def get_salary_data_by_industry_tool(industry: str) -> str:
    try:
        records = await get_salary_data_by_industry(industry=industry)
    except ValueError as exc:
        return json.dumps({"feil": str(exc)}, ensure_ascii=False)

    return json.dumps(
        {"antall": len(records), "data": records},
        ensure_ascii=False,
    )


@server.tool(
    name="get_all_salary_data",
    title="Get FULL dataset (klartekst, wide-nested)",
    description=(
        "Returner HELE lønnsdatasettet for SSB-tabell 11656. ADVARSEL: stor "
        "respons (~10 000 records). Bruk get_salary_data eller "
        "get_salary_data_by_industry i stedet for normale spørringer."
    ),
)
async def get_all_salary_data_tool() -> str:
    records = await get_all_salary_data()
    return json.dumps(
        {"antall": len(records), "data": records},
        ensure_ascii=False,
    )
