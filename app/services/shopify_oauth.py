import hmac
import hashlib
import httpx
import logging
from urllib.parse import urlencode
from typing import Dict, Optional
from app.config import settings
from app.utils.helpers import sanitize_shop_domain

logger = logging.getLogger(__name__)


class ShopifyOAuth:
    def __init__(self):
        self.api_key = settings.SHOPIFY_API_KEY
        self.api_secret = settings.SHOPIFY_API_SECRET
        self.api_version = settings.SHOPIFY_API_VERSION
        self.scopes = settings.SHOPIFY_SCOPES
        self.redirect_url = settings.OAUTH_REDIRECT_URL

    def get_authorization_url(self, shop_domain: str, state: Optional[str] = None) -> str:
        """
        Generate the OAuth authorization URL for a Shopify store

        Args:
            shop_domain: The shop's domain (e.g., mystore.myshopify.com)
            state: Optional state parameter for CSRF protection

        Returns:
            Authorization URL
        """
        shop_domain = sanitize_shop_domain(shop_domain)

        params = {
            "client_id": self.api_key,
            "scope": self.scopes,
            "redirect_uri": self.redirect_url,
        }

        if state:
            params["state"] = state

        base_url = f"https://{shop_domain}/admin/oauth/authorize"
        return f"{base_url}?{urlencode(params)}"

    def verify_hmac(self, params: Dict[str, str]) -> bool:
        """
        Verify the HMAC signature from Shopify callback

        Args:
            params: Query parameters from the OAuth callback

        Returns:
            True if HMAC is valid, False otherwise
        """
        if "hmac" not in params:
            logger.warning("[HMAC] No HMAC parameter found in request")
            return False

        hmac_to_verify = params["hmac"]

        # Create a copy without the hmac parameter
        params_copy = params.copy()
        params_copy.pop("hmac", None)

        # Sort parameters — Shopify requires raw (NOT URL-encoded) key=value pairs
        encoded_params = "&".join(
            f"{key}={value}"
            for key, value in sorted(params_copy.items())
        )

        logger.debug(f"[HMAC] Encoded params for verification: {encoded_params[:100]}...")
        logger.debug(f"[HMAC] Received HMAC: {hmac_to_verify[:10]}...")

        # Calculate HMAC
        computed_hmac = hmac.new(
            self.api_secret.encode("utf-8"),
            encoded_params.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        logger.debug(f"[HMAC] Computed HMAC: {computed_hmac[:10]}...")

        is_valid = hmac.compare_digest(computed_hmac, hmac_to_verify)
        logger.info(f"[HMAC] Verification result: {'VALID' if is_valid else 'INVALID'}")

        return is_valid

    async def exchange_code_for_token(self, shop_domain: str, code: str) -> Dict:
        """
        Exchange authorization code for access token

        Args:
            shop_domain: The shop's domain
            code: Authorization code from OAuth callback

        Returns:
            Dictionary containing access_token and scope
        """
        shop_domain = sanitize_shop_domain(shop_domain)

        url = f"https://{shop_domain}/admin/oauth/access_token"

        payload = {
            "client_id": self.api_key,
            "client_secret": self.api_secret,
            "code": code
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()

    async def get_access_scopes(self, shop_domain: str, access_token: str) -> str:
        """
        Return the scopes actually granted to this access token, as a
        comma-separated string (e.g. "read_products,read_orders").

        Works for both OAuth and Custom App tokens via the standard
        /admin/oauth/access_scopes.json endpoint. Used so we store the
        merchant's real granted scopes rather than a hardcoded assumption —
        the scope gate (utils.helpers.has_scope) depends on this being accurate.
        """
        shop_domain = sanitize_shop_domain(shop_domain)
        url = f"https://{shop_domain}/admin/oauth/access_scopes.json"
        headers = {"X-Shopify-Access-Token": access_token}

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
        handles = [s.get("handle") for s in data.get("access_scopes", []) if s.get("handle")]
        return ",".join(handles)

    async def get_shop_info(self, shop_domain: str, access_token: str) -> Dict:
        """
        Get shop information using the access token

        Args:
            shop_domain: The shop's domain
            access_token: OAuth access token

        Returns:
            Shop information
        """
        shop_domain = sanitize_shop_domain(shop_domain)

        url = f"https://{shop_domain}/admin/api/{self.api_version}/shop.json"

        headers = {
            "X-Shopify-Access-Token": access_token
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()

    async def get_shop_info_graphql(self, shop_domain: str, access_token: str) -> Dict:
        """
        Get shop information via Admin GraphQL API.
        Returns id, name, myshopifyDomain, primaryDomain, and all domains.
        Preferred over REST for post-OAuth shop identity resolution.

        Args:
            shop_domain: The shop's domain
            access_token: OAuth access token

        Returns:
            Dict with shop fields: name, myshopifyDomain, primaryDomain, domains
        """
        shop_domain = sanitize_shop_domain(shop_domain)
        url = f"https://{shop_domain}/admin/api/{self.api_version}/graphql.json"
        query = """
        {
          shop {
            id
            name
            myshopifyDomain
            primaryDomain {
              host
              sslEnabled
            }
            domains {
              host
            }
          }
        }
        """
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json={"query": query})
            response.raise_for_status()
            data = response.json()
            return data.get("data", {}).get("shop", {})

    async def _write_shop_metafield(
        self,
        shop_domain: str,
        access_token: str,
        key: str,
        value: str,
    ) -> None:
        url = f"https://{shop_domain}/admin/api/{self.api_version}/metafields.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }
        payload = {
            "metafield": {
                "namespace": "chekout_ai",
                "key": key,
                "value": value,
                "type": "single_line_text_field",
                "owner_resource": "shop",
            }
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code not in (200, 201):
                logger.warning(
                    f"[Metafield] Failed to write {key} for {shop_domain}: "
                    f"{response.status_code} {response.text}"
                )
            else:
                logger.info(f"[Metafield] Wrote {key} for {shop_domain}")

    async def write_merchant_id_metafield(
        self,
        shop_domain: str,
        access_token: str,
        merchant_id: str,
        chatbot_src: str = "https://ai-builder.chekout.ai/chatbot.js",
    ) -> None:
        """
        Write merchant_id and chatbot_src as shop metafields so the Liquid
        theme extension can load the correct chatbot build for this environment.
        """
        shop_domain = sanitize_shop_domain(shop_domain)
        await self._write_shop_metafield(shop_domain, access_token, "merchant_id", merchant_id)
        await self._write_shop_metafield(shop_domain, access_token, "chatbot_src", chatbot_src)

    async def make_shopify_request(
        self,
        shop_domain: str,
        access_token: str,
        endpoint: str,
        method: str = "GET",
        data: Optional[Dict] = None
    ) -> Dict:
        """
        Make an authenticated request to Shopify API

        Args:
            shop_domain: The shop's domain
            access_token: OAuth access token
            endpoint: API endpoint (e.g., '/products.json')
            method: HTTP method (GET, POST, PUT, DELETE)
            data: Optional request body for POST/PUT requests

        Returns:
            API response
        """
        shop_domain = sanitize_shop_domain(shop_domain)

        url = f"https://{shop_domain}/admin/api/{self.api_version}{endpoint}"

        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            if method.upper() == "GET":
                response = await client.get(url, headers=headers)
            elif method.upper() == "POST":
                response = await client.post(url, headers=headers, json=data)
            elif method.upper() == "PUT":
                response = await client.put(url, headers=headers, json=data)
            elif method.upper() == "DELETE":
                response = await client.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status()
            return response.json()
