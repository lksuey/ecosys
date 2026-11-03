"""Fase 2b — aplica o critério de qualificação (playbook §13).

Lê runs/<ts>/classified.jsonl e separa em:
  runs/<ts>/prospects.jsonl    — vão para a aba "Prospects" da planilha
  runs/<ts>/descartados.jsonl  — vão para a aba "Descartados", com o motivo

Regra: qualifica quem passa NOS DOIS filtros —
  1. classe_link ∈ classes_qualificam (exclusoes.yaml)
  2. n_avaliacoes >= min_avaliacoes
E não está na denylist de categoria/nome.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import config

BASE = Path(__file__).resolve().parent
RUNS_DIR = BASE / "runs"


def _motivo_descarte(rec: dict, exc: config.Exclusoes) -> str | None:
    """Retorna o motivo de descarte, ou None se o prospect qualifica."""
    if rec.get("descartado_no_scan"):
        return rec["descartado_no_scan"]
    if rec.get("fora_teresina"):
        return f"fora de Teresina: {rec.get('endereco')}"
    if rec.get("motivo_denylist"):
        return f"denylist: {rec['motivo_denylist']}"
    n = rec.get("n_avaliacoes") or 0
    if n < exc.min_avaliacoes:
        return f"< {exc.min_avaliacoes} avaliações ({n})"
    classe = rec.get("classe_link")
    if classe == "tem_site":
        return f"tem site: {rec.get('site_final_url') or rec.get('website_raw')}"
    if classe == "marketplace" and "marketplace" not in exc.classes_qualificam:
        return f"só marketplace: {rec.get('website_raw')}"
    if classe not in exc.classes_qualificam:
        return f"classe '{classe}' não qualifica"
    return None


def _prospect_row(rec: dict) -> dict:
    """Formato enxuto que vai para a planilha (aba Prospects)."""
    return {
        "cid": rec.get("cid"),
        "ftid": rec.get("ftid"),
        "nome": rec.get("nome"),
        "slug": rec.get("slug"),
        "vertical": rec.get("vertical"),
        "bairro": rec.get("bairro_real") or rec.get("bairro"),
        "categoria_maps": rec.get("categoria_maps"),
        "endereco": rec.get("endereco"),
        "telefone": rec.get("telefone_e164") or rec.get("telefone"),
        "instagram": rec.get("instagram_maps"),
        "link_site": rec.get("website_raw"),
        "classe_link": rec.get("classe_link"),
        "nota": rec.get("nota"),
        "n_avaliacoes": rec.get("n_avaliacoes"),
        "lat": rec.get("lat"),
        "lng": rec.get("lng"),
        "url_ficha_maps": rec.get("maps_url"),
        "foto_hero_url": rec.get("foto_hero_url"),
        "data_scan": rec.get("scan_ts"),
        "run": rec.get("run"),
        "estagio": "qualificado",
    }


def _run_dir(run: str | None) -> Path:
    if run:
        d = RUNS_DIR / run
        if not (d / "classified.jsonl").exists():
            raise SystemExit(f"sem classified.jsonl em {d} — rode 'classify' antes")
        return d
    dirs = sorted((d for d in RUNS_DIR.glob("*") if (d / "classified.jsonl").exists()),
                  key=lambda p: p.name)
    if not dirs:
        raise SystemExit("nenhuma run classificada — rode 'classify' antes")
    return dirs[-1]


def run(run: str | None = None) -> int:
    cfg = config.load()
    assert cfg.exclusoes is not None
    d = _run_dir(run)
    recs = [json.loads(l) for l in (d / "classified.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    prospects, descartados = [], []
    motivos: dict[str, int] = {}
    for rec in recs:
        motivo = _motivo_descarte(rec, cfg.exclusoes)
        if motivo:
            descartados.append({"cid": rec.get("cid"), "nome": rec.get("nome"),
                                "vertical": rec.get("vertical"), "n_avaliacoes": rec.get("n_avaliacoes"),
                                "classe_link": rec.get("classe_link"), "motivo": motivo,
                                "url_ficha_maps": rec.get("maps_url")})
            chave = motivo.split(":")[0].split("(")[0].strip()
            motivos[chave] = motivos.get(chave, 0) + 1
        else:
            prospects.append(_prospect_row(rec))

    (d / "prospects.jsonl").write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in prospects) + "\n", encoding="utf-8")
    (d / "descartados.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in descartados) + "\n", encoding="utf-8")

    print(f"{d.name}: {len(recs)} fichas -> {len(prospects)} prospects, {len(descartados)} descartados\n")
    print("descartados por motivo:")
    for k, v in sorted(motivos.items(), key=lambda x: -x[1]):
        print(f"  {v:4}  {k}")
    print("\nprospects por classe de link:")
    by_classe: dict[str, int] = {}
    for p in prospects:
        by_classe[p["classe_link"]] = by_classe.get(p["classe_link"], 0) + 1
    for k, v in sorted(by_classe.items(), key=lambda x: -x[1]):
        print(f"  {v:4}  {k}")
    print("\ntop 15 prospects (por avaliações):")
    for p in sorted(prospects, key=lambda x: -(x["n_avaliacoes"] or 0))[:15]:
        print(f"  {p['n_avaliacoes']:5}  {p['nome'][:42]:42} {p['classe_link']:14} {p['vertical']}")
    print(f"\n-> {(d / 'prospects.jsonl').relative_to(BASE.parent)}  |  "
          f"{(d / 'descartados.jsonl').relative_to(BASE.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
