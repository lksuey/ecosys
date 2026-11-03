"""Índice local do funil. Trello guarda o trabalho; este JSON guarda a chave Maps→card.

Não contém credenciais. Escritas são atômicas para sobreviver a um tick interrompido.
O lock do watcher impede dois escritores simultâneos.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .lock import exclusive

BASE = Path(__file__).resolve().parent
INDEX = BASE / "data" / "prospects.json"
INDEX_LOCK = BASE / "data" / "prospects.lock"
RUNS = BASE / "runs"


def key(row: dict) -> str:
    return str(row.get("cid") or row.get("ftid") or "").strip()


def load(path: Path | None = None) -> dict[str, dict]:
    path = path or INDEX
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"índice inválido: {path}")
    return data


def save(rows: dict[str, dict], path: Path | None = None) -> None:
    path = path or INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@contextmanager
def _write_lock(timeout: float = 30.0) -> Iterator[None]:
    deadline = time.monotonic() + timeout
    while True:
        with exclusive(INDEX_LOCK) as acquired:
            if acquired:
                yield
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("índice local ocupado por outro processo")
        time.sleep(0.05)


def update_rows(changes: dict[str, dict]) -> dict[str, dict]:
    """Mescla apenas os prospects alterados, preservando escritas concorrentes."""
    if not changes:
        return load()
    with _write_lock():
        current = load()
        current.update(changes)
        save(current)
        return current


def patch_rows(changes: dict[str, dict]) -> dict[str, dict]:
    """Atualiza somente campos alterados de prospects existentes."""
    if not changes:
        return load()
    with _write_lock():
        current = load()
        for key, fields in changes.items():
            current.setdefault(key, {}).update(fields)
        save(current)
        return current


def import_and_save(run: str) -> tuple[dict[str, dict], int]:
    with _write_lock():
        rows, added = import_run(run, load())
        save(rows)
        return rows, added


def import_run(run: str, rows: dict[str, dict] | None = None) -> tuple[dict[str, dict], int]:
    """Importa prospects qualificados de uma run; nunca sobrescreve decisões do card."""
    rows = rows if rows is not None else load()
    source = RUNS / run / "prospects.jsonl"
    if not source.is_file():
        raise FileNotFoundError(source)
    added = 0
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        prospect = json.loads(line)
        k = key(prospect)
        if not k:
            continue
        if k not in rows:
            rows[k] = {**prospect, "card_id": "", "stage": "qualificado"}
            added += 1
        else:
            preserved = {k2: rows[k].get(k2) for k2 in ("card_id", "stage", "job")}
            rows[k].update({x: v for x, v in prospect.items() if v not in (None, "")})
            rows[k].update({x: v for x, v in preserved.items() if v is not None})
    return rows, added


def bootstrap_runs(rows: dict[str, dict] | None = None) -> tuple[dict[str, dict], int]:
    """Migração inicial dos snapshots existentes, sem tocar no Trello."""
    rows = rows if rows is not None else load()
    added = 0
    for d in sorted(RUNS.glob("*")):
        if (d / "prospects.jsonl").is_file():
            rows, n = import_run(d.name, rows)
            added += n
    return rows, added
