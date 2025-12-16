import hmac
import hashlib
import httpx
import logging
from urllib.parse import urlencode, quote
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

        # Sort and encode parameters (Shopify requires URL encoding of values)
        encoded_params = "&".join(
            f"{key}={quote(str(value), safe='')}"
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
