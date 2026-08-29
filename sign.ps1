<#
    Authenticode signing for the build scripts.

    "Unknown publisher" in the UAC prompt and the SmartScreen warning is *not*
    read from the version resource. Windows reads it from the file's digital
    signature and nowhere else, so CompanyName can say whatever it likes and
    the installer still comes up unsigned. Only a code-signing certificate
    changes it, and once there is one this is where it gets used.

    Nothing here is required to build. With no certificate configured both
    build scripts say so once and carry on producing exactly what they produce
    today - an unsigned exe - so a machine without the certificate is never
    blocked from building.

    Configure it with environment variables, so the certificate itself never
    goes anywhere near the repository:

        SITEKEEPER_SIGN_THUMBPRINT  preferred: the SHA-1 thumbprint of a
                                    certificate already in the certificate
                                    store. This is what a hardware token or a
                                    cloud signing service gives you, and since
                                    June 2023 an OV/EV code-signing key is
                                    required to live on one of those - a .pfx
                                    on disk is no longer issued.
        SITEKEEPER_SIGN_PFX         a .pfx file. Note that a self-signed one
                                    will fail the verification below unless its
                                    root is trusted on this machine - and if it
                                    is only trusted here, it still says
                                    "Unknown publisher" everywhere else, which
                                    is the whole problem. Only a certificate
                                    from a public CA fixes that.
        SITEKEEPER_SIGN_PASS        password for that .pfx, if it has one.
        SITEKEEPER_SIGN_TIMESTAMP   RFC 3161 timestamp server. Defaults below.

    Timestamping is not optional in practice: an untimestamped signature stops
    being trusted the day the certificate expires, whereas a timestamped one
    stays valid for the life of the binary.
#>

$script:SignToldWhy = $false

function Get-SignTool {
    <# The newest x64 signtool.exe from the Windows SDK, or from PATH. #>
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $found = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Recurse `
                -Filter signtool.exe -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -match '\\x64\\' } |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Invoke-CodeSign {
    <#
        Sign one file if a certificate is configured; otherwise say why, once,
        and return $false. Never throws for "no certificate" - that is the
        normal state of a machine that only builds. It does throw when signing
        was asked for and failed, because a half-signed release is worse than
        an honestly unsigned one.
    #>
    param([Parameter(Mandatory)][string]$Path)

    $thumb = $env:SITEKEEPER_SIGN_THUMBPRINT
    $pfx   = $env:SITEKEEPER_SIGN_PFX
    if (-not $thumb -and -not $pfx) {
        if (-not $script:SignToldWhy) {
            Write-Host "Not signed: no certificate configured, so Windows will say" -ForegroundColor DarkYellow
            Write-Host "  'Unknown publisher'. Set SITEKEEPER_SIGN_THUMBPRINT (or" -ForegroundColor DarkYellow
            Write-Host "  SITEKEEPER_SIGN_PFX) to sign. See sign.ps1." -ForegroundColor DarkYellow
            $script:SignToldWhy = $true
        }
        return $false
    }

    $signtool = Get-SignTool
    if (-not $signtool) {
        throw "A signing certificate is configured but signtool.exe was not found. Install the Windows SDK."
    }

    $stamp = $env:SITEKEEPER_SIGN_TIMESTAMP
    if (-not $stamp) { $stamp = "http://timestamp.digicert.com" }

    # SHA-256 throughout: SHA-1 signatures are no longer accepted by Windows.
    $args = @("sign", "/fd", "SHA256", "/tr", $stamp, "/td", "SHA256", "/v")
    if ($thumb) {
        $args += @("/sha1", $thumb)
    } else {
        if (-not (Test-Path $pfx)) { throw "SITEKEEPER_SIGN_PFX does not exist: $pfx" }
        $args += @("/f", $pfx)
        if ($env:SITEKEEPER_SIGN_PASS) { $args += @("/p", $env:SITEKEEPER_SIGN_PASS) }
    }
    $args += $Path

    Write-Host "Signing $(Split-Path $Path -Leaf) ..."
    & $signtool @args | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "signtool failed ($LASTEXITCODE) on $Path" }

    # Verify rather than trust the exit code: /pa checks it against the policy
    # Windows itself uses for an executable, which is the question that matters.
    # A signature can apply cleanly and still be worthless - an untrusted chain
    # is exactly the "Unknown publisher" case we are trying to leave behind - so
    # this failing has to stop the build rather than ship a false reassurance.
    & $signtool verify /pa /v $Path | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw ("The signature on $Path applied but did not verify against the " +
               "policy Windows uses for executables. A self-signed or internal " +
               "certificate fails here unless its root is trusted on this " +
               "machine - and trusting it here does nothing for anyone else, " +
               "who will still see 'Unknown publisher'. Only a certificate " +
               "issued by a public CA removes that.")
    }

    $sig = Get-AuthenticodeSignature $Path
    Write-Host ("Signed: {0}" -f $sig.SignerCertificate.Subject) -ForegroundColor Green
    return $true
}
