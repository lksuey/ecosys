"""Watcher do funil: índice local + Trello + produção por Claude Code.

Um tick cria cards para prospects novos e observa os dois checklists humanos.
Com Prompt e Cores preenchidos e um ZIP válido (assets/), inicia Claude.
Não há dependência de Google Sheets ou biblioteca de templates.
"""

from __future__ import annotations

import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from . import production, store, trello
from .lock import exclusive

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "runs" / "watcher"
LOCK = LOG_DIR / ".lock"
ADMISSION_BATCH = 5
WORK_CAP = 40
PRODUCTION_WORKERS = 5

INITIAL = "Prospects qualificados"
COLLECTION = "Coleta de ativos"
BUILDING = "Em produção"
QA = "QA técnico"
READY = "Pronto para envio"
DISCARDED = "Descartado"

GATE_ASSETS = "Imagens/assets para a pagina anexadas"
GATE_COLORS = "Cores para a landing page definidas"
GATE_ASSETS_ITEM = "ZIP de assets anexado e conferido"
GATE_COLORS_ITEM = "Cores preenchidas na descrição"
AUTO_BUILDING = "Landing page em produção"
AUTO_ITEM = "Produção iniciada"
CHECKLIST_NAMES = (GATE_ASSETS, GATE_COLORS, AUTO_BUILDING)


def _can_admit(admissions: int, working: int) -> bool:
    return admissions < ADMISSION_BATCH and working < WORK_CAP


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


PROMPT_BASELINE = (
    "[Requisitos fixos do ecossistema — mantenha em toda página, não remova:]\n"
    "- Mínimo de 15 animações diferentes ao longo da página, variedade real (não "
    "repetir o mesmo efeito): transições ao rolar, blur progressivo, movimento de "
    "fundo/parallax, efeito de \"digitação\" em texto, fade/slide, hover states, etc.\n"
    "- Proibido usar emoji como ícone em qualquer parte da página. Ícones devem ser "
    "SVG/PNG reais (ex.: ícone de biblioteca aberta, ou logo oficial como WhatsApp/"
    "e-mail quando fizer sentido).\n"
    "- Botão flutuante de WhatsApp com animação de \"revelação circular\" igual à do "
    "botão oficial \"Open app\" da página de redirecionamento do WhatsApp (wa.me): um "
    "pseudo-elemento ::after circular, posicionado atrás do texto (z-index -1), que "
    "começa pequeno e baixo (transform: scale(1) translateY(67px)) e ao ativar (hover "
    "ou, em toque, uma classe equivalente disparada por JS) cresce cobrindo o botão "
    "(transform: scale(1.47) translateY(0)) com transition de 1s e "
    "cubic-bezier(.66,0,.34,1), trocando o preenchimento de uma cor para outra "
    "conforme cresce (ex.: verde -> escuro, texto acompanha com delay de .5s). Não é "
    "pulso/glow em loop — é uma transição de revelação ao interagir.\n"
    "[fim dos requisitos fixos — escreva a direção específica deste negócio abaixo:]"
)


def _description(r: dict) -> str:
    """Prompt editável em cima (com os requisitos fixos do ecossistema já
    inseridos); todos os dados conhecidos ficam abaixo."""
    fields = (
        ("CID", r.get("cid")), ("FTID", r.get("ftid")),
        ("Nome", r.get("nome")), ("Vertical", r.get("vertical")),
        ("Categoria no Maps", r.get("categoria_maps")),
        ("Endereço", r.get("endereco")), ("Bairro", r.get("bairro")),
        ("Telefone/WhatsApp", r.get("telefone")),
        ("Instagram", r.get("instagram")), ("Link informado como site", r.get("link_site")),
        ("Classificação do link", r.get("classe_link")),
        ("Nota no Maps", r.get("nota")), ("Número de avaliações", r.get("n_avaliacoes")),
        ("Latitude", r.get("lat")), ("Longitude", r.get("lng")),
        ("Ficha do Maps", r.get("url_ficha_maps")),
        ("Data da coleta", r.get("data_scan")), ("Slug", r.get("slug")),
    )
    dados = "\n".join(f"- {name}: {value if value not in (None, '') else 'não informado'}"
                      for name, value in fields)
    return f"Prompt:\n{PROMPT_BASELINE}\n\n\nCores:\n\n\nDados do prospecto:\n" + dados


def _legacy_description(desc: str) -> str:
    desc = desc or ""
    if not re.search(r"(?im)^dados do prospecto\s*:", desc):
        return "Prompt:\n\n\nCores:\n\n\nDados do prospecto:\n" + (desc or "(dados não disponíveis)")
    if not re.search(r"(?im)^prompt\s*:", desc):
        desc = "Prompt:\n\n\n" + desc
    if not re.search(r"(?im)^cores\s*:", desc):
        desc = re.sub(r"(?im)^dados do prospecto\s*:", "Cores:\n\n\nDados do prospecto:", desc, count=1)
    return desc


