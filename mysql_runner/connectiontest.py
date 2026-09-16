"""Try a connection and say what happened, in words a person can act on.

Saving a connection used to be an act of faith: you typed six fields from a
hosting email, pressed OK, and found out whether any of them were right the
next time you needed the server. When it failed, everything was suspect at
once - the host, the port, the password, the protocol, the folder - and the
only way to narrow it down was to change one and try again.

So this connects, and then says which parts were proven. The distinction it
draws is the useful one:

* **it worked** - the login was accepted, and here is what answered,
* **it worked, but** - the server is there and the credentials are good, and
  something you typed alongside them is not: a start folder that does not
  exist, a database that is not on that server. Those are worth seeing now
  rather than at the point of use, and they are not failures,
* **it did not work** - with the reason the server actually gave, not
  "connection failed".

Nothing here imports Qt, so the dialog, the connection list, the MCP server
and the sidecar all get the same answers. Nothing here writes to the vault,
and the only thing it can change on this machine is a host key, and only when
the caller has said it may.
"""

from __future__ import annotations

import base64
import html
import http.cookiejar
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from mysql_runner.storage.models import AuthType, ConnectionKind
from mysql_runner.transfer import backends, hostkeys
from mysql_runner.transfer.base import Capability, TransferError

#: How long any one step may take. Long enough for a slow SFTP handshake over
#: a bad link, short enough that a wrong port does not look like a hang.
TIMEOUT = 20.0

#: What the phpMyAdmin probe calls itself. A server's access log should be
#: able to say what this was.
USER_AGENT = "Sitekeeper/connection-test"

#: Names phpMyAdmin's login form has used across versions and themes. The
#: same list the auto-login script matches on, for the same reason.
_PMA_USER_FIELD = re.compile(
    r'name=["\'](pma_username|input_username)["\']', re.I
)
_HIDDEN_INPUT = re.compile(r"<input\b[^>]*>", re.I)
_ATTR = re.compile(r'([a-zA-Z_:][-\w:.]*)\s*=\s*"([^"]*)"|'
                   r"([a-zA-Z_:][-\w:.]*)\s*=\s*'([^']*)'")
_FORM = re.compile(r"<form\b[^>]*>.*?</form>", re.I | re.S)
_FORM_ACTION = re.compile(r'action\s*=\s*["\']([^"\']*)["\']', re.I)
#: What phpMyAdmin says when MySQL turned the credentials down. Matched so a
#: refused password reads as a refused password rather than as a page.
_PMA_REFUSED = re.compile(
    r"(Cannot log in to the MySQL server|Access denied for user|#1045)", re.I
)
_TAGS = re.compile(r"<[^>]+>")

#: The three things a test can conclude.
OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class TestResult:
    """What one attempt found out."""

    state: str            # OK, WARN or FAIL
    summary: str          # One line, for beside the button.
    #: The facts, one per line: what answered, what it can do, what is wrong.
    facts: list[str] = field(default_factory=list)
    #: Set when the only thing in the way is a server nobody has vouched for.
    #: The caller shows the fingerprint, and tries again if it is accepted.
    host_key: object = None

    @property
    def ok(self) -> bool:
        return self.state == OK

    def text(self) -> str:
        """Summary and facts as one block, for a log or a tool answer."""
        return "\n".join([self.summary, *self.facts]).strip()


def run(
    profile,
    *,
    jump: object = None,
    host_key_mode: str = hostkeys.PROMPT,
    timeout: float = TIMEOUT,
) -> TestResult:
    """Connect the way the app would, then disconnect. Never raises.

    ``host_key_mode`` is the caller's to choose, and the choice is real: with
    ``PROMPT`` an unconfirmed SSH server comes back as a question in
    ``host_key``, with ``AUTO`` it is recorded and the test continues. See
    transfer/backends.py.
    """
    try:
        if profile.kind == ConnectionKind.PHPMYADMIN:
            return _test_web(profile, timeout)
        if profile.kind.is_sql:
            return _test_sql(profile)
        if profile.kind.is_transfer:
            return _test_transfer(profile, jump, host_key_mode)
    except Exception as exc:  # a test that crashes has still found something
        return TestResult(FAIL, _unexpected(exc))
    return TestResult(
        FAIL, f"Sitekeeper does not know how to test a {profile.kind.value} connection."
    )


