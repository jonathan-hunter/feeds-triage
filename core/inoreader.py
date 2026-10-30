"""Minimal Inoreader API client (OAuth 2.0) — stdlib only, Python 3.11+.

Usage:
    export INOREADER_CLIENT_ID=... INOREADER_CLIENT_SECRET=...
    export INOREADER_REDIRECT_URI=http://localhost:8765/callback   # must match app registration
    python -m core.inoreader auth          # one-off: browser consent, paste redirect URL back
    python -m core.inoreader test          # read-only connectivity test (~4 Zone 1 calls)
    python -m core.inoreader test --write  # also add+remove two probe tags per call on one item (2 Zone 2 + 3 Zone 1 calls)

Tokens are written to $INOREADER_TOKEN_FILE (default ~/.config/inoreader-llm/tokens.json, mode 0600).
Refresh tokens rotate on every refresh, so the file is rewritten atomically after each refresh.

Docs: https://www.inoreader.com/developers/oauth, /rate-limiting, /stream-contents, /edit-tag
Pro plan limits: 100 req/day Zone 1 (reads), 100 req/day Zone 2 (edit-tag etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

BASE = "https://www.inoreader.com"
API = f"{BASE}/reader/api/0"
AUTH_URL = f"{BASE}/oauth2/auth"
TOKEN_URL = f"{BASE}/oauth2/token"
READING_LIST = "user/-/state/com.google/reading-list"
READ = "user/-/state/com.google/read"
REFRESH_MARGIN_S = 300


def _env(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError:
        sys.exit(f"missing env var {name}")


def _token_path() -> Path:
    default = Path.home() / ".config" / "inoreader-llm" / "tokens.json"
    return Path(os.environ.get("INOREADER_TOKEN_FILE", default)).expanduser()


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""

    @classmethod
    def from_response(cls, data: dict) -> Tokens:
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + int(data["expires_in"]),
            scope=data.get("scope", ""),
        )

    @classmethod
    def load(cls, path: Path) -> Tokens:
        return cls(**json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(self), f, indent=2)
        tmp.replace(path)


@dataclass
class RateLimits:
    zone1_limit: int | None = None
    zone1_usage: int | None = None
    zone2_limit: int | None = None
    zone2_usage: int | None = None
    reset_after_s: float | None = None

    def update(self, headers) -> None:
        for attr, key, cast in (
            ("zone1_limit", "X-Reader-Zone1-Limit", int),
            ("zone1_usage", "X-Reader-Zone1-Usage", int),
            ("zone2_limit", "X-Reader-Zone2-Limit", int),
            ("zone2_usage", "X-Reader-Zone2-Usage", int),
            ("reset_after_s", "X-Reader-Limits-Reset-After", float),
        ):
            if v := headers.get(key):
                setattr(self, attr, cast(float(v)) if cast is int else cast(v))


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


@dataclass
class Client:
    client_id: str
    client_secret: str
    token_path: Path
    tokens: Tokens
    limits: RateLimits = field(default_factory=RateLimits)

    @classmethod
    def from_env(cls) -> Client:
        path = _token_path()
        if not path.exists():
            sys.exit(f"no token file at {path}; run `python -m core.inoreader auth` first")
        return cls(
            _env("INOREADER_CLIENT_ID"), _env("INOREADER_CLIENT_SECRET"), path, Tokens.load(path)
        )

    def _refresh(self) -> None:
        data = _post_form(
            TOKEN_URL,
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.tokens.refresh_token,
            },
        )
        self.tokens = Tokens.from_response(data)
        self.tokens.save(self.token_path)

    def _request(self, method: str, path: str, params: list[tuple[str, str]] | None = None):
        if time.time() > self.tokens.expires_at - REFRESH_MARGIN_S:
            self._refresh()
        query = urllib.parse.urlencode(params or [])
        url, body = f"{API}/{path}", None
        if method == "GET" and query:
            url = f"{url}?{query}"
        elif method == "POST":
            body = query.encode()
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.tokens.access_token}")
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.limits.update(resp.headers)
                raw = resp.read()
                ctype = resp.headers.get_content_type()
        except urllib.error.HTTPError as e:
            self.limits.update(e.headers)
            detail = e.read().decode(errors="replace")[:500]
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from e
        except OSError as e:  # DNS, refused, reset, timeout: callers handle it like a 429
            raise RuntimeError(f"{method} {path} -> {e}") from e
        return json.loads(raw) if ctype == "application/json" else raw.decode()

    # --- API methods -----------------------------------------------------------

    def user_info(self) -> dict:
        return self._request("GET", "user-info")

    def subscriptions(self) -> list[dict]:
        return self._request("GET", "subscription/list")["subscriptions"]

    def tags(self) -> list[dict]:
        return self._request("GET", "tag/list")["tags"]

    def stream_contents(
        self,
        stream_id: str = READING_LIST,
        *,
        n: int = 100,
        unread_only: bool = True,
        newer_than: int | None = None,
        oldest_first: bool = False,
        continuation: str | None = None,
    ) -> dict:
        params = [("n", str(min(n, 100)))]
        if unread_only:
            params.append(("xt", READ))
        if newer_than is not None:
            params.append(("ot", str(newer_than)))
        if oldest_first:  # with ot, lookback is clamped to ~1 month
            params.append(("r", "o"))
        if continuation:
            params.append(("c", continuation))
        sid = urllib.parse.quote(stream_id, safe="")
        return self._request("GET", f"stream/contents/{sid}", params)

    def edit_tag(
        self,
        item_ids: list[str],
        *,
        add: str | list[str] | None = None,
        remove: str | list[str] | None = None,
    ) -> str:
        """One Zone 2 call regardless of len(item_ids) — batch accordingly.

        Several tags repeat `a`/`r`; the docs don't say that's supported, but `test --write` confirmed it (2026-10-30).
        """
        params = [("i", i) for i in item_ids]
        for key, tags in (("a", add), ("r", remove)):
            params += [(key, t) for t in ([tags] if isinstance(tags, str) else tags or [])]
        return self._request("POST", "edit-tag", params)


# --- CLI ----------------------------------------------------------------------


def cmd_auth(_: argparse.Namespace) -> None:
    client_id, secret = _env("INOREADER_CLIENT_ID"), _env("INOREADER_CLIENT_SECRET")
    redirect = _env("INOREADER_REDIRECT_URI")
    state = secrets.token_urlsafe(24)
    url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": "read write",
            "state": state,
        }
    )
    print(f"1. Open this URL and approve:\n\n{url}\n")
    print("2. The browser will redirect to your redirect URI (a 'connection refused' page is fine).")
    pasted = input("3. Paste the full redirected URL here: ").strip()
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
    if "error" in qs:
        sys.exit(f"authorisation refused: {qs['error'][0]}")
    if qs.get("state", [""])[0] != state:
        sys.exit("state mismatch — possible CSRF or wrong URL pasted; rerun auth")
    data = _post_form(
        TOKEN_URL,
        {
            "code": qs["code"][0],
            "redirect_uri": redirect,
            "client_id": client_id,
            "client_secret": secret,
            "scope": "",
            "grant_type": "authorization_code",
        },
    )
    tokens = Tokens.from_response(data)
    path = _token_path()
    tokens.save(path)
    print(f"saved tokens to {path} (scope={tokens.scope!r}, expires_in={data['expires_in']}s)")


def cmd_test(args: argparse.Namespace) -> None:
    c = Client.from_env()
    me = c.user_info()
    print(f"user: {me.get('userName')} <{me.get('userEmail')}> id={me.get('userId')}")
    subs = c.subscriptions()
    print(f"subscriptions: {len(subs)}")
    labels = [t["id"] for t in c.tags() if "/label/" in t["id"]]
    print(f"folders/tags: {len(labels)} (e.g. {labels[:5]})")
    stream = c.stream_contents(n=5)
    items = stream.get("items", [])
    print(f"unread sample ({len(items)}):")
    for it in items:
        print(f"  {it['id'].rsplit('/', 1)[-1]}  [{it.get('origin', {}).get('title', '?')}] {it.get('title', '')[:80]}")
    if args.write:
        if not items:
            print("write test skipped: no unread items")
        else:
            probes, iid = ["user/-/label/llm-probe", "user/-/label/llm-probe2"], items[0]["id"]

            def tagged(label: str) -> list[bool]:  # one Zone 1 call: which probes the item carries, seen via label
                got = c.stream_contents(label, n=20, unread_only=False).get("items", [])
                cats = next((it.get("categories", []) for it in got if it["id"] == iid), [])
                # categories use the real user id, not "-"
                return [any(cat.endswith(p.removeprefix("user/-")) for cat in cats) for p in probes]

            print(f"edit-tag add x2: {c.edit_tag([iid], add=probes)}, present={tagged(probes[1])}")
            removed = c.edit_tag([iid], remove=probes)
            left = [a or b for a, b in zip(tagged(probes[0]), tagged(probes[1]))]  # a lone leftover shows in its own stream
            print(f"edit-tag remove x2: {removed}, present={left}")
            print("multi-tag edit-tag works if present=[True, True] then [False, False]")
    L = c.limits
    print(
        f"rate limits: zone1 {L.zone1_usage}/{L.zone1_limit}, zone2 {L.zone2_usage}/{L.zone2_limit}, "
        f"reset in {L.reset_after_s}s; token scope={c.tokens.scope!r}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(required=True)
    sub.add_parser("auth", help="OAuth 2.0 authorisation-code bootstrap").set_defaults(fn=cmd_auth)
    t = sub.add_parser("test", help="connectivity test")
    t.add_argument("--write", action="store_true", help="add+remove two probe tags in one call each (2 Zone 2 + 3 Zone 1 calls)")
    t.set_defaults(fn=cmd_test)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
