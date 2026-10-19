import json
import proxies
import sys
import os
import db
import random
import requests
from requests.exceptions import HTTPError

# Add the parent directory to sys.path to import logger
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logger import get_logger
from pyVintedVN.settings import Urls

# Get logger for this module
logger = get_logger(__name__)


class Requester:
    """
    A class for handling HTTP requests to Vinted.

    This class manages session headers, cookies, and provides methods for making
    HTTP requests with retry logic for handling authentication issues.

    Since September 2026 the catalogue lives on api.vinted.<tld>/svc-catalogue and
    requires a bearer token. The token is the `access_token_web` cookie handed out
    by the www host on any plain request, so no account or API key is needed.
    """

    def __init__(self, debug=False):
        """
        Initialize the Requester with default headers and session.

        Sets up the request headers with a randomly selected User-Agent,
        initializes the session, and configures default settings.

        Args:
            debug (bool, optional): Whether to print debug messages. Defaults to False.
        """

        # Add the parent directory to sys.path to import db
        sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import db

        self.locale = "www.vinted.fr"
        self.VINTED_AUTH_URL = f"https://{self.locale}/"
        self.MAX_RETRIES = 3
        self._iso_locales = {}
        self.session = requests.Session()
        self.debug = debug
        self._refresh_headers()

        if self.debug:
            logger.debug(f"Using User-Agent: {self.HEADER['User-Agent']}")

    def _refresh_headers(self):
        """
        Rebuild the session headers, picking a fresh User-Agent from the database.

        No explicit Host header is set: requests derives it from the URL, which
        matters now that auth (www.vinted.<tld>) and data (api.vinted.<tld>) live
        on two different hosts.
        """
        user_agents_json = db.get_parameter("user_agents")
        default_headers_json = db.get_parameter("default_headers")

        # Parse JSON strings
        user_agents = json.loads(user_agents_json) if user_agents_json else []
        default_headers = (
            json.loads(default_headers_json) if default_headers_json else {}
        )

        self.HEADER = {
            # Grabs a user agent from the database
            "User-Agent": random.choice(user_agents) if user_agents else "Mozilla/5.0",
            **(default_headers or {}),
        }
        self.session.headers.update(self.HEADER)

    def set_locale(self, locale):
        """
        Set the locale of the requester.

        Updates the authentication URL and headers to use the specified locale.

        Args:
            locale (str): The locale domain to use (e.g., 'www.vinted.fr', 'www.vinted.de')
        """
        self.locale = locale
        self.VINTED_AUTH_URL = f"https://{locale}/"
        self._refresh_headers()
        if self.debug:
            logger.debug(
                f"Locale set to {locale} with User-Agent: {self.HEADER['User-Agent']}"
            )

    def get_api_host(self):
        """
        Return the API host matching the current locale.

        'www.vinted.nl' becomes 'api.vinted.nl'. Locales that do not carry the
        'www.' prefix simply get 'api.' prepended.

        Returns:
            str: The host serving the catalogue API.
        """
        locale = self.locale
        if locale.startswith(Urls.VINTED_AUTH_HOST_PREFIX):
            locale = locale[len(Urls.VINTED_AUTH_HOST_PREFIX) :]
        return f"{Urls.VINTED_API_HOST_PREFIX}{locale}"

    def get_iso_locale(self):
        """
        Return the ISO locale (e.g. 'de-AT') of the current www host.

        svc-catalogue picks the marketplace from the `Locale` request header, not
        from the api.vinted.<tld> host or the bearer token: without it every
        domain answers with the same catalogue, so a vinted.at query silently
        returns vinted.hu listings. The value is the `anonymous-iso-locale`
        cookie the www host hands out; it is fetched once per host with a
        throwaway request so the session jar never holds two conflicting
        `access_token_web` cookies.

        Returns:
            str or None: The ISO locale, or None if the host did not provide one.
        """
        if self.locale not in self._iso_locales:
            iso = None
            try:
                response = requests.head(
                    self.VINTED_AUTH_URL,
                    headers={"User-Agent": self.HEADER["User-Agent"]},
                    proxies=self.session.proxies,
                    timeout=15,
                )
                iso = response.cookies.get("anonymous-iso-locale")
            except Exception:
                logger.warning(
                    f"Could not fetch the ISO locale of {self.VINTED_AUTH_URL}",
                    exc_info=True,
                )
            if not iso:
                # Don't cache a failure, so the next search retries.
                return None
            self._iso_locales[self.locale] = iso
        return self._iso_locales[self.locale]

    def _auth_headers(self):
        """
        Build the per-request authentication headers for the catalogue API.

        Returns:
            dict: Authorization and anonymous-id headers, plus a Referer the API expects.
        """
        headers = {
            "Accept": "application/json",
            "Referer": self.VINTED_AUTH_URL,
            "Origin": self.VINTED_AUTH_URL.rstrip("/"),
        }
        token = self.session.cookies.get("access_token_web")
        anon_id = self.session.cookies.get("anon_id")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if anon_id:
            headers["x-anon-id"] = anon_id
        iso_locale = self.get_iso_locale()
        if iso_locale:
            headers["Locale"] = iso_locale
        return headers

    def get_page(self, url):
        """
        Fetch a plain web page (not the API) with the session's proxy and cookies.

        Unlike get(), no API bearer token is sent and no retry is done, so a
        failing page is just reported to the caller.

        Args:
            url (str): The URL of the page

        Returns:
            requests.Response: The response object
        """
        proxies.configure_proxy(self.session)
        return self.session.get(url, timeout=15)

    def get(self, url, params=None):
        """
        Make a GET request with retry logic.

        If the API rejects the bearer token (401/403), the cookies are refreshed
        and the request is retried up to MAX_RETRIES times.

        Args:
            url (str): The URL to request
            params (dict, optional): Query parameters for the request

        Returns:
            requests.Response: The response object if successful

        Raises:
            HTTPError: If the request fails after all retries
        """

        # Set a random proxy for this request
        proxy_configured = proxies.configure_proxy(self.session)
        if self.debug and proxy_configured:
            logger.debug(f"Using proxy: {self.session.proxies}")

        # The API is token-gated, so make sure we hold one before the first call.
        if not self.session.cookies.get("access_token_web"):
            self.set_cookies()

        tried = 0
        new_session = False
        while tried < self.MAX_RETRIES:
            tried += 1
            with self.session.get(
                url, params=params, headers=self._auth_headers()
            ) as response:
                if response.status_code == 200:
                    return response
                elif response.status_code in (401, 403) and tried < self.MAX_RETRIES:
                    logger.warning(
                        f"Token rejected ({response.status_code}), refreshing {tried}/{self.MAX_RETRIES}"
                    )
                    self.set_cookies()
                elif tried == self.MAX_RETRIES:
                    # If we've reached max retries, return the last response
                    # even if it's not a 200 status code

                    # New try : if we still get a 401 or 403, we reset the session
                    if response.status_code in (401, 403) and not new_session:
                        # Log the error details, including headers and body snippet
                        logger.error(
                            f"Received {response.status_code} error for URL: {url}\n"
                            f"Response headers: {dict(response.headers)}\n"
                            f"Response body (first 500 chars): {response.text[:500]}"
                        )

                        new_session = True
                        self.session = requests.Session()
                        self._refresh_headers()
                        # proxy
                        proxy_configured = proxies.configure_proxy(self.session)
                        self.set_cookies()
                        if self.debug:
                            logger.debug(
                                f"Session reset due to {response.status_code} error"
                            )
                        tried = 0
                        continue
                    return response

        # This should only happen if the loop exits without returning
        raise HTTPError(
            f"Failed to get a valid response after {self.MAX_RETRIES} attempts"
        )

    def post(self, url, params=None):
        """
        Make a POST request.

        Args:
            url (str): The URL to request
            params (dict, optional): Parameters for the request

        Returns:
            requests.Response: The response object if successful

        Raises:
            HTTPError: If the request fails
        """
        # Set a random proxy for this request
        proxy_configured = proxies.configure_proxy(self.session)
        if self.debug and proxy_configured:
            logger.debug(f"Using proxy: {self.session.proxies}")

        response = self.session.post(url, params)
        response.raise_for_status()
        return response

    def set_cookies(self):
        """
        Reset and fetch new cookies for authentication.

        Clears the current session cookies and makes a HEAD request to the Vinted
        www host, which hands back both `access_token_web` (the API bearer token)
        and `anon_id`. HEAD is enough and avoids downloading the ~2MB homepage.
        """
        self.session.cookies.clear_session_cookies()
        try:
            self.session.head(self.VINTED_AUTH_URL)
            if not self.session.cookies.get("access_token_web"):
                logger.warning(
                    f"No access_token_web cookie returned by {self.VINTED_AUTH_URL}"
                )
            elif self.debug:
                logger.debug("Cookies set!")
        except Exception:
            if self.debug:
                logger.error(
                    "There was an error fetching cookies for vinted", exc_info=True
                )

    def update_cookies(self, cookies: dict):
        """
        Update the session cookies with the provided dictionary.

        Args:
            cookies (dict): Dictionary of cookies to update
        """
        self.session.cookies.update(cookies)
        if self.debug:
            logger.debug(f"Cookies manually updated ({len(cookies)} cookies received)")

    # Alias for backward compatibility
    setLocale = set_locale
    setCookies = set_cookies


# Singleton instance of the Requester class
requester = Requester()
