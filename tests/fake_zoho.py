"""A fake Zoho Accounts for the sign-in tests: discovery, keys and a token endpoint, served through an httpx transport."""

# Standard library imports
import time
from typing import Any

# Third party imports
import httpx
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

_KID = "zoho-1"


class FakeZoho:
  """Answers as Zoho would, with an id_token built from `claims` over a valid default (a None value drops a claim)."""

  ISSUER = "https://accounts.zoho.test"

  def __init__(self, client_id: str = "page") -> None:
    self.client_id = client_id
    self.key = RSAKey.generate_key(2048, parameters={"kid": _KID})
    self.nonce: str | None = None  # the test copies it from the authorize URL, as Zoho would
    self.claims: dict[str, Any] = {}
    self.sign_with_other_key = False
    self.send_id_token = True
    self.transport = httpx.MockTransport(self._answer)

  def id_token(self) -> str:
    now = int(time.time())
    claims = {
      "iss": self.ISSUER,
      "aud": self.client_id,
      "sub": "1000",
      "iat": now,
      "exp": now + 3600,
      "nonce": self.nonce,
      "email": "ann@example.com",
      "email_verified": True,
      **self.claims,
    }
    key = RSAKey.generate_key(2048, parameters={"kid": _KID}) if self.sign_with_other_key else self.key
    return jwt.encode({"alg": "RS256", "kid": _KID}, {k: v for k, v in claims.items() if v is not None}, key)

  def _answer(self, request: httpx.Request) -> httpx.Response:
    match request.url.path:
      case "/.well-known/openid-configuration":
        return httpx.Response(
          200,
          json={
            "issuer": self.ISSUER,
            "authorization_endpoint": f"{self.ISSUER}/oauth/v2/auth",
            "token_endpoint": f"{self.ISSUER}/oauth/v2/token",
            "jwks_uri": f"{self.ISSUER}/oauth/v2/keys",
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
          },
        )
      case "/oauth/v2/keys":
        return httpx.Response(200, json=KeySet([self.key]).as_dict(private=False))
      case "/oauth/v2/token":
        token: dict[str, Any] = {"access_token": "access", "token_type": "Bearer", "expires_in": 3600}
        if self.send_id_token:
          token["id_token"] = self.id_token()
        return httpx.Response(200, json=token)
      case _:
        return httpx.Response(404)
