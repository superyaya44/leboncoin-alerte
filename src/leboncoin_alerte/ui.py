"""Interface web locale `lbc-alerte ui` : gérer ses alertes sans éditer config.toml.

Serveur stdlib, écoute sur 127.0.0.1 uniquement. Principes :
- on travaille sur le dict TOML brut : les clés inconnues de l'interface sont
  conservées telles quelles, les références ${VAR} aussi ;
- les secrets (webhooks, tokens, mots de passe) ne sortent jamais du serveur :
  la page sait seulement s'ils sont « configurés » et peut les remplacer ;
  les nouvelles valeurs vont dans .env, le TOML ne garde que ${VAR} ;
- avant d'écrire : validation par parse_config, sauvegarde .bak-<date>,
  écriture atomique. Une config invalide n'est jamais écrite.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import tomllib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

import tomli_w

from .config import Config, ConfigError, parse_config
from .insights import price_stats, test_search

# Champs secrets par canal → variable .env utilisée si on doit en créer une.
SECRET_FIELDS: dict[str, tuple[str, str]] = {
    "discord": ("webhook_url", "LBC_DISCORD_WEBHOOK"),
    "telegram": ("bot_token", "LBC_TELEGRAM_BOT_TOKEN"),
    "email": ("password", "LBC_SMTP_PASSWORD"),
    "webhook": ("url", "LBC_WEBHOOK_URL"),
}
_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]")


# ─── .env ──────────────────────────────────────────────────────────────────────

def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _backup(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    bak = path.with_name(f"{path.name}.bak-{stamp}")
    bak.write_bytes(path.read_bytes())
    os.chmod(bak, path.stat().st_mode & 0o777)
    return bak


def update_env_text(text: str, updates: dict[str, str]) -> str:
    """Remplace ou ajoute des lignes VAR=valeur, en gardant le reste du fichier."""
    lines = text.splitlines()
    pending = dict(updates)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{k}={v}" for k, v in pending.items()]
    return "\n".join(lines) + "\n"


# ─── Vue « publique » de la config (sans secrets) ─────────────────────────────

def _resolve(value: str, env: dict[str, str]) -> str:
    m = _VAR_RE.match(value.strip())
    if m:
        return env.get(m.group(1), "")
    return value


def public_view(raw: dict, env: dict[str, str]) -> dict:
    """Copie du dict TOML sans les valeurs secrètes + état de chaque secret."""
    data = copy.deepcopy(raw)
    secrets: dict[str, bool] = {}
    notify = data.get("notify", {})
    for ch, (field, _var) in SECRET_FIELDS.items():
        section = notify.get(ch)
        value = str(section.get(field, "")) if isinstance(section, dict) else ""
        secrets[ch] = bool(_resolve(value, env)) if value else False
        if isinstance(section, dict):
            section.pop(field, None)
    return {"config": data, "secrets": secrets}


def merge_submission(original: dict, submitted: dict, secret_updates: dict[str, str],
                     env: dict[str, str]) -> tuple[dict, dict[str, str]]:
    """Reconstruit le dict à écrire : config soumise + secrets d'origine réinjectés.

    Retourne (dict TOML, mises à jour .env). Un secret remplacé va dans .env et le
    TOML pointe dessus par ${VAR} (on réutilise la variable existante si possible).
    """
    raw = copy.deepcopy(submitted)
    env_updates: dict[str, str] = {}
    orig_notify = original.get("notify", {}) if isinstance(original.get("notify"), dict) else {}
    notify = raw.setdefault("notify", {})
    for ch, (field, default_var) in SECRET_FIELDS.items():
        section = notify.get(ch)
        orig_section = orig_notify.get(ch) if isinstance(orig_notify.get(ch), dict) else {}
        new_value = (secret_updates.get(ch) or "").strip()
        if new_value:
            if "\n" in new_value or "\r" in new_value:
                raise ConfigError(f"notify.{ch} : la valeur ne doit pas contenir de retour à la ligne.")
            m = _VAR_RE.match(str(orig_section.get(field, "")).strip())
            var = m.group(1) if m else default_var
            env_updates[var] = new_value
            section = notify.setdefault(ch, {}) if section is None else section
            section[field] = f"${{{var}}}"
        elif field in orig_section:
            section = notify.setdefault(ch, {}) if section is None else section
            section[field] = orig_section[field]
    if not notify:
        raw.pop("notify")
    return raw, env_updates


def validate(raw: dict, env: dict[str, str]) -> Config:
    """Valide avec le vrai parseur, en résolvant ${VAR} avec l'env fourni."""

    def expand(node: object) -> object:
        if isinstance(node, dict):
            return {k: expand(v) for k, v in node.items()}
        if isinstance(node, list):
            return [expand(v) for v in node]
        if isinstance(node, str):
            return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                          lambda m: env.get(m.group(1), m.group(0)), node)
        return node

    expanded = expand(raw)
    assert isinstance(expanded, dict)
    try:
        return parse_config(expanded)
    except ConfigError:
        raise
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError(f"valeur invalide : {exc}") from exc


