"""Multi-format Google cookie parsing, secure 0600 storage, and session verification."""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

ESSENTIAL_GOOGLE_COOKIES = frozenset(
    {
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
    }
)

ALLOWED_DOMAIN_SUFFIXES = (
    "google.com",
    "googleusercontent.com",
    "googleapis.com",
    "gstatic.com",
    "localhost",
    "127.0.0.1",
)


class CookieParseError(ValueError):
    """Raised when imported cookie text cannot be parsed into valid cookies."""


@dataclass(frozen=True)
class StoredCookie:
    """Normalized cookie compatible with both httpx and Playwright."""

    name: str
    value: str
    domain: str = ".google.com"
    path: str = "/"
    secure: bool = True
    httpOnly: bool = False
    expires: float | None = None
    sameSite: str | None = None

    def to_playwright(self, target_url: str | None = None) -> dict[str, Any]:
        """Convert to Playwright context.add_cookies() dictionary."""
        domain = self.domain
        if target_url:
            parsed = urlparse(target_url)
            host = parsed.hostname or ""
            if host in ("localhost", "127.0.0.1") or host.endswith(".local"):
                return {
                    "name": self.name,
                    "value": self.value,
                    "url": f"{parsed.scheme}://{parsed.netloc}",
                }

        # Chromium requires __Host- cookies to omit domain (or use url) with path=/ and secure=True
        if self.name.startswith("__Host-"):
            clean_host = domain.lstrip(".") or "play.google.com"
            return {
                "name": self.name,
                "value": self.value,
                "url": f"https://{clean_host}",
                "secure": True,
                "httpOnly": self.httpOnly,
            }

        item: dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "domain": domain,
            "path": self.path or "/",
            "secure": True if self.name.startswith("__Secure-") else self.secure,
            "httpOnly": self.httpOnly,
        }
        if self.expires is not None and self.expires > 0:
            item["expires"] = float(self.expires)
        if self.sameSite in ("Strict", "Lax", "None"):
            if self.sameSite == "None" and not item["secure"]:
                pass
            else:
                item["sameSite"] = self.sameSite
        return item


