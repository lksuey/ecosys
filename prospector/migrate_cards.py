"""Migração pontual dos cards existentes para o contrato atual do Trello."""

from __future__ import annotations

import argparse
import re

from . import trello, watcher

OLD_COLORS = re.compile(r"^\s*\*\*Cores:\*\*\s*(.*)$", re.I)
OLD_PATTERN = re.compile(r"^\s*-\s*pattern previsto\s*:", re.I)


def migrated_description(card: dict) -> str:
    original = card.get("desc") or ""
    if (re.search(r"(?im)^Prompt\s*:", original)
            and re.search(r"(?im)^Cores\s*:", original)
            and re.search(r"(?im)^Dados do prospecto\s*:", original)):
        return original

    colors = ""
    lines = []
    for line in original.splitlines():
        old_colors = OLD_COLORS.match(line)
        if old_colors:
            value = old_colors.group(1).strip()
            if value and not re.search(r"(?i)preencher aqui|exemplo|ex\.:", value):
                colors = value
            continue
        if OLD_PATTERN.match(line):
            continue
        lines.append(line)
    facts = "\n".join(lines).strip()
    return (f"Prompt:\n\n\nCores:\n{colors}\n\nDados do prospecto:\n"
            f"- Nome do card: {card['name']}\n" + facts + "\n")


def run(*, apply: bool = False, archived: bool = False) -> int:
    resolved = trello.load_resolved()
    if not resolved.get("board_id"):
        raise RuntimeError("board do ecossistema não configurado")
    with trello.Trello(board_id=resolved["board_id"]) as tr:
        if archived:
            cards = [c for c in tr._req("GET", f"/1/boards/{resolved['board_id']}/cards",
                                       fields="id,name,idList,idBoard,desc", filter="closed")
                     if c["idList"] != resolved["listas"]["Descartado"]]
        else:
            cards = tr.cards()
        snapshots = []
        for card in cards:
            snapshots.append((card, tr.checklists(card["id"])))

        changed = sum(migrated_description(c) != (c.get("desc") or "") for c, _ in snapshots)
        legacy = sum(sum(cl["name"] not in watcher.CHECKLIST_NAMES for cl in cls)
                     for _, cls in snapshots)
        print(f"cards {'arquivados' if archived else 'abertos'}: {len(cards)}; "
              f"descrições a migrar: {changed}; "
              f"checklists legados: {legacy}", flush=True)
        if not apply:
            print("simulação: nenhuma alteração no Trello")
            return 0

        done = 0
        for card, checklists in snapshots:
            new_desc = migrated_description(card)
            if new_desc != (card.get("desc") or ""):
                tr.update_card_desc(card["id"], new_desc)
            watcher._ensure_checklists(tr, card["id"], checklists)
            done += 1
            if done % 10 == 0 or done == len(cards):
                print(f"migrados: {done}/{len(cards)}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="aplica a migração no board")
    p.add_argument("--archived", action="store_true", help="processa apenas cards arquivados")
    args = p.parse_args()
    return run(apply=args.apply, archived=args.archived)


if __name__ == "__main__":
    raise SystemExit(main())
