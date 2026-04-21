#!/usr/bin/env bash
# Smoke test per il binary-content path di `get_vault_file`:
# inline image/audio block, metadata fallback per video/PDF/oversize.
#
# Platform: macOS (usa `say` + `afconvert` per generare il fixture M4A;
# gli altri fixture sono portabili). Su Linux sostituire il ramo M4A
# con `ffmpeg` o mettere un file M4A/MP3 a mano in tmp/smoke-fixtures/.
#
# Uso:
#
#   scripts/smoke-test-binary.sh generate   # crea fixture in ./tmp/smoke-fixtures/
#   scripts/smoke-test-binary.sh upload     # generate + PUT nel vault
#   scripts/smoke-test-binary.sh checklist  # stampa i passi Inspector + risultati attesi
#   scripts/smoke-test-binary.sh cleanup    # DELETE delle fixture dal vault
#   scripts/smoke-test-binary.sh all        # generate + upload + checklist (default)
#
# Env richieste per upload/cleanup:
#   OBSIDIAN_API_KEY   (obbligatoria)
#   OBSIDIAN_API_URL   (default https://127.0.0.1:27124)
#   VAULT_PREFIX       (default smoke-test-binary-pr2)

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly FIXTURE_DIR="${REPO_ROOT}/tmp/smoke-fixtures"
readonly API_URL="${OBSIDIAN_API_URL:-https://127.0.0.1:27124}"
readonly VAULT_PREFIX="${VAULT_PREFIX:-smoke-test-binary-pr2}"

# Fixture manifest: "<local-filename>|<vault-subpath>|<mime-note>|<expected-outcome>"
readonly FIXTURES=(
  "image-small.png|image-small.png|image/png|inline image block"
  "audio-small.m4a|audio-small.m4a|audio/mp4|inline audio block"
  "video-fake.mp4|video-fake.mp4|video/mp4|text metadata, hint=unsupported_type"
  "document-fake.pdf|document-fake.pdf|application/pdf|text metadata, hint=unsupported_type"
  "image-oversize.png|image-oversize.png|image/png (>10MiB)|text metadata, hint=too_large"
)

log()  { printf '\033[1;34m[smoke]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[smoke]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[smoke]\033[0m %s\n' "$*" >&2; }

require_env() {
  if [[ -z "${OBSIDIAN_API_KEY:-}" ]]; then
    err "OBSIDIAN_API_KEY non impostata. Esportala prima di eseguire l'upload."
    err "Es: export OBSIDIAN_API_KEY=\"\$(cat ~/.obsidian-api-key)\""
    exit 1
  fi
}

check_tools() {
  local missing=0
  for bin in curl base64 dd; do
    if ! command -v "${bin}" >/dev/null 2>&1; then
      err "Tool mancante: ${bin}"
      missing=1
    fi
  done
  if ! command -v say >/dev/null 2>&1 || ! command -v afconvert >/dev/null 2>&1; then
    warn "say/afconvert non disponibili (solo macOS). L'MP3/M4A non verrà generato."
  fi
  [[ ${missing} -eq 0 ]] || exit 1
}

ping_rest_api() {
  log "Ping Local REST API @ ${API_URL} …"
  local http_code
  http_code="$(curl -sk -o /dev/null -w '%{http_code}' \
    --connect-timeout 3 --max-time 5 \
    -H "Authorization: Bearer ${OBSIDIAN_API_KEY}" \
    "${API_URL}/")"
  if [[ "${http_code}" == "200" ]]; then
    log "  → OK (HTTP 200)"
  else
    err "  → HTTP ${http_code}. Verifica che Obsidian sia aperto e il plugin Local REST API sia attivo."
    exit 1
  fi
}

