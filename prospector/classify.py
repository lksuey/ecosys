"""Fase 2a — classifica o link de cada ficha e normaliza campos.

Lê runs/<ts>/raw.jsonl e escreve runs/<ts>/classified.jsonl com:
  classe_link    sem_site | whatsapp | rede_social | agregador | site_gbp |
                 marketplace | site_candidato | tem_site | site_morto | site_parqueado
  site_final_url URL final após redirects (só quando houve verificação HTTP)
  http_status    status HTTP da verificação (ou null)
  telefone_e164  telefone em formato +55DDDNÚMERO
  bairro_real    bairro extraído do endereço (best-effort)

A verificação HTTP só roda para "site_candidato" (domínio próprio aparente).
Sem custo — é só um GET.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config

BASE = Path(__file__).resolve().parent
RUNS_DIR = BASE / "runs"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
_BODY_CAP = 120_000


def _host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
        return h.removeprefix("www.")
    except ValueError:
        return ""


def _e164_br(raw: str | None) -> str | None:
    if not raw:
        return None
    d = re.sub(r"\D", "", raw)
    d = d.lstrip("0")
    if not d:
        return None
    if d.startswith("55") and len(d) in (12, 13):
        return "+" + d
    if len(d) in (10, 11):
        return "+55" + d
    if len(d) in (8, 9):
        return None
    return "+" + d if len(d) >= 12 else None


_BAIRRO_RE = re.compile(r"-\s*([A-Za-zÀ-ÿ0-9'.()\s]+?)\s*,\s*Teresina", re.I)
_FORA_RE = re.compile(r"\bTimon\b|,\s*MA\b|-\s*MA\b|\bAltos\s*-\s*PI|\bJosé de Freitas|"
                      r"\bUni[aã]o\s*-\s*PI|\bDemerval Lob[aã]o|\bMiguel Alves|\bCoivaras", re.I)


def _fora_de_teresina(endereco: str | None) -> bool:
    if not endereco:
        return False
    if _FORA_RE.search(endereco):
        return True
    return bool(re.search(r",\s*[A-Z][a-zÀ-ÿ]+\s*-\s*(PI|MA)\b", endereco)) and \
        "teresina" not in endereco.lower()


def _bairro_do_endereco(endereco: str | None) -> str | None:
    if not endereco:
        return None
    m = _BAIRRO_RE.search(endereco)
    if m:
        b = re.sub(r"\s*\(.*?\)\s*", "", m.group(1)).strip()
        b = re.sub(r"^\d+\s*", "", b)
        return b or None
    return None


def _verificar_site(url: str, exc: config.Exclusoes,
                    client: httpx.Client) -> tuple[str, str | None, int | None]:
    """Retorna (classe, url_final, status)."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    vs = exc.verificacao_site
    markers = [m.lower() for m in vs.get("parking_markers", [])]
    min_txt = int(vs.get("min_texto_util", 350))
    plataformas = tuple(vs.get("plataformas_ecommerce", []))
    try:
        with client.stream("GET", url) as r:
            status = r.status_code
            final_host = _host(str(r.url))
            classe_destino = exc.classificar_host(final_host)
            if classe_destino:
                return classe_destino, str(r.url), status
            if any(final_host == p or final_host.endswith("." + p) for p in plataformas):
                return "tem_site", str(r.url), status
            if status >= 400:
                return "site_morto", str(r.url), status
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) >= _BODY_CAP:
                    break
    except (httpx.HTTPError, OSError):
        return "site_morto", None, None

    texto = body.decode("utf-8", "ignore").lower()
    if any(m in texto for m in markers):
        return "site_parqueado", url, status
    visivel = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", texto, flags=re.S)
    visivel = re.sub(r"<[^>]+>", " ", visivel)
    visivel = re.sub(r"\s+", " ", visivel).strip()
    if len(visivel) < min_txt:
        return "site_parqueado", url, status
    return "tem_site", url, status


def classificar(rec: dict, exc: config.Exclusoes, client: httpx.Client) -> dict:
    rec["telefone_e164"] = _e164_br(rec.get("telefone"))
    rec["bairro_real"] = _bairro_do_endereco(rec.get("endereco"))
    rec["fora_teresina"] = _fora_de_teresina(rec.get("endereco"))
    rec.setdefault("site_final_url", None)
    rec.setdefault("http_status", None)

    if rec.get("descartado_no_scan"):
        rec["classe_link"] = None
        return rec

    url = rec.get("website_raw")
    if not url:
        rec["classe_link"] = "sem_site"
        return rec

    classe = exc.classificar_host(_host(url))
    if classe:
        rec["classe_link"] = classe
        return rec

    classe, final, status = _verificar_site(url, exc, client)
    rec["classe_link"] = classe
    rec["site_final_url"] = final
    rec["http_status"] = status
    return rec


def _run_dir(run: str | None) -> Path:
    if run:
        d = RUNS_DIR / run
        if not d.exists():
            raise SystemExit(f"run não encontrada: {d}")
        return d
    dirs = sorted((d for d in RUNS_DIR.glob("*") if (d / "raw.jsonl").exists()),
                  key=lambda p: p.name)
    if not dirs:
        raise SystemExit("nenhuma run com raw.jsonl em prospector/runs/")
    return dirs[-1]


def run(run: str | None = None) -> int:
    cfg = config.load()
    assert cfg.exclusoes is not None
    d = _run_dir(run)
    raw = [json.loads(l) for l in (d / "raw.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"classificando {len(raw)} fichas de {d.name}")

    out = d / "classified.jsonl"
    tally: dict[str, int] = {}
    n_http = 0
    with httpx.Client(follow_redirects=True, timeout=float(cfg.exclusoes.verificacao_site.get("timeout_s", 12)),
                      headers={"User-Agent": _UA}, max_redirects=6) as client, \
            out.open("w", encoding="utf-8") as fh:
        for rec in raw:
            antes = rec.get("classe_link")
            rec = classificar(rec, cfg.exclusoes, client)
            if rec.get("http_status") is not None or (not antes and rec.get("website_raw")
                                                      and rec["classe_link"] in ("tem_site", "site_morto", "site_parqueado")):
                n_http += 1
            k = rec.get("classe_link") or "descartado_no_scan"
            tally[k] = tally.get(k, 0) + 1
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"  {n_http} verificações HTTP")
    for k, v in sorted(tally.items(), key=lambda x: -x[1]):
        print(f"  {v:4}  {k}")
    print(f"-> {out.relative_to(BASE.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
