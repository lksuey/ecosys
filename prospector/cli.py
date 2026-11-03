"""CLI do ecossistema: Maps, indice local, Trello e Claude Code."""

from __future__ import annotations

import argparse
import importlib
import sys

from . import __version__, config, store


def cmd_check(_: argparse.Namespace) -> int:
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"Configuração inválida: {exc}", file=sys.stderr)
        return 1
    print(f"prospector {__version__}: {len(cfg.verticais)} verticais, "
          f"{len(cfg.bairros_ativos)} bairros ativos, {len(cfg.zonas)} zonas")
    print(f"índice local: {len(store.load())} prospects")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from . import scan_maps
    bairros = [b.strip() for b in (args.bairros or "").split(",") if b.strip()] or None
    return scan_maps.run(verticais=args.vertical, bairros=bairros,
                         headless=args.headless, limite=args.limit,
                         max_combos=args.max_combos, pause=not args.no_pause,
                         refresh=args.refresh, cobertura=args.cobertura)


def cmd_phase(module: str, run: str | None) -> int:
    return importlib.import_module(f".{module}", package="prospector").run(run)


def cmd_import(args: argparse.Namespace) -> int:
    rows, added = store.import_and_save(args.run)
    print(f"índice local: +{added} prospects; total {len(rows)}")
    return 0


def cmd_trello(name: str, args: argparse.Namespace) -> int:
    from . import trello
    try:
        return getattr(trello, name)(args)
    except trello.TrelloError as exc:
        print(f"[Trello] {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prospector", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="valida configurações e mostra tamanho do índice").set_defaults(func=cmd_check)

    scan = sub.add_parser("scan", help="varre Google Maps")
    scan.add_argument("--vertical", action="append")
    scan.add_argument("--bairros")
    scan.add_argument("--headless", action="store_true")
    scan.add_argument("--cobertura", choices=["cidade", "zonas", "bairros"])
    scan.add_argument("--limit", type=int)
    scan.add_argument("--max-combos", type=int)
    scan.add_argument("--no-pause", action="store_true")
    scan.add_argument("--refresh", action="store_true")
    scan.set_defaults(func=cmd_scan)

    for name in ("classify", "qualify"):
        phase = sub.add_parser(name, help=f"processa a run do Maps: {name}")
        phase.add_argument("--run")
        phase.set_defaults(func=lambda a, module=name: cmd_phase(module, a.run))
    imp = sub.add_parser("import-run", help="importa prospects qualificados de uma run para o índice")
    imp.add_argument("--run", required=True)
    imp.set_defaults(func=cmd_import)

    watch = sub.add_parser("watch", help="cria cards, observa gates e inicia produção")
    watch.add_argument("--limit", type=int)
    watch.add_argument("--slug")
    watch.set_defaults(func=lambda a: importlib.import_module(".watcher", "prospector").tick_com_lock(
        limite=a.limit, slug=a.slug))
    prospect = sub.add_parser("prospect", help="roda somente a prospecção incremental")
    prospect.add_argument("--teto", type=int, default=40)
    prospect.set_defaults(func=lambda a: importlib.import_module(".prospect_cycle", "prospector")
                         .manter_teto_com_lock(teto=a.teto))

    tp = sub.add_parser("trello", help="opera apenas o board configurado")
    tsub = tp.add_subparsers(dest="trello_command", required=True)
    tsub.add_parser("whoami").set_defaults(func=lambda a: cmd_trello("cmd_whoami", a))
    setup = tsub.add_parser("setup")
    setup.add_argument("--use-existing")
    setup.add_argument("--force-new", action="store_true")
    setup.set_defaults(func=lambda a: cmd_trello("cmd_setup", a))
    tsub.add_parser("check").set_defaults(func=lambda a: cmd_trello("cmd_check", a))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