def _ensure_checklists(tr: trello.Trello, card_id: str,
                       current: list[dict] | None = None) -> list[dict]:
    current = current if current is not None else tr.checklists(card_id)
    keep = set()
    kept_ids = set()
    old = []
    changed = False
    for cl in current:
        if cl["name"] in CHECKLIST_NAMES and cl["name"] not in keep:
            keep.add(cl["name"])
            kept_ids.add(cl["id"])
        else:
            old.append(cl)
    if old:
        for cl in old:
            tr.delete_checklist(card_id, cl["id"], known=current)
        changed = True
    for name in CHECKLIST_NAMES:
        if name not in keep:
            cl = tr.add_checklist(card_id, name)
            item_name = (GATE_ASSETS_ITEM if name == GATE_ASSETS else
                         GATE_COLORS_ITEM if name == GATE_COLORS else AUTO_ITEM)
            tr.add_checkitem(cl["id"], item_name)
            changed = True
    for cl in current:
        if cl["id"] in kept_ids and not cl.get("checkItems"):
            item_name = (GATE_ASSETS_ITEM if cl["name"] == GATE_ASSETS else
                         GATE_COLORS_ITEM if cl["name"] == GATE_COLORS else AUTO_ITEM)
            tr.add_checkitem(cl["id"], item_name)
            changed = True
    return tr.checklists(card_id) if changed else current


def _set_auto(tr: trello.Trello, card_id: str, name: str,
              checklists: list[dict] | None = None) -> None:
    for cl in (checklists if checklists is not None else tr.checklists(card_id)):
        if cl["name"] == name:
            for item in cl.get("checkItems", []):
                if item["state"] != "complete":
                    tr.set_checkitem(card_id, item["id"], checked=True)
            return
    raise RuntimeError(f"checklist automática ausente: {name}")


def _gates_ok(checklists: list[dict]) -> bool:
    state = {cl["name"]: [item["state"] == "complete"
                            for item in cl.get("checkItems", [])] for cl in checklists}
    return all(state.get(name) and all(state[name]) for name in (GATE_ASSETS, GATE_COLORS))


def _create(tr: trello.Trello, lists: dict, r: dict, log: Log) -> str:
    title = f"{r.get('nome', 'Prospecto')} — {r.get('bairro', '')}".strip(" —")
    card = tr.create_card(lists[COLLECTION], title, desc=_description(r), pos="bottom")
    _ensure_checklists(tr, card["id"])
    log(f"{r.get('slug')}: card criado em {COLLECTION} ({card['shortUrl']})")
    return card["id"]


def _legacy_cards(cards: dict[str, dict]) -> dict[str, str]:
    result: dict[str, str] = {}
    for c in cards.values():
        m = re.search(r"(?im)(?:^|\n)\s*(?:-\s*)?cid:\s*`?(\d{6,})", c.get("desc", ""))
        if m:
            result[m.group(1)] = c["id"]
    return result


def _mark_issue_once(tr: trello.Trello, r: dict, card_id: str,
                     marker: str, message: str, log: Log) -> None:
    job = r.setdefault("job", {})
    if job.get("notice") != marker:
        tr.comment(card_id, "⚠️ " + message)
        job["notice"] = marker
    log(f"{r.get('slug')}: {message}")


def _comment_best_effort(tr: trello.Trello, card_id: str, message: str, log: Log) -> None:
    try:
        tr.comment(card_id, message)
    except Exception as exc:
        log(f"{card_id}: comentário não enviado — {type(exc).__name__}: {exc}")


