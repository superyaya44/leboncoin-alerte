"""Export / import de bundles de configuration portables.

Un bundle est un répertoire contenant :
  - config.toml        : copie SANITISÉE (secrets → ${VAR}) de la config courante
  - listings.db        : état des annonces déjà vues (optionnel, --with-db)
  - profile/           : profil navigateur (optionnel, --with-profile, déconseillé)
  - manifest.json      : version, date, fichiers + sha256, vars d'env requises

`export` produit exactement ce que `import` (et le mode import de `init`) consomme.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

from . import __version__

BUNDLE_VERSION = 1

# Chemin (tuple de clés) → variable d'environnement de substitution.
# Noms alignés sur .env.example / docker-compose.yml.
# La sanitisation est SÉMANTIQUE (parse tomllib → remplacement → réécriture) :
# aucune forme syntaxique TOML (clé pointée, quotée, multi-lignes, commentaires)
# ne peut contourner le remplacement.
SECRET_PATHS: dict[tuple[str, ...], str] = {
    ("notify", "discord", "webhook_url"): "DISCORD_WEBHOOK_URL",
    ("notify", "email", "password"): "SMTP_PASSWORD",
    ("notify", "telegram", "bot_token"): "TELEGRAM_BOT_TOKEN",
    ("notify", "webhook", "url"): "WEBHOOK_URL",
}

# Toutes les valeurs sous ce chemin sont sensibles (en-têtes d'API) : vidées.
HEADERS_PATH = ("notify", "webhook", "headers")

_VAR_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
# Valeur RÉELLEMENT de la forme ${VAR} (et rien d'autre) : seul cas passthrough.
_FULL_VAR_RE = re.compile(r"\$\{[A-Za-z0-9_]+\}")


def _secret_values(node: object, path: tuple[str, ...] = ()) -> list[str]:
    """Collecte les valeurs actuelles des chemins sensibles (pour le post-check).

    Les non-chaînes (entier, booléen, array…) sont collectées via str() :
    un mot de passe tout-numérique écrit sans guillemets est un secret aussi.
    """
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = path + (k,)
            if p in SECRET_PATHS and not isinstance(v, dict):
                out.extend(str(x) for x in (v if isinstance(v, list) else [v]))
            elif p[: len(HEADERS_PATH)] == HEADERS_PATH:
                out.extend(x if isinstance(x, str) else str(x) for x in _flatten(v))
            else:
                out.extend(_secret_values(v, p))
    elif isinstance(node, list):
        for el in node:
            out.extend(_secret_values(el, path))
    return out


def _flatten(node: object) -> list[object]:
    if isinstance(node, dict):
        return [x for v in node.values() for x in _flatten(v)]
    if isinstance(node, list):
        return [x for el in node for x in _flatten(el)]
    return [node]


def sanitize_config_text(text: str) -> tuple[str, list[str]]:
    """Sanitise SÉMANTIQUEMENT : parse TOML, remplace les secrets, réécrit.

    Les valeurs déjà de la forme ${VAR} sont laissées telles quelles (et la var
    est listée comme requise). Les commentaires (même contenant d'anciens
    secrets) disparaissent : le bundle est régénéré depuis l'arbre parsé.
    Post-check anti-fuite : si une ancienne valeur secrete resurgit dans le
    texte de sortie, l'export est refusé (ValueError).
    Retourne (texte_sanitisé, liste_des_vars_requises).
    """
    tree = tomllib.loads(text)  # TOMLDecodeError remontée à l'appelant
    old_secrets = [s for s in _secret_values(tree) if not _FULL_VAR_RE.fullmatch(s)]
    required: list[str] = []

    KNOWN_NOTIFY = {"discord", "email", "telegram", "webhook"}

    def walk(node: dict, path: tuple[str, ...]) -> None:
        for k in list(node):
            p = path + (k,)
            v = node[k]
            if p in SECRET_PATHS and not isinstance(v, dict) and not (
                isinstance(v, str) and _FULL_VAR_RE.fullmatch(v)
            ):
                # Tout type de valeur (str, int, array…) est remplacé ; seul un
                # ${VAR} intégral est passthrough.
                var = SECRET_PATHS[p]
                node[k] = f"${{{var}}}"
                if var not in required:
                    required.append(var)
            elif p == HEADERS_PATH:
                if any(_flatten(v)):
                    print("⚠ notify.webhook.headers vidés — ré-entre les en-têtes après import.")
                node[k] = {}
            elif path == ("notify",) and k not in KNOWN_NOTIFY and isinstance(v, dict):
                print(f"⚠ section notify.{k} inconnue — vérifie qu'elle ne contient pas de secret.")
                walk(v, p)
            elif isinstance(v, dict):
                walk(v, p)
            elif isinstance(v, list):
                for el in v:
                    if isinstance(el, dict):
                        walk(el, p)

    walk(tree, ())

    for s in _flatten(tree):
        if isinstance(s, str):
            for var in _VAR_RE.findall(s):
                if var not in required:
                    required.append(var)

    out_text = tomli_w.dumps(tree)

    # Post-check : aucune ancienne valeur secrète ne doit resurgir.
    for s in old_secrets:
        if len(s) >= 4 and s in out_text:
            raise ValueError(
                f"Sanitisation KO : un secret traverse encore ({s[:12]}…) — export refusé."
            )
    return out_text, required


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_sha256(path: Path) -> str:
    """Empreinte d'un répertoire : sha256 des (chemin relatif + hash fichier), triés."""
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(path)).encode())
            h.update(_sha256(p).encode())
    return h.hexdigest()


