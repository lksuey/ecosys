"""Remove cards arquivados em Descartado do board do ecossistema."""

from __future__ import annotations

import argparse

from . import store, trello, watcher


def run(*, apply: bool = False) -> int:
    resolved = trello.load_resolved()
    board_id = resolved["board_id"]
    discarded_id = resolved["listas"]["Descartado"]
    with trello.Trello(board_id=board_id) as tr:
        archived = tr._req("GET", f"/1/boards/{board_id}/cards",
                           fields="id,name,idList,idBoard,desc,closed", filter="closed")
        targets = [c for c in archived if c["idList"] == discarded_id]
        if any(c["idBoard"] != board_id or not c.get("closed") for c in targets):
            raise RuntimeError("resposta do Trello contém card fora do escopo esperado")
        print(f"cards arquivados em Descartado: {len(targets)}", flush=True)
        if not apply:
            print("simulação: nenhuma exclusão")
            return 0

        rows = store.load()
        by_card = {r.get("card_id"): key for key, r in rows.items() if r.get("card_id")}
        by_cid = watcher._legacy_cards({c["id"]: c for c in targets})
        deleted = 0
        for card in targets:
            tr.delete_card(card["id"])
            key = by_card.get(card["id"])
            if not key:
                key = next((cid for cid, card_id in by_cid.items()
                            if card_id == card["id"] and cid in rows), None)
            if key:
                rows[key]["stage"] = "descartado_manual"
                rows[key]["card_id"] = ""
                store.save(rows)
            deleted += 1
            if deleted % 10 == 0 or deleted == len(targets):
                print(f"excluídos: {deleted}/{len(targets)}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="exclui os cards selecionados")
    return run(apply=parser.parse_args().apply)


if __name__ == "__main__":
    raise SystemExit(main())
