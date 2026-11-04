"""Cliente Trello do ecossistema — ESCOPADO A UM ÚNICO BOARD.

Regra dura do projeto: este código nunca lê nem escreve fora do board do próprio
ecossistema. A credencial em `prospector/.secrets/trello.json` dá acesso à conta
inteira do usuário (que tem outros boards importantes); toda operação de escrita
aqui valida que o alvo pertence ao board configurado antes de agir. A única
chamada fora do board é `me()` / a checagem read-only de colisão de nome no
`setup` inicial.

IDs resolvidos ficam em `prospector/config/trello.yaml` (gerado localmente, ignorado pelo Git; veja trello.example.yaml).

CLI:
    python -m prospector.cli trello whoami
    python -m prospector.cli trello setup      [--use-existing <boardId> | --force-new]
    python -m prospector.cli trello check
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import yaml

BASE = Path(__file__).resolve().parent
SECRET_PATH = BASE / ".secrets" / "trello.json"
BOARD_SPEC_PATH = BASE / "config" / "trello_board.yaml"
RESOLVED_PATH = BASE / "config" / "trello.yaml"
API = "https://api.trello.com"


class TrelloError(RuntimeError):
    pass


class TrelloScopeError(TrelloError):
    """Tentativa de operar fora do board do ecossistema — bloqueada."""


def load_auth() -> dict[str, str]:
    if not SECRET_PATH.exists():
        raise TrelloError(
            f"credencial ausente: {SECRET_PATH}\n"
            'Crie o arquivo com {"key": "...", "token": "..."}'
        )
    data = json.loads(SECRET_PATH.read_text(encoding="utf-8"))
    if not data.get("key") or not data.get("token"):
        raise TrelloError(f"{SECRET_PATH}: precisa de 'key' e 'token'")
    return {"key": data["key"], "token": data["token"]}


def load_resolved() -> dict[str, Any]:
    if RESOLVED_PATH.exists():
        return yaml.safe_load(RESOLVED_PATH.read_text(encoding="utf-8")) or {}
    return {}


class Trello:
    def __init__(self, board_id: str | None = None, *, timeout: float = 20.0) -> None:
        self._auth = load_auth()
        self._http = httpx.Client(base_url=API, timeout=timeout)
        self.board_id: str | None = board_id
        self._list_ids: set[str] = set()
        self._card_board: dict[str, str] = {}
        if board_id:
            self._load_board_lists()

    def _req(self, method: str, path: str, **params: Any) -> Any:
        import time

        p = {k: v for k, v in params.items() if v is not None}
        p.update(self._auth)
        for tentativa in range(4):
            r = self._http.request(method, path, params=p)
            if r.status_code == 429:
                time.sleep(1.5 * (tentativa + 1))
                continue
            if r.status_code >= 400:
                raise TrelloError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None
        raise TrelloError(f"{method} {path}: rate limit persistente (429)")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Trello":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_board(self) -> str:
        if not self.board_id:
            raise TrelloScopeError(
                "cliente sem board_id — escrita bloqueada. Rode `trello setup` "
                "ou passe o board_id de prospector/config/trello.yaml."
            )
        return self.board_id

    def _load_board_lists(self) -> None:
        bid = self._require_board()
        lists = self._req("GET", f"/1/boards/{bid}/lists", fields="id,name", filter="all")
        self._list_ids = {lst["id"] for lst in lists}

    def _assert_list_on_board(self, id_list: str) -> None:
        self._require_board()
        if id_list not in self._list_ids:
            self._load_board_lists()
        if id_list not in self._list_ids:
            raise TrelloScopeError(f"lista {id_list} não pertence ao board do ecossistema")

    def _assert_card_on_board(self, id_card: str) -> None:
        bid = self._require_board()
        cached = self._card_board.get(id_card)
        if cached is None:
            card = self._req("GET", f"/1/cards/{id_card}", fields="idBoard")
            cached = card["idBoard"]
            self._card_board[id_card] = cached
        if cached != bid:
            raise TrelloScopeError(
                f"card {id_card} está no board {cached}, não no do ecossistema ({bid}). "
                "Operação bloqueada."
            )

    def me(self) -> dict[str, Any]:
        return self._req("GET", "/1/members/me", fields="fullName,username,url")

    def find_board_by_name(self, nome: str) -> dict[str, Any] | None:
        """Read-only: procura board com esse nome na conta (checagem de colisão)."""
        boards = self._req("GET", "/1/members/me/boards", fields="id,name,closed")
        for b in boards:
            if b["name"] == nome and not b.get("closed"):
                return b
        return None

    def board(self) -> dict[str, Any]:
        bid = self._require_board()
        return self._req("GET", f"/1/boards/{bid}", fields="id,name,url,closed")

    def lists(self) -> list[dict[str, Any]]:
        bid = self._require_board()
        return self._req("GET", f"/1/boards/{bid}/lists", fields="id,name,pos", filter="all")

    def cards(self, *, id_list: str | None = None) -> list[dict[str, Any]]:
        bid = self._require_board()
        if id_list:
            self._assert_list_on_board(id_list)
            data = self._req("GET", f"/1/lists/{id_list}/cards",
                             fields="id,name,idList,idBoard,labels,desc")
        else:
            data = self._req("GET", f"/1/boards/{bid}/cards",
                             fields="id,name,idList,idBoard,labels,desc")
        for c in data:
            self._card_board[c["id"]] = c["idBoard"]
        return data

    def checklists(self, id_card: str) -> list[dict[str, Any]]:
        self._assert_card_on_board(id_card)
        return self._req("GET", f"/1/cards/{id_card}/checklists")

    def board_checklists(self) -> dict[str, list[dict[str, Any]]]:
        """Lê todos os checklists do board em uma chamada e agrupa por card."""
        bid = self._require_board()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for checklist in self._req("GET", f"/1/boards/{bid}/checklists"):
            grouped.setdefault(checklist["idCard"], []).append(checklist)
        return grouped

    def estado_checklists(self, id_card: str) -> dict[str, dict[str, bool]]:
        """{ nome_checklist: { nome_item: concluido? } } — para os gates do watcher."""
        out: dict[str, dict[str, bool]] = {}
        for cl in self.checklists(id_card):
            out[cl["name"]] = {it["name"]: it["state"] == "complete"
                               for it in cl.get("checkItems", [])}
        return out

    def attachments(self, id_card: str) -> list[dict[str, Any]]:
        self._assert_card_on_board(id_card)
        return self._req("GET", f"/1/cards/{id_card}/attachments",
                         fields="id,name,url,mimeType,bytes,date")

    def baixar_anexo(self, url: str, dest) -> None:
        """Baixa um anexo qualquer do card pra `dest`. A URL de download de
        anexo (.../attachments/<id>/download/<nome>) exige o header OAuth."""
        import httpx as _httpx
        from pathlib import Path as _Path

        dest = _Path(dest)
        auth_header = (f'OAuth oauth_consumer_key="{self._auth["key"]}", '
                      f'oauth_token="{self._auth["token"]}"')
        with _httpx.Client(timeout=60, follow_redirects=True) as x:
            r = x.get(url, headers={"Authorization": auth_header})
            r.raise_for_status()
            dest.write_bytes(r.content)

    def anexar_arquivo(self, id_card: str, path: Path) -> dict[str, Any]:
        """Anexa um artefato somente ao card do board configurado."""
        self._assert_card_on_board(id_card)
        with path.open("rb") as fh:
            r = self._http.post(f"/1/cards/{id_card}/attachments", params=self._auth,
                                files={"file": (path.name, fh)}, timeout=180)
        if r.status_code >= 400:
            raise TrelloError(f"anexo {path.name}: {r.status_code}: {r.text[:200]}")
        return r.json()

    def create_list(self, name: str, *, pos: Any = "bottom") -> dict[str, Any]:
        bid = self._require_board()
        lst = self._req("POST", "/1/lists", name=name, idBoard=bid, pos=pos)
        self._list_ids.add(lst["id"])
        return lst

    def create_label(self, name: str, color: str | None) -> dict[str, Any]:
        bid = self._require_board()
        return self._req("POST", f"/1/boards/{bid}/labels", name=name, color=color or "")

    def create_card(self, id_list: str, name: str, *, desc: str = "",
                    pos: Any = "bottom") -> dict[str, Any]:
        self._assert_list_on_board(id_list)
        card = self._req("POST", "/1/cards", idList=id_list, name=name, desc=desc, pos=pos)
        self._card_board[card["id"]] = card["idBoard"]
        return card

    def add_checklist(self, id_card: str, name: str) -> dict[str, Any]:
        self._assert_card_on_board(id_card)
        return self._req("POST", f"/1/cards/{id_card}/checklists", name=name)

    def delete_checklist(self, id_card: str, id_checklist: str,
                         *, known: list[dict[str, Any]] | None = None) -> None:
        self._assert_card_on_board(id_card)
        current = known if known is not None else self.checklists(id_card)
        if id_checklist not in {c["id"] for c in current}:
            raise TrelloScopeError("checklist não pertence ao card informado")
        self._req("DELETE", f"/1/cards/{id_card}/checklists/{id_checklist}")

    def add_checkitem(self, id_checklist: str, name: str, *, checked: bool = False) -> dict[str, Any]:
        return self._req("POST", f"/1/checklists/{id_checklist}/checkItems",
                         name=name, checked=str(checked).lower())

    def set_checkitem(self, id_card: str, id_checkitem: str, *, checked: bool) -> dict[str, Any]:
        self._assert_card_on_board(id_card)
        state = "complete" if checked else "incomplete"
        return self._req("PUT", f"/1/cards/{id_card}/checkItem/{id_checkitem}", state=state)

    def move_card(self, id_card: str, id_list: str, *, pos: Any = "top") -> dict[str, Any]:
        self._assert_card_on_board(id_card)
        self._assert_list_on_board(id_list)
        return self._req("PUT", f"/1/cards/{id_card}", idList=id_list, pos=pos)

    def update_card_desc(self, id_card: str, desc: str) -> dict[str, Any]:
        self._assert_card_on_board(id_card)
        return self._req("PUT", f"/1/cards/{id_card}", desc=desc)

    def delete_card(self, id_card: str) -> None:
        self._assert_card_on_board(id_card)
        self._req("DELETE", f"/1/cards/{id_card}")

    def comment(self, id_card: str, text: str) -> dict[str, Any]:
        self._assert_card_on_board(id_card)
        return self._req("POST", f"/1/cards/{id_card}/actions/comments", text=text)


def _write_resolved(data: dict[str, Any]) -> None:
    header = (
        "# GERADO por `prospector/trello.py setup` — IDs do board do ecossistema.\n"
        "# Não é segredo (a credencial fica em .secrets/). Editar só se souber o que faz.\n\n"
    )
    RESOLVED_PATH.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")


def setup(*, use_existing: str | None = None, force_new: bool = False) -> dict[str, Any]:
    spec = yaml.safe_load(BOARD_SPEC_PATH.read_text(encoding="utf-8"))
    nome = spec["board"]["nome"]
    resolved = load_resolved()

    tr = Trello()

    if resolved.get("board_id") and not force_new and not use_existing:
        tr.board_id = resolved["board_id"]
        try:
            b = tr.board()
            if not b.get("closed"):
                print(f"Board já configurado: {b['name']}\n  {b['url']}")
                return resolved
        except TrelloError:
            print("board_id em trello.yaml não abre mais — recriando.")

    if not use_existing and not force_new:
        existente = tr.find_board_by_name(nome)
        if existente:
            raise TrelloError(
                f'Já existe um board "{nome}" na conta (id {existente["id"]}).\n'
                f"  Reusar:  trello setup --use-existing {existente['id']}\n"
                f"  Novo:    trello setup --force-new"
            )

    if use_existing:
        tr.board_id = use_existing
        board = tr.board()
        print(f"Adotando board existente: {board['name']} ({board['url']})")
    else:
        board = tr._req(
            "POST", "/1/boards/", name=nome,
            desc=spec["board"].get("descricao", ""),
            defaultLists="false",
            prefs_permissionLevel=spec["board"].get("permissao", "private"),
        )
        tr.board_id = board["id"]
        print(f"Board criado: {board['name']} ({board['url']})")

    existentes = {lst["name"]: lst["id"] for lst in tr.lists()}
    list_ids: dict[str, str] = {}
    for i, nome_lista in enumerate(spec["listas"]):
        if nome_lista in existentes:
            list_ids[nome_lista] = existentes[nome_lista]
        else:
            lst = tr.create_list(nome_lista, pos=(i + 1) * 1000)
            list_ids[nome_lista] = lst["id"]
            print(f"  + lista: {nome_lista}")

    labels_atuais = {
        lb["name"] for lb in tr._req("GET", f"/1/boards/{tr.board_id}/labels", fields="name")
        if lb.get("name")
    }
    label_ids: dict[str, str] = {}
    for lb in spec.get("labels", []):
        if lb["nome"] not in labels_atuais:
            created = tr.create_label(lb["nome"], lb.get("cor"))
            label_ids[lb["nome"]] = created["id"]
            print(f"  + label: {lb['nome']}")

    modelo = spec.get("card_modelo")
    modelo_id = resolved.get("card_modelo_id")
    if modelo:
        primeira = list_ids[spec["listas"][0]]
        nomes_cards = {c["name"] for c in tr.cards(id_list=primeira)}
        if modelo["nome"] not in nomes_cards:
            card = tr.create_card(primeira, modelo["nome"], desc=modelo.get("descricao", ""),
                                  pos="bottom")
            modelo_id = card["id"]
            for cl in modelo.get("checklists", []):
                checklist = tr.add_checklist(card["id"], cl["nome"])
                for item in cl.get("itens", []):
                    tr.add_checkitem(checklist["id"], item)
            print(f"  + card MODELO com {len(modelo.get('checklists', []))} checklists")

    out = {
        "board_id": tr.board_id,
        "board_url": board["url"],
        "board_nome": board["name"],
        "listas": list_ids,
        "labels": label_ids or resolved.get("labels", {}),
        "card_modelo_id": modelo_id,
    }
    _write_resolved(out)
    print(f"\nIDs salvos em {RESOLVED_PATH.relative_to(BASE.parent)}")
    tr.close()
    return out


def cmd_whoami(_args: Any) -> int:
    with Trello() as tr:
        m = tr.me()
        print(f"{m['fullName']} (@{m['username']})\n{m['url']}")
    return 0


def cmd_setup(args: Any) -> int:
    setup(use_existing=getattr(args, "use_existing", None),
          force_new=getattr(args, "force_new", False))
    return 0


def cmd_check(_args: Any) -> int:
    resolved = load_resolved()
    if not resolved.get("board_id"):
        print("Board ainda não configurado. Rode: python -m prospector.cli trello setup")
        return 1
    with Trello(board_id=resolved["board_id"]) as tr:
        b = tr.board()
        lists = tr.lists()
        print(f"Board: {b['name']}  ({'FECHADO' if b['closed'] else 'ok'})\n  {b['url']}")
        print(f"Listas ({len(lists)}):")
        for lst in lists:
            n = len(tr.cards(id_list=lst["id"]))
            print(f"  - {lst['name']:24} {n} card(s)")
    return 0
