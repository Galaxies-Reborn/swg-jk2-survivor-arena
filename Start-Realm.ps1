# Requires PowerShell 7 and SpacetimeDB standalone 2.10.0.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Standalone,
    [string]$StateDirectory = '.local/survivor-host',
    [ValidateRange(1024,65535)][int]$Port = 38431,
    [switch]$Background
)
$ErrorActionPreference = 'Stop'
$server = (Resolve-Path -LiteralPath $Standalone).Path
$directory = [IO.Directory]::CreateDirectory([IO.Path]::GetFullPath($StateDirectory)).FullName
$privateKey = Join-Path $directory 'signing-key.pem'
$publicKey = Join-Path $directory 'verification-key.pem'
if (!(Test-Path -LiteralPath $privateKey) -and !(Test-Path -LiteralPath $publicKey)) {
    $signer = [Security.Cryptography.ECDsa]::Create([Security.Cryptography.ECCurve+NamedCurves]::nistP256)
    try {
        foreach ($item in @(
            @($privateKey,$signer.ExportPkcs8PrivateKeyPem()),
            @($publicKey,$signer.ExportSubjectPublicKeyInfoPem())
        )) {
            $output = [IO.File]::Open($item[0], [IO.FileMode]::CreateNew)
            try {
                $bytes = [Text.Encoding]::ASCII.GetBytes($item[1])
                $output.Write($bytes, 0, $bytes.Length)
            } finally { $output.Dispose() }
        }
    } finally { $signer.Dispose() }
}
if (!(Test-Path -LiteralPath $privateKey) -or !(Test-Path -LiteralPath $publicKey)) {
    throw 'Incomplete signing key pair. Restore the keys for this host from its backup.'
}
$serverArgs = @('start','--listen-addr',"127.0.0.1:$Port",'--data-dir',(Join-Path $directory 'db'),'--page_pool_max_size','268435456','--non-interactive','--jwt-pub-key-path',$publicKey,'--jwt-priv-key-path',$privateKey)
if ($Background) {
    $info = [Diagnostics.ProcessStartInfo]::new($server)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    foreach ($argument in $serverArgs) { $info.ArgumentList.Add($argument) }
    return [Diagnostics.Process]::Start($info)
}
& $server @serverArgs
exit $LASTEXITCODE
