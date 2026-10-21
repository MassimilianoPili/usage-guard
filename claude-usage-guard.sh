#!/usr/bin/env bash
# -----------------------------------------------------------------------------
#  claude-usage-guard.sh
# -----------------------------------------------------------------------------
#  Avvia usage_guard.py, che fa tutto: Claude Code, Codex e Copilot, ciascuno
#  attivabile, con il proprio limite e il proprio blocco. Senza argomenti apre
#  il widget floating. Elenco completo: --help.
#
#  claude-usage-guard.sh                      widget (= --widget)
#  claude-usage-guard.sh --loop [provider]    loop nel terminale
#  claude-usage-guard.sh --once [provider]    un poll solo
#  claude-usage-guard.sh --block [provider]   blocco a mano, fino a --unblock
#  claude-usage-guard.sh --pause|--resume [sessione ...]
#  claude-usage-guard.sh --status|--unblock|--config|--help
# -----------------------------------------------------------------------------
set -euo pipefail

here="$(dirname "$(cygpath -ua "${BASH_SOURCE[0]}")")"
py="$(command -v python)" || { echo "Errore: python non trovato nel PATH" >&2; exit 1; }
# -u: senza buffer, le domande di --config compaiono prima della risposta
exec "$py" -u "$(cygpath -w "$here/usage_guard.py")" "$@"
