#!/usr/bin/env bash
# Installateur unique ActionFlow pour les ordinateurs de Frederic.
# Double-cliquer ce fichier sur macOS, ou lancer :
#   bash Installer-ActionFlow-Frederic.command

set -Eeuo pipefail
umask 077

EXPECTED_CLICKUP_ID="112364942"
ACTIONFLOW_ORG="ActionFlowTech"
PROJECTS_ROOT="${HOME}/ActionFlowTech"
PROJECTS=(
  "crm-monday-jb"
  "classement-alexis-page"
  "assistant-direction-gabriel-gauthier"
)

BLUE=$'\033[1;34m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
RED=$'\033[0;31m'
RESET=$'\033[0m'

pause_before_close() {
  local status=$?
  printf '\n'
  if (( status == 0 )); then
    printf '%sInstallation terminee.%s\n' "$GREEN" "$RESET"
  else
    printf '%sInstallation arretee avant la fin.%s\n' "$RED" "$RESET"
  fi
  read -r -p "Appuie sur Entree pour fermer cette fenetre... " _ || true
  return "$status"
}
trap pause_before_close EXIT

section() {
  printf '\n%s=== %s ===%s\n' "$BLUE" "$1" "$RESET"
}

stop() {
  printf '\n%sERREUR :%s %s\n' "$RED" "$RESET" "$1" >&2
  exit 1
}

section "Installation ActionFlow pour Frederic"
printf '%s\n' \
  "Ce fichier va :" \
  "  1. verifier les outils requis;" \
  "  2. confirmer le compte GitHub de Frederic;" \
  "  3. enregistrer SON jeton ClickUp localement;" \
  "  4. configurer Claude Code;" \
  "  5. cloner seulement les projets deja autorises."
printf '\nIl ne supprime aucun fichier et ne place aucun secret dans GitHub.\n'
read -r -p "Continuer? [O/n] " answer
case "${answer:-O}" in
  O|o|Y|y) ;;
  *) exit 0 ;;
esac

section "1/5 - Outils requis"
missing=()
for command_name in git gh python3 curl claude; do
  if command -v "$command_name" >/dev/null 2>&1; then
    printf '  %sOK%s  %s\n' "$GREEN" "$RESET" "$command_name"
  else
    printf '  %sMANQUANT%s  %s\n' "$RED" "$RESET" "$command_name"
    missing+=("$command_name")
  fi
done
if (( ${#missing[@]} > 0 )); then
  stop "Installe d'abord les outils manquants ci-dessus, puis relance ce fichier."
fi

section "2/5 - Identite GitHub"
if ! gh auth status --hostname github.com >/dev/null 2>&1; then
  printf 'GitHub CLI va ouvrir la connexion dans le navigateur.\n'
  gh auth login --hostname github.com --git-protocol https --web
fi

github_login="$(gh api user --jq '.login')"
case "$github_login" in
  martinfrederick544-bit)
    printf '  %sOK%s  compte GitHub : %s\n' "$GREEN" "$RESET" "$github_login"
    ;;
  Actionflow9841)
    printf '  %sATTENTION%s  compte GitHub : %s\n' "$YELLOW" "$RESET" "$github_login"
    printf '  Ce compte doit accepter son invitation Gabriel avant de pouvoir cloner ce projet.\n'
    ;;
  *)
    stop "Le compte GitHub actif ($github_login) n'est pas un compte Frederic enregistre."
    ;;
esac

section "3/5 - Identite ClickUp"
printf '%s\n' \
  "Frederic doit generer son propre jeton :" \
  "ClickUp > avatar > Settings > Apps > API Token > Generate." \
  "La saisie ci-dessous est masquee. Rien ne s'affichera a l'ecran."
printf '\nJeton ClickUp de Frederic : '
IFS= read -rs clickup_token
printf '\n'

[[ -n "$clickup_token" ]] || stop "Aucun jeton ClickUp saisi."
[[ "$clickup_token" != *$'\n'* && "$clickup_token" != *$'\r'* ]] \
  || stop "Le jeton contient un caractere invalide."

identity_json="$(curl --fail --silent --show-error \
  --header "Authorization: ${clickup_token}" \
  'https://api.clickup.com/api/v2/user')" \
  || stop "Le jeton ClickUp ne repond pas."

observed_clickup_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("user",{}).get("id",""))' <<<"$identity_json")"
observed_clickup_name="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("user",{}).get("username",""))' <<<"$identity_json")"
unset identity_json

if [[ "$observed_clickup_id" != "$EXPECTED_CLICKUP_ID" ]]; then
  unset clickup_token
  stop "Ce jeton n'appartient pas au compte ClickUp Frederick Martin attendu."
fi
printf '  %sOK%s  compte ClickUp : %s (ID %s)\n' \
  "$GREEN" "$RESET" "$observed_clickup_name" "$observed_clickup_id"

actionflow_config_dir="${HOME}/.config/actionflow"
actionflow_env="${actionflow_config_dir}/env"
mkdir -p "$actionflow_config_dir"
printf 'export CLICKUP_API_TOKEN=%q\n' "$clickup_token" > "$actionflow_env"
chmod 600 "$actionflow_env"
export CLICKUP_API_TOKEN="$clickup_token"
unset clickup_token

shell_profile="${HOME}/.zshrc"
touch "$shell_profile"
source_line='[ -f "$HOME/.config/actionflow/env" ] && source "$HOME/.config/actionflow/env"'
if ! grep -Fq "$source_line" "$shell_profile"; then
  printf '\n# ActionFlow - jeton ClickUp local\n%s\n' "$source_line" >> "$shell_profile"