generate_fixtures() {
  log "Genero fixture in ${FIXTURE_DIR}"
  mkdir -p "${FIXTURE_DIR}"

  # PNG 1x1 nero, valido (67 byte)
  base64 -d > "${FIXTURE_DIR}/image-small.png" <<'B64'
iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8A
AAAASUVORK5CYII=
B64
  log "  ✓ image-small.png ($(wc -c < "${FIXTURE_DIR}/image-small.png" | tr -d ' ') B)"

  # M4A via afconvert (solo macOS). Se non disponibile, sostituisci manualmente.
  if command -v say >/dev/null 2>&1 && command -v afconvert >/dev/null 2>&1; then
    local tmp_aiff="${FIXTURE_DIR}/_audio.aiff"
    say -o "${tmp_aiff}" "smoke test" >/dev/null 2>&1
    afconvert -f m4af -d aac "${tmp_aiff}" "${FIXTURE_DIR}/audio-small.m4a" >/dev/null
    rm -f "${tmp_aiff}"
    log "  ✓ audio-small.m4a ($(wc -c < "${FIXTURE_DIR}/audio-small.m4a" | tr -d ' ') B)"
  else
    warn "  ! audio-small.m4a non generato (say/afconvert mancanti). Metti un file M4A/MP3 a mano in ${FIXTURE_DIR}/audio-small.m4a"
  fi

  # Video fake: il tool short-circuita sull'estensione SENZA fetchare il
  # contenuto, quindi un file vuoto con estensione giusta è sufficiente.
  : > "${FIXTURE_DIR}/video-fake.mp4"
  log "  ✓ video-fake.mp4 (0 B, contenuto irrilevante — il server non fa fetch)"

  : > "${FIXTURE_DIR}/document-fake.pdf"
  log "  ✓ document-fake.pdf (0 B, contenuto irrilevante)"

  # PNG da 11 MiB per il ramo too_large. Header PNG NON valido, ma il server
  # non decodifica — conta solo la lunghezza > MAX_INLINE_BINARY_BYTES (10 MiB).
  dd if=/dev/zero of="${FIXTURE_DIR}/image-oversize.png" bs=1048576 count=11 status=none
  log "  ✓ image-oversize.png ($(wc -c < "${FIXTURE_DIR}/image-oversize.png" | tr -d ' ') B → > 10 MiB)"
}

upload_fixtures() {
  require_env
  ping_rest_api
  log "Upload nel vault sotto /${VAULT_PREFIX}/ …"
  for entry in "${FIXTURES[@]}"; do
    local local_name="${entry%%|*}"
    local vault_path; vault_path="$(echo "${entry}" | cut -d'|' -f2)"
    local local_file="${FIXTURE_DIR}/${local_name}"
    local remote="/vault/${VAULT_PREFIX}/${vault_path}"

    if [[ ! -f "${local_file}" ]]; then
      warn "  ! skip ${local_name} (non esiste, genera prima con: $0 generate)"
      continue
    fi

    local http_code
    http_code="$(curl -sk -o /dev/null -w '%{http_code}' \
      -X PUT \
      -H "Authorization: Bearer ${OBSIDIAN_API_KEY}" \
      -H "Content-Type: application/octet-stream" \
      --data-binary "@${local_file}" \
      "${API_URL}${remote}")"
    if [[ "${http_code}" == "204" || "${http_code}" == "200" ]]; then
      log "  ✓ PUT ${remote} (HTTP ${http_code})"
    else
      err "  ✗ PUT ${remote} → HTTP ${http_code}"
    fi
  done
}

cleanup_vault() {
  require_env
  ping_rest_api
  log "Cleanup: elimino /${VAULT_PREFIX}/ dal vault …"
  for entry in "${FIXTURES[@]}"; do
    local vault_path; vault_path="$(echo "${entry}" | cut -d'|' -f2)"
    local remote="/vault/${VAULT_PREFIX}/${vault_path}"
    local http_code
    http_code="$(curl -sk -o /dev/null -w '%{http_code}' \
      -X DELETE \
      -H "Authorization: Bearer ${OBSIDIAN_API_KEY}" \
      "${API_URL}${remote}")"
    log "  DELETE ${remote} → HTTP ${http_code}"
  done
}

