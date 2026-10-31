#!/usr/bin/env bash
# Interactive setup wizard for the Codex-Claude agent orchestration.
#
#   ./setup.sh [PROJECT_PATH]
#
# Walks through: checks, project, providers, install, collector, billing attestation,
# the optional Codex orchestrator profile, and a summary. It needs a real terminal,
# because the billing attestation is a human step that must never be automated.
#
# Written for bash 3.2 (macOS): no associative arrays, no case-conversion expansions,
# no mapfile or readarray. Only standard tools plus tput/stty and python3.
#
# Test hook (no terminal needed):
#   ./setup.sh --print-provider-routing CODEX CLAUDE MODELS BRIDGE
# where CODEX, CLAUDE and BRIDGE are "on" or "off" and MODELS is a comma-separated
# list of Claude model names, or "-" for none. It prints provider-routing.json.

# Never let Python write bytecode into the project (it would dirty managed paths).
export PYTHONDONTWRITEBYTECODE=1
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
SETUP_VERSION=1
CLAUDE_MODELS_ALL="claude-opus-5-5 claude-sonnet-5-5 claude-haiku-5-5"
STEP_NAMES=("Checks" "Project" "Providers" "Install" "Collector" "Billing" "Profile" "Done")
STEP_TOTAL=8

# ---------------------------------------------------------------------------
# provider-routing.json (the closed key set the router validates)
# ---------------------------------------------------------------------------

# routing_json CODEX CLAUDE MODELS_CSV BRIDGE -> prints the JSON, returns 1 on bad input.
routing_json() {
  local codex=$1 claude=$2 models=$3 bridge=$4 first m out
  case "$codex" in on|off) ;; *) echo "codex must be on or off" >&2; return 1;; esac
  case "$claude" in on|off) ;; *) echo "claude must be on or off" >&2; return 1;; esac
  case "$bridge" in on|off) ;; *) echo "bridge must be on or off" >&2; return 1;; esac
  if [ "$codex" = off ] && [ "$claude" = off ]; then
    echo "at least one provider must be enabled" >&2; return 1
  fi
  if [ "$bridge" = on ] && [ "$claude" = off ]; then
    echo "bridging to Claude needs Claude children enabled" >&2; return 1
  fi
  out='{
  "version": 1,
  "providers": {
    "codex": {"enabled": '
  if [ "$codex" = on ]; then out="${out}true"; else out="${out}false"; fi
  out="${out}},
    \"claude\": {\"enabled\": "
  if [ "$claude" = on ]; then
    [ "$models" = "-" ] && models=""
    if [ -z "$models" ]; then echo "choose at least one Claude model" >&2; return 1; fi
    out="${out}true, \"models\": ["
    first=1
    local oldifs=$IFS
    IFS=,
    for m in $models; do
      IFS=$oldifs
      case "$m" in
        ''|*[!A-Za-z0-9._-]*) echo "invalid model name: $m" >&2; return 1;;
      esac
      if [ $first -eq 1 ]; then first=0; else out="${out}, "; fi
      out="${out}\"$m\""
      IFS=,
    done
    IFS=$oldifs
    out="${out}]"
  else
    out="${out}false"
  fi
  out="${out}}
  },
  \"codex_primary_may_bridge_to_claude\": "
  if [ "$bridge" = on ]; then out="${out}true"; else out="${out}false"; fi
  out="${out}
}"
  printf '%s\n' "$out"
}

# write_project_routing PROJECT CONTENT : atomic, symlink-safe write of provider-routing.json.
# Refuses a symlinked destination or any parent that is not a real directory in the project.
write_project_routing() {
  local project=$1 content=$2 part cur dest tmp
  cur=$project
  for part in .codex orchestration; do
    cur="$cur/$part"
    if [ -L "$cur" ] || [ ! -d "$cur" ]; then
      echo "refused: $part is not a real directory inside the project" >&2; return 1
    fi
  done
  if [ "$(CDPATH= cd -- "$cur" && pwd -P)" != "$(CDPATH= cd -- "$project" && pwd -P)/.codex/orchestration" ]; then
    echo "refused: the configuration directory resolves outside the project" >&2; return 1
  fi
  dest="$cur/provider-routing.json"
  if [ -L "$dest" ] || { [ -e "$dest" ] && [ ! -f "$dest" ]; }; then
    echo "refused: provider-routing.json is a symlink or not a regular file" >&2; return 1
  fi
  tmp=$(umask 022; mktemp "$cur/.provider-routing.XXXXXX") || return 1
  if printf '%s\n' "$content" >"$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$dest"; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}

usage() {
  cat <<'EOF'
Usage: ./setup.sh [PROJECT_PATH]

Interactive wizard that installs the Codex-Claude agent orchestration into a Git project.
Run it from a terminal in the root of this repository. Keys: Up/Down move, Enter selects,
Space toggles, q quits. Set NO_COLOR=1 for plain output.
EOF
}

case "${1:-}" in
  --print-provider-routing)
    if [ $# -ne 5 ]; then
      echo "usage: setup.sh --print-provider-routing CODEX CLAUDE MODELS BRIDGE" >&2; exit 2
    fi
    routing_json "$2" "$3" "$4" "$5"; exit $?;;
  --write-provider-routing)
    if [ $# -ne 6 ]; then
      echo "usage: setup.sh --write-provider-routing PROJECT CODEX CLAUDE MODELS BRIDGE" >&2; exit 2
    fi
    content=$(routing_json "$3" "$4" "$5" "$6") || exit 1
    write_project_routing "$2" "$content"; exit $?;;
  -h|--help) usage; exit 0;;
esac

# ---------------------------------------------------------------------------
# This wizard is a human step: refuse without a terminal.
# ---------------------------------------------------------------------------
if [ ! -t 0 ] || [ ! -t 1 ]; then
  echo "setup.sh is interactive and must run in a terminal (stdin and stdout)." >&2
  echo "The billing attestation is a human step, so it cannot be scripted or piped." >&2
  echo "Run ./setup.sh from a terminal window. For manual commands, see README.md (Advanced)." >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Terminal, colours, state
# ---------------------------------------------------------------------------
C_RESET="" C_BOLD="" C_DIM="" C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_MAGENTA="" C_CYAN=""
setup_colors() {
  [ -n "${NO_COLOR:-}" ] && return
  [ -z "${TERM:-}" ] || [ "${TERM:-}" = dumb ] && return
  command -v tput >/dev/null 2>&1 || return
  local n
  n=$(tput colors 2>/dev/null) || return
  [ "${n:-0}" -ge 8 ] 2>/dev/null || return
  C_RESET=$(tput sgr0 2>/dev/null)
  C_BOLD=$(tput bold 2>/dev/null)
  C_DIM=$(tput dim 2>/dev/null)
  C_RED=$(tput setaf 1 2>/dev/null)
  C_GREEN=$(tput setaf 2 2>/dev/null)
  C_YELLOW=$(tput setaf 3 2>/dev/null)
  C_BLUE=$(tput setaf 4 2>/dev/null)
  C_MAGENTA=$(tput setaf 5 2>/dev/null)
  C_CYAN=$(tput setaf 6 2>/dev/null)
}