fi
printf '  Jeton conserve localement avec permissions privees dans ~/.config/actionflow/env\n'

section "4/5 - Configuration Claude Code"
claude_dir="${HOME}/.claude"
claude_settings="${claude_dir}/settings.json"
claude_global="${claude_dir}/CLAUDE.md"
mkdir -p "$claude_dir"

if [[ ! -f "$claude_settings" ]]; then
  printf '{}\n' > "$claude_settings"
fi

python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$claude_settings" \
  || stop "~/.claude/settings.json existe, mais son JSON est invalide. Rien n'a ete remplace."

backup_stamp="$(date '+%Y%m%d-%H%M%S')"
cp "$claude_settings" "${claude_settings}.avant-actionflow-${backup_stamp}"

python3 - "$claude_settings" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    settings = json.load(handle)

command = "bash .actionflow/etat.sh 2>/dev/null || true"
hooks = settings.setdefault("hooks", {})
session_start = hooks.setdefault("SessionStart", [])

if not any(command in json.dumps(item) for item in session_start):
    session_start.append(
        {"hooks": [{"type": "command", "command": command, "timeout": 15000}]}
    )

with open(path, "w", encoding="utf-8") as handle:
    json.dump(settings, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
PY

doctrine_marker="ACTIONFLOW_SHARED_PROJECT_DOCTRINE_V1"
if ! grep -Fq "$doctrine_marker" "$claude_global" 2>/dev/null; then
  cat >> "$claude_global" <<'EOF'

<!-- ACTIONFLOW_SHARED_PROJECT_DOCTRINE_V1 -->
# Projets clients ActionFlow

Pour tout projet client ActionFlow :

- GitHub est le wiki technique partage et assaini du projet.
- ClickUp est le registre operationnel : etat, responsable, priorite, blocage, dependance et prochaine action.
- Obsidian est la memoire privee de Luca et ne doit jamais etre copie integralement dans GitHub ou ClickUp.
- Un projet ActionFlow se reconnait a `.clickup.json`; au demarrage, executer `.actionflow/etat.sh`.
- Au DEMARRAGE : passer la phase a `en cours`, s'assigner et commenter.
- En BLOCAGE : passer a `en attente client`, remplir `Bloque par` et commenter les faits utiles.
- En LIVRAISON : passer a `a reviser`, assigner Luca et commenter le livrable.
- En fin de session : ajouter un commentaire `AVANCEMENT` avec ce qui avance, ce qui reste et la prochaine action.
- Apres toute ecriture ClickUp, relire l'objet et verifier le resultat.
- Ne jamais inventer une echeance, une priorite, une assignation ou un fait client.
- Ne jamais copier de jeton, mot de passe, courriel brut, transcription brute, prix, paiement ou information financiere dans GitHub ou ClickUp.
- Ne travailler que dans les projets auxquels le compte courant a explicitement acces.
EOF
fi
printf '  %sOK%s  hook SessionStart et doctrine ActionFlow installes.\n' "$GREEN" "$RESET"
printf '  Sauvegarde : %s\n' "${claude_settings}.avant-actionflow-${backup_stamp}"

section "5/5 - Projets GitHub autorises"
mkdir -p "$PROJECTS_ROOT"
successful_projects=()
failed_projects=()

for project in "${PROJECTS[@]}"; do
  destination="${PROJECTS_ROOT}/${project}"
  if [[ -d "${destination}/.git" ]]; then
    if [[ -n "$(git -C "$destination" status --porcelain)" ]]; then
      printf '  %sCONSERVE%s  %s contient des changements locaux; aucun pull automatique.\n' \
        "$YELLOW" "$RESET" "$project"
    elif git -C "$destination" pull --ff-only >/dev/null; then
      printf '  %sMIS A JOUR%s  %s\n' "$GREEN" "$RESET" "$project"
    else
      printf '  %sECHEC PULL%s  %s\n' "$YELLOW" "$RESET" "$project"
    fi
    successful_projects+=("$project")
  elif gh repo clone "${ACTIONFLOW_ORG}/${project}" "$destination" >/dev/null; then
    printf '  %sCLONE%s  %s\n' "$GREEN" "$RESET" "$project"
    successful_projects+=("$project")
  else
    printf '  %sNON ACCESSIBLE%s  %s\n' "$YELLOW" "$RESET" "$project"
    failed_projects+=("$project")
  fi
done

gabriel_dir="${PROJECTS_ROOT}/assistant-direction-gabriel-gauthier"
if [[ -x "${gabriel_dir}/.actionflow/etat.sh" ]]; then
  printf '\nLecture de controle du projet Gabriel :\n'
  (
    cd "$gabriel_dir"
    bash .actionflow/etat.sh
  ) || printf '  %sATTENTION%s  La lecture ClickUp Gabriel doit etre verifiee manuellement.\n' \
    "$YELLOW" "$RESET"
fi

printf '\n%sConfiguration terminee.%s\n' "$GREEN" "$RESET"
printf 'Les projets sont dans : %s\n' "$PROJECTS_ROOT"
if (( ${#failed_projects[@]} > 0 )); then
  printf '%sAcces GitHub a regler :%s %s\n' "$YELLOW" "$RESET" "${failed_projects[*]}"
fi
printf '\nSur ce Mac, Claude Code doit toujours etre lance depuis la racine du projet concerne.\n'

if [[ -d "$gabriel_dir" ]]; then
  read -r -p "Ouvrir Claude Code dans le projet Gabriel maintenant? [O/n] " launch_claude
  case "${launch_claude:-O}" in
    O|o|Y|y)
      trap - EXIT
      cd "$gabriel_dir"
      exec claude
      ;;
  esac
fi
