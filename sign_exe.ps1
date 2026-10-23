<#
.SYNOPSIS
  Removes the "downloaded from the internet" mark from the app and digitally signs it.

.DESCRIPTION
  Run from the project folder, in a normal (non-admin) PowerShell:

    powershell -ExecutionPolicy Bypass -File .\sign_exe.ps1                      # self-signed certificate, trusted on THIS PC only
    powershell -ExecutionPolicy Bypass -File .\sign_exe.ps1 -Pfx C:\keys\mycert.pfx   # a real certificate you bought (.pfx file)
    powershell -ExecutionPolicy Bypass -File .\sign_exe.ps1 -Thumbprint 1A2B...      # certificate already in your Windows store
                                                                                     # (hardware token / cloud-signing client)
  What each step does
    1. Unblock-File     deletes the Mark-of-the-Web. THIS is what makes Windows ask "Do you want to run this file?"
                        every time you open a file that came from a download.
    2. Certificate      a self-signed certificate is created ONCE on this PC (the private key never leaves it and is
                        not exportable) and trusted for the current user. Windows then shows a valid signature here,
                        but other PCs will NOT trust it. For other people you need a certificate from a public CA.
    3. Sign + verify    signs with SHA-256 plus a timestamp, then prints the result.
#>
param(
    [string]$Exe = (Join-Path $PSScriptRoot 'dist\Appotech AX206 LCD Screen Manager.exe'),
    [string]$Pfx,
    [string]$Thumbprint,
    [string]$TimestampUrl = 'http://timestamp.digicert.com'
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Exe)) { throw "File not found: $Exe" }

# 1) Mark-of-the-Web
Unblock-File -LiteralPath $Exe
Write-Host "Unblocked: $Exe"

# 2) Certificate
if ($Pfx) {
    $password = Read-Host -AsSecureString "Password for $Pfx"
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($Pfx, $password)
}
elseif ($Thumbprint) {
    $cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My |
        Where-Object { $_.Thumbprint -eq $Thumbprint } | Select-Object -First 1
    if (-not $cert) { throw "No certificate with thumbprint $Thumbprint was found in your Windows certificate store." }
}
else {
    $subject = 'CN=AX206 LCD Screen Manager (self-signed)'
    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Subject -eq $subject -and $_.NotAfter -gt (Get-Date).AddDays(30) } | Select-Object -First 1
    if (-not $cert) {
        Write-Host "Creating a self-signed code-signing certificate (valid 3 years, private key stays on this PC)..."
        $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject -KeyAlgorithm RSA -KeyLength 3072 `
            -HashAlgorithm SHA256 -KeyExportPolicy NonExportable -CertStoreLocation Cert:\CurrentUser\My `
            -NotAfter (Get-Date).AddYears(3)
    }
    # Trust the PUBLIC part for the current user. Windows may show one confirmation dialog for the Root store: choose Yes.
    $public = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(, $cert.RawData)
    foreach ($storeName in 'TrustedPublisher', 'Root') {
        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($storeName, 'CurrentUser')
        $store.Open('ReadWrite')
        if (-not ($store.Certificates | Where-Object { $_.Thumbprint -eq $cert.Thumbprint })) { $store.Add($public) }
        $store.Close()
    }
}

# 3) Sign (with a timestamp so the signature stays valid after the certificate expires) and verify
try {
    $null = Set-AuthenticodeSignature -FilePath $Exe -Certificate $cert -HashAlgorithm SHA256 -TimestampServer $TimestampUrl
}
catch {
    Write-Warning "Could not reach the timestamp server ($TimestampUrl). Signing WITHOUT a timestamp."
    $null = Set-AuthenticodeSignature -FilePath $Exe -Certificate $cert -HashAlgorithm SHA256
}

$result = Get-AuthenticodeSignature -FilePath $Exe
$result | Format-List Status, StatusMessage, @{ n = 'Signer'; e = { $_.SignerCertificate.Subject } },
    @{ n = 'Timestamped by'; e = { $_.TimeStamperCertificate.Subject } }
if ($result.Status -eq 'Valid') { Write-Host "Done. Windows reports a VALID signature." -ForegroundColor Green }
else { Write-Warning "Signature status: $($result.Status). See the message above." }