COLS=80
LINES_N=24
term_size() {
  local c l
  c=$(tput cols 2>/dev/null) || c=""
  l=$(tput lines 2>/dev/null) || l=""
  if [ -z "$c" ]; then
    set -- $(stty size 2>/dev/null)
    l=${1:-24}; c=${2:-80}
  fi
  COLS=${c:-80}; LINES_N=${l:-24}
  [ "$COLS" -lt 30 ] 2>/dev/null && COLS=30
}

REDRAW=0
LOGDIR=""
FAILED_ANY=0
ACTIVE_PID=""
INSTALL_FAILED=0
FAIL_REASON=""
PROFILE_READY=0
CURSOR_HIDDEN=0
STEP=0
BODY=""
LIST=""
declare -a STAT NOTE
PROJECT=""
MODE="full"
PROFILE_WANTED=0
ROUTING_CONTENT=""
ROUTING_WRITE=0
INSTALL_OPTS=()
PREVIEW_FILE=""

hide_cursor() { printf '\033[?25l'; CURSOR_HIDDEN=1; }
show_cursor() { printf '\033[?25h'; CURSOR_HIDDEN=0; }

cleanup() {
  [ "$CURSOR_HIDDEN" -eq 1 ] && show_cursor
  printf '%s' "$C_RESET"
  stop_active
  [ -n "$LOGDIR" ] && [ -d "$LOGDIR" ] && rm -rf "$LOGDIR"
}
# Stop the running child's whole process group, then reap it.
stop_active() {
  local pid=$ACTIVE_PID
  [ -n "$pid" ] || return 0
  ACTIVE_PID=""
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  sleep 0.3
  kill -KILL -- "-$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  return 0
}
on_signal() { printf '\n'; show_cursor; exit 130; }
trap cleanup EXIT
trap 'REDRAW=1' WINCH
trap on_signal INT TERM

# ---------------------------------------------------------------------------
# Screen drawing
# ---------------------------------------------------------------------------
body() { # append a paragraph (plain text, wrapped to the terminal when drawn)
  if [ -n "$BODY" ]; then BODY="${BODY}

$1"; else BODY=$1; fi
}
list_add() { # list_add KIND TEXT  (KIND: ok fail warn info skip)
  local mark colour
  case "$1" in
    ok) mark="OK  "; colour=$C_GREEN;;
    fail) mark="FAIL"; colour=$C_RED;;
    warn) mark="WARN"; colour=$C_YELLOW;;
    skip) mark="SKIP"; colour=$C_YELLOW;;
    *) mark="  - "; colour=$C_CYAN;;
  esac
  LIST="${LIST}  ${colour}${C_BOLD}${mark}${C_RESET} ${2}
"
}
list_sub() { LIST="${LIST}       ${C_DIM}${1}${C_RESET}
"; }
screen_reset() { BODY=""; LIST=""; }

repeat_char() { # repeat_char CHAR COUNT
  local s="" i=0
  while [ $i -lt "$2" ]; do s="${s}$1"; i=$((i+1)); done
  printf '%s' "$s"
}

draw_header() {
  local i name line plain width filled bar
  printf '%s%s Agent orchestration setup %s' "$C_BOLD" "$C_MAGENTA" "$C_RESET"
  if [ "$STEP" -gt 0 ]; then
    printf '%s Step %d of %d%s' "$C_DIM" "$STEP" "$STEP_TOTAL" "$C_RESET"
  fi
  printf '\n'
  if [ "$STEP" -gt 0 ]; then
    plain=""; line=""
    i=1
    for name in "${STEP_NAMES[@]}"; do
      plain="${plain}  ${name}"
      if [ $i -eq "$STEP" ]; then
        line="${line}  ${C_CYAN}${C_BOLD}[${name}]${C_RESET}"
      elif [ "${STAT[$i]:-}" = ok ]; then
        line="${line}  ${C_GREEN}${name}${C_RESET}"
      elif [ "${STAT[$i]:-}" = fail ]; then
        line="${line}  ${C_RED}${name}${C_RESET}"
      elif [ "${STAT[$i]:-}" = skip ]; then
        line="${line}  ${C_YELLOW}${name}${C_RESET}"
      else
        line="${line}  ${C_DIM}${name}${C_RESET}"
      fi
      i=$((i+1))
    done
    if [ "${#plain}" -lt "$COLS" ]; then
      printf '%s\n' "$line"
    else
      printf ' %s%s%s\n' "$C_CYAN$C_BOLD" "${STEP_NAMES[$((STEP-1))]}" "$C_RESET"
    fi
    width=$((COLS - 4)); [ $width -gt 40 ] && width=40
    filled=$((STEP * width / STEP_TOTAL))
    bar="$(repeat_char '#' $filled)$(repeat_char '-' $((width - filled)))"
    printf ' %s%s%s\n' "$C_GREEN" "$bar" "$C_RESET"
  fi
  printf '%s%s%s\n' "$C_DIM" "$(repeat_char '-' $((COLS - 1)))" "$C_RESET"
}

render() {
  term_size
  printf '\033[H\033[2J'
  draw_header
  printf '\n'
  if [ -n "$BODY" ]; then
    printf '%s\n\n' "$BODY" | fold -s -w $((COLS - 4)) | sed 's/^/  /'
  fi
  [ -n "$LIST" ] && printf '%s\n' "$LIST"
}

# ---------------------------------------------------------------------------
# Keys, menus, toggles, prompts
# ---------------------------------------------------------------------------
KEY=""
read_key() {
  local k rest rc started
  KEY=""
  while :; do
    k=""
    started=$SECONDS
    IFS= read -rsn1 -t 1 k; rc=$?
    if [ $rc -ne 0 ]; then
      # A resize signal can interrupt read with any non-zero status.
      if [ "$REDRAW" -eq 1 ]; then REDRAW=0; KEY=WINCH; return; fi
      # Some bash 3.2 builds report the 1-second timeout as status 1. A failure that
      # returns at once, with no wait, means the terminal is gone.
      if [ $rc -gt 128 ] || [ "$SECONDS" -gt "$started" ]; then continue; fi
      KEY=EOF; return
    fi
    break
  done
  case "$k" in
    $'\033')
      rest=""
      IFS= read -rsn2 -t 1 rest
      case "$rest" in
        '[A'|'OA') KEY=UP;;
        '[B'|'OB') KEY=DOWN;;
        '[5') IFS= read -rsn1 -t 1 rest; KEY=PGUP;;
        '[6') IFS= read -rsn1 -t 1 rest; KEY=PGDN;;
        *) KEY=ESC;;
      esac;;
    '') KEY=ENTER;;
    ' ') KEY=SPACE;;
    *) KEY=$k;;
  esac
}

