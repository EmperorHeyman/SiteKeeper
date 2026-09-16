# Sitekeeper for hosting providers

> How to put a **"Open in Sitekeeper"** button next to an account in your control
> panel, so a customer goes from *"here are your FTP details"* to a working,
> saved connection without ever seeing a password field.

Written for the engineers who build the control panel. It assumes you can add a
button to a page and an authenticated endpoint to your backend; it assumes
nothing about Sitekeeper beyond that.

---

## The moment this replaces

Today a customer who buys hosting gets a welcome mail, or a panel page, holding
six facts: a hostname, a port, a username, a password, a protocol and a
directory. They then open a file transfer client and retype all six, usually
wrongly, usually into a client that stores the password in something reversible.
The first support ticket of the account's life is often *"I can't connect"*.

The flow below replaces that with one click. The customer clicks **Open in
Sitekeeper** in your panel, Sitekeeper asks them to confirm that `your-panel.tld`
may add connections, and the account appears in their sidebar - correct host,
correct port, correct protocol, opening straight into the document root, tinted
red if it is production. Nothing was typed and nothing was pasted.

---

## Status at a glance

All four routes work. The first two are the ones worth building.

| Route | Customer effort | Ships in |
| --- | --- | --- |
| **Claim ticket** - `sitekeeper://` link, app fetches the payload from you over HTTPS | one click, one confirmation | **1.12.0** |
| **Ticket file** - you serve a `.skc` file, they open it | one download, one double-click, one confirmation | **1.12.0** |
| **Connection-string list** - you serve a `.txt`, they use *Import sessions* | download, then a menu and a file picker | 1.11.0 |
| **Encrypted bundle** - you serve a `.mrx` and a passphrase | download, menu, file picker, passphrase | 1.11.0 |

Build the claim ticket. The `.skc` file is the same backend endpoint behind a
different button, so it costs almost nothing once the first is done, and it
covers the customers whose browser refuses custom schemes.

