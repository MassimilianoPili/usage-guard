<#
release.ps1 <versione>: rilascia usage-guard. Porta __version__ di usage_guard.py alla versione
(se serve, con un commit "Release vX.Y.Z"), crea il tag annotato vX.Y.Z e pusha main e il tag.
Lo zip della release e il suo sha256 li crea il workflow .github/workflows/release.yml sul tag.

Si ferma, senza toccare niente, se non si e' su main, se il working tree non e' pulito, se main
non e' allineato a origin/main o se il tag esiste gia'.

Esempio: .\release.ps1 1.0.1
#>
param([Parameter(Mandatory)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Invoke-Git {
    git @args
    if ($LASTEXITCODE) { throw "git $($args -join ' ') fallito (exit $LASTEXITCODE)" }
}

$tag = "v$Version"
if ((git branch --show-current) -ne 'main') { throw 'Serve il branch main.' }
if (git status --porcelain) { throw 'Working tree non pulito: committa o metti da parte prima.' }
Invoke-Git fetch -q origin
if ((git rev-parse HEAD) -ne (git rev-parse origin/main)) { throw 'main non e'' allineato a origin/main.' }
if (git tag --list $tag) { throw "Il tag $tag esiste gia'." }

$file = Join-Path $PSScriptRoot 'usage_guard.py'
$text = [IO.File]::ReadAllText($file)
$pattern = '(?m)^__version__ = "[^"]+"'
if (-not [regex]::IsMatch($text, $pattern)) { throw '__version__ non trovato in usage_guard.py.' }
$new = [regex]::Replace($text, $pattern, "__version__ = `"$Version`"")
if ($new -ne $text) {
    # UTF-8 senza BOM, a capo come prima: il file cambia solo nella riga della versione
    [IO.File]::WriteAllText($file, $new, [Text.UTF8Encoding]::new($false))
    Invoke-Git commit -q -m "Release $tag" -- usage_guard.py
}
Invoke-Git tag -a $tag -m $tag
Invoke-Git push origin main:main
Invoke-Git push origin "refs/tags/${tag}:refs/tags/$tag"
Write-Host "Pushato ${tag}: la release la crea il workflow, su https://github.com/MassimilianoPili/usage-guard/actions"
