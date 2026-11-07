from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from exam_backend import oauth, providers


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.secrets = {}
        store = SimpleNamespace(get_password=lambda service, name: self.secrets.get(name), set_password=lambda service, name, value: self.secrets.__setitem__(name, value), delete_password=lambda service, name: self.secrets.pop(name, None))
        for target, value in (("settings_dir", lambda: self.root), ("_keyring", lambda: store)):
            p = patch.object(providers, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(oauth.ATTEMPTS.clear)

    def transport(self, handler):
        import httpx
        transport = httpx.MockTransport(handler)
        module = SimpleNamespace(AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs), Client=lambda **kwargs: httpx.Client(transport=transport, **kwargs), Timeout=httpx.Timeout, HTTPError=httpx.HTTPError, TimeoutException=httpx.TimeoutException)
        mocked = patch.object(providers, "_httpx", return_value=module)
        mocked.start()
        self.addCleanup(mocked.stop)

    def deepseek(self):
        return providers.save_profile({"id": "deepseek", "kind": "deepseek", "api_key": "secret-example", "model": "deepseek-flash"})

    def test_keys_stay_out_of_profiles_and_url_changes_are_rejected(self):
        self.deepseek()
        self.assertNotIn("secret-example", (self.root / "profiles.json").read_text())
        self.assertNotIn("api_key", json.dumps(providers.list_profiles()))
        self.assertIn("secret-example", self.secrets["deepseek"])
        with self.assertRaises(providers.ProviderError):
            providers.save_profile({"id": "deepseek", "kind": "deepseek", "base_url": "https://other.example"})

    def test_only_secure_or_loopback_api_urls(self):
        for url in ("http://remote.example", "https://name:password@example.com", "https://example.com?key=value", "file:///tmp/test"):
            with self.subTest(url=url), self.assertRaises(providers.ProviderError):
                providers.validate_base_url(url)
        self.assertEqual(providers.validate_base_url("http://127.0.0.1:11434/"), "http://127.0.0.1:11434")

    def test_untrusted_exam_snapshot_cannot_redirect_saved_credentials(self):
        profile = self.deepseek()
        malicious = {**profile, "base_url": "https://attacker.example"}
        with patch.object(providers, "_stream") as network, self.assertRaises(providers.ProviderError):
            providers.complete("deepseek", "deepseek-flash", [{"role": "user", "content": "private answer"}], profile_snapshot=malicious)
        network.assert_not_called()
        stored = providers.get_secret("deepseek")
        self.assertEqual(stored["base_url"], "https://api.deepseek.com")

    def test_deepseek_stream_and_usage(self):
        import httpx
        self.deepseek()
        def handler(request):
            self.assertEqual(request.headers["Authorization"], "Bearer secret-example")
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":5}}\n\ndata: [DONE]\n\n')
        self.transport(handler)
        result = providers.complete("deepseek", "deepseek-flash", [{"role": "user", "content": "test"}])
        self.assertEqual(result.text, '{"ok":true}')
        self.assertEqual(result.usage["total_tokens"], 5)

    def test_ollama_stream(self):
        import httpx
        self.transport(lambda request: httpx.Response(200, text='{"message":{"content":"hello"},"done":false}\n{"message":{"content":" world"},"done":true,"done_reason":"stop","eval_count":2}\n'))
        result = providers.complete("ollama", "local-model", [{"role": "user", "content": "test"}])
        self.assertEqual(result.text, "hello world")
        self.assertEqual(result.usage, {"eval_count": 2})

    def test_chatgpt_payload_uses_official_supported_route(self):
        import httpx
        def handler(request):
            self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
            data = json.loads(request.content)
            self.assertFalse(data["store"])
            self.assertTrue(data["stream"])
            self.assertEqual(data["instructions"], "system prompt")
            self.assertEqual(data["input"], [{"role": "user", "content": "test"}])
            self.assertNotIn("temperature", data)
            return httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"{}"}\n\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":2}}}\n\n')
        self.transport(handler)
        with patch.object(oauth, "access_token", return_value="fake-oauth"):
            result = providers.complete("chatgpt", "available-model", [{"role": "system", "content": "system prompt"}, {"role": "user", "content": "test"}])
        self.assertEqual(result.text, "{}")

    def test_model_lists_come_from_selected_provider(self):
        import httpx
        self.transport(lambda request: httpx.Response(200, json={"models": [{"slug": "one", "visibility": "list"}, {"slug": "hidden", "visibility": "hidden"}]}))
        with patch.object(oauth, "access_token", return_value="fake-oauth"):
            self.assertEqual(providers.list_models("chatgpt"), ["one"])

    def test_authentication_and_balance_do_not_retry_or_leak_response(self):
        import httpx
        self.deepseek()
        for status, body, category in ((401, {"error": {"message": "secret-example"}}, "authentication"), (429, {"error": {"code": "insufficient_quota"}}, "balance")):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, json=body)
            self.transport(handler)
            with self.assertRaises(providers.ProviderError) as error:
                providers.complete("deepseek", "deepseek-flash", [{"role": "user", "content": "test"}])
            self.assertEqual(error.exception.category, category)
            self.assertNotIn("secret-example", str(error.exception))
            self.assertEqual(len(calls), 1)

    def test_rate_limit_retries_are_bounded(self):
        import httpx
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})
        self.transport(handler)
        with patch.object(providers.time, "sleep"), self.assertRaises(providers.ProviderError):
            providers.complete("ollama", "model", [{"role": "user", "content": "test"}])
        self.assertEqual(len(calls), 3)

    def test_missing_completion_and_invalid_json_are_errors(self):
        import httpx
        for body in ('{"message":{"content":"partial"},"done":false}\n', 'not json\n'):
            self.transport(lambda request: httpx.Response(200, text=body))
            with self.assertRaises(providers.ProviderError):
                providers.complete("ollama", "model", [{"role": "user", "content": "test"}])

    def test_multiline_server_sent_events(self):
        import httpx
        self.deepseek()
        self.transport(lambda request: httpx.Response(200, text='event: message\ndata: {"choices":\ndata: [{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'))
        self.assertEqual(providers.complete("deepseek", "deepseek-flash", [{"role": "user", "content": "test"}]).text, "{}")

    def test_cancellation_interrupts_a_blocked_stream(self):
        import httpx
        event = threading.Event()
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(30)
                yield b'{}'
        self.transport(lambda request: httpx.Response(200, stream=SlowStream()))
        timer = threading.Timer(0.05, event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(providers.GenerationCanceled):
                providers.complete("ollama", "model", [{"role": "user", "content": "test"}], cancel_event=event)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 2)

    def attempt(self, client="dynamic_agent_client", subject=""):
        return oauth.Attempt("chatgpt", "expected-state", "expected-nonce", "verifier", "http://127.0.0.1:1234/auth/callback", client, subject, time.time() + 60)

    def tokens(self, **extra):
        return {"access_token": "new-access", "refresh_token": "new-refresh", "id_token": "signed-token", "token_type": "Bearer", "expires_in": 3600, "scope": oauth.SCOPES, **extra}

    def test_callback_state_mismatch_does_not_exchange(self):
        attempt = self.attempt()
        with patch.object(oauth, "_json_request") as exchange:
            with self.assertRaises(providers.ProviderError):
                oauth.finish(attempt, {"state": ["wrong"], "code": ["code"], "client_id": ["issued"]})
            exchange.assert_not_called()
        self.assertFalse(attempt.consumed)

    def test_callback_denial_does_not_exchange_or_replace_credentials(self):
        with patch.object(oauth, "_json_request") as exchange:
            attempt = self.attempt()
            oauth.finish(attempt, {"state": [attempt.state], "error": ["access_denied"]})
            exchange.assert_not_called()
            self.assertEqual(attempt.status, "denied")
            self.assertEqual(self.secrets, {})

    def test_callback_requires_issued_client_and_rejects_replay(self):
        for client in (None, "dynamic_agent_client", "wrong-client"):
            attempt = self.attempt("registered" if client == "wrong-client" else "dynamic_agent_client")
            query = {"state": [attempt.state], "code": ["code"]}
            if client:
                query["client_id"] = [client]
            with patch.object(oauth, "_json_request") as exchange, self.assertRaises(providers.ProviderError):
                oauth.finish(attempt, query)
            exchange.assert_not_called()
        attempt = self.attempt()
        with patch.object(oauth, "_json_request", return_value=self.tokens()) as exchange, patch.object(oauth, "validate_id_token", return_value={"sub": "subject", "email": "person@example.test"}):
            query = {"state": [attempt.state], "code": ["code"], "client_id": ["issued"]}
            oauth.finish(attempt, query)
            self.assertEqual(attempt.status, "connected")
            self.assertEqual(exchange.call_args.args[1]["code_verifier"], "verifier")
            with self.assertRaises(providers.ProviderError):
                oauth.finish(attempt, query)
        profile = providers.get_profile("chatgpt")
        self.assertEqual(profile["client_id"], "issued")
        self.assertNotIn("new-access", json.dumps(profile))

    def test_missing_plan_permission_does_not_store_tokens(self):
        attempt = self.attempt()
        with patch.object(oauth, "_json_request", return_value=self.tokens(scope="openid profile")), patch.object(oauth, "validate_id_token", return_value={"sub": "subject"}), self.assertRaises(providers.ProviderError):
            oauth.finish(attempt, {"state": [attempt.state], "code": ["code"], "client_id": ["issued"]})
        self.assertEqual(self.secrets, {})

    def test_refresh_rotates_tokens_together_and_retains_scopes(self):
        providers.update_account("chatgpt", {"credential_saved": True, "client_id": "issued", "subject": "subject"})
        providers.set_secret("chatgpt", {"client_id": "issued", "subject": "subject", "access_token": "old", "refresh_token": "old-refresh", "id_token": "old-id", "scopes": oauth.SCOPES.split(), "expires_at": 0})
        response = self.tokens()
        response.pop("scope")
        response.pop("id_token")
        with patch.object(oauth, "_json_request", return_value=response) as refresh:
            self.assertEqual(oauth.access_token("chatgpt"), "new-access")
        self.assertEqual(refresh.call_args.args[1]["client_id"], "issued")
        self.assertNotIn("scope", refresh.call_args.args[1])
        saved = providers.get_secret("chatgpt")
        self.assertEqual(saved["refresh_token"], "new-refresh")
        self.assertEqual(saved["id_token"], "old-id")

    def test_revoked_refresh_does_not_replace_saved_registration(self):
        providers.update_account("chatgpt", {"credential_saved": True, "client_id": "issued", "subject": "subject"})
        saved = {"client_id": "issued", "subject": "subject", "access_token": "expired", "refresh_token": "revoked", "scopes": oauth.SCOPES.split(), "expires_at": 0}
        providers.set_secret("chatgpt", saved)
        with patch.object(oauth, "_json_request", side_effect=providers.ProviderError("authentication", "Bitte erneut anmelden.")), self.assertRaises(providers.ProviderError) as error:
            oauth.access_token("chatgpt")
        self.assertEqual(error.exception.category, "authentication")
        self.assertEqual(providers.get_secret("chatgpt"), saved)
        self.assertEqual(providers.get_profile("chatgpt")["client_id"], "issued")

    def test_start_uses_pkce_stable_host_and_a_loopback_callback(self):
        class Server:
            server_port = 54321
            def __init__(self, address, handler):
                self.address = address
            def serve_forever(self): pass
            def shutdown(self): pass
            def server_close(self): pass
        with patch.object(oauth, "LoopbackServer", Server):
            first = oauth.start("chatgpt")
            query = parse_qs(urlsplit(first["authorization_url"]).query)
            attempt = oauth.ATTEMPTS["chatgpt"]
            expected = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest()).decode().rstrip("=")
            self.assertEqual(query["code_challenge"], [expected])
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertEqual(query["client_id"], ["dynamic_agent_client"])
            self.assertEqual(query["redirect_uri"], ["http://127.0.0.1:54321/auth/callback"])
            host = query["ext_agent_host_id"]
            second = parse_qs(urlsplit(oauth.start("chatgpt")["authorization_url"]).query)
            self.assertEqual(second["ext_agent_host_id"], host)
            self.assertNotEqual(second["state"], query["state"])
            self.assertEqual(attempt.status, "replaced")
            oauth.disconnect("chatgpt")

    def test_disconnect_revokes_and_clears_credentials_but_keeps_registration(self):
        import httpx
        providers.update_account("chatgpt", {"credential_saved": True, "client_id": "issued", "subject": "subject"})
        providers.set_secret("chatgpt", {"refresh_token": "refresh", "client_id": "issued"})
        def handler(request):
            self.assertEqual(str(request.url), "https://auth.openai.com/revoke")
            self.assertIn(b"token=refresh", request.content)
            return httpx.Response(200)
        self.transport(handler)
        with patch.object(oauth, "_json_request", return_value={"revocation_endpoint": "https://auth.openai.com/revoke"}):
            result = oauth.disconnect("chatgpt")
        self.assertTrue(result["revocation_confirmed"])
        self.assertEqual(providers.get_secret("chatgpt"), {})
        self.assertEqual(providers.get_profile("chatgpt")["client_id"], "issued")
        self.assertFalse(providers.get_profile("chatgpt")["credential_saved"])

    def test_unconfirmed_remote_revocation_is_reported(self):
        providers.update_account("chatgpt", {"credential_saved": True, "client_id": "issued"})
        providers.set_secret("chatgpt", {"refresh_token": "refresh", "client_id": "issued"})
        with patch.object(oauth, "_json_request", side_effect=providers.ProviderError("network", "network")), patch.object(oauth.time, "sleep"):
            result = oauth.disconnect("chatgpt")
        self.assertFalse(result["revocation_confirmed"])
        self.assertEqual(providers.get_secret("chatgpt"), {})

    def test_real_jwt_signature_nonce_identity_and_expiry_checks(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk["kid"] = "test-key"
        claims = {"sub": "subject", "iss": oauth.ISSUER, "aud": "issued", "iat": int(time.time()), "exp": int(time.time()) + 60, "nonce": "nonce"}
        def encoded(**changes):
            return jwt.encode({**claims, **changes}, key, algorithm="RS256", headers={"kid": "test-key"})
        with patch.object(oauth, "_json_request", return_value={"keys": [jwk]}):
            self.assertEqual(oauth.validate_id_token(encoded(), "issued", "nonce")["sub"], "subject")
            for token, client, nonce, subject in ((encoded(), "issued", "wrong", ""), (encoded(), "other", "nonce", ""), (encoded(exp=0), "issued", "nonce", ""), (encoded(), "issued", "nonce", "other"), (encoded(iss="https://untrusted.example"), "issued", "nonce", "")):
                with self.subTest(client=client, nonce=nonce, subject=subject), self.assertRaises(providers.ProviderError):
                    oauth.validate_id_token(token, client, nonce, subject)
            attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            forged = jwt.encode(claims, attacker_key, algorithm="RS256", headers={"kid": "test-key"})
            with self.assertRaises(providers.ProviderError):
                oauth.validate_id_token(forged, "issued", "nonce")


if __name__ == "__main__":
    unittest.main()