def _normalize_same_site(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    lowered = raw.strip().lower()
    if lowered == "strict":
        return "Strict"
    if lowered == "lax":
        return "Lax"
    if lowered in ("none", "no_restriction"):
        return "None"
    return None


def _is_relevant_domain(domain: str) -> bool:
    clean = domain.strip().lstrip(".").lower()
    if not clean:
        return True
    return any(
        clean == suffix or clean.endswith(f".{suffix}") or clean.startswith("google.")
        for suffix in ALLOWED_DOMAIN_SUFFIXES
    )


def parse_cookies_input(raw_text: str, default_domain: str = ".google.com") -> list[StoredCookie]:
    """Parse cookies from Netscape cookies.txt, JSON, cURL command, or raw Cookie header."""
    text = raw_text.strip()
    if not text:
        raise CookieParseError("Cookie input is empty.")

    # 1. Try JSON (Cookie-Editor array, Playwright export, or {name: value} map)
    if text[0] in ("[", "{"):
        cookies = _parse_json_cookies(text, default_domain=default_domain)
        if cookies:
            return _deduplicate_cookies(cookies)

    # 2. Try Netscape cookies.txt format (tab-separated, including #HttpOnly_ lines)
    if "\t" in text or "# Netscape HTTP Cookie File" in text or "# HTTP Cookie File" in text:
        cookies = _parse_netscape_cookies(text)
        if cookies:
            return _deduplicate_cookies(cookies)

    # 3. Try cURL command ("curl 'https://play.google.com/...' -H 'cookie: ...' -b '...'")
    if text.lstrip().startswith("curl "):
        cookies = _parse_curl_cookies(text, default_domain=default_domain)
        if cookies:
            return _deduplicate_cookies(cookies)

    # 4. Try raw Cookie: header or semicolon-delimited name=value pairs
    cookies = _parse_cookie_header(text, default_domain=default_domain)
    if cookies:
        return _deduplicate_cookies(cookies)

    raise CookieParseError(
        "Unrecognized cookie format. Provide Netscape cookies.txt, Cookie-Editor JSON, "
        "a DevTools 'Copy as cURL' snippet, or a raw Cookie header."
    )


def _parse_json_cookies(text: str, default_domain: str) -> list[StoredCookie]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CookieParseError(f"Invalid JSON cookie input: {exc}") from exc

    if isinstance(payload, dict):
        if isinstance(payload.get("cookies"), list):
            payload = payload["cookies"]
        else:
            # Simple {"SID": "...", "HSID": "..."} dictionary
            out: list[StoredCookie] = []
            for k, v in payload.items():
                if isinstance(k, str) and isinstance(v, str) and k.strip():
                    name = k.strip()
                    out.append(
                        StoredCookie(
                            name=name,
                            value=v,
                            domain=default_domain,
                            secure=True,
                        )
                    )
            return out

    if not isinstance(payload, list):
        raise CookieParseError("JSON cookie payload must be a list or object.")

    cookies: list[StoredCookie] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name or "value" not in entry:
            continue
        value = str(entry.get("value", ""))
        domain = str(entry.get("domain") or default_domain).strip()
        if not _is_relevant_domain(domain):
            continue
        path = str(entry.get("path") or "/").strip() or "/"
        secure = bool(entry.get("secure", True))
        if name.startswith(("__Secure-", "__Host-")):
            secure = True
        http_only = bool(entry.get("httpOnly", False))
        raw_exp = entry.get("expirationDate", entry.get("expires"))
        expires: float | None = None
        if isinstance(raw_exp, (int, float)) and raw_exp > 0:
            expires = float(raw_exp)
        same_site = _normalize_same_site(entry.get("sameSite"))
        cookies.append(
            StoredCookie(
                name=name,
                value=value,
                domain=domain,
                path=path,
                secure=secure,
                httpOnly=http_only,
                expires=expires,
                sameSite=same_site,
            )
        )
    if not cookies:
        raise CookieParseError("No Google cookies found in JSON payload.")
    return cookies


def _parse_netscape_cookies(text: str) -> list[StoredCookie]:
    cookies: list[StoredCookie] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _include_sub, path, secure_flag, expiry_str, name, value = (
            parts[0].strip(),
            parts[1].strip(),
            parts[2].strip() or "/",
            parts[3].strip().upper(),
            parts[4].strip(),
            parts[5].strip(),
            parts[6],
        )
        if not name or not _is_relevant_domain(domain):
            continue
        secure = secure_flag == "TRUE" or name.startswith(("__Secure-", "__Host-"))
        expires: float | None = None
        if expiry_str.isdigit() and int(expiry_str) > 0:
            expires = float(int(expiry_str))
        cookies.append(
            StoredCookie(
                name=name,
                value=value,
                domain=domain,
                path=path,
                secure=secure,
                httpOnly=http_only,
                expires=expires,
            )
        )
    return cookies


def _parse_curl_cookies(text: str, default_domain: str) -> list[StoredCookie]:
    # Normalize line continuations in multiline cURL commands
    normalized = text.replace("\\\r\n", " ").replace("\\\n", " ")
    cookie_headers: list[str] = []
    detected_domain = default_domain

    try:
        tokens = shlex.split(normalized)
    except ValueError:
        tokens = []

    if tokens:
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith(("http://", "https://")):
                host = urlparse(tok).hostname
                if host:
                    detected_domain = ".google.com" if host.endswith("google.com") else host
            elif tok in ("-H", "--header") and i + 1 < len(tokens):
                hdr = tokens[i + 1]
                if hdr.lower().startswith("cookie:"):
                    cookie_headers.append(hdr.split(":", 1)[1].strip())
                i += 1
            elif tok in ("-b", "--cookie") and i + 1 < len(tokens):
                cookie_headers.append(tokens[i + 1].strip())
                i += 1
            i += 1

    if not cookie_headers:
        for match in re.finditer(
            r"""(?:-H|--header)\s+['"]cookie:\s*([^'"]+)['"]""",
            normalized,
            flags=re.IGNORECASE,
        ):
            cookie_headers.append(match.group(1))
        for match in re.finditer(
            r"""(?:-b|--cookie)\s+['"]([^'"]+)['"]""",
            normalized,
            flags=re.IGNORECASE,
        ):
            cookie_headers.append(match.group(1))

    cookies: list[StoredCookie] = []
    for header in cookie_headers:
        cookies.extend(_parse_cookie_header(header, default_domain=detected_domain))
    return cookies


def _parse_cookie_header(text: str, default_domain: str) -> list[StoredCookie]:
    cleaned = text.strip()
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if "\n" in cleaned and ";" not in cleaned:
        return []

    cookies: list[StoredCookie] = []
    for chunk in cleaned.split(";"):
        pair = chunk.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or any(ch.isspace() for ch in name):
            continue
        # Reject common cookie attributes if accidentally pasted from Set-Cookie
        if name.lower() in ("path", "domain", "expires", "max-age", "samesite", "secure", "httponly"):
            continue
        cookies.append(
            StoredCookie(
                name=name,
                value=value,
                domain=default_domain,
                path="/",
                secure=True,
            )
        )
    return cookies


def _deduplicate_cookies(cookies: list[StoredCookie]) -> list[StoredCookie]:
    deduped: dict[tuple[str, str, str], StoredCookie] = {}
    for cookie in cookies:
        deduped[(cookie.domain, cookie.path, cookie.name)] = cookie
    return list(deduped.values())


class CookieStore:
    """Manages persistent 0600 storage and verification of Google Play Books cookies."""

    def __init__(self, cookies_path: Path) -> None:
        self.cookies_path = cookies_path

    def save(self, cookies: list[StoredCookie]) -> None:
        self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "cookies": [asdict(c) for c in cookies],
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.cookies_path.parent),
            prefix=".cookies-",
            suffix=".tmp",
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp_name, self.cookies_path)
            os.chmod(self.cookies_path, 0o600)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def load(self) -> tuple[list[StoredCookie], str | None]:
        if not self.cookies_path.exists():
            return [], None
        data = json.loads(self.cookies_path.read_text(encoding="utf-8"))
        updated_at = data.get("updated_at")
        raw_list = data.get("cookies", [])
        cookies = [StoredCookie(**item) for item in raw_list if isinstance(item, dict)]
        return cookies, updated_at

    def clear(self) -> None:
        if self.cookies_path.exists():
            self.cookies_path.unlink()

    def build_httpx_cookies(self) -> httpx.Cookies:
        cookies, _ = self.load()
        jar = httpx.Cookies()
        for c in cookies:
            domain = c.domain.lstrip(".") if c.domain in ("localhost", "127.0.0.1") else c.domain
            jar.set(c.name, c.value, domain=domain, path=c.path or "/")
        return jar

    def build_cookie_header(self) -> str:
        """Build a deduplicated Cookie header prioritizing play.google.com and .google.com."""
        cookies, _ = self.load()
        by_name: dict[str, str] = {}
        # Load general cookies first, then more specific play.google.com overrides
        sorted_cookies = sorted(
            cookies,
            key=lambda c: (1 if "play.google.com" in c.domain else 0),
        )
        for c in sorted_cookies:
            by_name[c.name] = c.value
        return "; ".join(f"{k}={v}" for k, v in by_name.items())

    def redacted_summary(self) -> dict[str, Any]:
        cookies, updated_at = self.load()
        names = sorted({c.name for c in cookies})
        essential_present = sorted(ESSENTIAL_GOOGLE_COOKIES.intersection(names))
        return {
            "has_cookies": len(cookies) > 0,
            "cookie_count": len(cookies),
            "cookie_names": names,
            "essential_cookies_present": essential_present,
            "has_essential_google_cookies": len(essential_present) >= 2,
            "updated_at": updated_at,
        }