def _unexpected(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


# ----- databases ----------------------------------------------------------
def _test_sql(profile) -> TestResult:
    """Log in to MySQL or SQL Server, read who answered, log out again."""
    from mysql_runner.db import engines
    from mysql_runner.db.engines import ConnectionParams

    engine = engines.engine_for(profile.kind)
    if not engine.available():
        return TestResult(
            FAIL,
            f"No {engine.name} driver is available.",
            [engine.missing_message()],
        )
    if not profile.host.strip():
        return TestResult(FAIL, "No host to connect to.")

    params = ConnectionParams.from_profile(profile)
    try:
        connection = engine.connect(params)
    except Exception as exc:
        return TestResult(
            FAIL,
            f"{engine.name} refused the connection.",
            [engine.describe_error(exc)],
        )
    try:
        version, connection_id, database = engine.server_facts(connection)
    finally:
        try:
            connection.close()
        except Exception:
            pass

    who = (
        "Windows authentication"
        if params.windows_auth
        else (profile.username or "no username")
    )
    facts = [f"Logged in as {who}."]
    if version:
        facts.append(f"{engine.name} {version}")
    if database:
        facts.append(f"Database: {database}")
    elif profile.database:
        # Connecting names the database, so the server would have refused an
        # unknown one - but an account whose default schema is something else
        # is worth a word rather than silence.
        facts.append(f"Database: {profile.database}")
    if connection_id:
        facts.append(f"Connection id: {connection_id}")
    if profile.startup_script.strip():
        facts.append("Startup SQL is not run by a test.")
    return TestResult(OK, f"Connected to {profile.describe_target()}.", facts)


# ----- file transfer ------------------------------------------------------
def _test_transfer(profile, jump, host_key_mode: str) -> TestResult:
    """Log in over FTP, FTPS or SFTP and look at the folder it starts in."""
    if not profile.host.strip():
        return TestResult(FAIL, "No host to connect to.")
    remote = backends.for_profile(
        profile, jump=jump, host_key_mode=host_key_mode
    )
    try:
        banner = remote.connect()
    except hostkeys.HostKeyUnknown as unknown:
        return TestResult(
            WARN,
            f"{unknown.host} has not been confirmed as your server yet.",
            [str(unknown)],
            host_key=unknown,
        )
    except TransferError as exc:
        return TestResult(FAIL, "The server refused the login.", [str(exc)])
    except Exception as exc:
        return TestResult(FAIL, "Could not connect.", [_unexpected(exc)])

    state = OK
    facts = []
    if banner:
        facts.append(banner.strip())
    try:
        home = remote.home()
        if home:
            facts.append(f"Starts in {home}")
    except Exception:
        pass  # a home directory is a nicety, not the test

    wanted = profile.remote_dir.strip()
    if wanted:
        # The single most common thing to get wrong after the password, and
        # the one that otherwise shows up as an empty pane nobody can explain.
        try:
            entries = remote.listdir(wanted)
            facts.append(f"{wanted} opens, with {len(entries)} item(s) in it.")
        except TransferError as exc:
            state = WARN
            facts.append(f"But {wanted} could not be opened: {exc}")
        except Exception as exc:
            state = WARN
            facts.append(f"But {wanted} could not be opened: {_unexpected(exc)}")

    facts.append(_describe_capabilities(remote))
    try:
        remote.close()
    except Exception:
        pass

    summary = (
        f"Connected to {profile.host} as {profile.username or 'anonymous'}."
        if state == OK
        else f"Logged in to {profile.host}, but not everything checked out."
    )
    return TestResult(state, summary, facts)


def _describe_capabilities(remote) -> str:
    """What this login turned out to be allowed to do."""
    try:
        capabilities = remote.capabilities()
    except Exception:
        return ""
    names = [
        label
        for capability, label in (
            (Capability.EXEC, "run commands"),
            (Capability.CHMOD, "change permissions"),
            (Capability.SYMLINK, "read symlinks"),
            (Capability.SET_MTIME, "set timestamps"),
        )
        if capability in capabilities
    ]
    return "Can " + ", ".join(names) + "." if names else "Files only, no shell."


# ----- phpMyAdmin ---------------------------------------------------------
def _test_web(profile, timeout: float) -> TestResult:
    """Fetch the phpMyAdmin page, and log in to it if it asks.

    Reaching the page proves the URL and the certificate; logging in proves
    the credentials, which is the half a person actually doubts. Both are
    reported separately, because "the page is there but the password is
    wrong" and "there is no page" are different problems with different
    fixes.
    """
    url = (profile.url or "").strip()
    if not url.startswith(("http://", "https://")):
        return TestResult(FAIL, "The URL must start with http:// or https://.")

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    opener.addheaders = [("User-Agent", USER_AGENT)]
    wants_basic = profile.auth_type == AuthType.HTTP_BASIC

    try:
        body, final_url, _status = _fetch(opener, url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return _test_basic_auth(opener, url, profile, timeout)
        return TestResult(
            FAIL,
            f"The server answered {exc.code} ({exc.reason}).",
            [f"Asked for {url}"],
        )
    except ssl.SSLError as exc:
        return TestResult(
            FAIL,
            "The site's certificate could not be verified.",
            [str(exc), "An expired or self-signed certificate does this."],
        )
    except urllib.error.URLError as exc:
        return TestResult(
            FAIL, "The site could not be reached.", [str(exc.reason)]
        )
    except OSError as exc:
        return TestResult(FAIL, "The site could not be reached.", [str(exc)])

    if wants_basic:
        # Configured for Basic, and the page came back without a challenge.
        return TestResult(
            WARN,
            "The page loaded without asking for HTTP Basic credentials.",
            [
                f"Reached {final_url}",
                "This connection is set to HTTP Basic Auth, so either the "
                "server no longer uses it or this is not the protected page.",
            ],
        )
    if not _PMA_USER_FIELD.search(body):
        return TestResult(
            WARN,
            "The page loaded, but it is not a phpMyAdmin login form.",
            [
                f"Reached {final_url}",
                "Check the URL points at phpMyAdmin itself - often the "
                "address ends in /phpmyadmin/.",
            ],
        )
    if not profile.username:
        return TestResult(
            WARN,
            "phpMyAdmin is there; no username is saved to log in with.",
            [f"Reached {final_url}"],
        )
    return _test_cookie_login(opener, body, final_url, profile, timeout)


def _fetch(opener, url: str, timeout: float, data: bytes | None = None):
    """GET or POST, and give back the decoded body, final URL and status."""
    with opener.open(url, data=data, timeout=timeout) as response:
        raw = response.read(512 * 1024)
        charset = response.headers.get_content_charset() or "utf-8"
        return (
            raw.decode(charset, errors="replace"),
            response.geturl(),
            response.status,
        )


def _test_basic_auth(opener, url: str, profile, timeout: float) -> TestResult:
    """The server asked for HTTP Basic. Answer it, and see if it agrees."""
    if not profile.username:
        return TestResult(
            WARN,
            "The server asks for a username and password, and none is saved.",
            [f"Reached {url}", "This page uses HTTP Basic Auth."],
        )
    token = base64.b64encode(
        f"{profile.username}:{profile.password}".encode("utf-8")
    ).decode("ascii")
    request = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}",
        "User-Agent": USER_AGENT,
    })
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(512 * 1024).decode(
                response.headers.get_content_charset() or "utf-8",
                errors="replace",
            )
            final = response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return TestResult(
                FAIL,
                "The server refused that username and password.",
                [f"HTTP Basic Auth at {url} answered 401."],
            )
        return TestResult(
            FAIL, f"The server answered {exc.code} ({exc.reason}).", [url]
        )
    except OSError as exc:
        return TestResult(FAIL, "The site could not be reached.", [str(exc)])

    facts = [f"HTTP Basic Auth at {final} accepted {profile.username}."]
    if _PMA_USER_FIELD.search(body):
        # Past the web server, stopped at phpMyAdmin's own form. Both layers
        # exist on plenty of hosts and only one of them has been proven.
        return TestResult(
            WARN,
            "The web server accepted those credentials; phpMyAdmin then "
            "asked for its own.",
            facts + [
                "Set this connection's authentication to the phpMyAdmin "
                "login form if that is the password you saved."
            ],
        )
    return TestResult(OK, "Logged in.", facts)


