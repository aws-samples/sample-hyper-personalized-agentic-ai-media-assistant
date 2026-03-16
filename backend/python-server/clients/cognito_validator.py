#!/usr/bin/env python3
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
# and limitations under the License.
#
import logging
import json
import base64
import time
import httpx
from jose import jwt, jwk
from jose.exceptions import JWTError, ExpiredSignatureError


# Configure logging
logger = logging.getLogger(__name__)


class CognitoTokenValidator:
    def __init__(self, user_pool_id, region, client_id=None):
        self.user_pool_id = user_pool_id
        self.region = region
        self.client_id = client_id  # Optional: validate aud/client_id claim
        self.jwks = None
        self.jwks_url = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json"
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"

    async def _fetch_jwks(self):
        """Fetch JWKS from Cognito if not cached."""
        if self.jwks is None:
            async with httpx.AsyncClient() as client:
                response = await client.get(self.jwks_url)
                self.jwks = response.json()["keys"]
        return self.jwks

    async def validate_token(self, token: str) -> bool:
        try:
            # 1. Decode header to get kid
            header = json.loads(base64.b64decode(token.split(".")[0] + "=="))
            kid = header["kid"]

            # 2. Find matching key in JWKS
            jwks = await self._fetch_jwks()
            key = next((k for k in jwks if k["kid"] == kid), None)
            if not key:
                logger.error("No matching key found in JWKS")
                return False

            # 3. Verify and decode the token
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"verify_aud": False}  # Access tokens don't have aud
            )

            # 4. Validate token_use claim (must be "access" for access tokens)
            if claims.get("token_use") != "access":
                logger.error("Invalid token_use claim")
                return False

            # 5. Optionally validate client_id
            if self.client_id and claims.get("client_id") != self.client_id:
                logger.error("Invalid client_id claim")
                return False

            return True

        except ExpiredSignatureError as e:
            logger.error("Token has expired: {e}")
            return False
        except JWTError as e:
            logger.error(f"JWT validation error: {e}")
            return False
        except Exception as e:
            logger.error(f"Token validation error: {e}")
            return False