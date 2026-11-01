# RECIPE: release di usage-guard

## Scopo

Pubblicare una versione nuova: `__version__` aggiornato, tag `vX.Y.Z` su GitHub, release con
`usage-guard.zip` e `usage-guard.zip.sha256` creata dal workflow. Le copie installate la prendono
dal widget (scheda Aggiornamenti o menu, tasto destro) o con `--install`.

## Precondizioni

- Branch `main`, working tree pulito (anche niente file non tracciati), allineato a `origin/main`.
  `release.ps1` si ferma da solo se non e' cosi'.
- Le modifiche della release gia' committate e pushate.
- Credenziali git per github.com (Git Credential Manager) e `gh` loggato come MassimilianoPili.
- PowerShell 7 (`pwsh`), non Windows PowerShell 5.1.

## Passi

1. Dalla cartella del repo, in PowerShell 7:
   ```powershell
   & .\release.ps1 1.2.3
   ```
   Porta `__version__` a 1.2.3 con un commit "Release v1.2.3", crea il tag annotato, pusha main e il tag.
2. Aspetta il workflow `release` su https://github.com/MassimilianoPili/usage-guard/actions:
   ```bash
   gh run list --repo MassimilianoPili/usage-guard --workflow release.yml --limit 1
   gh run watch <id> --repo MassimilianoPili/usage-guard --exit-status
   ```
3. Controlla la release e il redirect che legge l'updater:
   ```bash
   gh release view v1.2.3 --repo MassimilianoPili/usage-guard
   curl -sSI https://github.com/MassimilianoPili/usage-guard/releases/latest | grep -i '^location'
   ```

## Gotcha noti

- **Windows PowerShell 5.1 non esegue lo script**: `powershell -File release.ps1` da bash fallisce con
  "running scripts is disabled on this system" (execution policy). Usare `pwsh` o un prompt di
  PowerShell 7. Non usare `-ExecutionPolicy Bypass`: e' una stringa che l'EDR segnala.
  (2026-11-01, v1.1.1)
- **Una pipe nasconde l'errore**: in `cmd && ... | tail && ...` la catena prende l'exit di `tail`,
  non del comando. Il primo tentativo di v1.1.1 e' andato avanti dopo il fallimento dello script.
  Lanciare `release.ps1` da solo e guardarne l'exit prima del passo dopo. (2026-11-01)
- **`"$tag:"` in una stringa PowerShell** si legge come variabile con scope: scrivere `"${tag}:"`.
  Corretto in `release.ps1`. (2026-10-31)
- **Il workflow rifiuta il tag** se `__version__` non combacia: succede solo se il tag si crea a mano
  senza `release.ps1`.
- **git colora l'output anche in pipe** (`color.ui=always` globale): per controllare l'output di git
  usare `git -c color.ui=never ...`.

## Verifica finale

- Il workflow e' `success` e la release ha `usage-guard.zip` e `usage-guard.zip.sha256`.
- `Location` di `releases/latest` punta a `/releases/tag/v1.2.3`.
- Nella copia installata, dopo l'aggiornamento: `python %LOCALAPPDATA%\usage-guard\usage_guard.py --version`
  dice v1.2.3, e il log ha "installata v1.2.3" e "riavvio del widget: da ... a v1.2.3".
