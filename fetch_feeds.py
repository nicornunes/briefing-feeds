
"""
fetch_feeds.py — coletor de feeds para o Morning Briefing.

Roda no GitHub Actions (internet liberada), busca os RSS/Atom das fontes,
normaliza cada item para um schema comum, deduplica, ordena do mais novo
para o mais antigo, limita por fonte e grava feeds.json.

Resiliente: se um feed falhar, registra em _meta.failed e continua —
nunca derruba a execução inteira. Preferimos feedparser; se ele não
estiver disponível, cai para um parser mínimo da stdlib.

Para adicionar/remover uma fonte, basta editar a lista SOURCES abaixo.
Limitação honesta: isto cobre apenas RSS/headlines gratuitos.
Conteúdo com paywall (FT/Bloomberg/WSJ full-text) NÃO é coberto.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from email.utils import parsedate_to_datetime

try:
    import feedparser  # type: ignore
    HAVE_FEEDPARSER = True
except Exception:  # pragma: no cover
    HAVE_FEEDPARSER = False

try:
    import requests  # type: ignore
    HAVE_REQUESTS = True
except Exception:  # pragma: no cover
    HAVE_REQUESTS = False


# --- Configuração das fontes -------------------------------------------------
# section: rótulo usado pelo briefing (seções do skill).
# Edite aqui para incluir/excluir feeds. url precisa ser um RSS/Atom.
SOURCES = [
    # Brasil — mercado
    {"id": "valor",        "section": "brasil_mercado", "url": "https://pox.globo.com/rss/valor"},
    {"id": "infomoney",    "section": "brasil_mercado", "url": "https://www.infomoney.com.br/feed/"},
    {"id": "exame_economia","section": "brasil_mercado","url": "https://exame.com/feed/"},
    # Brasil geral & mundo
    {"id": "cnn_brasil",   "section": "brasil_geral",   "url": "https://www.cnnbrasil.com.br/feed/"},
    {"id": "g1",           "section": "brasil_geral",   "url": "https://g1.globo.com/rss/g1/"},
    # Mundo / mercados
    {"id": "investing",    "section": "mundo",          "url": "https://www.investing.com/rss/news.rss"},
    {"id": "yahoo_finance","section": "mundo",          "url": "https://finance.yahoo.com/news/rssindex"},
    # Oficiais
    {"id": "fed",          "section": "oficiais",       "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"id": "fazenda",      "section": "oficiais",       "url": "https://news.google.com/rss/search?q=site:gov.br/fazenda&hl=pt-BR&gl=BR&ceid=BR:pt-419
"},
    {"id": "cvm",          "section": "oficiais",       "url": "https://www.gov.br/cvm/pt-br/assuntos/noticias/RSS"},
    {"id": "ecb",          "section": "oficiais",       "url": "https://www.ecb.europa.eu/rss/press.html"},
    {"id": "world_bank",   "section": "oficiais",       "url": "https://www.worldbank.org/en/news/all?format=rss"},
]

PER_SOURCE_CAP = 30
USER_AGENT = "briefing-feeds/1.0 (+github actions; morning-briefing collector)"
TIMEOUT = 25


def _now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _to_utc_iso(value) -> str | None:
    """Normaliza várias formas de data para ISO-8601 em UTC (sufixo +00:00)."""
    if not value:
        return None
    # feedparser struct_time (UTC)
    if isinstance(value, tuple) and len(value) >= 6:
        try:
            d = dt.datetime(*value[:6], tzinfo=dt.timezone.utc)
            return d.replace(microsecond=0).isoformat()
        except Exception:
            return None
    if isinstance(value, str):
        # tenta RFC 2822 (ex.: "Fri, 05 Sep 2026 18:00:00 +0200" ou "GMT")
        try:
            d = parsedate_to_datetime(value)
            if d is not None:
                if d.tzinfo is None:
                    d = d.replace(tzinfo=dt.timezone.utc)
                return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
        except Exception:
            pass
        # tenta ISO (ex.: "2026-09-05T16:00:00Z")
        try:
            s = value.strip().replace("Z", "+00:00")
            d = dt.datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
        except Exception:
            return None
    return None


def _fetch_raw(url: str) -> bytes:
    if HAVE_REQUESTS:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content
    # fallback stdlib
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec - fontes fixas
        return resp.read()


def _parse_items(raw: bytes):
    """Retorna lista de dicts {title, url, summary, published_raw}."""
    if HAVE_FEEDPARSER:
        d = feedparser.parse(raw)
        out = []
        for e in d.entries:
            published = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
            out.append({
                "title": (getattr(e, "title", "") or "").strip(),
                "url": (getattr(e, "link", "") or "").strip(),
                "summary": (getattr(e, "summary", "") or "").strip(),
                "published_raw": published,
            })
        return out
    # fallback mínimo da stdlib (RSS 2.0 e Atom básicos)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw)
    out = []
    # RSS: channel/item ; Atom: feed/entry
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item")
    if items:
        for it in items:
            out.append({
                "title": (it.findtext("title") or "").strip(),
                "url": (it.findtext("link") or "").strip(),
                "summary": (it.findtext("description") or "").strip(),
                "published_raw": it.findtext("pubDate"),
            })
    else:
        for it in root.findall(".//atom:entry", ns):
            link_el = it.find("atom:link", ns)
            out.append({
                "title": (it.findtext("atom:title", default="", namespaces=ns) or "").strip(),
                "url": (link_el.get("href") if link_el is not None else "").strip(),
                "summary": (it.findtext("atom:summary", default="", namespaces=ns) or "").strip(),
                "published_raw": it.findtext("atom:updated", default=None, namespaces=ns),
            })
    return out


def main() -> int:
    fetched_iso = _now_utc_iso()
    all_items = []
    succeeded, failed = [], []

    for src in SOURCES:
        try:
            raw = _fetch_raw(src["url"])
            parsed = _parse_items(raw)
            count = 0
            for it in parsed[: PER_SOURCE_CAP * 2]:  # margem antes do cap final
                if not it["title"] or not it["url"]:
                    continue
                all_items.append({
                    "source": src["id"],
                    "section": src["section"],
                    "title": it["title"],
                    "url": it["url"],
                    "summary": it["summary"],
                    "published_iso": _to_utc_iso(it["published_raw"]),
                    "fetched_iso": fetched_iso,
                })
                count += 1
            succeeded.append({"id": src["id"], "items": count})
        except Exception as exc:  # nunca derruba a execução inteira
            failed.append({"id": src["id"], "url": src["url"], "error": str(exc)[:300]})
            print(f"FAIL  {src['id']}: {exc}", file=sys.stderr)

    # dedupe por URL (mantém o primeiro)
    seen = set()
    deduped = []
    for it in all_items:
        key = it["url"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    # ordena do mais novo para o mais antigo (itens sem data vão para o fim)
    deduped.sort(key=lambda x: x["published_iso"] or "", reverse=True)

    # cap por fonte após ordenação
    per_src_count: dict[str, int] = {}
    capped = []
    for it in deduped:
        n = per_src_count.get(it["source"], 0)
        if n >= PER_SOURCE_CAP:
            continue
        per_src_count[it["source"]] = n + 1
        capped.append(it)

    payload = {
        "_meta": {
            "generated_iso": fetched_iso,
            "sources_total": len(SOURCES),
            "succeeded": succeeded,
            "failed": failed,
            "items_total": len(capped),
            "note": "Somente RSS/headlines gratuitos. Paywall (FT/Bloomberg/WSJ) nao coberto.",
        },
        "items": capped,
    }

    with open("feeds.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"OK: {len(capped)} itens de {len(succeeded)} fontes; {len(failed)} falharam.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