confirm_quit() {
  local ans
  printf '\n  %sQuit setup? Nothing further will change. [y/N] %s' "$C_YELLOW" "$C_RESET"
  show_cursor
  IFS= read -rsn1 ans
  hide_cursor
  case "$ans" in
    y|Y) printf '\n'; show_cursor; echo "Setup stopped. Run ./setup.sh again any time."; exit 130;;
  esac
}

MENU_IDX=0
menu() { # menu DEFAULT_INDEX ITEM...
  local sel=$1 n i it width
  shift
  local items=("$@")
  n=${#items[@]}
  hide_cursor
  while :; do
    render
    width=$((COLS - 8))
    i=0
    for it in "${items[@]}"; do
      if [ $i -eq $sel ]; then
        printf '   %s%s> %s%s\n' "$C_CYAN" "$C_BOLD" "${it:0:$width}" "$C_RESET"
      else
        printf '     %s\n' "${it:0:$width}"
      fi
      i=$((i+1))
    done
    printf '\n  %sUp/Down move   Enter select   q quit%s\n' "$C_DIM" "$C_RESET"
    read_key
    case "$KEY" in
      UP|k) sel=$(( (sel + n - 1) % n ));;
      DOWN|j) sel=$(( (sel + 1) % n ));;
      ENTER) MENU_IDX=$sel; show_cursor; return 0;;
      q|Q) confirm_quit;;
      EOF) show_cursor; echo "Input closed; stopping." >&2; exit 1;;
    esac
  done
}

TOG_STATE=""
toggles() { # toggles STATE ITEM...  (STATE like 1101; Space toggles, Enter accepts)
  local state=$1 sel=0 n i it width mark
  shift
  local items=("$@")
  n=${#items[@]}
  hide_cursor
  while :; do
    render
    width=$((COLS - 12))
    i=0
    for it in "${items[@]}"; do
      if [ "${state:$i:1}" = 1 ]; then mark="[x]"; else mark="[ ]"; fi
      if [ $i -eq $sel ]; then
        printf '   %s%s> %s %s%s\n' "$C_CYAN" "$C_BOLD" "$mark" "${it:0:$width}" "$C_RESET"
      else
        printf '     %s %s\n' "$mark" "${it:0:$width}"
      fi
      i=$((i+1))
    done
    printf '\n  %sUp/Down move   Space toggle   Enter accept   q quit%s\n' "$C_DIM" "$C_RESET"
    read_key
    case "$KEY" in
      UP|k) sel=$(( (sel + n - 1) % n ));;
      DOWN|j) sel=$(( (sel + 1) % n ));;
      SPACE)
        if [ "${state:$sel:1}" = 1 ]; then
          state="${state:0:$sel}0${state:$((sel+1))}"
        else
          state="${state:0:$sel}1${state:$((sel+1))}"
        fi;;
      ENTER) TOG_STATE=$state; show_cursor; return 0;;
      q|Q) confirm_quit;;
      EOF) show_cursor; echo "Input closed; stopping." >&2; exit 1;;
    esac
  done
}

ANSWER=""
ask_text() { # ask_text PROMPT DEFAULT -> ANSWER
  render
  show_cursor
  printf '  %s%s%s [%s]\n  > ' "$C_BOLD" "$1" "$C_RESET" "$2"
  IFS= read -r -e ANSWER || { echo "Input closed; stopping." >&2; exit 1; }
  [ -z "$ANSWER" ] && ANSWER=$2
  case "$ANSWER" in
    q|Q) confirm_quit; ask_text "$1" "$2";;
    '~') ANSWER=$HOME;;
    '~/'*) ANSWER="$HOME/${ANSWER#\~/}";;
  esac
}

pause() { menu 0 "Continue"; }

# ---------------------------------------------------------------------------
# Running commands: spinner, private logs
# ---------------------------------------------------------------------------
CMD_N=0
LAST_OUT=""
LAST_ERR=""
LAST_RC=0

init_logdir() {
  LOGDIR=$(umask 077; mktemp -d "${TMPDIR:-/tmp}/agent-orch-setup.XXXXXX") || {
    echo "Could not create a private temporary directory." >&2; exit 1; }
  chmod 700 "$LOGDIR"
  mkdir "$LOGDIR/inputs"
}

# run_cmd LABEL CMD... : runs with a spinner in its own process group. Output stays in the
# private log directory (deleted on exit), except when RUN_QUIET=1 (git, project hooks),
# whose output is discarded because it may carry identity details.
RUN_QUIET=0
run_cmd() {
  local label=$1 pid i=0 frames='|/-\'
  shift
  CMD_N=$((CMD_N + 1))
  if [ "$RUN_QUIET" -eq 1 ]; then
    LAST_OUT=/dev/null; LAST_ERR=/dev/null
  else
    LAST_OUT="$LOGDIR/cmd$CMD_N.out"
    LAST_ERR="$LOGDIR/cmd$CMD_N.err"
    ( umask 077; : >"$LAST_OUT"; : >"$LAST_ERR" )
  fi
  set -m
  "$@" >"$LAST_OUT" 2>"$LAST_ERR" </dev/null &
  pid=$!
  set +m
  ACTIVE_PID=$pid
  hide_cursor
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r  %s%s%s %s ' "$C_CYAN" "${frames:$((i % 4)):1}" "$C_RESET" "$label"
    i=$((i + 1))
    sleep 0.15
  done
  wait "$pid"; LAST_RC=$?
  ACTIVE_PID=""
  printf '\r\033[K'
  show_cursor
  return $LAST_RC
}

# run_identity CMD... : for identity commands; output is parsed, never logged or shown.
run_identity() {
  "$@" 2>/dev/null </dev/null
}

# Only step names, command labels, exit codes and fixed hints ever reach the screen or the
# saved summary. Raw command output is never shown, logged or exported.
FAIL_SUMMARY=""
FAIL_HINT_DEFAULT="Run the same command yourself to see details."
record_failure() { # record_failure LABEL HINT
  FAILED_ANY=1
  FAIL_SUMMARY="${FAIL_SUMMARY}step ${STEP} (${STEP_NAMES[$((STEP-1))]}): $1 | exit code $LAST_RC | $2
"
}

failure_detail() { # failure_detail LABEL [HINT]
  local hint=${2:-$FAIL_HINT_DEFAULT}
  record_failure "$1" "$hint"
  list_add fail "$1 (exit $LAST_RC)"
  list_sub "$hint"
}