print_checklist() {
  cat <<EOF

============================================================
 SMOKE TEST MCP Inspector — PR #2 / issue #59 (binary content)
============================================================

PRE-REQUISITI
  ✓ Obsidian aperto con plugin "Local REST API" attivo
  ✓ OBSIDIAN_API_KEY esportata nell'ambiente del terminale
  ✓ Fixture caricati nel vault:
    - ${VAULT_PREFIX}/image-small.png
    - ${VAULT_PREFIX}/audio-small.m4a
    - ${VAULT_PREFIX}/video-fake.mp4
    - ${VAULT_PREFIX}/document-fake.pdf
    - ${VAULT_PREFIX}/image-oversize.png

AVVIO INSPECTOR
  cd packages/mcp-server
  bun run inspector
  # L'Inspector apre http://localhost:6274 (porta può variare, guarda output)

CASI DI TEST (invocare lo strumento \`get_vault_file\` nell'Inspector)

┌────┬─────────────────────────────────────────┬──────────────────────────────────────────┐
│ #  │ filename (arg)                          │ risultato atteso                         │
├────┼─────────────────────────────────────────┼──────────────────────────────────────────┤
│ 1  │ ${VAULT_PREFIX}/image-small.png         │ content[0].type = "image"                │
│    │                                         │ content[0].mimeType = "image/png"        │
│    │                                         │ content[0].data = base64 non vuoto       │
│    │                                         │ Inspector rendera l'immagine inline      │
├────┼─────────────────────────────────────────┼──────────────────────────────────────────┤
│ 2  │ ${VAULT_PREFIX}/audio-small.m4a         │ content[0].type = "audio"                │
│    │                                         │ content[0].mimeType ≈ "audio/mp4"        │
│    │                                         │ content[0].data = base64 non vuoto       │
│    │                                         │ Inspector mostra player audio (se supp.) │
├────┼─────────────────────────────────────────┼──────────────────────────────────────────┤
│ 3  │ ${VAULT_PREFIX}/video-fake.mp4          │ content[0].type = "text"                 │
│    │                                         │ text = JSON con:                         │
│    │                                         │   kind:"binary_file"                     │
│    │                                         │   mimeType:"video/mp4"                   │
│    │                                         │   hint contiene "unsupported_type"       │
│    │                                         │   (NB: nessuna fetch lato server)        │
├────┼─────────────────────────────────────────┼──────────────────────────────────────────┤
│ 4  │ ${VAULT_PREFIX}/document-fake.pdf       │ content[0].type = "text"                 │
│    │                                         │ text JSON con hint "unsupported_type"    │
│    │                                         │ mimeType = "application/pdf"             │
├────┼─────────────────────────────────────────┼──────────────────────────────────────────┤
│ 5  │ ${VAULT_PREFIX}/image-oversize.png      │ content[0].type = "text"                 │
│    │                                         │ text JSON con hint = "too_large"         │
│    │                                         │ (il server fetcha 11 MiB, poi fallback)  │
└────┴─────────────────────────────────────────┴──────────────────────────────────────────┘

CHECKLIST DA SPUNTARE (manuale)
  [ ] Caso 1 — PNG inline OK
  [ ] Caso 2 — M4A inline OK
  [ ] Caso 3 — MP4 fallback unsupported_type
  [ ] Caso 4 — PDF fallback unsupported_type
  [ ] Caso 5 — PNG oversize fallback too_large
  [ ] Nessun errore nella console dell'Inspector
  [ ] Nessun stack trace nei log del server

TEARDOWN (quando hai finito)
  $0 cleanup

EOF
}

main() {
  local cmd="${1:-all}"
  check_tools
  case "${cmd}" in
    generate)
      generate_fixtures
      ;;
    upload)
      [[ -d "${FIXTURE_DIR}" ]] || generate_fixtures
      upload_fixtures
      ;;
    checklist)
      print_checklist
      ;;
    cleanup)
      cleanup_vault
      ;;
    all)
      generate_fixtures
      upload_fixtures
      print_checklist
      ;;
    -h|--help|help)
      sed -n '2,14p' "${BASH_SOURCE[0]}"
      ;;
    *)
      err "Comando sconosciuto: ${cmd}"
      sed -n '2,14p' "${BASH_SOURCE[0]}"
      exit 1
      ;;
  esac
}

main "$@"
