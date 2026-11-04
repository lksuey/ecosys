"""Fase 1 — varredura do Google Maps (categoria x bairro) via Playwright.

Para cada termo de busca de cada vertical, cruzado com cada bairro ativo, abre a
busca do Maps, rola a lista até o fim, entra em cada ficha e extrai os dados que
alimentam a classificação (Fase 2). Sem API paga.

Usa o Google Chrome real (channel="chrome") — o Chromium empacotado recebe uma
página degradada do Google, sem o nº de avaliações. Nota e nº de avaliações vêm
do card da lista; o resto vem da ficha aberta.

Saída: runs/<timestamp>/raw.jsonl  (1 linha por ficha)  +  run.log
Dedup: data/seen-places.json  (por ftid — id estável da ficha do Maps)

O que NÃO acontece aqui: classificação do link ("tem site?" — ver classify.py) e
captura do texto das avaliações. Apenas a contagem é usada na qualificação.

Seletores do Maps quebram sem aviso quando o Google mexe no DOM. O código tolera
falha por ficha (loga e segue) e detecta CAPTCHA / consentimento.

Uso:
    python -m prospector.cli scan --vertical noivas --bairros "Jóquei,Fátima"
    python -m prospector.cli scan --vertical moda-feminina --vertical calcados --limit 20
    python -m prospector.cli scan            # todas as verticais x todos os bairros ativos
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import config

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
RUNS_DIR = BASE / "runs"
PROFILE_DIR = BASE / ".secrets" / "chrome-profile"
LEDGER_PATH = DATA_DIR / "seen-places.json"

MAPS_SEARCH = "https://www.google.com/maps/search/{q}/@{lat},{lng},{zoom}z?hl=pt-BR&entry=ttu"
FTID_RE = re.compile(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
LATLNG_AT_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
LATLNG_3D4D_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
FIM_DA_LISTA = re.compile(r"chegou ao fim da lista|reached the end of the list", re.I)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_FAST = os.environ.get("PROSPECTOR_FAST") == "1"


def _sleep(a: float, b: float) -> None:
    time.sleep(random.uniform(0.15, 0.4) if _FAST else random.uniform(a, b))


def _slugify(txt: str) -> str:
    txt = txt.lower().strip()
    subs = {"á": "a", "à": "a", "ã": "a", "â": "a", "é": "e", "ê": "e", "í": "i",
            "ó": "o", "ô": "o", "õ": "o", "ú": "u", "ç": "c"}
    for k, v in subs.items():
        txt = txt.replace(k, v)
    txt = re.sub(r"[^a-z0-9]+", "-", txt).strip("-")
    return re.sub(r"-{2,}", "-", txt)


def _cid_from_ftid(ftid: str) -> str | None:
    try:
        return str(int(ftid.split(":")[1], 16))
    except (ValueError, IndexError):
        return None


class Ledger:
    def __init__(self, path: Path = LEDGER_PATH) -> None:
        self.path = path
        self._seen: dict[str, str] = {}
        if path.exists():
            try:
                self._seen = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._seen = {}

    def has(self, ftid: str) -> bool:
        return ftid in self._seen

    def add(self, ftid: str, nome: str) -> None:
        self._seen[ftid] = f"{nome} @ {_now_iso()}"

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._seen, ensure_ascii=False, indent=1), encoding="utf-8")

    def __len__(self) -> int:
        return len(self._seen)


@dataclass
class RunWriter:
    run_id: str
    _dir: Path = field(init=False)
    _raw: Path = field(init=False)
    _log: Path = field(init=False)

    def __post_init__(self) -> None:
        self._dir = RUNS_DIR / self.run_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._raw = self._dir / "raw.jsonl"
        self._log = self._dir / "run.log"
        self._raw.touch(exist_ok=True)

    def record(self, obj: dict) -> None:
        with self._raw.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def log(self, msg: str) -> None:
        line = f"{_now_iso()}  {msg}"
        print(line, flush=True)
        with self._log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def screenshot_dir(self) -> Path:
        d = self._dir / "shots"
        d.mkdir(exist_ok=True)
        return d


def _txt(page, selector: str) -> str | None:
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return None
        t = (loc.text_content(timeout=1500) or "").strip()
        return t or None
    except Exception:
        return None


def _attr(page, selector: str, name: str) -> str | None:
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return None
        return loc.get_attribute(name, timeout=1500)
    except Exception:
        return None


def _parse_nota_e_avaliacoes(page) -> tuple[float | None, int | None]:
    raw = _txt(page, "div.F7nice")
    if not raw:
        raw = _attr(page, 'div.F7nice span[role="img"]', "aria-label") or ""
    nota = None
    n = None
    m = re.search(r"(\d+[.,]\d)", raw or "")
    if m:
        nota = float(m.group(1).replace(",", "."))
    m = re.search(r"\(?\s*([\d.\u00a0]+)\s*\)?\s*(avalia|review)?", raw or "", re.I)
    if m:
        try:
            n = int(re.sub(r"[.\u00a0\s]", "", m.group(1)))
        except ValueError:
            n = None
    return nota, n


def _extract_place(page, ctx: dict) -> dict | None:
    """Espera o painel de detalhe estar carregado. Retorna o registro cru."""
    try:
        page.wait_for_selector("h1.DUwDvf, h1", state="attached", timeout=10000)
    except Exception:
        return None

    url = page.url
    m_ft = FTID_RE.search(url)
    ftid = m_ft.group(1) if m_ft else None
    if not ftid:
        _sleep(1.0, 1.8)
        url = page.url
        m_ft = FTID_RE.search(url)
        ftid = m_ft.group(1) if m_ft else None
    if not ftid:
        return None

    nome = _txt(page, "h1.DUwDvf") or _txt(page, "h1")
    if not nome:
        return None

    categoria = _txt(page, "button[jsaction*='category']") or _txt(page, "div.DkEaL")
    endereco = None
    for sel in ('button[data-item-id="address"]',):
        endereco = _attr(page, sel, "aria-label") or _txt(page, f"{sel} div.Io6YTe")
        if endereco:
            endereco = re.sub(r"^Endereço:\s*", "", endereco).strip()
            break

    telefone = None
    tel_btn = page.locator('button[data-item-id^="phone:tel:"]').first
    try:
        if tel_btn.count():
            did = tel_btn.get_attribute("data-item-id") or ""
            telefone = did.replace("phone:tel:", "").strip() or None
            if not telefone:
                al = tel_btn.get_attribute("aria-label") or ""
                telefone = re.sub(r"^Telefone:\s*", "", al).strip() or None
    except Exception:
        pass

    website_raw = _attr(page, 'a[data-item-id="authority"]', "href")

    instagram = None
    try:
        ig = page.locator('a[href*="instagram.com"]').first
        if ig.count():
            instagram = ig.get_attribute("href")
    except Exception:
        pass

    plus_code = _txt(page, 'button[data-item-id="oloc"] div.Io6YTe')

    lat = lng = None
    m_ll = LATLNG_3D4D_RE.search(url) or LATLNG_AT_RE.search(url)
    if m_ll:
        lat, lng = float(m_ll.group(1)), float(m_ll.group(2))

    foto_hero = _attr(page, 'button[jsaction*="heroHeaderImage"] img', "src") or \
        _attr(page, 'div[role="main"] img[decoding]', "src")

    nota, n_aval = _parse_nota_e_avaliacoes(page)

    rec = {
        "ftid": ftid,
        "cid": _cid_from_ftid(ftid),
        "nome": nome,
        "slug": _slugify(nome),
        "vertical": ctx["vertical"],
        "termo_busca": ctx["termo"],
        "bairro": ctx["bairro"],
        "categoria_maps": categoria,
        "endereco": endereco,
        "telefone": telefone,
        "website_raw": website_raw,
        "instagram_maps": instagram,
        "nota": nota,
        "n_avaliacoes": n_aval,
        "lat": lat,
        "lng": lng,
        "plus_code": plus_code,
        "maps_url": url,
        "foto_hero_url": foto_hero,
        "reviews": [],
        "scan_ts": _now_iso(),
        "run": ctx["run"],
    }
    return rec



_CONSENT_BTN = (
    'button[aria-label="Aceitar tudo"]', 'button[aria-label="Rejeitar tudo"]',
    'button[aria-label="Accept all"]', 'button[aria-label="Reject all"]',
    'form[action*="consent"] button', 'button:has-text("Aceitar tudo")',
    'button:has-text("Rejeitar tudo")', 'button:has-text("Accept all")',
    '[aria-modal="true"] button:has-text("Aceitar")',
)


def _dismiss_consent(page, writer: RunWriter) -> bool:
    """Trata a tela/modal de consentimento do Google. Retorna True se agiu."""
    is_consent_url = "consent.google" in page.url or "consent.youtube" in page.url
    try:
        has_modal = page.locator('[aria-modal="true"]:has-text("cookies"), '
                                 'form[action*="consent"]').count() > 0
    except Exception:
        has_modal = False
    if not is_consent_url and not has_modal:
        return False
    for sel in _CONSENT_BTN:
        try:
            b = page.locator(sel).first
            if b.count():
                b.click(timeout=4000)
                writer.log("  consentimento de cookies tratado")
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                _sleep(1.5, 2.5)
                return True
        except Exception:
            continue
    writer.log("  !! tela de consentimento detectada mas nenhum botão clicou")
    return False


def _check_captcha(page, writer: RunWriter, pause: bool) -> None:
    blocked = "/sorry/" in page.url or page.locator(
        "form#captcha-form, iframe[src*='recaptcha'], iframe[title*='recaptcha']"
    ).count() > 0
    if not blocked:
        return
    shot = writer.screenshot_dir() / f"captcha-{int(time.time())}.png"
    try:
        page.screenshot(path=str(shot))
    except Exception:
        pass
    writer.log(f"  !! CAPTCHA/bloqueio detectado — {page.url}  (screenshot: {shot.name})")
    if not pause:
        raise RuntimeError("bloqueado pelo Google (CAPTCHA). Rode sem --no-pause e resolva no navegador.")
    input("\n>>> Resolva o CAPTCHA na janela do Chrome e tecle ENTER para continuar...\n")


_FEED_JS = r"""() => [...document.querySelectorAll('div.Nv2PK')].map(c => {
  const a = c.querySelector('a.hfpxzc');
  const w4 = [...c.querySelectorAll('.W4Efsd')].map(x => x.textContent || '');
  return {
    href: a ? a.href : null,
    nome: a ? a.getAttribute('aria-label') : null,
    rating: (c.querySelector('span.MW4etd') || {}).textContent || null,
    count: (c.querySelector('span.UY7F9') || {}).textContent || null,
    w4: w4,
  };
}).filter(x => x.href && x.href.includes('/maps/place/'))"""


def _feed_categoria(w4: list[str]) -> str | None:
    """A categoria costuma ser o 1º trecho, antes do ' · ', de um dos spans W4Efsd."""
    for txt in w4:
        t = (txt or "").replace(" ", " ").strip()
        if not t or re.match(r"^\d+[.,]\d", t):
            continue
        cat = t.split("·")[0].strip()
        if cat and not re.search(r"\d{2,}", cat):
            return cat
    return None


def _collect_feed(page) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    try:
        raw = page.evaluate(_FEED_JS)
    except Exception:
        raw = []
    for r in raw:
        href = r["href"]
        m = FTID_RE.search(href)
        key = m.group(1) if m else href
        if key in seen:
            continue
        seen.add(key)
        nota = None
        if r.get("rating"):
            try:
                nota = float(r["rating"].replace(",", ".").strip())
            except ValueError:
                pass
        n_aval = None
        if r.get("count"):
            digits = re.sub(r"\D", "", r["count"])
            n_aval = int(digits) if digits else None
        out.append({
            "href": href, "ftid": key if m else None, "nome": r.get("nome"),
            "nota": nota, "n_avaliacoes": n_aval,
            "categoria": _feed_categoria(r.get("w4") or []),
        })
    return out


def _scan_combo(page, vert: config.Vertical, termo: str, bairro: config.Bairro,
                incluir_nome: bool, run_id: str, ledger: Ledger, writer: RunWriter,
                exc: config.Exclusoes, limite: int | None, pause: bool,
                refresh: bool) -> tuple[int, int, int]:
    partes = [termo, bairro.nome if incluir_nome else "", "Teresina", "PI"]
    q = "+".join(p for p in " ".join(partes).split() if p)
    url = MAPS_SEARCH.format(q=q, lat=bairro.lat, lng=bairro.lng, zoom=bairro.zoom)
    writer.log(f"[{vert.id} / {bairro.nome}] {termo!r}")
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    _sleep(2.0, 3.5)
    _dismiss_consent(page, writer)
    _check_captcha(page, writer, pause)

    feed = page.locator('div[role="feed"]')
    try:
        feed.wait_for(timeout=8000)
    except Exception:
        writer.log("    (sem feed — 0 resultados ou layout inesperado)")
        return 0, 0, 0

    prev = -1
    estagnado = 0
    for _ in range(30):
        try:
            feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
        except Exception:
            break
        time.sleep(1.4 if _FAST else random.uniform(1.6, 2.8))
        if page.get_by_text(FIM_DA_LISTA).count() > 0 or \
           page.locator("span.HlvSq, div.PbZDve").count() > 0:
            break
        n = page.locator('div[role="feed"] a.hfpxzc').count()
        if n == prev:
            estagnado += 1
            if estagnado >= 4:
                break
        else:
            estagnado, prev = 0, n

    fichas = _collect_feed(page)
    if limite:
        fichas = fichas[:limite]
    writer.log(f"    {len(fichas)} fichas na lista")

    novos = pulados = descartados = 0
    for idx, ficha in enumerate(fichas, 1):
        href, nome_card = ficha["href"], ficha["nome"] or "?"
        ftid = ficha["ftid"] or (FTID_RE.search(href).group(1) if FTID_RE.search(href) else None)
        if ftid and ledger.has(ftid) and not refresh:
            pulados += 1
            continue

        n_feed = ficha["n_avaliacoes"]
        if n_feed is None and ficha["nota"] is None:
            n_feed = 0
        cat_feed = ficha["categoria"]
        motivo_pre = None
        if n_feed is not None and n_feed < exc.min_avaliacoes:
            motivo_pre = f"< {exc.min_avaliacoes} avaliações ({n_feed})"
        elif cat_feed:
            m = exc.motivo_denylist(cat_feed, ficha["nome"])
            if m:
                motivo_pre = m
        if motivo_pre and ftid:
            writer.record({
                "ftid": ftid, "cid": _cid_from_ftid(ftid), "nome": ficha["nome"],
                "slug": _slugify(ficha["nome"] or ""), "vertical": vert.id,
                "termo_busca": termo, "bairro": bairro.nome,
                "categoria_maps": cat_feed, "nota": ficha["nota"],
                "n_avaliacoes": n_feed, "maps_url": href, "website_raw": None,
                "reviews": [], "descartado_no_scan": motivo_pre,
                "scan_ts": _now_iso(), "run": run_id,
            })
            ledger.add(ftid, ficha["nome"] or "?")
            descartados += 1
            continue

        try:
            page.goto(href, wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            writer.log(f"    [{idx}] goto falhou ({nome_card}): {e}")
            continue
        _sleep(1.6, 3.2)
        _dismiss_consent(page, writer)
        _check_captcha(page, writer, pause)

        ctx = {"vertical": vert.id, "termo": termo, "bairro": bairro.nome, "run": run_id}
        rec = _extract_place(page, ctx)
        if not rec:
            writer.log(f"    [{idx}] extração falhou ({nome_card})")
            continue
        if ledger.has(rec["ftid"]) and not refresh:
            pulados += 1
            continue

        if ficha["nota"] is not None:
            rec["nota"] = ficha["nota"]
        if ficha["n_avaliacoes"] is not None:
            rec["n_avaliacoes"] = ficha["n_avaliacoes"]
        elif rec["n_avaliacoes"] is None:
            rec["n_avaliacoes"] = 0

        motivo = exc.motivo_denylist(rec["categoria_maps"], rec["nome"])
        rec["excluir_denylist"] = bool(motivo)
        rec["motivo_denylist"] = motivo

        writer.record(rec)
        ledger.add(rec["ftid"], rec["nome"])
        novos += 1
        flag = " [denylist]" if motivo else ""
        writer.log(f"    [{idx}] {rec['nome']}{flag} — {rec['n_avaliacoes']} aval, "
                   f"site={'sim' if rec['website_raw'] else 'não'}")

        if novos % 10 == 0:
            ledger.flush()
        _sleep(2.6, 6.0)

    ledger.flush()
    if descartados:
        writer.log(f"    ({descartados} descartados pelo card: <{exc.min_avaliacoes} aval / denylist)")
    return novos, pulados, descartados


Combo = tuple[config.Vertical, str, config.Bairro, bool]


def _locais(cfg: config.Config, vert: config.Vertical,
            bairros_arg: list[str] | None, cobertura_arg: str | None) -> list[tuple[config.Bairro, bool]]:
    """Locais a varrer para uma vertical, conforme cobertura. bool = incluir o
    nome do local no texto da busca (só faz sentido em 'bairros')."""
    modo = cobertura_arg or vert.cobertura
    if bairros_arg:
        alvo = {b.strip().lower() for b in bairros_arg}
        bs = [b for b in cfg.bairros if b.nome.lower() in alvo]
        faltando = alvo - {b.nome.lower() for b in bs}
        if faltando:
            print(f"[aviso] bairros fora de bairros.yaml: {sorted(faltando)}", file=sys.stderr)
        return [(b, True) for b in bs]
    if modo == "cidade":
        return [(cfg.centro, False)]
    if modo == "zonas":
        return [(z, False) for z in cfg.zonas]
    return [(b, True) for b in cfg.bairros_ativos]


def _iter_combos(cfg: config.Config, verticais: list[str] | None,
                 bairros: list[str] | None, cobertura: str | None) -> Iterator[Combo]:
    verts = [cfg.vertical(v) for v in verticais] if verticais else list(cfg.verticais)
    for v in verts:
        locais = _locais(cfg, v, bairros, cobertura)
        for termo in v.termos_busca:
            for local, incluir in locais:
                yield v, termo, local, incluir


def run(verticais: list[str] | None = None, bairros: list[str] | None = None, *,
        headless: bool = False, limite: int | None = None, max_combos: int | None = None,
        pause: bool = True, refresh: bool = False, cobertura: str | None = None,
        combo_offset: int = 0) -> int:
    from playwright.sync_api import sync_playwright

    cfg = config.load()
    assert cfg.exclusoes is not None
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    writer = RunWriter(run_id)
    ledger = Ledger()
    combos = list(_iter_combos(cfg, verticais, bairros, cobertura))[combo_offset:]
    if max_combos:
        combos = combos[:max_combos]

    writer.log(f"run {run_id} — {len(combos)} combos (termo x local), "
               f"ledger tem {len(ledger)} fichas conhecidas")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    total_novos = total_pulados = 0
    had_failure = False
    with sync_playwright() as p:
        launch_kwargs = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            locale="pt-BR",
            timezone_id="America/Fortaleza",
            viewport={"width": 1360, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
            writer.log("  navegador: Google Chrome (channel=chrome)")
        except Exception:
            ctx = p.chromium.launch_persistent_context(**launch_kwargs)
            writer.log("  navegador: Chromium empacotado — ATENÇÃO: nº de avaliações "
                       "pode não vir (Google degrada a página). Instale o Chrome.")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(15000)
        total_descartados = 0
        try:
            for i, (vert, termo, local, incluir) in enumerate(combos, 1):
                writer.log(f"--- combo {i}/{len(combos)} ---")
                try:
                    novos, pulados, descartados = _scan_combo(
                        page, vert, termo, local, incluir, run_id, ledger, writer,
                        cfg.exclusoes, limite, pause, refresh,
                    )
                    total_novos += novos
                    total_pulados += pulados
                    total_descartados += descartados
                except RuntimeError as e:
                    writer.log(f"ABORTADO: {e}")
                    had_failure = True
                    break
                except Exception as e:
                    writer.log(f"combo falhou: {type(e).__name__}: {e}")
                    had_failure = True
                _sleep(8.0, 20.0)
        finally:
            ledger.flush()
            ctx.close()

    writer.log(f"FIM — {total_novos} fichas coletadas, {total_descartados} descartadas "
               f"pelo card (<{cfg.exclusoes.min_avaliacoes} aval/denylist), "
               f"{total_pulados} puladas (dedup). raw: runs/{run_id}/raw.jsonl")
    return 1 if had_failure else 0