# explain_installer LABEL MANUAL_COMMAND : map known installer refusals to fixed messages.
# The installer's own text is only matched, never echoed.
explain_installer() {
  local label=$1 manual=$2 raw msg
  raw=$(pj raw "$LAST_OUT")
  case "$raw" in
    *"merge"*"progress"*|*"rebase"*"progress"*|*"cherry-pick"*"progress"*|*"revert"*"progress"*|*"bisect"*"progress"*|*unmerged*)
      msg="A Git operation (merge, rebase, cherry-pick or similar) is unfinished in the project. Finish or abort it first.";;
    *"managed paths"*|*"uncommitted"*)
      msg="Managed paths (.codex, .agents, .claude) have uncommitted changes. Commit or discard them first.";;
    *"top of its working tree"*|*"not a usable git"*)
      msg="The path is not the top folder of a Git working tree. Choose the project root.";;
    *symlink*|*unsafe_or_unavailable_path*|*"not a regular file"*)
      msg="A managed path is a symlink or cannot be used safely. Replace it with a real file or folder.";;
    *"differs"*|*conflict*|*collision*|*"modified obsolete"*|*"already exists"*)
      msg="A file in the project collides with a managed file (it differs from what would be installed). Move or commit it, then retry.";;
    *"configuration is missing"*|*"required"*"configuration"*)
      msg="Project configuration is missing (project-agent-routing.json or operational-checks.json).";;
    *"not committed"*|*"verification"*)
      msg="Verification failed: the project configuration and installed files must be committed and clean.";;
    *)
      msg="The installer refused: run it manually to see why:"
      record_failure "$label" "installer refusal not recognised; run: $manual"
      list_add fail "$label"
      list_sub "$msg"
      list_cmd "$manual"
      return 0;;
  esac
  record_failure "$label" "$msg"
  list_add fail "$label"
  list_sub "$msg"
}

save_summary() {
  local dest
  ask_text "Save the summary to (new file)" "$PWD/agent-orchestration-setup-summary.txt"
  dest=$ANSWER
  screen_reset; begin_step 8
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    list_add fail "$dest already exists; nothing written"
  elif ( umask 077; set -C; printf 'Agent orchestration setup: what failed\n%s' "$FAIL_SUMMARY" >"$dest" ) 2>/dev/null; then
    list_add ok "Saved a summary of what failed (step names, labels, exit codes and fixed hints only)"
  else
    list_add fail "Could not write the summary"
  fi
  menu 0 "Continue"
}

# ---------------------------------------------------------------------------
# JSON helper (python3 is required anyway)
# ---------------------------------------------------------------------------
IFS= read -r -d '' PY_HELPER <<'PY'
import json, os, re, sys

def load(path):
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None

def walk(data, path):
    for key in path.split("."):
        if key == "":
            continue
        if isinstance(data, dict) and key in data:
            data = data[key]
        else:
            return None
    return data

def fmt(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (list, dict)):
        return str(len(value))
    return str(value)

def safe(value):
    text = re.sub(r"[^A-Za-z0-9_ .+-]", "", str(value or ""))[:30]
    return text or "unknown"

op = sys.argv[1]
if op == "get":
    print(fmt(walk(load(sys.argv[2]), sys.argv[3])))
elif op == "claude":
    data = load("-")
    if isinstance(data, dict) and data.get("loggedIn") is True:
        print("yes|%s|%s" % (safe(data.get("subscriptionType")), safe(data.get("authMethod"))))
    else:
        print("no||")
elif op == "plan":
    import hashlib
    data = load(sys.argv[2]) or {}
    target, bundle = sys.argv[3], sys.argv[4]
    codex = data.get("codex") or {}
    claude = data.get("claude") or {}

    def digest(path):
        try:
            with open(path, "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return None

    files = codex.get("files") or []
    for rel in files:
        installed = os.path.join(target, rel)
        if not os.path.lexists(installed):
            print("ADD %s" % rel)
        elif digest(installed) == digest(os.path.join(bundle, rel)):
            print("KEEP %s" % rel)
        else:
            print("UPDATE %s" % rel)
    manifest = load(os.path.join(target, ".codex", "orchestration", "install-manifest.json"))
    old = (manifest or {}).get("files") if isinstance(manifest, dict) else None
    if isinstance(old, dict):
        for rel in sorted(set(old) - set(files)):
            print("REMOVE %s" % rel)
    for rel in claude.get("changed_paths") or []:
        print(("UPDATE %s" if os.path.lexists(os.path.join(target, rel)) else "ADD %s") % rel)
    for rel, action in sorted((codex.get("project_configuration") or {}).items()):
        print("CONFIG %s -> %s" % (rel, action))
elif op == "raw":
    data = load(sys.argv[2]) or {}
    parts = []
    for key in ("refused", "error"):
        if isinstance(data.get(key), str):
            parts.append(data[key])
    for source in (data.get("codex") or {}, data.get("claude") or {}):
        for key in ("errors", "project_errors", "package_errors"):
            for item in source.get(key) or []:
                if isinstance(item, str):
                    parts.append(item)
        for key in ("error", "required_action"):
            if isinstance(source.get(key), str):
                parts.append(source[key])
    print(" ".join(parts).lower().replace("\n", " "))
PY
pj() { python3 -I -c "$PY_HELPER" "$@"; }

# ---------------------------------------------------------------------------
# Step bookkeeping
# ---------------------------------------------------------------------------
begin_step() { STEP=$1; screen_reset; }
mark_step() { STAT[$1]=$2; NOTE[$1]=$3; }

# ---------------------------------------------------------------------------
# Step 1: welcome and checks
# ---------------------------------------------------------------------------
step_checks() {
  local choice os hard=0 soft=0 out plan method logged
  while :; do
    begin_step 1
    hard=0; soft=0
    body "Welcome. This wizard sets up delegation between Codex and Claude children in one of your Git projects. It uses only your existing subscription logins and never any metered API billing. First, a quick look at this machine."
    os=$(uname -s)
    case "$os" in
      Darwin|Linux) list_add ok "Operating system: $os";;
      *) list_add fail "Operating system: $os"; list_sub "Fix: run this on macOS or Linux (WSL counts as Linux)."; hard=1;;
    esac
    if command -v bash >/dev/null 2>&1; then
      list_add ok "bash: ${BASH_VERSION%%(*}"
    else
      list_add fail "bash not found"; list_sub "Fix: install bash."; hard=1
    fi
    if command -v git >/dev/null 2>&1; then
      list_add ok "git: $(git --version 2>/dev/null | cut -d' ' -f3)"
    else
      list_add fail "git not found"; list_sub "Fix: install Git (macOS: xcode-select --install)."; hard=1
    fi
    if command -v python3 >/dev/null 2>&1; then
      if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        list_add ok "python3: $(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
      else
        list_add fail "python3 is older than 3.11"; list_sub "Fix: install Python 3.11 or later and put it first on PATH."; hard=1
      fi
    else
      list_add fail "python3 not found"; list_sub "Fix: install Python 3.11 or later."; hard=1
    fi
    if command -v codex >/dev/null 2>&1; then
      if run_identity codex login status >/dev/null; then
        list_add ok "codex: installed, signed in"
      else
        list_add fail "codex: installed but not signed in"; list_sub "Fix: run: codex login"; soft=1
      fi
    else
      list_add fail "codex not found"; list_sub "Fix: install the Codex CLI, then run: codex login"; soft=1
    fi
    if command -v claude >/dev/null 2>&1; then
      out=$(run_identity claude auth status --json | pj claude)
      logged=${out%%|*}
      if [ "$logged" = yes ]; then
        plan=${out#*|}; method=${plan#*|}; plan=${plan%%|*}
        list_add ok "claude: signed in (plan: $plan, login: $method)"
      else
        list_add fail "claude: installed but not signed in"; list_sub "Fix: run: claude auth login"; soft=1
      fi
    else
      list_add fail "claude not found"; list_sub "Fix: install Claude Code, then run: claude auth login"; soft=1
    fi
    if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${OPENAI_API_KEY:-}" ]; then
      list_add fail "API key set in the environment (name only shown)"
      [ -n "${ANTHROPIC_API_KEY:-}" ] && list_sub "ANTHROPIC_API_KEY is set"
      [ -n "${OPENAI_API_KEY:-}" ] && list_sub "OPENAI_API_KEY is set"
      list_sub "Metered billing is never used, so child runs would be refused while these are set."
      list_sub "Fix: quit (q), run 'unset ANTHROPIC_API_KEY OPENAI_API_KEY' in your shell, then run ./setup.sh again."
      soft=1
    else
      list_add ok "No ANTHROPIC_API_KEY or OPENAI_API_KEY set"
    fi
    if [ $hard -eq 1 ]; then
      body "Fix the failures above, then re-check. Setup cannot continue without them."
      menu 0 "Re-check" "Quit"
      [ "$MENU_IDX" -eq 1 ] && { echo "Stopped."; exit 1; }
      continue
    fi
    if [ $soft -eq 1 ]; then
      body "Some checks failed. You can continue now, but providers that are not signed in cannot be attested or used until fixed."
      menu 0 "Re-check" "Continue anyway" "Quit"
      case $MENU_IDX in
        0) continue;;
        2) echo "Stopped."; exit 1;;
      esac
      mark_step 1 ok "checks done with warnings"
    else
      body "Everything looks good."
      menu 0 "Continue" "Quit"
      [ "$MENU_IDX" -eq 1 ] && { echo "Stopped."; exit 0; }
      mark_step 1 ok "all checks passed"
    fi
    return 0
  done
}