def render(raw: dict) -> str:
    header = ("# Géré par lbc-alerte ui — les commentaires d'origine sont dans les "
              "sauvegardes config.toml.bak-*.\n\n")
    return header + tomli_w.dumps(raw)


# ─── Stockage ──────────────────────────────────────────────────────────────────

class ConfigStore:
    def __init__(self, config_path: Path):
        self.path = config_path.resolve()
        self.env_path = self.path.parent / ".env"

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(read_env_file(self.env_path))
        return env

    def version(self) -> str:
        h = hashlib.sha256()
        for p in (self.path, self.env_path):
            h.update(p.read_bytes() if p.exists() else b"")
        return h.hexdigest()[:16]

    def load_raw(self) -> dict:
        with self.path.open("rb") as f:
            return tomllib.load(f)

    def get(self) -> dict:
        view = public_view(self.load_raw(), self._env())
        view["version"] = self.version()
        view["path"] = str(self.path)
        return view

    def prepare(self, payload: dict) -> tuple[dict, dict[str, str], dict[str, str]]:
        submitted = payload.get("config")
        if not isinstance(submitted, dict):
            raise ConfigError("requête invalide : 'config' manquant.")
        updates = payload.get("secrets") or {}
        if not isinstance(updates, dict) or not all(isinstance(v, str) for v in updates.values()):
            raise ConfigError("requête invalide : 'secrets'.")
        raw, env_updates = merge_submission(self.load_raw(), submitted, updates, self._env())
        env = self._env()
        env.update(env_updates)
        validate(raw, env)
        tomli_w.dumps(raw)  # lève si une valeur n'est pas sérialisable
        return raw, env_updates, env

    def db_path(self, raw: dict | None = None) -> Path:
        raw = raw if raw is not None else self.load_raw()
        data_dir = Path(str(raw.get("general", {}).get("data_dir", "./data")))
        if not data_dir.is_absolute():
            data_dir = self.path.parent / data_dir
        return data_dir / "listings.db"

    def test(self, payload: dict) -> dict:
        raw, _, env = self.prepare(payload)
        config = validate(raw, env)
        try:
            index = int(payload.get("index", 0))
            search = config.searches[index]
        except (ValueError, IndexError) as exc:
            raise ConfigError("alerte introuvable.") from exc
        return test_search(self.db_path(raw), search)

    def listing(self, search_name: str, listing_id: str) -> dict | None:
        db = self.db_path()
        if not db.exists():
            return None
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT title, price, url, search_text, seller_question FROM listings "
                "WHERE search = ? AND listing_id = ?", (search_name, listing_id)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"id": listing_id, "search": search_name, "title": row[0], "price": row[1],
                "url": row[2], "text": (row[3] or "")[:600], "question": row[4]}

    def prices(self, name: str) -> dict:
        raw = self.load_raw()
        kind = next((s.get("market_kind") for s in raw.get("search", []) if s.get("name") == name), None)
        return price_stats(self.db_path(raw), name, kind)

    def preview(self, payload: dict) -> str:
        raw, _, _ = self.prepare(payload)
        return render(raw)

    def save(self, payload: dict) -> dict:
        if payload.get("version") != self.version():
            raise ConflictError("La configuration a changé entre-temps : recharge la page.")
        raw, env_updates, _ = self.prepare(payload)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backups = [b for b in (_backup(self.path, stamp),) if b]
        if env_updates:
            for k in env_updates:
                if not _ENV_KEY_RE.match(k):
                    raise ConfigError(f"nom de variable invalide : {k}")
            old = self.env_path.read_text() if self.env_path.exists() else ""
            if self.env_path.exists():
                backups.append(_backup(self.env_path, stamp))
            _atomic_write(self.env_path, update_env_text(old, env_updates), 0o600)
        mode = self.path.stat().st_mode & 0o777 if self.path.exists() else 0o600
        _atomic_write(self.path, render(raw), mode)
        return {"ok": True, "backups": [b.name for b in backups if b], "version": self.version()}