The `.txt` route still works and is the cheapest thing you can ship today - but
read [Security](#security-what-we-will-not-accept) before you choose it. A
plaintext file in a Downloads folder has consequences you should decide about
deliberately, and the ticket flow exists to avoid them.

Customers on 1.11.0 and earlier get nothing when they click a `sitekeeper://`
link: the scheme is registered by the 1.12.0 installer. That is the same failure
as not having the app at all, and the
[fallback below](#when-sitekeeper-is-not-installed-yet) covers both.

---

## The recommended flow: a claim ticket

```
  panel page                    Sitekeeper                    your backend
  ----------                    ----------                    ------------
  [Open in Sitekeeper] --------> opens on the
        |                        sitekeeper:// link
        |                              |
        |                        "panel.example.com
        |                         wants to add
        |                         connections. Allow?"
        |                              |
        |                              +---- GET /.well-known/sitekeeper/... -->
        |                                    Authorization: Bearer <ticket>
        |                              <---- 200 {"connections": [...]} --------+
        |                              |                              ticket burned
        |                        "Add 2 connections
        |                         from CloudCore?"
        |                         - webhosting (SFTP)
        |                         - db (MySQL)
        |                              |
        |                        saved to the vault
```

The link carries a **ticket**, never a credential. Credentials travel one way
only: over TLS, from your backend, in response to a request that presents that
ticket exactly once.

### 1. The link

```
sitekeeper://provision/v1?src=panel.example.com&t=<ticket>
```

| Parameter | Meaning |
| --- | --- |
| `src` | The host Sitekeeper will fetch from. A hostname, optionally `host:port`. No scheme, no path, no userinfo. |
| `t` | The ticket. Opaque to Sitekeeper. URL-safe, at least 128 bits of entropy, at most 512 characters. |

That is the whole link. There is deliberately no `url=` parameter, no product
name, no connection count and no display label - anything a link can say about
itself is something an attacker can also say. The only thing shown to the
customer before a byte leaves their machine is `src`, and `src` is the one thing
TLS will verify a moment later.

### 2. The endpoint

Sitekeeper derives the URL from `src` and a fixed path. You do not get to choose
the path:

```http
GET https://panel.example.com/.well-known/sitekeeper/v1/provision HTTP/1.1
Authorization: Bearer <ticket>
Accept: application/json
Cache-Control: no-store
Connection: close
User-Agent: Sitekeeper/1.12.0 (Windows)
```

Requirements, all enforced by the app and none negotiable:

- **HTTPS with a valid chain.** No self-signed certificates, no pinning
  exceptions, no plain HTTP even on a port you think is internal.
- **No IP literals in `src`**, and no name that resolves to a loopback, link-local
  or RFC 1918 address. A link must not be able to point the app at the customer's
  own network.
- **At most two redirects, same host only.** A redirect to another host is a
  failure, not a hop.
- **Response body at most 256 KB, at most 50 connections.**
- A response that takes more than 15 seconds is abandoned.

### 3. The response

```json
{
  "version": 1,
  "issuer": "CloudCore Webhosting",
  "issued_at": "2026-09-15T12:04:11Z",
  "expires_at": "2026-09-15T12:14:11Z",
  "connections": [
    {
      "label": "example.cz - web",
      "kind": "sftp",
      "host": "web12.cloudcore.cz",
      "port": 22,
      "username": "example_cz",
      "password": "…",
      "remote_dir": "/home/example_cz/public_html",
      "group": "example.cz",
      "environment": "prod"
    },
    {
      "label": "example.cz - database",
      "kind": "mysql",
      "host": "sql12.cloudcore.cz",
      "port": 3306,
      "username": "example_cz_admin",
      "password": "…",
      "database": "example_cz_wp",
      "group": "example.cz",
      "environment": "prod"
    }
  ]
}
```

`issuer` is what the confirmation dialog names as the source, and it is rendered
as plain text - markup, control characters and right-to-left overrides in it are
stripped, because a dialog that asks "do you trust this" must not let the thing
being trusted paint the dialog.

`expires_at` is advisory, for display. The ticket's real lifetime is whatever
your backend enforces.

### 4. Errors

Anything other than `200` is a failure the customer sees. Use the shape:

```json
{ "error": "ticket_expired", "message": "This link has already been used. Generate a new one from your panel." }
```

| Status | `error` | When |
| --- | --- | --- |
| `401` | `ticket_invalid` | Unknown, malformed, or already redeemed |
| `401` | `ticket_expired` | Past its lifetime |
| `403` | `not_permitted` | The account it belongs to no longer exists or lost access |
| `429` | `rate_limited` | Honour this with `Retry-After` |
| `503` | `unavailable` | Your side is down; say so rather than returning an empty list |

`message` is shown to the customer verbatim, as plain text. Write it for them,
not for your logs: *"Generate a new one from your panel"* beats *"token nonce
mismatch"*.

### 5. The ticket's rules

These are your responsibility, and they are the whole security model:

1. **One use.** Redeem it inside a transaction and burn it. A second `GET`
   returns `401`, even ten milliseconds later, even from the same IP.
2. **Short-lived.** Ten minutes is generous. The customer is standing at the
   screen; they are not coming back tomorrow.
3. **Bound to the session that minted it.** A ticket is proof that *this*
   logged-in customer asked, at *this* moment, for *these* accounts. It is not a
   bearer key to the account.
4. **Scoped to what the button was next to.** A ticket minted by the FTP page of
   `example.cz` returns that account. It does not return every account the
   customer owns, and it certainly does not return anyone else's.
5. **Minted on `POST`, never on page render.** A ticket that exists because a
   page was loaded is a ticket that exists in the customer's browser history, in
   your access log, and in the referrer sent to every third-party script on that
   page. Mint it from a CSRF-protected `POST` when the button is clicked.
6. **Revocable.** Password changed, account suspended, customer clicked
   *"that wasn't me"* - every outstanding ticket for that account dies.

---

## The connection payload

Each object in `connections` describes one saved connection. Only `label`,
`kind` and the fields that kind needs are required.

| Field | Type | Applies to | Notes |
| --- | --- | --- | --- |
| `label` | string | all | What the customer sees in the sidebar. Lead with the domain: `example.cz - web`, not `SFTP Account 1`. Max 120 chars. |
| `kind` | string | all | `sftp`, `ftp`, `ftps`, `mysql`, `mssql` or `phpmyadmin`. |
| `host` | string | all but `phpmyadmin` | Hostname or IP of the service. |
| `port` | int | all but `phpmyadmin` | Omit or `0` to use the protocol default (22 / 21 / 3306 / 1433). |
| `username` | string | all | |
| `password` | string | all | See [Credentials](#credentials-what-to-hand-over) before you send the customer's real one. |
| `url` | string | `phpmyadmin` | Full `https://` URL of the phpMyAdmin login page. |
| `auth_type` | string | `phpmyadmin` | `auto` (default), `cookie` or `basic`. Leave it `auto` unless you know. |
| `database` | string | `mysql`, `mssql` | Pre-selects a schema. |
| `instance` | string | `mssql` | A named SQL Server instance, the part after the backslash in `MACHINE\SQLEXPRESS`. Omit for a default instance on a port. |
| `encrypt` | bool | `mssql` | Default `true`. |
| `trust_certificate` | bool | `mssql` | Default `true`. Set it `false` only if the server presents a certificate issued by an authority the customer's PC already trusts; with a self-signed one, `false` means the connection cannot be made at all. |
| `remote_dir` | string | `sftp`, `ftp`, `ftps` | Absolute path the remote pane opens in. **Set this.** See below. |
| `passive` | bool | `ftp`, `ftps` | Default `true`. Change it only if your firewall genuinely requires active mode. |
| `ssh_port` | int | `ftp`, `ftps` | The SSH port on the same host, if the account has shell access. Lets the terminal work for an FTP account without asking the customer. Omit if there is no shell. |
| `use_agent` | bool | `sftp` | Default `true`. |
| `group` | string | all | Files the connection in the sidebar. **Use the domain or the account name** - a customer with nine sites gets nine tidy groups instead of one wall of entries. |
| `environment` | string | all | `none`, `dev`, `staging` or `prod`. `prod` gives the tab a red dot and tint, which is the thing that stops a customer running a delete on the wrong server. Set it honestly. |

### `remote_dir` is the field that earns the integration

A transfer pane that opens in `/` shows the customer `bin`, `etc`, `logs`, `tmp`
and - somewhere down there - their website. A pane that opens in
`/home/example_cz/public_html` shows them their website. You know that path and
they do not. It is one string and it is the difference between the integration
feeling magic and feeling like a file browser.

### Fields we will not accept

Sending these is not an error you will notice - they are silently dropped - but
know why, because each one is a decision we took away from you on purpose:

| Field | Why it is stripped |
| --- | --- |
| `proxy_command` | It is a local shell command run on connect. A remote party setting it is remote code execution on your customer's PC. There is no version of this we will ever accept from a payload. |
| `local_dir` | A folder on the customer's machine. Combined with a synced folder it becomes a write target chosen by someone who is not sitting at the keyboard. |
| `startup_script` | SQL that runs automatically on every connect. Nothing you need it for, and "runs by itself" belongs to the person who owns the machine. |
| `private_key_path` | A path into the customer's filesystem. To ship the key itself, send `private_key` - see [Credentials](#credentials-what-to-hand-over). |
| `jump_profile_id` | Refers to a profile id in *this* vault. It cannot mean anything coming from outside it. |
| `id`, `order` | Assigned locally. An import that could pick its own id could overwrite an existing connection. |

---

## Credentials: what to hand over

The obvious payload is the customer's own FTP password, which your panel already
knows. It works, and over a one-time TLS fetch it is defensible. But there is a
better answer, in this order of preference:

1. **Mint a dedicated credential for this handover.** An SFTP sub-account or a
   scoped FTP user, created when the ticket is redeemed, named so the customer
   can see it in your panel (`example_cz-sitekeeper`) and revoke it in one click
   without touching the password they use elsewhere. If the machine is later
   lost or sold, that is one revocation, not a password reset cascade.
2. **Ship a key instead of a password.** Generate an Ed25519 pair, install the
   public half in the account's `authorized_keys`, and send the private half as
   `private_key` (PEM, OpenSSH format) in the payload. Sitekeeper writes it to
   `%APPDATA%\Sitekeeper\keys` - only after the customer accepts, never
   before - and points the connection at it. The password never exists. SFTP
   only: there is nothing to do with a key on an FTP account.
3. **Send the existing password.** Acceptable. If you do, do not also print it on
   the page next to the button - having built a flow where the customer never
   sees the password, do not undo it.

Whichever you choose: after a successful redemption, tell the customer in your
panel what happened. *"Added to Sitekeeper on 15 Sep - revoke"* is the line that
turns a credential handover into something they can audit.

---

## When Sitekeeper is not installed yet

You cannot reliably ask a browser whether a custom scheme is registered. Every
technique for it is a timing heuristic that breaks on the next browser release,
and the failure mode - a customer staring at a page where nothing happened - is
the one you were trying to avoid. So do not detect. Make both paths visible and
cheap:

```html
<a class="primary" href="sitekeeper://provision/v1?src=panel.example.com&t=TICKET">
  Open in Sitekeeper
</a>
<p class="muted">
  Nothing happened?
  <a href="https://sitekeeper.example/download">Install Sitekeeper</a>,
  then click the button above again.
</p>
```

Two things make that second sentence work:

- **Keep the ticket alive, or re-mint it silently.** The customer is going to
  leave for four minutes to run an installer. Either give the ticket a lifetime
  that survives that, or have the button re-`POST` for a fresh one on every
  click, which is simpler and strictly better.
- **Link the publisher's own signed installer.** Do not re-host the `.exe` on
  your CDN. A mirrored copy goes stale, loses its signature in transit more often
  than you would believe, and trains your customers to accept a hosting company's
  unsigned binary - which is exactly the habit a phishing page needs them to have.

If you would rather not depend on a URL scheme at all, serve the same ticket as a
**`.skc` file** from the button instead. It is the two link parameters as JSON:

```json
{ "version": 1, "src": "panel.example.com", "ticket": "…" }
```

Serve it as `application/vnd.sitekeeper.claim+json`, with
`Content-Disposition: attachment; filename="example.cz.skc"`. Once Sitekeeper is
installed the file association does the rest, and this route survives browsers
that have got stricter about custom schemes. Same endpoint, same payload, same
confirmation.

---

## What you can ship today

Sitekeeper 1.11.0 already imports a **list of connection strings**. Serve a
`.txt`, and the customer uses *Import sessions* in the app and points at it.
One line per connection, `Label = url`, `#` for comments:

```
# CloudCore Webhosting - example.cz
example.cz - web      = sftp://example_cz:s3cret@web12.cloudcore.cz:22/home/example_cz/public_html
example.cz - database = mysql://example_cz_admin:s3cret@sql12.cloudcore.cz:3306/example_cz_wp
example.cz - pma      = https://pma.cloudcore.cz/
```

Percent-encode the username and password. Schemes understood: `sftp`, `ssh`,
`scp`, `ftp`, `ftps`, `ftpes`, `mysql`, `mssql`, `sqlserver`, `http`, `https`
(the last two become phpMyAdmin connections). A bare `host:port/path` with no
scheme is read as SFTP. The path becomes the remote directory, or for the two
database schemes the database. A named SQL Server instance has no place in a
URL - a backslash in a host is not one - so a `mssql://` line names a port,
and an instance is set in the dialog afterwards.

```php
<?php
// Behind your existing session + CSRF check. POST only.
function sitekeeper_txt(array $accounts, string $domain): string {
    $lines = ["# " . PANEL_BRAND . " - {$domain}"];
    foreach ($accounts as $a) {
        $auth = rawurlencode($a['user']) . ':' . rawurlencode($a['pass']);
        $lines[] = sprintf(
            '%s = %s://%s@%s:%d%s',
            $a['label'], $a['scheme'], $auth, $a['host'], $a['port'], $a['path']
        );
    }
    return implode("\n", $lines) . "\n";
}

header('Content-Type: text/plain; charset=utf-8');
header('Content-Disposition: attachment; filename="' . $domain . '.txt"');
header('Cache-Control: no-store');
echo sitekeeper_txt($accounts, $domain);
```

**Be honest with yourself about what this file is.** It is every credential for
that account, in plaintext, in a Downloads folder, where it will sit until the
disk is reformatted - and in whatever backup runs over that folder tonight. If
you ship this route, say so on the page ("delete this file once you have
imported it"), set `Cache-Control: no-store`, and treat it as the stepping stone
to the ticket flow rather than the destination.

The encrypted **`.mrx`** bundle avoids the plaintext problem - it is
PBKDF2-HMAC-SHA256 over a passphrase you choose, then Fernet - but it trades it
for a worse one: you now have to get a passphrase to the customer over some
channel, and the channel everyone reaches for is the same page or the same email
that carries the file. Use `.mrx` for a customer migrating their own machines.
Do not use it as a provisioning mechanism.

---

## Security: what we will not accept

The one-click flow exists because the alternative - customers pasting
credentials - is worse. It stops being worth it the moment a link can do
something the customer did not ask for. So:

**A confirmation dialog always appears.** Every provisioning, every time, naming
the host and listing what will be added. There is no "always trust this
provider", no allowlist you can get onto, and no silent mode. If your onboarding
flow depends on the customer not seeing a dialog, this integration is not the
one you want.

**Never put a credential in the `sitekeeper://` link.** It is not a transport, it
is a claim check. A URL passed to a shell handler lands in the browser's history
and its address bar, in your access logs if the page ever reloads with it, in the
referrer header, in the clipboard the moment anyone right-clicks *Copy link
address*, and on Windows in a process command line that other processes on that
machine can read. TLS protects none of that. The endpoint fetch is the only place
credentials belong.

**Assume every link is hostile until the dialog is accepted.** Anyone can send
your customer a `sitekeeper://` link naming your panel. What stops it is that the
ticket is worthless without your backend honouring it, and your backend only
honours tickets it minted for a live session. That is why rules 1, 3 and 5 above
are not optional garnish.

Your side of the checklist:

- [ ] Tickets minted on `POST`, behind session auth and CSRF protection
- [ ] Single use, burned inside a transaction
- [ ] Expiry of ten minutes or less, enforced server-side
- [ ] Scoped to the one account the button was next to
- [ ] Revoked on password change, suspension and account deletion
- [ ] Redemptions logged with time, IP and what was handed over - and shown to the customer in their panel
- [ ] Rate limited per account and per IP
- [ ] Endpoint returns `Cache-Control: no-store` and never appears in a CDN cache
- [ ] The ticket is not in any URL that gets logged, cached, or sent as a referrer
- [ ] `environment` set to `prod` for production accounts

---

## Testing your integration

Before you have app-side support to test against, the contract is testable on its
own:

```bash
# Should return the payload exactly once.
curl -sS -H "Authorization: Bearer $TICKET" \
     https://panel.example.com/.well-known/sitekeeper/v1/provision | jq .

# Should return 401 ticket_invalid.
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TICKET" \
     https://panel.example.com/.well-known/sitekeeper/v1/provision
```

Then check the four failures that actually happen in production:

1. The customer clicks the button twice in a row. (Fresh ticket each click, or a
   clear "already used" message - never a silent nothing.)
2. The customer clicks, installs Sitekeeper, comes back and clicks again.
3. The account is suspended between minting and redemption.
4. The label, group or password contains a non-ASCII character, an apostrophe or
   a space. Percent-encode for the `.txt` route; JSON handles it for the ticket
   route.

---

## Versioning and support

The `v1` in the link and the path is the contract version, and the `version` in
the payload confirms it. A future `v2` will be a new path; `v1` keeps working.
Unknown fields in a payload are ignored rather than rejected, so a field added
later will not break a panel written today.

The app's half of this contract is one file, `mysql_runner/storage/provisioning.py`,
and it is the authority wherever this document and the code disagree. Questions
about the contract, or a field your panel needs that is not here: open an issue
on the Sitekeeper repository with the panel you are integrating and the account
shape you are trying to describe.