# ---------------------------------------------------------------------------
# Step 2: choose the project
# ---------------------------------------------------------------------------
resolve_dir() { (CDPATH= cd -- "$1" 2>/dev/null && pwd -P); }

# basic_problem DIR : the directory must be the top of a Git working tree.
basic_problem() {
  local dir=$1 top
  [ -d "$dir" ] || { echo "That path is not a directory."; return 1; }
  top=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) || {
    echo "That directory is not inside a Git working tree. Run: git init"; return 1; }
  top=$(resolve_dir "$top")
  if [ "$top" != "$dir" ]; then
    echo "That is inside a Git project but not its top. Use the project root instead: $top"
    return 1
  fi
  return 0
}

# managed_problem DIR : an install or update needs clean managed paths.
managed_problem() {
  local dirty
  dirty=$(git -C "$1" status --porcelain --untracked-files=all -- .codex .agents .claude 2>/dev/null | head -n 3)
  if [ -n "$dirty" ]; then
    echo "Uncommitted changes exist in managed paths (.codex, .agents, .claude). Commit or discard them first."
    return 1
  fi
  return 0
}

# Gather the project-owned configuration the Codex installer requires.
prepare_project_inputs() {
  local dir=$1 path
  INSTALL_OPTS=()
  if [ ! -f "$dir/.codex/orchestration/project-agent-routing.json" ]; then
    printf '%s\n' '{"version": 1, "roles": {}, "verifiers": {}}' >"$LOGDIR/inputs/project-agent-routing.json"
    INSTALL_OPTS+=(--project-agent-routing "$LOGDIR/inputs/project-agent-routing.json")
  fi
  if [ ! -f "$dir/.codex/orchestration/operational-checks.json" ]; then
    while :; do
      screen_reset
      begin_step 2
      body "This project has no operational checks file yet. The installer needs one: it names the exact test and security commands, with their hashes, that operational QA may run. It cannot be guessed. README.md, section 'Advanced: manual setup', has a short recipe to build it (a wrapper script plus two hashes). A future wizard step will generate it; for now it is a manual preparation step."
      body "Enter the path to your prepared operational-checks.json (it is copied into the project), or type q to quit."
      ask_text "Path to operational-checks.json" ""
      path=$ANSWER
      if [ -f "$path" ]; then
        path="$(CDPATH= cd -- "$(dirname -- "$path")" && pwd -P)/$(basename -- "$path")"
        INSTALL_OPTS+=(--operational-checks "$path")
        return 0
      fi
      screen_reset; begin_step 2
      list_add fail "No such file: $path"
      menu 0 "Try again" "Quit"
      [ "$MENU_IDX" -eq 1 ] && { echo "Stopped."; exit 1; }
    done
  fi
}

step_project() {
  local default dir reason out rc
  default=${1:-$PWD}
  while :; do
    begin_step 2
    body "Choose the project to set up. It must be the top folder of a Git working tree. Nothing is changed in this step; it only previews."
    ask_text "Project path" "$default"
    dir=$(resolve_dir "$ANSWER")
    screen_reset; begin_step 2
    if [ -z "$dir" ]; then
      list_add fail "Cannot open: $ANSWER"
      menu 0 "Choose another path" "Quit"
      [ "$MENU_IDX" -eq 1 ] && { echo "Stopped."; exit 1; }
      continue
    fi
    if reason=$(basic_problem "$dir"); then :; else
      list_add fail "$dir"; list_sub "$reason"
      menu 0 "Choose another path" "Quit"
      [ "$MENU_IDX" -eq 1 ] && { echo "Stopped."; exit 1; }
      default=$dir
      continue
    fi
    # Existing installs are detected first, so verify and re-attest never need an update preview.
    MODE=full
    if [ -f "$dir/.codex/orchestration/install-manifest.json" ]; then
      list_add ok "Project: $dir"
      body "An existing install was found in this project. Choose what to do."
      menu 0 "Update the install and walk through every step" \
             "Verify the install only" \
             "Re-run the billing attestation only" \
             "Choose another path" \
             "Quit"
      case $MENU_IDX in
        0) MODE=full;;
        1) MODE=verify;;
        2) MODE=attest;;
        3) default=$dir; continue;;
        4) echo "Stopped."; exit 0;;
      esac
      screen_reset; begin_step 2
    fi
    if [ "$MODE" != full ]; then
      PROJECT=$dir
      mark_step 2 ok "$PROJECT"
      return 0
    fi
    if reason=$(managed_problem "$dir"); then :; else
      list_add fail "$dir"; list_sub "$reason"
      menu 0 "Choose another path" "Quit"
      [ "$MENU_IDX" -eq 1 ] && { echo "Stopped."; exit 1; }
      default=$dir
      continue
    fi
    prepare_project_inputs "$dir"
    screen_reset; begin_step 2
    body "Previewing the install for: $dir"
    render
    PREVIEW_FILE="$LOGDIR/preview.json"
    run_cmd "Previewing the install" python3 "$ROOT_DIR/scripts/install.py" install --target "$dir" "${INSTALL_OPTS[@]}"
    rc=$?
    cp "$LAST_OUT" "$PREVIEW_FILE"
    screen_reset; begin_step 2
    if [ "$rc" -ne 0 ]; then
      explain_installer "The install preview was refused" "$(manual_installer install "$dir")"
      menu 0 "Choose another path" "Quit"
      [ "$MENU_IDX" -eq 1 ] && { echo "Stopped."; exit 1; }
      default=$dir
      continue
    fi
    PROJECT=$dir
    PLAN_TEXT=$(pj plan "$PREVIEW_FILE" "$PROJECT" "$ROOT_DIR/packages/codex-agents/native-package")
    list_add ok "Project: $PROJECT"
    body "A fresh install is possible. Continue to choose providers."
    menu 0 "Continue" "Choose another path" "Quit"
    case $MENU_IDX in
      1) default=$dir; continue;;
      2) echo "Stopped."; exit 0;;
    esac
    mark_step 2 ok "$PROJECT"
    return 0
  done
}

