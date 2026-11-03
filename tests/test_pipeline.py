"""Contratos locais do fluxo Trello → insumos → Claude (sem chamadas remotas)."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
import base64
from pathlib import Path
from unittest.mock import patch

from prospector import migrate_cards, production, prospect_cycle, store, watcher


class FakeTrello:
    def __init__(self, checklists=None):
        self.items = list(checklists or [])
        self.comments = []
        self.files = []
        self.moves = []
        self.attachment_list = []

    def checklists(self, _):
        return self.items

    def delete_checklist(self, _, checklist_id, *, known=None):
        self.items = [c for c in self.items if c["id"] != checklist_id]

    def add_checklist(self, _, name):
        checklist = {"id": f"new-{name}", "name": name, "checkItems": []}
        self.items.append(checklist)
        return checklist

    def add_checkitem(self, checklist_id, name):
        cl = next(c for c in self.items if c["id"] == checklist_id)
        cl["checkItems"].append({"id": f"item-{checklist_id}", "name": name,
                                 "state": "incomplete"})

    def estado_checklists(self, _):
        return {c["name"]: {x["name"]: x["state"] == "complete"
                             for x in c["checkItems"]} for c in self.items}

    def attachments(self, _):
        return self.attachment_list

    def comment(self, _, message):
        self.comments.append(message)

    def move_card(self, _, list_id):
        self.moves.append(list_id)

    def set_checkitem(self, _, item_id, *, checked):
        for cl in self.items:
            for item in cl["checkItems"]:
                if item["id"] == item_id:
                    item["state"] = "complete" if checked else "incomplete"
                    return
        raise AssertionError(item_id)

    def anexar_arquivo(self, _, path):
        self.files.append(path)


class PipelineTests(unittest.TestCase):
    def _valid_site(self, site: Path) -> None:
        (site / "assets").mkdir(exist_ok=True)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
            "lXcAAAAASUVORK5CYII=")
        (site / "assets" / "foto.png").write_bytes(png)
        (site / "index.html").write_text(
            "<!doctype html><html><head><title>Loja de roupas para festas</title>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'></head>"
            "<body><h1>Loja de roupas para festas</h1><p>Conheça nossa seleção "
            "de roupas para momentos especiais. Explore as peças disponíveis e "
            "entre em contato para saber mais sobre as opções para sua próxima "
            "celebração.</p><img src='assets/foto.png' alt='Peça da loja'></body></html>",
            encoding="utf-8")

    def test_existing_card_description_preserves_facts_and_clears_old_color_placeholder(self):
        old = {"name": "Loja — Centro", "desc": "**Fonte:** Google Maps\n"
               "- Endereço: Rua Um\n- Telefone: 123\n- pattern previsto: pattern-01\n"
               "**Cores:** _preencher aqui com 2 a 5 códigos hex da marca_"}
        desc = migrate_cards.migrated_description(old)
        self.assertIn("- Endereço: Rua Um", desc)
        self.assertIn("- Telefone: 123", desc)
        self.assertNotIn("pattern previsto", desc)
        self.assertNotIn("preencher aqui", desc)
        self.assertEqual(production.extract_colors(desc), "")
        self.assertEqual(desc, migrate_cards.migrated_description({"name": old["name"], "desc": desc}))

    def test_card_description_sections_and_prompt_parsing(self):
        desc = watcher._description({"cid": "123456", "nome": "Loja", "endereco": "Rua Um"})
        self.assertLess(desc.index("Prompt:"), desc.index("Cores:"))
        self.assertLess(desc.index("Cores:"), desc.index("Dados do prospecto:"))
        self.assertIn("- CID: 123456", desc)
        # o Prompt já nasce com os requisitos fixos do ecossistema (animações, sem
        # emoji, pulso no WhatsApp); o usuário escreve a direção do negócio depois deles.
        self.assertEqual(production.extract_prompt(desc), watcher.PROMPT_BASELINE)
        self.assertIn("15 animações", production.extract_prompt(desc))
        self.assertIn("emoji", production.extract_prompt(desc))
        desc = desc.replace(watcher.PROMPT_BASELINE + "\n",
                            watcher.PROMPT_BASELINE + "\nFaça uma página elegante\n", 1)
        desc = desc.replace("Cores:\n", "Cores:\n#000000, dourado\n", 1)
        prompt = production.extract_prompt(desc)
        self.assertIn("Faça uma página elegante", prompt)
        self.assertIn(watcher.PROMPT_BASELINE, prompt)
        self.assertEqual(production.extract_colors(desc), "#000000, dourado")

    def test_zip_requires_only_assets_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "input.zip"
            with zipfile.ZipFile(package, "w") as z:
                z.writestr("assets/fachada.jpg", b"image")
                z.writestr("assets/video.mp4", b"video")
            assets = production.unpack(package, root / "output")
            self.assertTrue((assets / "fachada.jpg").is_file())
            with zipfile.ZipFile(package, "w") as z:
                z.writestr("../outside.jpg", b"bad")
            with self.assertRaisesRegex(ValueError, "inseguro"):
                production.unpack(package, root / "output")
            with zipfile.ZipFile(package, "w") as z:
                z.writestr("cores/cores.txt", "dourado")
            with self.assertRaisesRegex(ValueError, "assets"):
                production.unpack(package, root / "output")

    def test_multiple_zips_merge_into_one_assets_folder(self):
        """O Trello limita cada anexo a 10 MB; um card grande divide os insumos em
        vários ZIPs que precisam ser mesclados no mesmo assets/ de destino."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zip1, zip2 = root / "parte1.zip", root / "parte2.zip"
            with zipfile.ZipFile(zip1, "w") as z:
                z.writestr("assets/foto-a.jpg", b"a")
                z.writestr("assets/repetido.jpg", b"velho")
            with zipfile.ZipFile(zip2, "w") as z:
                z.writestr("assets/foto-b.jpg", b"b")
                z.writestr("assets/repetido.jpg", b"novo")
            assets = production.unpack_many([zip1, zip2], root / "output")
            self.assertTrue((assets / "foto-a.jpg").is_file())
            self.assertTrue((assets / "foto-b.jpg").is_file())
            self.assertEqual((assets / "repetido.jpg").read_bytes(), b"novo")

    def test_source_zips_and_fingerprint_cover_all_attachments(self):
        attachments = [
            {"id": "z2", "name": "parte2.zip", "date": "2026-01-02"},
            {"id": "z1", "name": "parte1.zip", "date": "2026-01-01"},
            {"id": "out", "name": "loja.zip", "date": "2026-01-03"},
        ]
        found = production.source_zips(attachments, "loja")
        self.assertEqual([a["id"] for a in found], ["z1", "z2"])
        fp_a = production.fingerprint("desc", found)
        fp_b = production.fingerprint("desc", list(reversed(found)))
        self.assertEqual(fp_a, fp_b)
        fp_missing_one = production.fingerprint("desc", found[:1])
        self.assertNotEqual(fp_a, fp_missing_one)

    def test_output_zip_over_trello_limit_splits_into_valid_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            zpath = site / "loja.zip"
            chunk = b"x" * 6_000_000
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
                z.writestr("assets/foto-a.jpg", chunk)
                z.writestr("assets/foto-b.jpg", chunk)
                z.writestr("index.html", b"<html></html>")
            parts = production.split_zip_for_upload(zpath)
            self.assertGreater(len(parts), 1)
            for part in parts:
                self.assertLessEqual(part.stat().st_size, production.MAX_ATTACHMENT_BYTES + 1024)
            seen = set()
            for part in parts:
                with zipfile.ZipFile(part) as z:
                    z.testzip()
                    seen.update(z.namelist())
            self.assertEqual(seen, {"assets/foto-a.jpg", "assets/foto-b.jpg", "index.html"})

    def test_small_output_zip_is_not_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "loja.zip"
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("index.html", b"<html></html>")
            self.assertEqual(production.split_zip_for_upload(zpath), [zpath])

    def test_checklist_migration_keeps_only_three(self):
        old = [{"id": f"old-{i}", "name": f"Antiga {i}",
                "checkItems": [{"id": f"item-{i}", "state": "complete", "name": "feito"}]}
               for i in range(6)]
        tr = FakeTrello(old)
        watcher._ensure_checklists(tr, "card123")
        self.assertEqual({c["name"] for c in tr.items}, set(watcher.CHECKLIST_NAMES))
        self.assertEqual(len(tr.items), 3)
        watcher._ensure_checklists(tr, "card123")
        self.assertEqual(len(tr.items), 3)

    def test_duplicate_empty_checklist_is_removed_without_editing_deleted_id(self):
        dup = [{"id": "first", "name": watcher.GATE_ASSETS, "checkItems": []},
               {"id": "duplicate", "name": watcher.GATE_ASSETS, "checkItems": []}]
        tr = FakeTrello(dup)
        watcher._ensure_checklists(tr, "card456")
        self.assertEqual(len(tr.items), 3)
        self.assertEqual({c["id"] for c in tr.items} & {"duplicate"}, set())

    def test_production_gate_and_successful_card_transition_without_claude(self):
        tr = FakeTrello()
        watcher._ensure_checklists(tr, "card")
        tr.attachment_list = [{"id": "zip1", "name": "insumos.zip", "date": "2026-01-01"}]
        desc = watcher._description({"cid": "123456", "slug": "loja"})
        desc = desc.replace("Prompt:\n", "Prompt:\nPágina elegante\n", 1)
        desc = desc.replace("Cores:\n", "Cores:\nPreto e dourado\n", 1)
        row = {"slug": "loja"}
        card = {"id": "card", "desc": desc}
        lists = {watcher.BUILDING: "building", watcher.QA: "qa", watcher.READY: "ready"}
        with patch.object(watcher.shutil, "which", return_value="claude"), \
             patch.object(production, "prepare") as prepare, \
             patch.object(production, "run_claude") as run_claude, \
             patch.object(production, "promote", side_effect=lambda site, _: site):
            self.assertFalse(watcher._produce(tr, row, card, lists, lambda _: None))
            self.assertFalse(prepare.called)
            for cl in tr.items:
                if cl["name"] in (watcher.GATE_ASSETS, watcher.GATE_COLORS):
                    cl["checkItems"][0]["state"] = "complete"
            with tempfile.TemporaryDirectory() as tmp:
                job = Path(tmp)
                site = job / "site"
                site.mkdir()
                self._valid_site(site)
                prepare.return_value = (job, job / "assets")
                run_claude.return_value = (site, "ok")
                self.assertTrue(watcher._produce(tr, row, card, lists, lambda _: None))
        self.assertEqual(tr.moves, ["building", "ready"])
        self.assertEqual(row["job"]["status"], "pronto_para_revisao")
        self.assertEqual(len(tr.files), 1)
        self.assertTrue(tr.estado_checklists("card")[watcher.AUTO_BUILDING][watcher.AUTO_ITEM])

    def test_claude_failure_routes_card_to_qa(self):
        tr = FakeTrello()
        watcher._ensure_checklists(tr, "card")
        for cl in tr.items:
            if cl["name"] in (watcher.GATE_ASSETS, watcher.GATE_COLORS):
                cl["checkItems"][0]["state"] = "complete"
        tr.attachment_list = [{"id": "zip1", "name": "insumos.zip"}]
        desc = watcher._description({"cid": "123456", "slug": "loja"})
        desc = desc.replace("Prompt:\n", "Prompt:\nSite\n", 1)
        desc = desc.replace("Cores:\n", "Cores:\nAzul\n", 1)
        row = {"slug": "loja"}
        lists = {watcher.BUILDING: "building", watcher.QA: "qa", watcher.READY: "ready"}
        with patch.object(watcher.shutil, "which", return_value="claude"), \
             patch.object(production, "prepare", return_value=(Path("job"), Path("assets"))), \
             patch.object(production, "run_claude", side_effect=RuntimeError("falha simulada")):
            self.assertTrue(watcher._produce(tr, row, {"id": "card", "desc": desc},
                                             lists, lambda _: None))
        self.assertEqual(tr.moves, ["building", "qa"])
        self.assertEqual(row["job"]["status"], "falhou")

    def test_json_index_atomic_and_active_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "prospects.json"
            store.save({"123": {"cid": "123", "card_id": "abc"}}, index)
            self.assertEqual(store.load(index)["123"]["card_id"], "abc")
            self.assertFalse(index.with_name("prospects.json.tmp").exists())
        lists = {"Coleta de ativos": "a", "Fechado": "b", "Descartado": "c"}
        cards = [{"name": "Loja", "idList": "a"}, {"name": "Cliente", "idList": "b"},
                 {"name": "MODELO — teste", "idList": "a"}]
        self.assertEqual(prospect_cycle.contar_ativos(cards, lists), 1)

    def test_basic_package_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            self._valid_site(site)
            package, problems = production.package(site, "teste")
            self.assertEqual(problems, [])
            self.assertTrue((site / "_qa" / "desktop.png").is_file())
            self.assertTrue((site / "_qa" / "mobile.png").is_file())
            with zipfile.ZipFile(package) as z:
                self.assertIn("index.html", z.namelist())
                self.assertNotIn("_qa/report.md", z.namelist())


if __name__ == "__main__":
    unittest.main()
