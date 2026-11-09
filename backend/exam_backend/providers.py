"""Provider profiles, protected credentials, and one cancellable inference transport."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import uuid


SERVICE = "app.exam-generator.desktop"
PROFILE_LOCK = threading.RLock()
DEFAULT_PROFILE = {"id": "ollama", "kind": "ollama", "name": "Ollama", "base_url": "http://127.0.0.1:11434", "model": ""}
DEFAULT_PROFILES = [DEFAULT_PROFILE, {"id": "deepseek", "kind": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"}, {"id": "chatgpt", "kind": "chatgpt", "name": "ChatGPT", "base_url": "https://api.openai.com/v1", "model": ""}]
MAX_RESPONSE_BYTES = 12 * 1024 * 1024


class ProviderError(RuntimeError):
    def __init__(self, category: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class GenerationCanceled(ProviderError):
    def __init__(self):
        super().__init__("canceled", "Vorgang abgebrochen.")


@dataclass
class ProviderResult:
    text: str
    usage: dict[str, Any]
    finish_reason: str


def settings_dir() -> Path:
    # Isolate app state in tests without changing HOME, which macOS Keychain needs.
    if directory := os.environ.get("EXAM_GENERATOR_DATA_DIR"):
        return Path(directory).expanduser().resolve()
    return Path.home() / ".exam-generator-desktop"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
        backend = keyring.get_keyring()
        # Never accept third-party plaintext fallback backends.
        module = type(backend).__module__
        if isinstance(backend, FailKeyring) or module not in {"keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService", "keyring.backends.kwallet", "keyring.backends.libsecret"}:
            raise RuntimeError("no protected credential store")
        return keyring
    except Exception as exc:
        raise ProviderError("credentials", "Der geschützte Schlüsselspeicher ist nicht verfügbar. Bitte die App-Installation prüfen.") from exc


def get_secret(profile_id: str) -> dict[str, Any]:
    try:
        raw = _keyring().get_password(SERVICE, profile_id)
        return json.loads(raw) if raw else {}
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError("credentials", "Die Zugangsdaten konnten nicht aus dem Schlüsselspeicher gelesen werden.") from exc


def set_secret(profile_id: str, secret: dict[str, Any]) -> None:
    try:
        _keyring().set_password(SERVICE, profile_id, json.dumps(secret))
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError("credentials", "Die Zugangsdaten konnten nicht geschützt gespeichert werden.") from exc


def clear_secret(profile_id: str) -> None:
    store = _keyring()
    try:
        if store.get_password(SERVICE, profile_id) is not None:
            store.delete_password(SERVICE, profile_id)
    except Exception as exc:
        raise ProviderError("credentials", "Die Zugangsdaten konnten nicht gelöscht werden.") from exc


def list_profiles() -> list[dict[str, Any]]:
    with PROFILE_LOCK:
        path = settings_dir() / "profiles.json"
        if not path.exists():
            return [dict(profile) for profile in DEFAULT_PROFILES]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or any(not isinstance(p, dict) for p in data):
                raise ValueError()
            return [{k: v for k, v in p.items() if k in {"id", "kind", "name", "base_url", "model", "credential_saved", "account", "client_id", "subject"}} for p in data]
        except (OSError, ValueError) as exc:
            raise ProviderError("settings", "Die KI-Einstellungen sind beschädigt. Bitte die Einstellungsdatei prüfen; sie wurde nicht überschrieben.") from exc


def get_profile(profile_id: str) -> dict[str, Any]:
    for profile in list_profiles():
        if profile.get("id") == profile_id:
            return profile
    raise ProviderError("settings", "Die gewählte KI-Verbindung existiert nicht mehr.")


def validate_base_url(value: str) -> str:
    url = urlsplit(value)
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ProviderError("settings", "Bitte eine API-Adresse ohne Zugangsdaten, Suchparameter oder Fragment eingeben.")
    local = url.hostname == "localhost"
    try:
        local = local or ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        pass
    if url.scheme != "https" and not (url.scheme == "http" and local):
        raise ProviderError("settings", "Entfernte APIs benötigen HTTPS. HTTP ist nur für lokale Verbindungen erlaubt.")
    return value.rstrip("/")


def save_profile(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProviderError("settings", "Die Verbindungseinstellungen sind ungültig.")
    kind = payload.get("kind")
    if kind not in {"ollama", "deepseek", "compatible", "chatgpt"}:
        raise ProviderError("settings", "Unbekannter KI-Anbieter.")
    profile_id = str(payload.get("id") or uuid.uuid4())
    if not profile_id or len(profile_id) > 100 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in profile_id):
        raise ProviderError("settings", "Ungültige Verbindungs-ID.")
    defaults = {"ollama": "http://127.0.0.1:11434", "deepseek": "https://api.deepseek.com", "chatgpt": "https://api.openai.com/v1"}
    base_url = defaults["chatgpt"] if kind == "chatgpt" else str(payload.get("base_url") or defaults.get(kind, ""))
    profile = {"id": profile_id, "kind": kind, "name": str(payload.get("name") or kind)[:100], "base_url": validate_base_url(base_url), "model": str(payload.get("model") or "").strip()[:200]}
    with PROFILE_LOCK:
        profiles = list_profiles()
        existing = next((p for p in profiles if p["id"] == profile_id), {})
        if existing and existing["kind"] != kind:
            raise ProviderError("settings", "Für einen anderen Anbietertyp bitte eine neue Verbindung anlegen.")
        for key in ("credential_saved", "account", "client_id", "subject"):
            if key in existing:
                profile[key] = existing[key]
        api_key = payload.get("api_key")
        if api_key:
            if kind not in {"deepseek", "compatible", "ollama"} or not isinstance(api_key, str) or len(api_key) > 8192:
                raise ProviderError("settings", "Ungültiger API-Schlüssel.")
            if existing and existing.get("base_url") != profile["base_url"]:
                raise ProviderError("settings", "Bitte für eine andere API-Adresse eine neue Verbindung anlegen.")
            set_secret(profile_id, {"api_key": api_key.strip(), "kind": kind, "base_url": profile["base_url"]})
            profile["credential_saved"] = True
        elif existing.get("credential_saved") and existing.get("base_url") != profile["base_url"]:
            raise ProviderError("settings", "Eine gespeicherte Verbindung mit Zugangsdaten darf nicht auf eine andere API-Adresse umgeleitet werden.")
        profiles = [profile if p["id"] == profile_id else p for p in profiles]
        if not existing:
            profiles.append(profile)
        atomic_json(settings_dir() / "profiles.json", profiles)
    return profile


def update_account(profile_id: str, account: dict[str, Any]) -> None:
    with PROFILE_LOCK:
        profiles = list_profiles()
        for profile in profiles:
            if profile["id"] == profile_id:
                for field in ("account", "subject", "client_id", "credential_saved"):
                    if field in account:
                        profile[field] = account[field]
                break
        else:
            raise ProviderError("settings", "Die Verbindung existiert nicht mehr.")
        atomic_json(settings_dir() / "profiles.json", profiles)


def delete_profile(profile_id: str) -> None:
    profile = get_profile(profile_id)
    if profile.get("credential_saved"):
        if profile["kind"] == "chatgpt":
            from . import oauth
            oauth.disconnect(profile_id)
        else:
            clear_secret(profile_id)
    with PROFILE_LOCK:
        atomic_json(settings_dir() / "profiles.json", [p for p in list_profiles() if p["id"] != profile_id])


def _httpx():
    try:
        import httpx
        return httpx
    except ImportError as exc:
        raise ProviderError("installation", "Die Netzwerkkomponente fehlt. Bitte die App neu installieren.") from exc


def _http_error(status: int, body: Any) -> ProviderError:
    code = str((body.get("error") or {}).get("code", "")) if isinstance(body, dict) and isinstance(body.get("error"), dict) else ""
    if status in {401, 403} or code in {"invalid_api_key", "invalid_token", "authentication_error"}:
        return ProviderError("authentication", "Die KI-Verbindung wurde abgelehnt. Bitte Zugangsdaten oder Anmeldung prüfen.")
    if status == 402 or code in {"insufficient_quota", "insufficient_balance", "billing_hard_limit_reached"}:
        return ProviderError("balance", "Das Guthaben oder Nutzungslimit dieser KI-Verbindung ist ausgeschöpft.")
    if status == 429 or code in {"rate_limit_exceeded", "rate_limit_error"}:
        return ProviderError("rate_limit", "Die KI-Verbindung ist gerade ausgelastet. Bitte später erneut versuchen.", True)
    if status in {408, 409, 425} or 500 <= status <= 599:
        return ProviderError("unavailable", "Der KI-Anbieter ist vorübergehend nicht erreichbar.", True)
    if status == 404:
        return ProviderError("model", "Das gewählte Modell oder die API-Adresse ist nicht verfügbar.")
    return ProviderError("request", f"Der KI-Anbieter hat die Anfrage abgelehnt (HTTP {status}). Bitte Modell und Verbindung prüfen.")


async def _cancellable(coroutine, cancel_event):
    task = asyncio.create_task(coroutine)
    try:
        while not task.done():
            if cancel_event is not None and cancel_event.is_set():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise GenerationCanceled()
            await asyncio.wait({task}, timeout=0.1)
        return await task
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _credentials(profile: dict[str, Any]) -> dict[str, str]:
    registered = get_profile(profile["id"])
    # Exam metadata is untrusted: a saved credential must never follow its URL.
    if registered["kind"] != profile["kind"] or registered["base_url"] != profile["base_url"]:
        raise ProviderError("settings", "Die gespeicherte Prüfung verwendet eine andere KI-Adresse. Bitte eine passende Verbindung ausdrücklich auswählen.")
    if profile["kind"] == "chatgpt":
        from . import oauth
        return {"Authorization": "Bearer " + oauth.access_token(profile["id"])}
    if registered.get("credential_saved"):
        secret = get_secret(profile["id"])
        if secret.get("kind", profile["kind"]) != profile["kind"] or secret.get("base_url", profile["base_url"]) != profile["base_url"]:
            raise ProviderError("credentials", "Der gespeicherte API-Schlüssel gehört zu einer anderen Verbindung. Bitte die Verbindung neu einrichten.")
        key = secret.get("api_key")
        if not key:
            raise ProviderError("authentication", "Bitte einen API-Schlüssel speichern.")
        return {"Authorization": "Bearer " + key}
    if profile["kind"] == "deepseek":
        raise ProviderError("authentication", "Bitte zuerst den DeepSeek-API-Schlüssel speichern.")
    return {}


def list_models(profile_id: str) -> list[str]:
    profile = get_profile(profile_id)
    route = "/api/tags" if profile["kind"] == "ollama" else "/models"
    base = profile["base_url"]
    if profile["kind"] == "compatible" and not base.endswith("/v1"):
        # The configured base URL is exact; callers can supply /v1 when needed.
        base = profile["base_url"]
    httpx = _httpx()
    try:
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = client.get(base + route, headers=_credentials(profile))
            if response.status_code >= 300:
                raise _http_error(response.status_code, {})
            payload = response.json()
    except ProviderError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("network", "Die Modellliste konnte nicht geladen werden. Bitte die Verbindung prüfen.") from exc
    if not isinstance(payload, dict):
        raise ProviderError("response", "Der Anbieter hat eine ungültige Modellliste geliefert.")
    if profile["kind"] == "chatgpt":
        return [item["slug"] for item in payload.get("models", []) if isinstance(item, dict) and item.get("visibility") == "list" and isinstance(item.get("slug"), str)]
    field = "models" if profile["kind"] == "ollama" else "data"
    key = "name" if profile["kind"] == "ollama" else "id"
    return sorted({item[key] for item in payload.get(field, []) if isinstance(item, dict) and isinstance(item.get(key), str)})


def _request(profile, model, messages, temperature):
    headers = {"Content-Type": "application/json", **_credentials(profile)}
    if profile["kind"] == "chatgpt":
        instructions = "\n\n".join(str(m["content"]) for m in messages if m["role"] in {"system", "developer"})
        payload = {"model": model, "store": False, "stream": True, "input": [m for m in messages if m["role"] not in {"system", "developer"}]}
        if instructions:
            payload["instructions"] = instructions
        return profile["base_url"] + "/responses", headers, payload
    if profile["kind"] == "ollama":
        return profile["base_url"] + "/api/chat", headers, {"model": model, "messages": messages, "stream": True, "format": "json", "options": {"temperature": temperature}}
    payload = {"model": model, "messages": messages, "stream": True, "temperature": temperature}
    if profile["kind"] == "deepseek":
        payload["stream_options"] = {"include_usage": True}
    return profile["base_url"] + "/chat/completions", headers, payload


async def _events(response, kind):
    byte_count = 0
    buffered = []
    def decode(raw):
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError()
            return event
        except ValueError as exc:
            raise ProviderError("response", "Der Anbieter hat einen ungültigen Antwortstrom geliefert.") from exc
    async for line in response.aiter_lines():
        byte_count += len(line.encode("utf-8"))
        if byte_count > MAX_RESPONSE_BYTES:
            raise ProviderError("response", "Die KI-Antwort überschreitet die zulässige Größe.")
        if kind == "ollama":
            if line.strip():
                yield decode(line)
        elif not line:
            if buffered:
                raw = "\n".join(buffered)
                buffered.clear()
                if raw != "[DONE]":
                    yield decode(raw)
        elif line.startswith("data:"):
            buffered.append(line[5:].lstrip(" "))
        elif not line.startswith((":", "event:", "id:", "retry:")):
            raise ProviderError("response", "Der Anbieter hat keinen gültigen Ereignisstrom geliefert.")
    if buffered and "\n".join(buffered) != "[DONE]":
        yield decode("\n".join(buffered))


async def _stream(url, headers, payload, kind, timeout):
    httpx = _httpx()
    pieces = []
    usage: dict[str, Any] = {}
    finish = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 15)), follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 300:
                    raw = await response.aread()
                    try:
                        body = json.loads(raw[:65536])
                    except ValueError:
                        body = {}
                    raise _http_error(response.status_code, body)
                async for event in _events(response, kind):
                    if event.get("error"):
                        raise _http_error(400, event)
                    if kind == "ollama":
                        message = event.get("message")
                        if message is not None and not isinstance(message, dict):
                            raise ProviderError("response", "Die KI-Antwort enthält keine gültige Nachricht.")
                        content = (message or {}).get("content", "")
                        if isinstance(content, str):
                            pieces.append(content)
                        if event.get("done"):
                            finish = event.get("done_reason") or "stop"
                            usage = {k: event[k] for k in ("prompt_eval_count", "eval_count") if k in event}
                    elif kind == "chatgpt":
                        event_type = event.get("type")
                        if event_type == "response.output_text.delta":
                            pieces.append(str(event.get("delta") or ""))
                        elif event_type == "response.completed":
                            finish = "stop"
                            completed = event.get("response")
                            if not isinstance(completed, dict):
                                raise ProviderError("response", "Die ChatGPT-Antwort ist ungültig.")
                            usage = completed.get("usage") or {}
                        elif event_type in {"response.failed", "response.incomplete", "error"}:
                            raise ProviderError("response", "Die ChatGPT-Antwort wurde nicht vollständig abgeschlossen. Bitte erneut versuchen.")
                    else:
                        if isinstance(event.get("usage"), dict):
                            usage = event["usage"]
                        choices = event.get("choices", [])
                        if not isinstance(choices, list) or any(not isinstance(choice, dict) for choice in choices):
                            raise ProviderError("response", "Die KI-Antwort enthält ungültige Auswahlwerte.")
                        for choice in choices:
                            if choice.get("index", 0) != 0:
                                continue
                            delta = choice.get("delta") or {}
                            if not isinstance(delta, dict):
                                raise ProviderError("response", "Die KI-Antwort enthält ungültige Nachrichtenteile.")
                            content = delta.get("content")
                            if isinstance(content, str):
                                pieces.append(content)
                            finish = choice.get("finish_reason") or finish
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", "Die KI-Anfrage hat zu lange gedauert. Bitte erneut versuchen.", True) from exc
    except httpx.HTTPError as exc:
        raise ProviderError("network", "Die KI-Verbindung wurde unterbrochen. Bitte die Verbindung prüfen.", True) from exc
    if finish not in {"stop", "eos"}:
        raise ProviderError("truncated", "Die KI-Antwort ist unvollständig oder wurde abgeschnitten. Bitte kleinere Prüfungen erzeugen.")
    text = "".join(pieces).strip()
    if not text:
        raise ProviderError("response", "Die KI hat keine verwertbare Antwort geliefert.")
    return ProviderResult(text, usage, finish)


def complete(profile_id: str, model: str, messages: list[dict[str, Any]], timeout: int = 600, cancel_event=None, temperature: float = 0.25, profile_snapshot: dict[str, Any] | None = None) -> ProviderResult:
    if cancel_event is not None and cancel_event.is_set():
        raise GenerationCanceled()
    profile = dict(profile_snapshot) if profile_snapshot is not None else get_profile(profile_id)
    if profile.get("id") != profile_id or profile.get("kind") not in {"ollama", "deepseek", "compatible", "chatgpt"}:
        raise ProviderError("settings", "Die gespeicherte KI-Verbindung ist ungültig.")
    validate_base_url(profile["base_url"])
    if profile["kind"] == "chatgpt" and profile["base_url"] != "https://api.openai.com/v1":
        raise ProviderError("settings", "Die ChatGPT-API-Adresse ist ungültig.")
    selected = (model or profile.get("model") or "").strip()
    if not selected:
        raise ProviderError("model", "Bitte zuerst ein KI-Modell auswählen.")
    if not isinstance(messages, list) or any(not isinstance(m, dict) or m.get("role") not in {"system", "developer", "user", "assistant"} or not isinstance(m.get("content"), (str, list)) for m in messages):
        raise ProviderError("request", "Die KI-Nachrichten sind ungültig.")
    url, headers, payload = _request(profile, selected, messages, temperature)
    deadline = time.monotonic() + timeout
    for attempt in range(3):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("timeout", "Die Zeitgrenze für die KI-Anfrage wurde erreicht.")
            return asyncio.run(asyncio.wait_for(_cancellable(_stream(url, headers, payload, profile["kind"], remaining), cancel_event), remaining))
        except TimeoutError as exc:
            raise ProviderError("timeout", "Die Zeitgrenze für die KI-Anfrage wurde erreicht.") from exc
        except ProviderError as exc:
            if not exc.retryable or attempt == 2:
                raise
            delay = min(2 ** attempt, max(0, deadline - time.monotonic()))
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    raise GenerationCanceled()
            else:
                time.sleep(delay)
    raise AssertionError("unreachable")


def test_profile(profile_id: str, model: str | None = None) -> dict[str, Any]:
    selected = model or get_profile(profile_id).get("model") or ""
    try:
        result = complete(profile_id, selected, [{"role": "user", "content": 'Return exactly this JSON object: {"ok":true}'}], timeout=30, temperature=0)
        if not isinstance(json.loads(result.text), dict):
            raise ValueError()
        return {"ok": True, "profile_id": profile_id, "model": selected, "detail": "Das Modell hat eine vollständige Antwort geliefert.", "usage": result.usage}
    except (ProviderError, ValueError) as exc:
        return {"ok": False, "profile_id": profile_id, "model": selected, "detail": str(exc), "category": getattr(exc, "category", "response")}