# ---------------------------------------------------------------------------
# Step 3: choose providers
# ---------------------------------------------------------------------------
CH_CODEX=on CH_CLAUDE=on CH_MODELS="" CH_BRIDGE=off

step_providers() {
  local state="111110" m i err existing
  existing="$PROJECT/.codex/orchestration/provider-routing.json"
  ROUTING_WRITE=1
  begin_step 3
  if [ -L "$existing" ] || { [ -e "$existing" ] && [ ! -f "$existing" ]; }; then
    list_add fail "provider-routing.json is a symlink or not a regular file; refused"
    list_sub "Replace it with a regular file in the project, commit, and run setup again."
    mark_step 3 fail "provider-routing.json refused"
    FAIL_REASON="provider configuration refused"
    menu 0 "Continue to the summary"
    return 1
  fi
  if [ -f "$existing" ]; then
    body "This project already has a provider configuration (.codex/orchestration/provider-routing.json). Keep it, or choose providers again and replace it."
    menu 0 "Keep the existing configuration" "Choose providers again"
    if [ "$MENU_IDX" -eq 0 ]; then
      ROUTING_WRITE=0
      ROUTING_CONTENT=$(cat "$existing")
      if grep -q '"codex_primary_may_bridge_to_claude": *true' "$existing"; then PROFILE_WANTED=1; fi
      mark_step 3 ok "kept existing configuration"
      return 0
    fi
  fi
  while :; do
    begin_step 3
    body "Choose which providers may run child agents in this project. Use Space to toggle, Enter to accept. The Claude model rows only matter while Claude children are on."
    body "'Let Codex sessions orchestrate Claude children' lets a Codex primary start Claude children. Codex runs commands without network by default, so this needs the Codex orchestrator profile, which step 7 offers to create."
    [ -n "${err:-}" ] && list_add fail "$err"
    toggles "$state" \
      "Codex children" \
      "Claude children" \
      "    claude-opus-5-5" \
      "    claude-sonnet-5-5" \
      "    claude-haiku-5-5" \
      "Let Codex sessions orchestrate Claude children"
    state=$TOG_STATE
    CH_CODEX=off; CH_CLAUDE=off; CH_BRIDGE=off; CH_MODELS=""
    [ "${state:0:1}" = 1 ] && CH_CODEX=on
    [ "${state:1:1}" = 1 ] && CH_CLAUDE=on
    i=2
    for m in $CLAUDE_MODELS_ALL; do
      if [ "${state:$i:1}" = 1 ]; then
        if [ -z "$CH_MODELS" ]; then CH_MODELS=$m; else CH_MODELS="$CH_MODELS,$m"; fi
      fi
      i=$((i+1))
    done
    [ "${state:5:1}" = 1 ] && CH_BRIDGE=on
    err=""
    if [ "$CH_CLAUDE" = off ]; then
      CH_MODELS="-"
      if [ "$CH_BRIDGE" = on ]; then err="Bridging to Claude needs Claude children turned on."; fi
    fi
    if [ -z "$err" ]; then
      ROUTING_CONTENT=$(routing_json "$CH_CODEX" "$CH_CLAUDE" "$CH_MODELS" "$CH_BRIDGE" 2>&1) || {
        err=$ROUTING_CONTENT; ROUTING_CONTENT=""; }
    fi
    [ -z "$err" ] && break
  done
  begin_step 3
  body "This will be written to .codex/orchestration/provider-routing.json in step 4:"
  body "$ROUTING_CONTENT"
  [ "$CH_BRIDGE" = on ] && { PROFILE_WANTED=1; body "Step 7 will offer the Codex orchestrator profile that bridging needs."; }
  menu 0 "Looks right, continue" "Choose again" "Quit"
  case $MENU_IDX in
    1) step_providers; return;;
    2) echo "Stopped."; exit 0;;
  esac
  mark_step 3 ok "codex=$CH_CODEX claude=$CH_CLAUDE bridge=$CH_BRIDGE"
}

# ---------------------------------------------------------------------------
# Step 4: install
# ---------------------------------------------------------------------------
COMMIT_MSG_1="Install agent orchestration packages"
COMMIT_MSG_2="Adds the managed Codex and Claude agent packages and the provider routing configuration."
PLAN_TEXT=""

list_cmd() { LIST="${LIST}    $1
"; }

# The per-path plan: add, update (content differs), remove (obsolete), keep (unchanged).
show_plan() {
  local kind line n shown cap label
  for kind in ADD UPDATE REMOVE; do
    n=$(printf '%s\n' "$PLAN_TEXT" | grep -c "^$kind ")
    case $kind in
      ADD) label="Files to add"; cap=6;;
      UPDATE) label="Files to update (content differs)"; cap=15;;
      REMOVE) label="Obsolete files to remove"; cap=15;;
    esac
    list_add info "$label: $n"
    shown=0
    while IFS= read -r line; do
      [ $shown -ge $cap ] && break
      list_sub "${kind} ${line#* }"
      shown=$((shown + 1))
    done < <(printf '%s\n' "$PLAN_TEXT" | grep "^$kind ")
    [ "$n" -gt "$shown" ] && list_sub "... and $((n - shown)) more"
  done
  list_add info "Files unchanged: $(printf '%s\n' "$PLAN_TEXT" | grep -c '^KEEP ')"
  while IFS= read -r line; do
    list_add info "Project config: ${line#CONFIG }"
  done < <(printf '%s\n' "$PLAN_TEXT" | grep '^CONFIG ')
}

# manual_installer OPERATION PROJECT [--apply] : the exact, shell-quoted command to run by hand.
manual_installer() {
  printf 'python3 %q %s --target %q%s' "$ROOT_DIR/scripts/install.py" "$1" "$2" "${3:+ $3}"
}

