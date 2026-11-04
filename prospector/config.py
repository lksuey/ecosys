"""Carga e validação de prospector/config/*.yaml.

Uma única fonte para ler as verticais, os bairros e as regras de exclusão. Falha
cedo e com mensagem clara se algum YAML estiver malformado ou incompleto — o
teste de aceite da Fase 0 é justamente `cli.py check` passar aqui.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@lru_cache(maxsize=512)
def _termo_regex(termo: str) -> re.Pattern[str]:
    """Casa o termo como palavra(s) inteira(s), tolerando plural em 's'.

    Evita o falso-positivo de substring: 'upa' (UPA) não pode casar dentro de
    'roupa', mas 'UPA 24h' sim.
    """
    corpo = re.escape(termo.strip()).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){corpo}s?(?!\w)", re.IGNORECASE)


def _algum_termo(texto: str, termos: tuple[str, ...]) -> str | None:
    if not texto:
        return None
    for t in termos:
        if _termo_regex(t).search(texto):
            return t
    return None

CONFIG_DIR = Path(__file__).parent / "config"


class ConfigError(ValueError):
    """YAML de configuração inválido ou incompleto."""


@dataclass(frozen=True)
class Vertical:
    id: str
    rotulo: str
    termos_busca: tuple[str, ...]
    schema_type: str
    prioridade: str
    cobertura: str = "zonas"

    _PRIORIDADES = ("alta", "media", "baixa")
    _COBERTURAS = ("cidade", "zonas", "bairros")

    def __post_init__(self) -> None:
        if not self.id or " " in self.id:
            raise ConfigError(f"vertical.id inválido: {self.id!r} (slug sem espaços)")
        if not self.termos_busca:
            raise ConfigError(f"vertical {self.id!r} sem termos_busca")
        if self.prioridade not in self._PRIORIDADES:
            raise ConfigError(
                f"vertical {self.id!r}: prioridade {self.prioridade!r} "
                f"fora de {self._PRIORIDADES}"
            )
        if self.cobertura not in self._COBERTURAS:
            raise ConfigError(
                f"vertical {self.id!r}: cobertura {self.cobertura!r} fora de {self._COBERTURAS}"
            )


@dataclass(frozen=True)
class Bairro:
    nome: str
    lat: float
    lng: float
    zoom: int
    ativo: bool = True

    def __post_init__(self) -> None:
        if not (-6.0 < self.lat < -4.0) or not (-43.5 < self.lng < -42.0):
            raise ConfigError(
                f"bairro {self.nome!r}: lat/lng {self.lat},{self.lng} "
                "fora da caixa de Teresina — coordenada trocada?"
            )
        if not (10 <= self.zoom <= 18):
            raise ConfigError(f"bairro {self.nome!r}: zoom {self.zoom} fora de 10..18")


@dataclass(frozen=True)
class Exclusoes:
    categorias_denylist: tuple[str, ...]
    nomes_denylist: tuple[str, ...]
    link_rules: dict[str, tuple[str, ...]]
    verificacao_site: dict[str, Any]
    min_avaliacoes: int
    classes_qualificam: frozenset[str]
    avaliacoes: dict[str, Any]

    _CLASSES_VALIDAS = {
        "sem_site",
        "whatsapp",
        "rede_social",
        "agregador",
        "site_gbp",
        "marketplace",
        "site_morto",
        "site_parqueado",
        "tem_site",
    }

    def __post_init__(self) -> None:
        if self.min_avaliacoes < 0:
            raise ConfigError("qualificacao.min_avaliacoes negativo")
        desconhecidas = self.classes_qualificam - self._CLASSES_VALIDAS
        if desconhecidas:
            raise ConfigError(f"classes_qualificam desconhecidas: {sorted(desconhecidas)}")
        for classe in self.link_rules:
            if classe not in self._CLASSES_VALIDAS:
                raise ConfigError(f"link_rules: classe desconhecida {classe!r}")

    def classificar_host(self, host: str) -> str | None:
        """Retorna a classe de um host pelas link_rules, ou None (site_candidato)."""
        h = host.lower().removeprefix("www.")
        for classe, hosts in self.link_rules.items():
            for padrao in hosts:
                if h == padrao or h.endswith("." + padrao):
                    return classe
        return None

    def motivo_denylist(self, categoria: str | None, nome: str | None) -> str | None:
        """Se o negócio cai na denylist, retorna o motivo legível; senão None.

        Match por palavra inteira (ver _termo_regex) — 'Loja de roupa' NÃO casa
        com 'upa', mas 'UPA Zona Sul' sim.
        """
        cat_hit = _algum_termo(categoria or "", self.categorias_denylist)
        if cat_hit:
            return f"categoria '{categoria}' bate com denylist '{cat_hit}'"
        nome_hit = _algum_termo(nome or "", self.nomes_denylist)
        if nome_hit:
            return f"nome bate com rede/franquia '{nome_hit}'"
        return None


@dataclass
class Config:
    verticais: list[Vertical] = field(default_factory=list)
    bairros: list[Bairro] = field(default_factory=list)
    zonas: list[Bairro] = field(default_factory=list)
    exclusoes: Exclusoes | None = None
    cidade: dict[str, Any] = field(default_factory=dict)

    @property
    def bairros_ativos(self) -> list[Bairro]:
        return [b for b in self.bairros if b.ativo]

    @property
    def centro(self) -> Bairro:
        c = self.cidade.get("centro", {})
        return Bairro(nome="Teresina", lat=float(c.get("lat", -5.0892)),
                      lng=float(c.get("lng", -42.8016)), zoom=int(c.get("zoom", 12)))

    def vertical(self, vid: str) -> Vertical:
        for v in self.verticais:
            if v.id == vid:
                return v
        raise KeyError(f"vertical {vid!r} não existe em categorias.yaml")


def _load_yaml(nome: str) -> Any:
    caminho = CONFIG_DIR / nome
    if not caminho.exists():
        raise ConfigError(f"config ausente: {caminho}")
    try:
        return yaml.safe_load(caminho.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{nome}: YAML malformado — {exc}") from exc


def load(config_dir: Path | None = None) -> Config:
    """Carrega e valida os três YAMLs. Levanta ConfigError no primeiro problema."""
    global CONFIG_DIR
    if config_dir is not None:
        CONFIG_DIR = config_dir

    cat = _load_yaml("categorias.yaml") or {}
    verticais = [
        Vertical(
            id=str(item["id"]).strip(),
            rotulo=str(item["rotulo"]).strip(),
            termos_busca=tuple(t.strip() for t in item["termos_busca"]),
            schema_type=str(item["schema_type"]).strip(),
            prioridade=str(item.get("prioridade", "media")).strip(),
            cobertura=str(item.get("cobertura", "zonas")).strip(),
        )
        for item in cat.get("verticais", [])
    ]
    if not verticais:
        raise ConfigError("categorias.yaml: nenhuma vertical definida")
    ids = [v.id for v in verticais]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"categorias.yaml: id de vertical duplicado em {ids}")

    bai = _load_yaml("bairros.yaml") or {}
    bairros = [
        Bairro(
            nome=str(b["nome"]).strip(),
            lat=float(b["lat"]),
            lng=float(b["lng"]),
            zoom=int(b.get("zoom", 15)),
            ativo=bool(b.get("ativo", True)),
        )
        for b in bai.get("bairros", [])
    ]
    if not bairros:
        raise ConfigError("bairros.yaml: nenhum bairro definido")
    nomes = [b.nome for b in bairros]
    if len(nomes) != len(set(nomes)):
        raise ConfigError("bairros.yaml: nome de bairro duplicado")

    zonas = [
        Bairro(nome=str(z["nome"]).strip(), lat=float(z["lat"]), lng=float(z["lng"]),
               zoom=int(z.get("zoom", 13)))
        for z in bai.get("zonas", [])
    ]
    if not zonas:
        raise ConfigError("bairros.yaml: seção 'zonas' vazia (cobertura 'zonas' precisa dela)")

    exc = _load_yaml("exclusoes.yaml") or {}
    qual = exc.get("qualificacao", {})
    exclusoes = Exclusoes(
        categorias_denylist=tuple(s.lower() for s in exc.get("categorias_denylist", [])),
        nomes_denylist=tuple(s.lower() for s in exc.get("nomes_denylist", [])),
        link_rules={k: tuple(v) for k, v in (exc.get("link_rules") or {}).items()},
        verificacao_site=exc.get("verificacao_site", {}),
        min_avaliacoes=int(qual.get("min_avaliacoes", 5)),
        classes_qualificam=frozenset(qual.get("classes_qualificam", [])),
        avaliacoes=exc.get("avaliacoes", {}),
    )

    return Config(
        verticais=verticais,
        bairros=bairros,
        zonas=zonas,
        exclusoes=exclusoes,
        cidade=bai.get("cidade", {}),
    )
