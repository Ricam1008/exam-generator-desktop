"""Official Sign in with ChatGPT public-client flow; credentials stay in Keychain."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

from . import providers


ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
LOCK = threading.RLock()
REFRESH_LOCK = threading.RLock()
ATTEMPTS: dict[str, "Attempt"] = {}
ATTEMPT_SECONDS = 300


@dataclass
class Attempt:
    profile_id: str
    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    client_id: str
    subject: str
    expires_at: float
    server: Any = None
    status: str = "pending"
    detail: str = "Bitte die Anmeldung im Browser abschließen."
    consumed: bool = False


def _host_id() -> str:
    with providers.PROFILE_LOCK:
        path = providers.settings_dir() / "host.json"
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))["host_id"]
                if not isinstance(value, str) or not value.startswith("urn:uuid:"):
                    raise ValueError()
                uuid.UUID(value.removeprefix("urn:uuid:"))
                return value
            except (OSError, ValueError, KeyError) as exc:
                raise providers.ProviderError("settings", "Die lokale Anmeldekennung ist beschädigt. Sie wurde nicht überschrieben.") from exc
        value = "urn:uuid:" + str(uuid.uuid4())
        providers.atomic_json(path, {"host_id": value})
        return value


def _json_request(url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    httpx = providers._httpx()
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = client.get(url) if data is None else client.post(url, data=data)
            if response.status_code >= 300:
                if response.status_code in {400, 401, 403}:
                    raise providers.ProviderError("authentication", "Die ChatGPT-Anmeldung ist abgelaufen oder wurde abgelehnt. Bitte erneut anmelden.")
                raise providers._http_error(response.status_code, {})
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
    except providers.ProviderError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise providers.ProviderError("network", "Die ChatGPT-Anmeldung konnte nicht bestätigt werden. Bitte die Verbindung prüfen.") from exc


def validate_id_token(token: str, client_id: str, nonce: str | None, subject: str = "") -> dict[str, Any]:
    try:
        import jwt
    except ImportError as exc:
        raise providers.ProviderError("installation", "Die sichere Anmeldeprüfung fehlt. Bitte die App neu installieren.") from exc
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in {"RS256", "ES256"} or not isinstance(header.get("kid"), str):
            raise ValueError()
        jwks = _json_request(JWKS_URL)
        key = next(item for item in jwks.get("keys", []) if item.get("kid") == header["kid"])
        claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=[header["alg"]], audience=client_id, issuer=ISSUER, leeway=5, options={"require": ["sub", "exp", "iat"]})
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError()
        if nonce is not None and (not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce)):
            raise ValueError()
        if subject and not secrets.compare_digest(claims["sub"], subject):
            raise ValueError()
        return claims
    except Exception as exc:
        if isinstance(exc, providers.ProviderError):
            raise
        raise providers.ProviderError("authentication", "Die Identität der ChatGPT-Anmeldung konnte nicht sicher bestätigt werden. Bitte erneut anmelden.") from exc


def _credential_record(tokens: dict[str, Any], client_id: str, claims: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or {}
    scopes = str(tokens.get("scope") or "").split()
    if "chatgpt.tokens.use.direct" not in scopes:
        raise providers.ProviderError("permission", "Die Nutzung des ChatGPT-Abos wurde nicht freigegeben. Bitte bei der Anmeldung den Zugriff erlauben.")
    if str(tokens.get("token_type") or "").lower() != "bearer" or not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
        raise providers.ProviderError("authentication", "ChatGPT hat keine gültigen Zugangsdaten geliefert.")
    try:
        lifetime = int(tokens.get("expires_in", 0))
    except (ValueError, TypeError) as exc:
        raise providers.ProviderError("authentication", "ChatGPT hat eine ungültige Sitzungsdauer geliefert.") from exc
    if lifetime <= 0:
        raise providers.ProviderError("authentication", "Die ChatGPT-Sitzung ist bereits abgelaufen.")
    return {"client_id": client_id, "subject": claims["sub"], "email": claims.get("email", previous.get("email", "")), "access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token") or previous.get("refresh_token", ""), "id_token": tokens.get("id_token") or previous.get("id_token", ""), "scopes": scopes, "expires_at": time.time() + lifetime}


def _stop_listener(attempt: Attempt) -> None:
    if attempt.server is not None:
        server, attempt.server = attempt.server, None
        def stop():
            server.shutdown()
            server.server_close()
        threading.Thread(target=stop, daemon=True).start()


def finish(attempt: Attempt, query: dict[str, list[str]]) -> None:
    """Consume one callback; validation precedes any token exchange or credential write."""
    with LOCK:
        state = query.get("state", [""])
        if attempt.consumed or time.time() > attempt.expires_at:
            raise providers.ProviderError("authentication", "Die Anmeldeanfrage ist abgelaufen. Bitte erneut starten.")
        if len(state) != 1 or not secrets.compare_digest(state[0], attempt.state):
            raise providers.ProviderError("authentication", "Die Anmeldeanfrage passt nicht zu dieser Sitzung.")
        attempt.consumed = True
    try:
        if query.get("error"):
            attempt.status = "denied"
            attempt.detail = "Die ChatGPT-Anmeldung wurde nicht freigegeben. Du kannst sie erneut starten."
            return
        codes, clients = query.get("code", []), query.get("client_id", [])
        if len(codes) != 1 or not codes[0] or len(clients) > 1:
            raise providers.ProviderError("authentication", "Die Anmeldeantwort ist unvollständig.")
        client_id = clients[0] if clients else attempt.client_id
        if not client_id or client_id == "dynamic_agent_client" or (attempt.client_id != "dynamic_agent_client" and client_id != attempt.client_id):
            raise providers.ProviderError("authentication", "Die ChatGPT-Registrierung konnte nicht bestätigt werden.")
        tokens = _json_request(TOKEN_URL, {"grant_type": "authorization_code", "client_id": client_id, "code": codes[0], "code_verifier": attempt.verifier, "redirect_uri": attempt.redirect_uri, "resource": RESOURCE})
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise providers.ProviderError("authentication", "Die Identitätsbestätigung von ChatGPT fehlt.")
        claims = validate_id_token(id_token, client_id, attempt.nonce, attempt.subject)
        record = _credential_record(tokens, client_id, claims)
        with LOCK:
            if attempt.status != "pending" or time.time() > attempt.expires_at:
                raise providers.ProviderError("authentication", "Diese Anmeldeanfrage ist nicht mehr aktiv.")
            with providers.PROFILE_LOCK:
                profile = providers.get_profile(attempt.profile_id)
                if profile["kind"] != "chatgpt":
                    raise providers.ProviderError("settings", "Die ChatGPT-Verbindung existiert nicht mehr.")
                providers.set_secret(attempt.profile_id, record)
                providers.update_account(attempt.profile_id, {"account": record["email"] or record["subject"], "subject": record["subject"], "client_id": client_id, "credential_saved": True})
            attempt.status = "connected"
            attempt.detail = "ChatGPT ist angemeldet. Bitte ein Modell auswählen und die Verbindung testen."
    except providers.ProviderError as exc:
        attempt.status = "error"
        attempt.detail = str(exc)
        raise
    finally:
        _stop_listener(attempt)


def start(profile_id: str) -> dict[str, Any]:
    profile = providers.get_profile(profile_id)
    if profile["kind"] != "chatgpt":
        raise providers.ProviderError("settings", "Diese Verbindung verwendet keine ChatGPT-Anmeldung.")
    providers._keyring()
    host_id = _host_id()
    old = providers.get_secret(profile_id) if profile.get("credential_saved") else {}
    with LOCK:
        previous = ATTEMPTS.get(profile_id)
        if previous:
            previous.consumed = True
            previous.status = "replaced"
            _stop_listener(previous)
        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Authorization codes and login hints must never enter logs.

            def do_GET(self):
                request = urlsplit(self.path)
                host = self.headers.get("Host", "")
                if request.path != "/auth/callback" or host != f"127.0.0.1:{self.server.server_port}":
                    self.send_error(404)
                    return
                try:
                    finish(attempt, parse_qs(request.query, keep_blank_values=True))
                    body = "Anmeldung abgeschlossen. Du kannst dieses Fenster schließen und zur App zurückkehren."
                    status_code = 200
                except providers.ProviderError:
                    body = "Die Anmeldung konnte nicht bestätigt werden. Bitte zur App zurückkehren und erneut versuchen."
                    status_code = 400
                data = body.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
        server.daemon_threads = True
        verifier = secrets.token_urlsafe(48)
        attempt = Attempt(profile_id, secrets.token_urlsafe(32), secrets.token_urlsafe(32), verifier, f"http://127.0.0.1:{server.server_port}/auth/callback", profile.get("client_id") or "dynamic_agent_client", profile.get("subject") or "", time.time() + ATTEMPT_SECONDS, server)
        ATTEMPTS[profile_id] = attempt
        query = {"client_id": attempt.client_id, "ext_agent_host_id": host_id, "response_type": "code", "redirect_uri": attempt.redirect_uri, "scope": SCOPES, "resource": RESOURCE, "state": attempt.state, "nonce": attempt.nonce, "code_challenge_method": "S256", "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
        if attempt.client_id == "dynamic_agent_client":
            query["agent_name_hint"] = "Exam Generator"
        if profile.get("credential_saved"):
            if old.get("id_token"):
                query["id_token_hint"] = old["id_token"]
        if profile.get("account"):
            query["login_hint"] = profile["account"]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        def expire():
            with LOCK:
                if attempt.status == "pending":
                    attempt.consumed = True
                    attempt.status = "expired"
                    attempt.detail = "Die Anmeldung ist abgelaufen. Bitte erneut starten."
                    _stop_listener(attempt)
        timer = threading.Timer(ATTEMPT_SECONDS, expire)
        timer.daemon = True
        timer.start()
        return {"profile_id": profile_id, "status": "pending", "authorization_url": AUTHORIZE_URL + "?" + urlencode(query)}


def status(profile_id: str) -> dict[str, Any]:
    profile = providers.get_profile(profile_id)
    with LOCK:
        attempt = ATTEMPTS.get(profile_id)
        if attempt:
            return {"profile_id": profile_id, "status": attempt.status, "detail": attempt.detail, "account": profile.get("account", "")}
    connected = bool(profile.get("credential_saved"))
    return {"profile_id": profile_id, "status": "connected" if connected else "disconnected", "detail": "ChatGPT ist angemeldet." if connected else "Bitte mit ChatGPT anmelden.", "account": profile.get("account", "")}


def access_token(profile_id: str) -> str:
    with REFRESH_LOCK:
        profile = providers.get_profile(profile_id)
        if not profile.get("credential_saved"):
            raise providers.ProviderError("authentication", "Bitte zuerst mit ChatGPT anmelden.")
        record = providers.get_secret(profile_id)
        if "chatgpt.tokens.use.direct" not in record.get("scopes", []):
            raise providers.ProviderError("permission", "Die ChatGPT-Abo-Nutzung wurde nicht freigegeben. Bitte erneut anmelden.")
        if float(record.get("expires_at", 0)) > time.time() + 60 and record.get("access_token"):
            return record["access_token"]
        if not record.get("refresh_token"):
            raise providers.ProviderError("authentication", "Die ChatGPT-Anmeldung ist abgelaufen. Bitte erneut anmelden.")
        tokens = _json_request(TOKEN_URL, {"grant_type": "refresh_token", "client_id": record["client_id"], "refresh_token": record["refresh_token"], "resource": RESOURCE})
        if "scope" not in tokens:
            tokens["scope"] = " ".join(record["scopes"])
        claims = {"sub": record["subject"], "email": record.get("email", "")}
        if tokens.get("id_token"):
            claims = validate_id_token(tokens["id_token"], record["client_id"], None, record["subject"])
        updated = _credential_record(tokens, record["client_id"], claims, record)
        providers.set_secret(profile_id, updated)
        return updated["access_token"]


def disconnect(profile_id: str) -> dict[str, Any]:
    profile = providers.get_profile(profile_id)
    with LOCK:
        attempt = ATTEMPTS.pop(profile_id, None)
        if attempt:
            attempt.consumed = True
            attempt.status = "disconnected"
            _stop_listener(attempt)
    confirmed = not bool(profile.get("credential_saved"))
    with REFRESH_LOCK:
        if profile.get("credential_saved"):
            record = providers.get_secret(profile_id)
            if record.get("refresh_token"):
                for index in range(2):
                    try:
                        discovery = _json_request(ISSUER + "/.well-known/openid-configuration")
                        endpoint = discovery.get("revocation_endpoint", "")
                        parsed = urlsplit(endpoint)
                        if parsed.scheme != "https" or parsed.hostname != "auth.openai.com" or parsed.username or parsed.query or parsed.fragment:
                            raise providers.ProviderError("authentication", "Der Abmeldeendpunkt konnte nicht bestätigt werden.")
                        httpx = providers._httpx()
                        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
                            response = client.post(endpoint, data={"token": record["refresh_token"], "token_type_hint": "refresh_token", "client_id": record["client_id"]})
                        if response.status_code == 200:
                            confirmed = True
                            break
                        if response.status_code < 500:
                            break
                    except Exception:
                        pass
                    if index == 0:
                        time.sleep(0.5)
            providers.clear_secret(profile_id)
            providers.update_account(profile_id, {"credential_saved": False})
    return {"profile_id": profile_id, "status": "disconnected", "revocation_confirmed": confirmed, "detail": "ChatGPT wurde abgemeldet." if confirmed else "Lokal abgemeldet. Die serverseitige Abmeldung konnte nicht bestätigt werden; bei Bedarf die Verbindung in den ChatGPT-Einstellungen trennen."}
