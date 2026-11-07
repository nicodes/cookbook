# Select a compatible runner-owned control-plane Python without installing one.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion -lt [Version]'7.3') { throw 'Native bootstrap argument transport requires PowerShell 7.3 or newer' }
$PSNativeCommandArgumentPassing = 'Standard'
if ($env:RUNNER_OS -ne 'Windows') { throw 'Windows bootstrap host selection requires the native Windows runner' }
$architecture = switch ($env:RUNNER_ARCH) {
    'X64' { 'x64' }
    'ARM64' { 'arm64' }
    default { throw 'Unsupported native Windows bootstrap architecture' }
}
if ([string]::IsNullOrWhiteSpace($env:RUNNER_TOOL_CACHE) -or
    -not [IO.Path]::IsPathFullyQualified($env:RUNNER_TOOL_CACHE)) {
    throw 'Windows bootstrap requires the trusted absolute runner tool cache'
}
$cache = [IO.Path]::GetFullPath($env:RUNNER_TOOL_CACHE)
if ($cache -match '[\r\n\x00]') { throw 'Invalid runner tool cache path' }
if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_WORKSPACE)) {
    $workspace = [IO.Path]::GetFullPath($env:GITHUB_WORKSPACE).TrimEnd('\', '/')
    if ($cache.Equals($workspace, [StringComparison]::OrdinalIgnoreCase) -or
        $cache.StartsWith($workspace + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'A project directory cannot provide the bootstrap host interpreter'
    }
}
$probe = @'
import json,os,sys,sysconfig
expected=sys.argv[1]
print(json.dumps({"executable":sys.executable,"version":list(sys.version_info[:3]),
 "implementation":sys.implementation.name,"platform":sys.platform,"target":sysconfig.get_platform(),"isolated":sys.flags.isolated,
 "no_bytecode":sys.dont_write_bytecode,"same_file":os.path.samefile(sys.executable,expected)}))
'@
$selected = $null
$directory = Join-Path $cache 'Python'
if (Test-Path -LiteralPath $directory -PathType Container) {
    if ((Get-Item -LiteralPath $directory -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Runner Python cache must not be an installation link'
    }
    $versions = @(Get-ChildItem -LiteralPath $directory -Directory)
    if ($versions.Count -gt 64) { throw 'Runner Python cache inventory exceeds its bound' }
    $candidates = @($versions | ForEach-Object {
        $version = $null
        if ([Version]::TryParse($_.Name, [ref]$version) -and $version -ge [Version]'3.13.0') {
            [PSCustomObject]@{ directory = $_; version = $version }
        }
    } | Sort-Object -Property version -Descending)
    foreach ($entry in $candidates) {
        $installation = Join-Path $entry.directory.FullName $architecture
        $marker = $installation + '.complete'
        $candidate = Join-Path $installation 'python.exe'
        if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or
            -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        foreach ($path in @($entry.directory.FullName, $installation, $marker, $candidate)) {
            if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Runner bootstrap interpreter must not traverse an installation link'
            }
        }
        $raw = & $candidate '-I' '-B' '-c' $probe $candidate 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Existing runner Python failed its isolated capability probe' }
        $record = ($raw -join "`n") | ConvertFrom-Json
        $expectedTarget = if ($architecture -eq 'arm64') { 'win-arm64' } else { 'win-amd64' }
        if ($record.implementation -ne 'cpython' -or $record.platform -ne 'win32' -or $record.target -ne $expectedTarget -or
            $record.isolated -ne 1 -or $record.no_bytecode -ne $true -or $record.same_file -ne $true -or
            ($record.version -join '.') -ne $entry.version.ToString(3)) {
            throw 'Runner bootstrap interpreter identity, version, or native architecture differs'
        }
        $selected = [PSCustomObject]@{ found = $true; path = $candidate; native = $record }
        break
    }
}
if ($null -eq $selected) { $selected = [PSCustomObject]@{ found = $false; path = '' } }
$lines = @("found=$($selected.found.ToString().ToLowerInvariant())", "python-path=$($selected.path)")
if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_OUTPUT)) {
    [IO.File]::AppendAllText($env:GITHUB_OUTPUT, ($lines -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
}
$selected | ConvertTo-Json -Depth 5 -Compress