_GOOGLE_ACCOUNT_LABEL_RE = re.compile(
    r"Google Account:[^(]{0,160}\(([A-Za-z0-9_.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})\)"
)
_EMAIL_CANDIDATE_RE = re.compile(
    r"(?<![:/A-Za-z0-9_.+-])([A-Za-z0-9][A-Za-z0-9_.+-]{0,63}@"
    r"(?:gmail\.com|googlemail\.com|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}))\b"
)
_ASSET_TLD_DENYLIST = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "woff", "woff2"})


def _extract_account_email_hint(body: str) -> str:
    """Extract a real account email hint from Google Play Books HTML without matching CSS/font tokens."""
    label_match = _GOOGLE_ACCOUNT_LABEL_RE.search(body)
    if label_match:
        return label_match.group(1)

    for match in _EMAIL_CANDIDATE_RE.finditer(body):
        candidate = match.group(1)
        tld = candidate.rsplit(".", 1)[-1].lower()
        if tld in _ASSET_TLD_DENYLIST:
            continue
        return candidate
    return ""


async def verify_google_play_session(
    cookie_store: CookieStore,
    base_url: str,
    user_agent: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Probe Google Play Books library endpoint to verify session cookies are active."""
    cookies, _ = cookie_store.load()
    checked_at = datetime.now(timezone.utc).isoformat()
    if not cookies:
        return {
            "authenticated": False,
            "checked_at": checked_at,
            "account_hint": "",
            "message": "No session cookies imported yet.",
        }

    cookie_header = cookie_store.build_cookie_header()
    target_url = f"{base_url.rstrip('/')}/books"
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_header,
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            transport=transport,
        ) as client:
            response = await client.get(target_url, headers=headers)
    except httpx.HTTPError as exc:
        return {
            "authenticated": False,
            "checked_at": checked_at,
            "account_hint": "",
            "message": f"Unable to reach {target_url}: {exc}",
        }

    final_url = str(response.url)
    final_host = (urlparse(final_url).hostname or "").lower()
    body = response.text

    if (
        "accounts.google.com" in final_host
        or "/servicelogin" in final_url.lower()
        or "/signin" in final_url.lower()
        or response.status_code in (401, 403)
    ):
        return {
            "authenticated": False,
            "checked_at": checked_at,
            "account_hint": "",
            "message": "Session expired or rejected (redirected to Google Sign-In). Re-import fresh cookies.",
        }

    if response.status_code >= 400:
        return {
            "authenticated": False,
            "checked_at": checked_at,
            "account_hint": "",
            "message": f"Unexpected HTTP {response.status_code} from Google Play Books.",
        }

    # Check for unauthenticated sign-in gate vs authenticated session markers
    account_hint = _extract_account_email_hint(body)

    has_sign_in_gate = bool(
        re.search(r"accounts\.google\.com/ServiceLogin", body, re.IGNORECASE)
        and not account_hint
        and "data-Authenticated" not in body
        and "play-books-authenticated" not in body
        and "Sign out" not in body
    )
    if has_sign_in_gate:
        return {
            "authenticated": False,
            "checked_at": checked_at,
            "account_hint": "",
            "message": "Google Play Books returned an unauthenticated page. Verify cookies from play.google.com.",
        }

    msg = (
        f"Authenticated with Google Play Books ({account_hint})."
        if account_hint
        else "Authenticated session verified against Google Play Books."
    )
    return {
        "authenticated": True,
        "checked_at": checked_at,
        "account_hint": account_hint,
        "message": msg,
    }
