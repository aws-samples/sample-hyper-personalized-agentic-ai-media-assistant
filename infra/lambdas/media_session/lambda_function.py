"""GET /media/session (Cognito): CloudFront signed cookies for /media/*.

The app's CloudFront distribution serves the live-sports HLS streams from a
private bucket under /media/*, and only answers requests carrying valid signed
cookies. This route hands a signed-in viewer those cookies. The frontend is on
the same distribution, so the browser stores them for its own origin.

The signing key is owner-managed (infra/deployment/media_signing_key.py): a
Secrets Manager secret holding {"keyPairId", "privateKey"} (PKCS#1 PEM).

RSA PKCS#1 v1.5 / SHA-256 signing uses only the standard library, keeping the
"no third-party dependencies" rule the other Lambdas follow.
"""
import base64
import hashlib
import json
import os
import time

import boto3

from ibc_common import respond

SECRET_NAME = os.environ.get("MEDIA_KEY_SECRET", "")
COOKIE_TTL_S = int(os.environ.get("MEDIA_COOKIE_TTL_S", "14400"))  # 4 hours
# Any host: the key group is trusted only by this deployment's distribution.
RESOURCE = "https://*/media/*"

# DER prefix of DigestInfo for SHA-256 (RFC 8017 §9.2, note 1).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_key = None


def _der_ints(der):
    """Integers of a DER SEQUENCE (enough for an RSAPrivateKey)."""
    def length(i):
        first = der[i]
        if first < 0x80:
            return first, i + 1
        n = first & 0x7F
        return int.from_bytes(der[i + 1:i + 1 + n], "big"), i + 1 + n
    if der[0] != 0x30:
        raise ValueError("expected a DER SEQUENCE")
    _, i = length(1)
    out = []
    while i < len(der):
        if der[i] != 0x02:
            raise ValueError("expected a DER INTEGER")
        size, i = length(i + 1)
        out.append(int.from_bytes(der[i:i + size], "big"))
        i += size
    return out


def load_private_key(pem):
    """(n, d) from a PKCS#1 'RSA PRIVATE KEY' PEM."""
    if "BEGIN RSA PRIVATE KEY" not in pem:
        raise ValueError("expected a PKCS#1 RSA PRIVATE KEY")
    body = "".join(line for line in pem.strip().splitlines() if not line.startswith("-----"))
    ints = _der_ints(base64.b64decode(body))
    # RSAPrivateKey: version, n, e, d, p, q, dp, dq, qinv
    return ints[1], ints[3]


def rsa_sha256_sign(message, n, d):
    """RSASSA-PKCS1-v1_5 signature with SHA-256 (RFC 8017 §8.2.1)."""
    k = (n.bit_length() + 7) // 8
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def _cf_b64(data):
    """CloudFront's URL-safe base64: + -> -, = -> _, / -> ~."""
    return base64.b64encode(data).decode().translate(str.maketrans("+=/", "-_~"))


def signed_cookies(key_pair_id, n, d, expires):
    policy = json.dumps({"Statement": [{"Resource": RESOURCE,
        "Condition": {"DateLessThan": {"AWS:EpochTime": expires}}}]}, separators=(",", ":")).encode()
    return {
        "CloudFront-Policy": _cf_b64(policy),
        "CloudFront-Signature": _cf_b64(rsa_sha256_sign(policy, n, d)),
        "CloudFront-Key-Pair-Id": key_pair_id,
        "CloudFront-Hash-Algorithm": "SHA256",
    }


def _signing_key():
    """Cached for 15 minutes, so a rotated key is picked up without a cold start."""
    global _key
    if _key is None or time.time() - _key[0] > 900:
        secret = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_NAME)["SecretString"])
        _key = (time.time(), secret["keyPairId"], *load_private_key(secret["privateKey"]))
    return _key[1:]


def lambda_handler(event, context):
    if event.get("httpMethod") != "GET":
        return respond(405, {"error": "method not allowed"})
    key_pair_id, n, d = _signing_key()
    expires = int(time.time()) + COOKIE_TTL_S
    return respond(200, {"cookies": signed_cookies(key_pair_id, n, d, expires),
                         "expires": expires, "path": "/media/"})
