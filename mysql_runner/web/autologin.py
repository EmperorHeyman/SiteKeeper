"""Auto-login helpers for phpMyAdmin.

The login form for phpMyAdmin's cookie auth varies between versions and themes,
so we match several known field selectors. The injected JavaScript fills the
username/password fields and submits the form. A guard flag on ``window`` keeps
us from re-submitting endlessly if a login fails.
"""

from __future__ import annotations

import json
from functools import lru_cache

from mysql_runner.paths import resource_path


def build_login_script(username: str, password: str) -> str:
    """Return JS that fills and submits the phpMyAdmin cookie login form.

    Returns ``true`` (as the JS result) when a login form was found and
    submitted, ``false`` otherwise.
    """
    user_js = json.dumps(username)
    pass_js = json.dumps(password)
    return f"""
(function () {{
    var userSelectors = ['#input_username', 'input[name="pma_username"]'];
    var passSelectors = ['#input_password', 'input[name="pma_password"]'];

    function findFirst(selectors) {{
        for (var i = 0; i < selectors.length; i++) {{
            var el = document.querySelector(selectors[i]);
            if (el) {{ return el; }}
        }}
        return null;
    }}

    var userField = findFirst(userSelectors);
    var passField = findFirst(passSelectors);
    if (!userField || !passField) {{
        return false;
    }}
    if (window.__mysqlRunnerSubmitted) {{
        return false;
    }}
    window.__mysqlRunnerSubmitted = true;

    userField.value = {user_js};
    passField.value = {pass_js};
    userField.dispatchEvent(new Event('input', {{ bubbles: true }}));
    passField.dispatchEvent(new Event('input', {{ bubbles: true }}));

    var form = passField.form || userField.form;
    if (form) {{
        if (typeof form.requestSubmit === 'function') {{
            form.requestSubmit();
        }} else {{
            form.submit();
        }}
        return true;
    }}
    return false;
}})();
"""


def build_login_form_present_script() -> str:
    """Return JS evaluating to ``true`` if a login form is on the page."""
    return """
(function () {
    var sel = ['#input_username', 'input[name="pma_username"]',
               '#input_password', 'input[name="pma_password"]'];
    for (var i = 0; i < sel.length; i++) {
        if (document.querySelector(sel[i])) { return true; }
    }
    return false;
})();
"""


# Dark mode is delegated to Dark Reader (https://darkreader.org) - the same
# open-source engine behind the browser extension. It reads each element's
# *computed* colours at runtime and generates correct dark equivalents (text,
# backgrounds, borders, even images), watching the DOM for changes. That avoids
# the two failure modes of doing this by hand: a naive ``invert`` filter
# (washed-out grey, miscoloured images) and a static stylesheet (missed
# elements → white-on-white, smudged text). The library is vendored so the app
# works offline and inside the PyInstaller build.
_DARKREADER_RESOURCE = "mysql_runner/web/vendor/darkreader.js"


@lru_cache(maxsize=1)
def _darkreader_source() -> str:
    """Return the vendored Dark Reader UMD source (cached after first read)."""
    return resource_path(_DARKREADER_RESOURCE).read_text(encoding="utf-8")


# Theme tuning: full brightness, slightly softened contrast so large white
# tables don't glare, no sepia. Tweak here if you want a warmer/cooler dark.
_DARKREADER_THEME = json.dumps({"brightness": 100, "contrast": 90, "sepia": 0})


def build_dark_mode_script(enable: bool) -> str:
    """Return JS that enables/disables Dark Reader on the current document.

    When enabling, the vendored Dark Reader UMD source is injected once per
    document (guarded by ``window.DarkReader``) and ``DarkReader.enable`` is
    called; its dynamic engine then themes the page and keeps watching for DOM
    changes. Disabling calls ``DarkReader.disable``, fully restoring phpMyAdmin's
    stock light theme.
    """
    if not enable:
        return "try { if (window.DarkReader) { DarkReader.disable(); } } catch (e) {}"
    try:
        source = _darkreader_source()
    except OSError:
        # Library missing (e.g. not bundled): degrade to no-op rather than error.
        return "/* dark mode unavailable: Dark Reader resource missing */"
    return f"""
try {{
    if (!window.DarkReader) {{
{source}
    }}
    DarkReader.enable({_DARKREADER_THEME});
}} catch (e) {{
    if (window.console && console.warn) {{ console.warn('dark mode failed', e); }}
}}
"""


def build_startup_script(query: str) -> str:
    """Return JS that opens phpMyAdmin's SQL tab and fills ``query``.

    Best-effort across phpMyAdmin versions: it clicks the SQL tab link, then
    fills the CodeMirror editor (if present) or the raw ``#sqlquery`` textarea.
    A guard flag prevents it from running more than once per session.
    """
    query_js = json.dumps(query)
    return f"""
(function () {{
    var query = {query_js};
    if (!query || window.__mysqlRunnerStartupDone) {{ return false; }}

    function fill() {{
        var cm = document.querySelector('.CodeMirror');
        if (cm && cm.CodeMirror) {{
            cm.CodeMirror.setValue(query);
            return true;
        }}
        var ta = document.querySelector('#sqlquery, textarea[name="sql_query"]');
        if (ta) {{
            ta.value = query;
            ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
            return true;
        }}
        return false;
    }}

    // Already on a page that has a SQL box?
    if (fill()) {{ window.__mysqlRunnerStartupDone = true; return true; }}

    // Otherwise try to navigate to the SQL tab.
    var sqlLink = document.querySelector('a[href*="sql.php"], #topmenu a[href*="route=/table/sql"], a[href*="route=/sql"]');
    if (sqlLink) {{
        window.__mysqlRunnerStartupDone = true;
        sqlLink.click();
        return true;
    }}
    return false;
}})();
"""