# view_plan : every add, update and remove path in a scrollable view.
view_plan() {
  local text off=0 page n i end k
  local lines=()
  text=$(printf '%s\n' "$PLAN_TEXT" | grep -E '^(ADD|UPDATE|REMOVE) ')
  hide_cursor
  while :; do
    term_size
    lines=()
    while IFS= read -r k; do lines+=("$k"); done < <(printf '%s\n' "$text" | fold -s -w $((COLS - 6)))
    n=${#lines[@]}
    page=$((LINES_N - 9)); [ $page -lt 3 ] && page=3
    [ $off -gt $((n - page)) ] && off=$((n - page))
    [ $off -lt 0 ] && off=0
    printf '\033[H\033[2J'
    draw_header
    printf '\n  %sFull plan: lines %d-%d of %d%s\n\n' "$C_BOLD" $((off + 1)) $((off + page > n ? n : off + page)) "$n" "$C_RESET"
    i=$off; end=$((off + page))
    while [ $i -lt $end ] && [ $i -lt "$n" ]; do
      printf '    %s\n' "${lines[$i]}"
      i=$((i + 1))
    done
    printf '\n  %sUp/Down scroll   PageUp/PageDown page   q back%s\n' "$C_DIM" "$C_RESET"
    read_key
    case "$KEY" in
      UP|k) off=$((off - 1));;
      DOWN|j) off=$((off + 1));;
      PGUP) off=$((off - page));;
      PGDN|SPACE) off=$((off + page));;
      q|Q|ENTER|ESC) show_cursor; return 0;;
      EOF) show_cursor; return 0;;
    esac
  done
}

verify_install() { # verify_install -> sets LAST_* and records problems in LIST
  run_cmd "Verifying the install" python3 "$ROOT_DIR/scripts/install.py" verify --target "$PROJECT"
  local rc=$? line
  if [ $rc -eq 0 ]; then
    list_add ok "Verification passed: the install is ready"
    return 0
  fi
  explain_installer "Verification is not ready yet" "$(manual_installer verify "$PROJECT")"
  return 1
}

step_install() {
  local routing_path line commit_done=0 verify_ok=0 q_project
  q_project=$(printf '%q' "$PROJECT")
  begin_step 4
  body "Install into: $PROJECT"
  show_plan
  body "This adds or updates files under .codex, .agents and .claude, then writes .codex/orchestration/provider-routing.json. Nothing outside the project is touched."
  while :; do
    menu 0 "Install now" "Show the full list of files" "Skip the install" "Quit"
    [ "$MENU_IDX" -eq 1 ] && { view_plan; continue; }
    break
  done
  case $MENU_IDX in
    2) mark_step 4 skip "install skipped"; return 0;;
    3) echo "Stopped."; exit 0;;
  esac
  screen_reset; begin_step 4
  render
  if ! run_cmd "Installing" python3 "$ROOT_DIR/scripts/install.py" install --target "$PROJECT" "${INSTALL_OPTS[@]}" --apply; then
    screen_reset; begin_step 4
    explain_installer "The install failed" "$(manual_installer "install" "$PROJECT" --apply)"
    list_sub "The installer rolls back a partial install where it can."
    mark_step 4 fail "install failed"
    FAIL_REASON="install failed"
    menu 0 "Continue to the summary"
    return 1
  fi
  screen_reset; begin_step 4
  list_add ok "Packages installed"
  if [ "$ROUTING_WRITE" -eq 1 ]; then
    if write_project_routing "$PROJECT" "$ROUTING_CONTENT" 2>"$LOGDIR/routing.err"; then
      list_add ok "Wrote .codex/orchestration/provider-routing.json"
    else
      list_add fail "Could not write provider-routing.json"
      list_sub "The destination is a symlink, or its folders are not real folders inside the project."
      record_failure "provider-routing.json not written" "destination refused or not writable"
      mark_step 4 fail "provider routing not written"
      FAIL_REASON="provider routing not written"
      menu 0 "Continue to the summary"
      return 1
    fi
  else
    list_add ok "Kept the existing provider-routing.json"
  fi
  body "Next: commit the installed files in the project. Only these paths are committed, nothing is pushed. The exact commands are:"
  list_cmd "git -C $q_project add -- .agents .claude .codex"
  list_cmd "git -C $q_project commit -m \"$COMMIT_MSG_1\" -m \"$COMMIT_MSG_2\" -- .agents .claude .codex"
  menu 0 "Commit now (recommended)" "I will commit myself"
  screen_reset; begin_step 4
  list_add ok "Packages installed"
  if [ "$MENU_IDX" -eq 0 ]; then
    render
    RUN_QUIET=1
    if run_cmd "Committing" git -C "$PROJECT" add -- .agents .claude .codex &&
       run_cmd "Committing" git -C "$PROJECT" commit -m "$COMMIT_MSG_1" -m "$COMMIT_MSG_2" -- .agents .claude .codex; then
      RUN_QUIET=0
      commit_done=1
      list_add ok "Committed the installed files (not pushed)"
    else
      RUN_QUIET=0
      failure_detail "The commit failed" "Check your Git identity (user.name, user.email) and any commit hooks. Commit the files yourself, then run ./setup.sh again and choose Verify."
    fi
  else
    list_add skip "Commit left to you; verification needs the files committed"
  fi
  render
  if verify_install; then verify_ok=1; fi
  if [ $verify_ok -eq 1 ]; then
    mark_step 4 ok "installed and verified"
  elif [ $commit_done -eq 0 ]; then
    mark_step 4 ok "installed; commit, then verify"
  else
    mark_step 4 fail "installed but verification failed"
    FAILED_ANY=1
  fi
  menu 0 "Continue"
  return 0
}

# ---------------------------------------------------------------------------
# Step 5: learning collector
# ---------------------------------------------------------------------------
version_hint() {
  case "$(cat "$LAST_OUT" "$LAST_ERR" 2>/dev/null)" in
    *runtime*|*newer*|*authorized*|*conflict*)
      list_sub "The shared collector is authorised for a different version, so other projects on the old version need updating (run this setup there).";;
  esac
}

step_collector() {
  local cli ready
  begin_step 5
  cli="$PROJECT/.codex/orchestration/cli.py"
  if [ ! -f "$cli" ]; then
    list_add skip "The project CLI is not installed, so the collector cannot be registered."
    mark_step 5 skip "no installed CLI"
    pause; return 0
  fi
  body "The learning collector is a small local service that records run outcomes on this machine only. One collector is shared by every project on this machine. This step registers this project with it and starts it."
  body "Command: python3 .codex/orchestration/cli.py learning-register --project-root <project>, then learning-status."
  menu 0 "Register now" "Skip this step" "Quit"
  case $MENU_IDX in
    1) mark_step 5 skip "skipped"; return 0;;
    2) echo "Stopped."; exit 0;;
  esac
  screen_reset; begin_step 5
  render
  run_cmd "Registering the collector" python3 "$cli" learning-register --project-root "$PROJECT"
  if [ $? -ne 0 ]; then
    failure_detail "Collector registration failed"
    version_hint
    mark_step 5 fail "registration failed"
    pause; return 1
  fi
  list_add ok "Collector registered"
  render
  run_cmd "Checking collector status" python3 "$cli" learning-status --project-root "$PROJECT"
  ready=$(pj get "$LAST_OUT" ready)
  if [ "$ready" = true ]; then
    list_add ok "Collector status: ready (runtime $(pj get "$LAST_OUT" collector_runtime_version))"
    mark_step 5 ok "registered, ready"
  else
    failure_detail "Collector status: not ready"
    version_hint
    mark_step 5 fail "not ready"
  fi
  pause
}

