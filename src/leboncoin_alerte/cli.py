"""Entrypoint CLI : lbc-alerte <commande>.

Commandes : once | daemon | login | notify-test | status | init | export | import
"""

from __future__ import annotations

import argparse
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .browser import BrowserSession
from .bundle import export_bundle, import_bundle
from .config import ConfigError, load_config
from .notify.dispatcher import test_channels
from .runner import run_cycle
from .wizard import run_init

DEFAULT_CONFIG = Path("config.toml")


def _cmd_once(config, args) -> int:
    run_cycle(config, headless=not args.headful)
    return 0


def _cmd_daemon(config, args) -> int:
    stop = False

    def _stop(signum, frame):
        nonlocal stop
        stop = True
        print("Arrêt demandé — fin du cycle en cours…")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    config_path = Path(args.config)
    while not stop:
        try:
            # Recharger la config à chaque cycle : modifier config.toml/.env
            # (webhook, critères) est pris en compte sans redémarrer le daemon.
            config = load_config(config_path)
            run_cycle(config, headless=not args.headful)
        except ConfigError as exc:
            print(f"Config invalide (on garde l'ancienne, corrige config.toml) : {exc}")
        except Exception as exc:  # noqa: BLE001 — un daemon ne doit pas mourir
            print(f"Erreur de cycle (on continue) : {exc}")
        for _ in range(config.interval_minutes * 60):
            if stop:
                break
            time.sleep(1)
    return 0


def _cmd_login(config, args) -> int:
    BrowserSession(config.data_dir / "profile", headless=False).interactive(
        url=config.searches[0].url if config.searches else "https://www.leboncoin.fr"
    )
    return 0


def _cmd_notify_test(config, args) -> int:
    test_channels(config)
    return 0


def _cmd_status(config, args) -> int:
    from .store import Store

    store = Store(config.data_dir / "listings.db")
    counts = store.counts()
    if not counts:
        print("Aucune donnée — lance d'abord : lbc-alerte once")
        return 0
    for search_name, by_status in counts.items():
        print(f"\n[{search_name}]")
        for status in ("seen", "sent", "old"):
            print(f"  {status:>4} : {by_status.get(status, 0)}")
        rows = store.recent_sent(search_name, limit=5)
        if rows:
            print("  dernières notifiées :")
            for title, price, sent_at, _url in rows:
                print(f"    {sent_at[:16] if sent_at else '?'}  {price or '?':>5} €  {title[:60]}")
    store.close()
    return 0


def _cmd_init_config(args) -> int:
    dest = Path(args.config)
    if dest.exists():
        print(f"{dest} existe déjà — je n'écrase rien.")
        return 1
    src = Path(__file__).parent.parent.parent / "config.example.toml"
    if not src.exists():
        src = Path.cwd() / "config.example.toml"
    shutil.copy(src, dest)
    print(f"{dest} créé — édite-le (URL de recherche + webhooks) puis : lbc-alerte notify-test")
    return 0


def _cmd_init(args) -> int:
    try:
        return run_init(Path(args.config), force=args.force,
                        from_bundle=Path(args.from_bundle) if args.from_bundle else None)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"Init impossible : {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nInit interrompu — rien n'a été écrit.")
        return 1


def _cmd_export(args) -> int:
    config_path = Path(args.config)
    out = Path(args.out) if args.out else Path(f"lbc-bundle-{datetime.now():%Y%m%d-%H%M}")
    try:
        path, required = export_bundle(config_path, out, with_db=args.with_db,
                                       with_profile=args.with_profile)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"Export impossible : {exc}")
        return 1
    print(f"✓ Bundle exporté : {path}")
    print(f"  secrets : remplacés par ${{VAR}} — vars requises : {', '.join(required) or 'aucune'}")
    if args.with_profile:
        print("  ⚠ profil navigateur inclus : ne le réutilise que si l'IP change aussi, "
              "sinon préfère un profil neuf (lbc-alerte login).")
    print("  transfert : scp/rsync du répertoire, puis sur la cible : lbc-alerte init --from-bundle <dir>")
    return 0


def _cmd_import(args) -> int:
    try:
        import_bundle(Path(args.bundle), Path.cwd(), force=args.force)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"Import impossible : {exc}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lbc-alerte",
        description="Alertes Leboncoin : recherche navigateur humain-like + notifications.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "-c", "--config", default=str(DEFAULT_CONFIG), help="chemin du config.toml"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -c accepté avant OU après la sous-commande (dest distinct pour ne pas écraser).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", dest="sub_config", default=None, help=argparse.SUPPRESS)

    for name, help_ in [
        ("once", "un cycle de vérification"),
        ("daemon", "boucle permanente (interval_minutes)"),
        ("login", "ouvrir le navigateur visible pour valider la session/DataDome"),
        ("notify-test", "envoyer une fausse annonce sur les canaux activés"),
        ("init", "wizard : config manuelle, import d'un bundle, ou exemple"),
        ("init-config", "créer config.toml depuis l'exemple (alias non interactif)"),
        ("export", "exporter la config sanitisée (+ prix/DB) en bundle portable"),
        ("import", "importer un bundle exporté dans le répertoire courant"),
        ("status", "compteurs new/seen/sent/old et dernières alertes envoyées"),
    ]:
        p = sub.add_parser(name, help=help_, parents=[common])
        if name in ("once", "daemon"):
            p.add_argument(
                "--headful", action="store_true",
                help="navigateur visible (debug ; xvfb-run sur serveur headless)",
            )
        if name == "init":
            p.add_argument("--force", action="store_true", help="écraser un config.toml existant")
            p.add_argument("--from-bundle", metavar="DIR",
                           help="importer ce bundle sans passer par le menu")
        if name == "export":
            p.add_argument("--out", metavar="DIR", help="répertoire de destination (défaut : lbc-bundle-<date>)")
            p.add_argument("--with-db", action="store_true",
                           help="inclure listings.db (état des annonces déjà vues)")
            p.add_argument("--with-profile", action="store_true",
                           help="inclure le profil navigateur (déconseillé si l'IP change)")
        if name == "import":
            p.add_argument("bundle", help="répertoire du bundle (contenant manifest.json)")
            p.add_argument("--force", action="store_true", help="écraser les fichiers existants")

    args = parser.parse_args(argv)
    # -c peut être placé avant ou après la sous-commande ; le sous-parser écrit dans
    # sub_config pour ne pas écraser la valeur du parser global.
    args.config = getattr(args, "sub_config", None) or args.config

    if args.command == "init-config":
        return _cmd_init_config(args)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "import":
        return _cmd_import(args)

    try:
        config = load_config(Path(args.config))
    except FileNotFoundError:
        print(f"Config introuvable : {args.config}. Lance : lbc-alerte init-config")
        return 1
    except ConfigError as exc:
        print(f"Config invalide : {exc}")
        return 1

    handlers = {
        "once": _cmd_once,
        "daemon": _cmd_daemon,
        "login": _cmd_login,
        "notify-test": _cmd_notify_test,
        "status": _cmd_status,
    }
    return handlers[args.command](config, args)


if __name__ == "__main__":
    sys.exit(main())
