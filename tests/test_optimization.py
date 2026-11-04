"""Regressões dos limites, retomada e escrita concorrente do ecossistema."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from prospector import production, prospect_cycle, store, trello, watcher
from prospector.lock import exclusive


class FakeCardTrello:
    def __init__(self, attachments=None):
        self.attachment_list = attachments or []
        self.moves = []
        self.comments = []

    def attachments(self, _):
        return self.attachment_list

    def move_card(self, _, list_id):
        self.moves.append(list_id)

    def comment(self, _, message):
        self.comments.append(message)


class OptimizationTests(unittest.TestCase):
    def test_process_lock_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            with exclusive(path) as first:
                self.assertTrue(first)
                with exclusive(path) as second:
                    self.assertFalse(second)
            with exclusive(path) as again:
                self.assertTrue(again)

    def test_index_updates_merge_instead_of_overwriting_other_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "prospects.json"
            lock = Path(tmp) / "prospects.lock"
            with patch.object(store, "INDEX", index), patch.object(store, "INDEX_LOCK", lock):
                store.update_rows({"a": {"card_id": "card-a"}})
                stale = store.load()
                store.update_rows({"b": {"card_id": "card-b"}})
                stale["a"]["stage"] = "Coleta de ativos"
                store.update_rows({"a": stale["a"]})
                self.assertEqual(set(store.load()), {"a", "b"})
                store.patch_rows({"a": {"job": {"status": "em_producao"}}})
                store.patch_rows({"a": {"stage": "QA técnico"}})
                self.assertEqual(store.load()["a"]["job"]["status"], "em_producao")

    def test_scanner_cursor_advances_only_a_batch(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(prospect_cycle, "ESTADO_PATH", Path(tmp) / "state.json"):
            prospect_cycle._advance(0, 0, 12, 5, 23)
            self.assertEqual(json.loads(prospect_cycle.ESTADO_PATH.read_text()),
                             {"pos": 0, "offset": 5})
            prospect_cycle._advance(0, 10, 12, 2, 23)
            self.assertEqual(json.loads(prospect_cycle.ESTADO_PATH.read_text()),
                             {"pos": 1, "offset": 0})

    def test_capacity_excludes_ready_and_commercial_but_supply_counts_queue(self):
        lists = {name: name for name in prospect_cycle.TRABALHO | prospect_cycle.FILA |
                 {"Pronto para envio", "Contato feito"}}
        cards = [{"name": "A", "idList": "Coleta de ativos"},
                 {"name": "B", "idList": "Prospects qualificados"},
                 {"name": "C", "idList": "Pronto para envio"},
                 {"name": "D", "idList": "Contato feito"}]
        self.assertEqual(prospect_cycle.contar_ativos(cards, lists), 1)
        self.assertEqual(prospect_cycle.contar_oferta(cards, lists), 2)
        self.assertTrue(watcher._can_admit(4, 39))
        self.assertFalse(watcher._can_admit(5, 39))
        self.assertFalse(watcher._can_admit(4, 40))

    def test_interrupted_job_goes_to_qa_without_restarting_claude(self):
        tr = FakeCardTrello()
        row = {"slug": "loja", "job": {"status": "em_producao"}}
        lists = {watcher.QA: "qa", watcher.READY: "ready"}
        self.assertTrue(watcher._recover(tr, row, "card", lists, lambda _: None))
        self.assertEqual(tr.moves, ["qa"])
        self.assertEqual(row["job"]["status"], "interrompido")

    def test_same_fingerprint_reuses_site_without_claude(self):
        card_id = "12345678abcdef"
        slug = production.artifact_slug("loja", card_id)
        desc = watcher._description({"cid": "123456", "slug": "loja"})
        desc = desc.replace("Prompt:\n", "Prompt:\nSite para loja\n", 1)
        desc = desc.replace("Cores:\n", "Cores:\nAzul\n", 1)
        attachment = {"id": "zip-id", "name": "assets.zip"}
        fp = production.fingerprint(desc, [attachment])
        tr = FakeCardTrello([attachment])
        row = {"slug": "loja", "job": {"last_success_fingerprint": fp}}
        checks = [{"name": name, "checkItems": [{"state": "complete"}]}
                  for name in (watcher.GATE_ASSETS, watcher.GATE_COLORS)]
        with patch.object(production, "prepare") as prepare, \
             patch.object(production, "run_claude") as run_claude:
            self.assertTrue(watcher._produce(tr, row, {"id": card_id, "desc": desc},
                                             {watcher.READY: "ready"}, lambda _: None, checks))
            prepare.assert_not_called()
            run_claude.assert_not_called()
        self.assertEqual(tr.moves, ["ready"])
        self.assertTrue(slug.startswith("loja-"))

    def test_board_checklists_are_grouped_from_one_response(self):
        client = object.__new__(trello.Trello)
        with patch.object(client, "_require_board", return_value="board"), \
             patch.object(client, "_req", return_value=[
                 {"idCard": "a", "name": "X"}, {"idCard": "a", "name": "Y"},
                 {"idCard": "b", "name": "Z"}]) as req:
            grouped = client.board_checklists()
        self.assertEqual(len(grouped["a"]), 2)
        self.assertEqual(len(grouped["b"]), 1)
        req.assert_called_once()

    def test_tick_produces_ready_cards_concurrently(self):
        """Vários cards em Coleta com os gates ok devem ser construídos ao mesmo
        tempo num único tick, não um de cada vez esperando o anterior terminar."""

        lists = {watcher.INITIAL: "L0", "Triagem": "LT", watcher.COLLECTION: "LC",
                 watcher.BUILDING: "LB", watcher.QA: "LQ", watcher.READY: "LR",
                 watcher.DISCARDED: "LD"}

        def checklist_set():
            return [
                {"id": "cl-assets", "name": watcher.GATE_ASSETS,
                 "checkItems": [{"id": "i1", "name": watcher.GATE_ASSETS_ITEM, "state": "complete"}]},
                {"id": "cl-colors", "name": watcher.GATE_COLORS,
                 "checkItems": [{"id": "i2", "name": watcher.GATE_COLORS_ITEM, "state": "complete"}]},
                {"id": "cl-auto", "name": watcher.AUTO_BUILDING,
                 "checkItems": [{"id": "i3", "name": watcher.AUTO_ITEM, "state": "incomplete"}]},
            ]

        n_cards = 3
        cards, descs = [], {}
        for i in range(n_cards):
            slug = f"loja{i}"
            desc = watcher._description({"cid": str(100 + i), "slug": slug})
            desc = desc.replace("Prompt:\n", "Prompt:\nSite elegante\n", 1)
            desc = desc.replace("Cores:\n", "Cores:\nPreto e dourado\n", 1)
            descs[str(100 + i)] = desc
            cards.append({"id": f"card{i}", "idList": lists[watcher.COLLECTION],
                          "name": f"Loja {i}", "desc": desc})

        class FakeBoardTrello:
            def __init__(self):
                self.moves = {}
                self.files = []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cards(self):
                return cards

            def board_checklists(self):
                return {c["id"]: checklist_set() for c in cards}

            def checklists(self, card_id):
                return checklist_set()

            def attachments(self, card_id):
                return [{"id": f"zip-{card_id}", "name": "insumos.zip", "date": "2026-01-01"}]

            def comment(self, card_id, text):
                pass

            def move_card(self, card_id, id_list):
                self.moves.setdefault(card_id, []).append(id_list)

            def update_card_desc(self, card_id, desc):
                pass

            def set_checkitem(self, card_id, item_id, *, checked):
                pass

            def anexar_arquivo(self, card_id, path):
                self.files.append((card_id, path))

        fake = FakeBoardTrello()
        active, max_active = [0], [0]
        lock = threading.Lock()

        def prepare_side_effect(tr, card_id, slug, attachment, desc):
            job = Path(root) / card_id
            job.mkdir()
            assets = job / "assets"
            assets.mkdir()
            return job, assets

        def run_claude_side_effect(job, assets, slug, executable="claude", timeout=3600):
            with lock:
                active[0] += 1
                max_active[0] = max(max_active[0], active[0])
            time.sleep(0.3)
            with lock:
                active[0] -= 1
            site = job / "site"
            site.mkdir()
            return site, "ok"

        with tempfile.TemporaryDirectory() as root, \
             tempfile.TemporaryDirectory() as data_tmp:
            index = Path(data_tmp) / "prospects.json"
            index_lock = Path(data_tmp) / "prospects.lock"
            rows = {str(100 + i): {"cid": str(100 + i), "slug": f"loja{i}",
                                   "card_id": f"card{i}", "stage": watcher.COLLECTION}
                   for i in range(n_cards)}
            store.save(rows, index)
            with patch.object(store, "INDEX", index), patch.object(store, "INDEX_LOCK", index_lock), \
                 patch.object(trello, "load_resolved",
                              return_value={"board_id": "b", "listas": lists}), \
                 patch.object(trello, "Trello", return_value=fake), \
                 patch.object(watcher.shutil, "which", return_value="claude"), \
                 patch.object(production, "prepare", side_effect=prepare_side_effect), \
                 patch.object(production, "run_claude", side_effect=run_claude_side_effect), \
                 patch.object(production, "package", return_value=(Path("site.zip"), [])), \
                 patch.object(production, "split_zip_for_upload", return_value=[Path("site.zip")]), \
                 patch.object(production, "promote", side_effect=lambda site, _: site):
                started = time.monotonic()
                acted = watcher.tick()
                elapsed = time.monotonic() - started

        self.assertEqual(acted, 0)
        self.assertGreater(max_active[0], 1,
                           "cards prontos deveriam rodar em paralelo, não um de cada vez")
        self.assertLess(elapsed, 0.3 * n_cards,
                        "processar 3 cards não deveria levar o tempo de 3 execuções sequenciais")
        self.assertEqual(len(fake.moves), n_cards)
        for moves in fake.moves.values():
            self.assertEqual(moves, ["LB", "LR"])

    def test_qa_rejects_missing_asset_and_broken_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "index.html").write_text(
                '<!doctype html><html><head><title>Loja</title>'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '</head><body><h1>Loja</h1><p>' + ('Uma descrição real da loja. ' * 8) +
                '</p><img src="assets/foto.jpg"><a href="#ausente">Conheça</a></body></html>',
                encoding="utf-8")
            _, problems = production.package(site, "loja")
            self.assertTrue(any("arquivo local ausente" in p for p in problems))
            self.assertTrue(any("âncora sem destino" in p for p in problems))
            self.assertTrue((site / "_qa" / "desktop.png").exists())
            self.assertTrue((site / "_qa" / "mobile.png").exists())


if __name__ == "__main__":
    unittest.main()