# ---------------------------------------------------------------------------
# Step 6: billing attestation (human step, never wrapped)
# ---------------------------------------------------------------------------
step_billing() {
  local rc
  begin_step 6
  if [ ! -f "$PROJECT/.codex/orchestration/cli.py" ]; then
    list_add skip "The project CLI is not installed, so attestation is not possible yet."
    mark_step 6 skip "no installed CLI"
    pause; return 0
  fi
  body "No child agent runs until you confirm that each account cannot be charged beyond its subscription. This is a statement about your own billing settings, which only you can check."
  body "Only you can sign it: attest.py asks for your owner key's passphrase, and a passphrase is what stops a program, including an AI agent, from signing in your name. The next screens belong to attest.py; this wizard will not touch them."
  menu 0 "Start the attestation" "Skip for now" "Quit"
  case $MENU_IDX in
    1) mark_step 6 skip "not attested yet"; return 0;;
    2) echo "Stopped."; exit 0;;
  esac
  show_cursor
  printf '\033[H\033[2J'
  python3 "$ROOT_DIR/scripts/attest.py" --target "$PROJECT"
  rc=$?
  # Keep attest.py's own summary on screen until the owner has read it.
  printf '\n  attest.py finished (exit code %s). Press Enter to continue. ' "$rc"
  IFS= read -r _ || true
  screen_reset; begin_step 6
  if [ $rc -eq 0 ]; then
    list_add ok "attest.py ran. Check its summary for which providers it listed as attested."
    mark_step 6 ok "attestation ran; check which providers were attested"
  else
    list_add fail "attest.py stopped with exit code $rc (see its message)"
    list_sub "Run it again later: ./setup.sh, then choose the attestation option."
    mark_step 6 fail "attestation incomplete"
  fi
  pause
}

# ---------------------------------------------------------------------------
# Step 7: Codex orchestrator profile
# ---------------------------------------------------------------------------
PROFILE_CONTENT='sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true'

step_profile() {
  local dir file
  dir="${CODEX_HOME:-$HOME/.codex}"; file="$dir/orchestrator.config.toml"
  begin_step 7
  if [ "$PROFILE_WANTED" -ne 1 ]; then
    list_add skip "Not chosen in step 3, so no profile is needed."
    mark_step 7 skip "not chosen"
    pause; return 0
  fi
  body "Codex runs commands without network access by default, so a Codex session cannot reach Claude. The orchestrator profile turns network on for sessions you start with: codex -p orchestrator. Ordinary sessions keep network off. Your main config.toml is never edited."
  body "File: $file (mode 0600)

$PROFILE_CONTENT"
  if [ -L "$file" ] || { [ -e "$file" ] && [ ! -f "$file" ]; }; then
    list_add skip "$file is not a regular file; left alone."
    mark_step 7 skip "left alone"
    pause; return 0
  fi
  if [ -f "$file" ]; then
    if [ "$(cat "$file")" = "$PROFILE_CONTENT" ]; then
      list_add ok "The profile already exists with this content."
      PROFILE_READY=1
      mark_step 7 ok "already present"
      pause; return 0
    fi
    list_add warn "The profile exists and differs from the proposed content. It is left unchanged and its contents are not shown."
    mark_step 7 skip "exists, differs, left alone"
    pause; return 0
  fi
  menu 0 "Create the profile" "Skip" "Quit"
  case $MENU_IDX in
    1) mark_step 7 skip "skipped"; return 0;;
    2) echo "Stopped."; exit 0;;
  esac
  screen_reset; begin_step 7
  if ( umask 077; mkdir -p "$dir"; set -C; printf '%s\n' "$PROFILE_CONTENT" >"$file" ) 2>/dev/null; then
    chmod 600 "$file"
    list_add ok "Created $file (mode 0600)"
    PROFILE_READY=1
    mark_step 7 ok "created"
  else
    list_add fail "Could not create $file"
    mark_step 7 fail "not created"
  fi
  pause
}

# ---------------------------------------------------------------------------
# Verify-only mode and step 8: done
# ---------------------------------------------------------------------------
step_verify_only() {
  begin_step 4
  render
  if verify_install; then mark_step 4 ok "verified"; else mark_step 4 fail "verification failed"; FAILED_ANY=1; fi
  pause
}

step_done() {
  local i s label codex_cmd
  begin_step 8
  mark_step 8 ok "finished"
  i=1
  for label in "${STEP_NAMES[@]}"; do
    s=${STAT[$i]:-}
    case "$s" in
      ok) list_add ok "$i. $label: ${NOTE[$i]}";;
      fail) list_add fail "$i. $label: ${NOTE[$i]}";;
      skip) list_add skip "$i. $label: ${NOTE[$i]}";;
      *) list_add skip "$i. $label: skipped";;
    esac
    i=$((i+1))
  done
  [ "$PROFILE_READY" -eq 1 ] && codex_cmd="codex -p orchestrator" || codex_cmd="codex"
  body "How to use it:
 - In Claude Code, open the project and use the agent-orchestration skill.
 - In Codex, start a session with: $codex_cmd
 - To remove everything, run:"
  list_cmd "python3 $(printf '%q' "$ROOT_DIR/scripts/install.py") remove --target $(printf '%q' "$PROJECT") --apply"
  if [ "$FAILED_ANY" -eq 1 ] && [ -n "$FAIL_SUMMARY" ]; then
    menu 0 "Exit" "Save a summary of what failed"
    [ "$MENU_IDX" -eq 1 ] && save_summary
    show_cursor
    return 0
  fi
  render
  show_cursor
  printf '  %sPress Enter to exit.%s ' "$C_DIM" "$C_RESET"
  IFS= read -r _ || true
  printf '\n'
}

skip_rest() { # skip_rest REASON : mark collector, billing and profile as skipped
  mark_step 5 skip "$1"; mark_step 6 skip "$1"; mark_step 7 skip "$1"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  setup_colors
  term_size
  init_logdir
  step_checks
  step_project "${1:-$PWD}"
  case "$MODE" in
    full)
      if ! step_providers; then
        mark_step 4 skip "$FAIL_REASON"; skip_rest "$FAIL_REASON"
      elif ! step_install; then
        skip_rest "$FAIL_REASON"
      else
        step_collector
        step_billing
        step_profile
      fi;;
    verify)
      step_verify_only;;
    attest)
      step_billing;;
  esac
  step_done
}

main "$@"