def _produce(tr: trello.Trello, r: dict, card: dict, lists: dict, log: Log,
             checklists: list[dict] | None = None, persist=None) -> bool:
    card_id = card["id"]
    slug = production.artifact_slug(r["slug"], card_id)
    desc = card.get("desc", "")
    prompt = production.extract_prompt(desc)
    if not prompt:
        return False
    if not production.extract_colors(desc):
        return False
    if not _gates_ok(checklists if checklists is not None else tr.checklists(card_id)):
        return False
    attachments = production.source_zips(tr.attachments(card_id), slug)
    if not attachments:
        _mark_issue_once(tr, r, card_id, "zip_missing",
                         "Anexe um ou mais ZIPs com a pasta assets/ (o Trello limita cada "
                         "anexo a 10 MB; pode dividir em vários ZIPs no mesmo card).", log)
        return False
    fp = production.fingerprint(desc, attachments)
    job_state = r.setdefault("job", {})
    if job_state.get("last_success_fingerprint") == fp:
        tr.move_card(card_id, lists[READY])
        r["stage"] = READY
        log(f"{r['slug']}: insumos já produzidos; card retornou a {READY} sem chamar Claude")
        return True
    if not shutil.which("claude"):
        _mark_issue_once(tr, r, card_id, "claude_missing",
                         "Claude Code CLI não encontrado nesta máquina; produção aguardando instalação/autenticação.", log)
        return False
    try:
        job, assets = production.prepare(tr, card_id, slug, attachments, desc)
    except Exception as exc:
        _mark_issue_once(tr, r, card_id, "invalid_" + fp,
                         f"ZIP de insumos inválido: {exc}", log)
        return False

    job_state.update(fingerprint=fp, status="em_producao", started_at=_hora())
    if persist:
        persist()
    try:
        tr.move_card(card_id, lists[BUILDING])
        r["stage"] = BUILDING
        _set_auto(tr, card_id, AUTO_BUILDING, checklists)
        log(f"{slug}: Claude Code iniciado")
        site, _ = production.run_claude(job, assets, slug)
        zip_path, problems = production.package(site, slug)
        if problems:
            job_state.update(status="qa_pendente", finished_at=_hora())
            if persist:
                persist()
            tr.move_card(card_id, lists[QA])
            r["stage"] = QA
            _comment_best_effort(tr, card_id, "⚠️ Site gerado, mas QA técnico encontrou: " + "; ".join(problems), log)
            log(f"{slug}: QA pendente — {problems}")
        else:
            final_site = production.promote(site, slug)
            parts = production.split_zip_for_upload(final_site / zip_path.name)
            for part in parts:
                tr.anexar_arquivo(card_id, part)
            job_state.update(status="pronto_para_revisao", finished_at=_hora(),
                             last_success_fingerprint=fp)
            if persist:
                persist()
            tr.move_card(card_id, lists[READY])
            r["stage"] = READY
            if len(parts) > 1:
                msg = (f"✅ Landing page criada por Claude. O ZIP passou do limite de anexo "
                       f"do Trello (10 MB) e foi dividido em {len(parts)} arquivos — baixe "
                       "todos e extraia no mesmo diretório para reconstruir o site completo. "
                       "Revisão visual e publicação são manuais.")
            else:
                msg = "✅ Landing page criada por Claude e ZIP anexado. Revisão visual e publicação são manuais."
            _comment_best_effort(tr, card_id, msg, log)
            log(f"{slug}: pronto para revisão")
    except Exception as exc:
        if job_state.get("status") == "pronto_para_revisao":
            log(f"{slug}: artefato pronto; transição Trello pendente — {type(exc).__name__}: {exc}")
            raise
        job_state.update(status="falhou", finished_at=_hora(), error=str(exc)[:500])
        if persist:
            persist()
        tr.move_card(card_id, lists[QA])
        r["stage"] = QA
        _comment_best_effort(tr, card_id, f"⚠️ Produção falhou: {type(exc).__name__}: {str(exc)[:300]}. "
                             f"Após corrigir, mova o card para '{COLLECTION}' para tentar novamente.", log)
        log(f"{slug}: produção falhou — {type(exc).__name__}: {exc}")
    return True


def _recover(tr: trello.Trello, r: dict, card_id: str, lists: dict, log: Log) -> bool:
    """Um tick novo com o lock adquirido indica que a execução anterior terminou."""
    job = r.get("job") or {}
    status = job.get("status")
    if status == "pronto_para_revisao":
        tr.move_card(card_id, lists[READY])
        r["stage"] = READY
        log(f"{r.get('slug')}: transição final recuperada sem chamar Claude")
        return True
    if status in ("em_producao", "interrompido", "falhou", "qa_pendente"):
        job.update(status="interrompido" if status == "em_producao" else status,
                   finished_at=_hora())
        tr.move_card(card_id, lists[QA])
        r["stage"] = QA
        if status == "em_producao":
            _comment_best_effort(tr, card_id, "⚠️ Execução interrompida. Confira os arquivos locais; "
                                 f"para refazer, mova o card para '{COLLECTION}'.", log)
        log(f"{r.get('slug')}: execução anterior recuperada em {QA}")
        return True
    return False


