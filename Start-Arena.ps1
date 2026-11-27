#Requires -Version 7
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Standalone,
    [string]$ClientRoot,
    [ValidateSet(1,2)][int[]]$Seats = @(1,2),
    [ValidateRange(1024,65535)][int]$Port = 38431,
    [string]$StateDirectory = (Join-Path $PSScriptRoot '.local/arena')
)
$ErrorActionPreference = 'Stop'
$standalonePath = (Resolve-Path -LiteralPath $Standalone).Path
$state = [IO.Path]::GetFullPath($StateDirectory)
if ($ClientRoot) { $ClientRoot = (Resolve-Path -LiteralPath $ClientRoot).Path }
$endpoint = "http://127.0.0.1:$Port"
$hostProcess = $null
Push-Location $PSScriptRoot
try {
    & cmake -S engine -B build
    if ($LASTEXITCODE) { throw 'CMake configuration failed' }
    & cmake --build build --config Release
    if ($LASTEXITCODE) { throw 'JK2 kernel build failed' }
    & cargo build --locked --release --manifest-path realm/Cargo.toml --target wasm32-unknown-unknown --target-dir target
    if ($LASTEXITCODE) { throw 'Realm build failed' }
    $kernel = Get-ChildItem -LiteralPath build -File -Recurse | Where-Object { $_.Name -in @('jk2_arena.dll','libjk2_arena.so','libjk2_arena.dylib') } | Select-Object -First 1
    if (!$kernel) { throw 'Built combat library missing' }
    $wasm = (Resolve-Path 'target/wasm32-unknown-unknown/release/survivor_realm.wasm').Path
    $probe = [Net.Sockets.TcpClient]::new()
    try { try { $probe.Connect('127.0.0.1',$Port) } catch [Net.Sockets.SocketException] {}
          if ($probe.Connected) { throw "Port $Port is already occupied" } }
    finally { $probe.Dispose() }
    $hostProcess = & ./Start-Realm.ps1 -Standalone $standalonePath -StateDirectory (Join-Path $state 'host') -Port $Port -Background
    $ready = $false
    for ($i=0; $i -lt 80; $i++) {
        if ($hostProcess.HasExited) { throw 'The arena host exited during startup' }
        try { $null = Invoke-WebRequest "$endpoint/v1/ping" -TimeoutSec 2 -NoProxy; $ready = $true; break } catch { Start-Sleep -Milliseconds 250 }
    }
    if (!$ready) { throw 'The arena host did not become ready' }
    $profiles = Join-Path $state 'profiles'
    if (!(Test-Path -LiteralPath $profiles)) {
        & python server.py provision $wasm --endpoint $endpoint --profiles $profiles
        if ($LASTEXITCODE) { throw 'Provisioning failed' }
    }
    $published = (Get-Content -Raw -LiteralPath (Join-Path $profiles 'module.sha256')).Trim()
    if ($published -ine (Get-FileHash -Algorithm SHA256 $wasm).Hash) {
        throw 'The persisted realm uses another module build. Use its original checkout or provision a fresh state directory.'
    }
    $worker = Join-Path $profiles 'worker.json'
    if ((Get-Content -Raw -LiteralPath $worker | ConvertFrom-Json).endpoint -cne $endpoint) {
        throw "The existing realm profiles belong to another endpoint. Use the original port or a fresh state directory."
    }
    if ($ClientRoot) {
        foreach ($seat in $Seats) {
            & python install.py $ClientRoot (Join-Path $profiles "player-$seat.json")
            if ($LASTEXITCODE) { throw 'Client installation failed' }
        }
    }
    Write-Host 'Survivor Arena is ready. Start the compatible Bevy client and choose Custom, then Survivor Arena.'
    & python server.py serve --profile $worker --kernel $kernel.FullName
    if ($LASTEXITCODE) { throw 'Arena worker stopped' }
} finally {
    if ($hostProcess -and !$hostProcess.HasExited) { $hostProcess.Kill(); $hostProcess.WaitForExit() }
    Pop-Location
}