class ConflictError(Exception):
    pass


def check(config_path: Path) -> tuple[bool, str]:
    """Aller-retour lecture → réécriture en mémoire. Rien n'est écrit sur disque."""
    store = ConfigStore(config_path)
    original_text = store.path.read_text()
    raw = store.load_raw()
    rendered = render(raw)
    lines = []
    same = tomllib.loads(rendered) == raw
    lines.append("Données identiques après réécriture : " + ("oui ✓" if same else "NON ✗"))
    try:
        validate(raw, store._env())
        lines.append("Validation (load_config) : OK ✓")
        valid = True
    except ConfigError as exc:
        lines.append(f"Validation : ÉCHEC — {exc}")
        valid = False
    diff = difflib.unified_diff(original_text.splitlines(), rendered.splitlines(),
                                "config.toml (actuel)", "config.toml (réécrit par ui)", lineterm="")
    lines.append("")
    lines.append("Différences de texte (les commentaires disparaissent, gardés dans le .bak) :")
    lines.extend(diff)
    return same and valid, "\n".join(lines)


# ─── HTTP ──────────────────────────────────────────────────────────────────────

def _page() -> bytes:
    return resources.files("leboncoin_alerte").joinpath("ui_page.html").read_bytes()


def make_handler(store: ConfigStore):
    class Handler(BaseHTTPRequestHandler):
        server_version = "lbc-alerte-ui"

        def log_message(self, fmt, *args):  # pas de log des corps / secrets
            print(f"[ui] {self.command} {self.path.split('?')[0]} → {args[1] if len(args) > 1 else ''}")

        def _host_ok(self) -> bool:
            # Anti DNS-rebinding : seul un Host local est accepté.
            host = self.headers.get("Host") or ""
            name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
            return name in ALLOWED_HOSTS

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"Forbidden", "text/plain")
            route = self.path.split("?")[0]
            if route in ("/", "/index.html"):
                return self._send(200, _page(), "text/html; charset=utf-8")
            if route == "/api/config":
                try:
                    return self._json(200, store.get())
                except (OSError, tomllib.TOMLDecodeError) as exc:
                    return self._json(500, {"error": f"Lecture impossible : {exc}"})
            if route == "/api/listing":
                from urllib.parse import parse_qs, urlsplit

                q = parse_qs(urlsplit(self.path).query)
                info = store.listing(q.get("search", [""])[0], q.get("id", [""])[0])
                if info is None:
                    return self._json(404, {"error": "Annonce introuvable dans la base."})
                return self._json(200, info)
            if route == "/api/prices":
                from urllib.parse import parse_qs, urlsplit

                name = parse_qs(urlsplit(self.path).query).get("search", [""])[0]
                try:
                    return self._json(200, store.prices(name))
                except (OSError, sqlite3.Error) as exc:
                    return self._json(500, {"error": f"Lecture des prix impossible : {exc}"})
            if route == "/favicon.ico":
                return self._send(204, b"", "text/plain")
            return self._send(404, b"Not found", "text/plain")

        def do_POST(self):
            # Anti-CSRF : en-tête custom (impossible sans preflight CORS) + Host local.
            if not self._host_ok() or self.headers.get("X-LBC-UI") != "1":
                return self._json(403, {"error": "Requête refusée."})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(415, {"error": "JSON attendu."})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1_000_000:
                    return self._json(413, {"error": "Requête trop grosse."})
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"error": "JSON invalide."})
            route = self.path.split("?")[0]
            try:
                if route == "/api/preview":
                    return self._json(200, {"toml": store.preview(payload)})
                if route == "/api/test":
                    return self._json(200, store.test(payload))
                if route == "/api/save":
                    return self._json(200, store.save(payload))
            except ConflictError as exc:
                return self._json(409, {"error": str(exc)})
            except ConfigError as exc:
                return self._json(422, {"error": str(exc)})
            return self._json(404, {"error": "Route inconnue."})

    return Handler


def serve(config_path: Path, host: str = "127.0.0.1", port: int = 8898) -> int:
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Refus : l'interface n'écoute qu'en local (127.0.0.1). Utilise un tunnel SSH.")
        return 2
    store = ConfigStore(config_path)
    if not store.path.exists():
        print(f"Config introuvable : {store.path}. Lance d'abord : lbc-alerte init")
        return 1
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    print(f"Interface des alertes : http://{host}:{port}  (config : {store.path})  — Ctrl+C pour arrêter")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