def tick(log: Log | None = None, limite: int | None = None, slug: str | None = None) -> int:
    log = log or Log()
    resolved = trello.load_resolved()
    if not resolved.get("board_id"):
        log("board do Trello não configurado")
        return 1
    lists = resolved["listas"]
    by_id = {v: k for k, v in lists.items()}
    rows = store.load()
    if not rows:
        rows, added = store.bootstrap_runs(rows)
        store.update_rows(rows)
        log(f"índice local iniciado a partir das runs: {added} prospects")

    with trello.Trello(board_id=resolved["board_id"]) as tr:
        cards_list = tr.cards()
        cards = {c["id"]: c for c in cards_list}
        all_checklists = tr.board_checklists()
        legacy = _legacy_cards({cid: c for cid, c in cards.items()
                                if c.get("idList") != lists[DISCARDED]})
        reconciled: dict[str, dict] = {}
        for cid, card_id in legacy.items():
            if cid in rows and not rows[cid].get("card_id"):
                rows[cid]["card_id"] = card_id
                reconciled[cid] = {"card_id": card_id}
        if reconciled:
            store.patch_rows(reconciled)

        acted = 0
        working_ids = {lists[name] for name in (COLLECTION, BUILDING, QA)}
        working = sum(c.get("idList") in working_ids and
                      not str(c.get("name", "")).startswith("MODELO") for c in cards_list)
        admissions = 0
        ready: list[tuple[str, dict, dict, list[dict], dict]] = []
        for cid, r in rows.items():
            if slug and r.get("slug") != slug:
                continue
            if limite is not None and acted >= limite:
                break
            before = deepcopy(r)
            queued = False
            try:
                card_id = r.get("card_id")
                if not card_id:
                    if r.get("stage") in ("descartado_manual", DISCARDED, "Fechado") or \
                       r.get("estagio") in ("descartado_manual", "fechado"):
                        continue
                    if not _can_admit(admissions, working):
                        continue
                    r["card_id"] = _create(tr, lists, r, log)
                    r["stage"] = COLLECTION
                    admissions += 1
                    working += 1
                    acted += 1
                    continue
                card = cards.get(card_id)
                if card is None:
                    # Card arquivado/removido: nunca recriar por engano.
                    continue
                current = by_id.get(card.get("idList"), "")
                r["stage"] = current
                if current == DISCARDED:
                    continue
                if current in (INITIAL, "Triagem"):
                    if not _can_admit(admissions, working):
                        continue
                    tr.move_card(card_id, lists[COLLECTION])
                    r["stage"] = COLLECTION
                    admissions += 1
                    working += 1
                    acted += 1
                    continue
                if current == BUILDING:
                    if _recover(tr, r, card_id, lists, log):
                        acted += 1
                        if r.get("stage") == READY:
                            working -= 1
                    continue
                if current != COLLECTION:
                    continue
                desc = _legacy_description(card.get("desc", ""))
                if desc != card.get("desc", ""):
                    tr.update_card_desc(card_id, desc)
                    card["desc"] = desc
                    acted += 1
                checklists = _ensure_checklists(tr, card_id, all_checklists.get(card_id, []))
                # Só as verificações rápidas (desc/checklists) rodam aqui; o Claude
                # em si roda depois, em paralelo com os outros cards prontos.
                ready.append((cid, r, card, checklists, before))
                queued = True
            except Exception as exc:
                log(f"{r.get('slug', cid)}: erro no tick — {type(exc).__name__}: {exc}")
            finally:
                if not queued:
                    delta = {key: value for key, value in r.items() if before.get(key) != value}
                    if delta:
                        store.patch_rows({cid: delta})

        if ready:
            log(f"tick: {len(ready)} card(s) prontos para produção; "
                f"até {min(PRODUCTION_WORKERS, len(ready))} em paralelo")
            with ThreadPoolExecutor(max_workers=min(PRODUCTION_WORKERS, len(ready))) as pool:
                futures = {}
                for cid, r, card, checklists, before in ready:
                    def _persist(cid=cid, r=r):
                        store.patch_rows({cid: {"job": r.get("job"), "stage": r.get("stage")}})
                    futures[pool.submit(_produce, tr, r, card, lists, log, checklists, _persist)] = (cid, r, before)
                for fut in as_completed(futures):
                    cid, r, before = futures[fut]
                    try:
                        if fut.result():
                            acted += 1
                    except Exception as exc:
                        log(f"{r.get('slug', cid)}: erro na produção — {type(exc).__name__}: {exc}")
                    delta = {key: value for key, value in r.items() if before.get(key) != value}
                    if delta:
                        store.patch_rows({cid: delta})

        log(f"tick: {acted} ação(ões), {len(rows)} prospects no índice, {len(cards)} cards abertos")
    return 0


def tick_com_lock(**kw) -> int:
    with exclusive(LOCK) as acquired:
        if not acquired:
            print(f"{_hora()}  outro watcher em andamento — pulando", flush=True)
            return 0
        return tick(**kw)


if __name__ == "__main__":
    raise SystemExit(tick_com_lock())