def _data_dir_of(config_path: Path, base: Path | None = None) -> Path:
    """Lit data_dir du config.toml sans valider toute la config.

    Les chemins relatifs sont résolus par rapport à `base` (par défaut le
    répertoire du fichier de config).
    """
    base = base if base is not None else config_path.parent
    try:
        with config_path.open("rb") as f:
            raw = tomllib.load(f)
        d = str(raw.get("general", {}).get("data_dir", "./data"))
    except Exception:  # noqa: BLE001 — fallback raisonnable
        d = "./data"
    p = Path(d).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def export_bundle(
    config_path: Path,
    out_dir: Path,
    with_db: bool = False,
    with_profile: bool = False,
) -> tuple[Path, list[str]]:
    """Exporte un bundle vers out_dir (créé). Retourne (chemin, vars d'env requises)."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config introuvable : {config_path}")
    raw_text = config_path.read_text(encoding="utf-8")
    try:
        tomllib.loads(raw_text)  # pas d'export d'une config syntaxiquement invalide
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"config.toml invalide, export refusé : {exc}") from exc
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} existe déjà et n'est pas vide — choisis un autre --out.")

    sanitized, required = sanitize_config_text(raw_text)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.toml").write_text(sanitized, encoding="utf-8")

    files: dict[str, str] = {"config.toml": _sha256(out_dir / "config.toml")}

    data_dir = _data_dir_of(config_path)
    db = data_dir / "listings.db"
    if with_db and db.exists():
        shutil.copy2(db, out_dir / "listings.db")
        files["listings.db"] = _sha256(out_dir / "listings.db")
    if with_profile and (data_dir / "profile").is_dir():
        shutil.copytree(data_dir / "profile", out_dir / "profile")
        files["profile/"] = _dir_sha256(out_dir / "profile")

    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "tool_version": __version__,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "required_env_vars": required,
        "files": files,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out_dir, required


def import_bundle(bundle: Path, dest_dir: Path, force: bool = False) -> list[str]:
    """Importe un bundle dans dest_dir (repo cloné). Retourne les vars d'env requises.

    Ne touche jamais à un fichier existant sans force=True. listings.db et
    profile/ sont restaurés dans le data_dir lu dans le config.toml du bundle.
    Crée/complète .env (chmod 600) avec les vars requises vides (jamais de valeur).
    """
    manifest_path = bundle / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Bundle invalide (manifest.json absent) : {bundle}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest.get("bundle_version", 0)) > BUNDLE_VERSION:
        raise ValueError(
            f"Bundle v{manifest['bundle_version']} plus récent que cet outil (v{BUNDLE_VERSION}) "
            "— mets à jour lbc-alerte avant d'importer."
        )
    required: list[str] = manifest.get("required_env_vars", [])

    # Intégrité : sha256 de chaque fichier/répertoire déclaré.
    for name, digest in manifest.get("files", {}).items():
        p = bundle / name
        if not p.exists():
            raise FileNotFoundError(f"Bundle incomplet : {name} manquant")
        actual = _dir_sha256(p) if name.endswith("/") else _sha256(p)
        if actual != digest:
            raise ValueError(f"Intégrité KO : {name} ne correspond pas au manifest (sha256).")

    dest_dir.mkdir(parents=True, exist_ok=True)
    data_dir = _data_dir_of(bundle / "config.toml", base=dest_dir)
    copied: list[str] = []

    def _place(src: Path, dst: Path, label: str) -> None:
        if dst.exists() and not force:
            raise FileExistsError(f"{dst} existe déjà — relance avec --force pour écraser.")
        if dst.is_dir():
            shutil.rmtree(dst)  # remplace, ne fusionne pas
        dst.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
        copied.append(label)

    if (bundle / "config.toml").exists():
        _place(bundle / "config.toml", dest_dir / "config.toml", "config.toml")
    if (bundle / "listings.db").exists():
        _place(bundle / "listings.db", data_dir / "listings.db", f"{data_dir.name}/listings.db")
    if (bundle / "profile").is_dir():
        _place(bundle / "profile", data_dir / "profile", f"{data_dir.name}/profile/")

    # .env : ajoute les vars manquantes (vides), sans toucher aux existantes.
    env_path = dest_dir / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    have = {l.split("=", 1)[0].strip() for l in existing.splitlines()
            if "=" in l and not l.strip().startswith("#")}
    missing = [v for v in required if v not in have]
    if missing:
        block = "\n# Importé du bundle — à compléter\n" + "\n".join(f"{v}=" for v in missing) + "\n"
        env_path.write_text(existing.rstrip("\n") + ("\n" if existing else "") + block.lstrip("\n"),
                            encoding="utf-8")
    if env_path.exists():
        env_path.chmod(0o600)

    print(f"Bundle importé dans {dest_dir} : {', '.join(copied) or 'rien'}")
    if required:
        print(f"Variables d'env requises dans {env_path} : {', '.join(required)}")
    print("Validation : lbc-alerte notify-test   (après avoir rempli .env)")
    return required