def _test_cookie_login(opener, body: str, url: str, profile, timeout: float) -> TestResult:
    """Fill in phpMyAdmin's login form the way the browser tab would."""
    form = _login_form(body)
    fields = _hidden_fields(form)
    fields.update({
        "pma_username": profile.username,
        "pma_password": profile.password,
    })
    action = _FORM_ACTION.search(form)
    target = urllib.parse.urljoin(url, html.unescape(action.group(1))) if action else url
    data = urllib.parse.urlencode(fields).encode("utf-8")
    try:
        result, final, _status = _fetch(opener, target, timeout, data=data)
    except urllib.error.HTTPError as exc:
        return TestResult(
            FAIL,
            f"The login page answered {exc.code} ({exc.reason}).",
            [f"Posted to {target}"],
        )
    except OSError as exc:
        return TestResult(FAIL, "The login could not be sent.", [str(exc)])

    refused = _PMA_REFUSED.search(result)
    if refused:
        return TestResult(
            FAIL,
            "phpMyAdmin refused that username and password.",
            [_alert_text(result) or refused.group(1)],
        )
    if _PMA_USER_FIELD.search(result):
        # Back at the login form with nothing to quote. Usually a lost
        # session (cookies blocked, or a redirect across hosts) rather than a
        # bad password, and saying "wrong password" here would be a guess.
        return TestResult(
            WARN,
            "phpMyAdmin is there, but the login could not be completed from "
            "here.",
            [
                f"Reached {url}",
                _alert_text(result)
                or "The form came back without saying why. Opening the tab "
                   "will show the real page.",
            ],
        )
    return TestResult(
        OK,
        f"Logged in to phpMyAdmin as {profile.username}.",
        [f"Reached {final}"],
    )


