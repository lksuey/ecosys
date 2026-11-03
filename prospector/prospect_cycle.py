"""Scanner incremental independente da produção de landing pages."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import classify, config, qualify, scan_maps, store, trello
from .lock import exclusive

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "runs" / "prospeccao"
ESTADO_PATH = LOG_DIR / "estado.json"
LOCK = LOG_DIR / ".lock"
TETO = 40
COMBOS_POR_LOTE = 5
TRABALHO = {"Coleta de ativos", "Em produção", "QA técnico"}
FILA = {"Prospects qualificados", "Triagem"}


def _hora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


class Log:
    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / f"{datetime.now():%Y%m%d}.log"

    def __call__(self, msg: str) -> None:
        line = f"{_hora()}  {msg}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _count(cards: list[dict], listas: dict[str, str], stages: set[str]) -> int:
    ids = {listas[name] for name in stages if name in listas}
    return sum(c.get("idList") in ids and
               not str(c.get("name", "")).startswith("MODELO") for c in cards)


def contar_ativos(cards: list[dict], listas: dict[str, str]) -> int:
    """Apenas LPs ainda em trabalho ocupam a capacidade de 40."""
    return _count(cards, listas, TRABALHO)


def contar_oferta(cards: list[dict], listas: dict[str, str]) -> int:
    """Fila antiga também evita uma busca nova enquanto há prospects suficientes."""
    return _count(cards, listas, TRABALHO | FILA)


def _cursor(cfg: config.Config) -> tuple[config.Vertical, int, int, int]:
    state = {"pos": 0, "offset": 0}
    if ESTADO_PATH.exists():
        try:
            state.update(json.loads(ESTADO_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            pass
    pos = int(state["pos"]) % len(cfg.verticais)
    vertical = cfg.verticais[pos]
    total = sum(1 for _ in scan_maps._iter_combos(cfg, [vertical.id], None, None))
    offset = int(state["offset"])
    if offset >= total or offset < 0:
        offset = 0
    return vertical, pos, offset, total


def _advance(pos: int, offset: int, total: int, n: int, n_verticais: int) -> None:
    next_offset = offset + n
    next_pos = pos
    if next_offset >= total:
        next_pos = (pos + 1) % n_verticais
        next_offset = 0
    ESTADO_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ESTADO_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pos": next_pos, "offset": next_offset}), encoding="utf-8")
    tmp.replace(ESTADO_PATH)


def manter_teto(*, teto: int = TETO, log: Log | None = None,
               cards: list[dict] | None = None, listas: dict[str, str] | None = None) -> int:
    log = log or Log()
    resolved = trello.load_resolved()
    if not resolved.get("board_id"):
        raise RuntimeError("board não configurado")
    listas = listas or resolved["listas"]
    if cards is None:
        with trello.Trello(board_id=resolved["board_id"]) as tr:
            cards = tr.cards()
    pending = sum(not r.get("card_id") and r.get("stage") != "descartado_manual"
                  for r in store.load().values())
    supply = contar_oferta(cards, listas) + pending
    if supply >= teto:
        log(f"prospecção: oferta suficiente ({supply}/{teto}; {pending} aguardam card)")
        return 0

    cfg = config.load()
    vertical, pos, offset, total = _cursor(cfg)
    n = min(COMBOS_POR_LOTE, total - offset)
    log(f"prospecção: oferta {supply}/{teto}; '{vertical.rotulo}', "
        f"combos {offset + 1}–{offset + n}/{total}")

    before = {d.name for d in store.RUNS.glob("*") if (d / "raw.jsonl").exists()}
    scan_rc = scan_maps.run(verticais=[vertical.id], headless=False, pause=False,
                            max_combos=n, combo_offset=offset)
    after = sorted((d for d in store.RUNS.glob("*") if (d / "raw.jsonl").exists()
                    and d.name not in before), key=lambda d: d.name)
    if not after:
        raise RuntimeError("scan não gerou uma run nova")
    run = after[-1].name
    if classify.run(run) != 0 or qualify.run(run) != 0:
        return 1
    rows, added = store.import_and_save(run)
    if scan_rc == 0:
        _advance(pos, offset, total, n, len(cfg.verticais))
    log(f"prospecção: run {run}, {added} novos; "
        f"cursor {'avançou' if scan_rc == 0 else 'mantido para repetir lote'}")
    return scan_rc


def manter_teto_com_lock(**kw) -> int:
    with exclusive(LOCK) as acquired:
        if not acquired:
            print(f"{_hora()}  outra prospecção em andamento — pulando", flush=True)
            return 0
        return manter_teto(teto=kw.get("teto", TETO))


if __name__ == "__main__":
    raise SystemExit(manter_teto_com_lock())