def _login_form(body: str) -> str:
    """The form holding the username field, or the whole page if unsure."""
    for match in _FORM.finditer(body):
        if _PMA_USER_FIELD.search(match.group(0)):
            return match.group(0)
    return body


def _hidden_fields(form: str) -> dict:
    """The form's hidden inputs - phpMyAdmin's token lives among them.

    Without the token the server discards the login, so a test that skipped
    them would report a wrong password for every correct one.
    """
    fields = {}
    for tag in _HIDDEN_INPUT.finditer(form):
        attributes = _attributes(tag.group(0))
        if attributes.get("type", "").lower() != "hidden":
            continue
        name = attributes.get("name")
        if name:
            fields[name] = attributes.get("value", "")
    return fields


def _attributes(tag: str) -> dict:
    """An HTML tag's attributes, unescaped, lowercased names."""
    found = {}
    for match in _ATTR.finditer(tag):
        name = (match.group(1) or match.group(3) or "").lower()
        value = match.group(2) if match.group(2) is not None else match.group(4)
        if name:
            found[name] = html.unescape(value or "")
    return found


def _alert_text(body: str) -> str:
    """Whatever phpMyAdmin printed in its error box, as plain text."""
    match = re.search(
        r'<div[^>]*class="[^"]*alert[^"]*danger[^"]*"[^>]*>(.*?)</div>',
        body,
        re.I | re.S,
    )
    if not match:
        return ""
    text = html.unescape(_TAGS.sub(" ", match.group(1)))
    return " ".join(text.split())[:300]
