#!/bin/bash
# ===========================================================================
#  UniMic launcher for macOS.
#
#  The counterpart to unimic.bat: checks the two things macOS needs that
#  Linux does not -- a real Python, and a virtual audio cable -- offers to
#  install whichever is missing, and then starts the server. Any arguments
#  are passed straight through, so "./unimic.command --port 9000" works like
#  "python3 unimic.py --port 9000".
#
#  The .command extension is what makes this double-clickable in Finder;
#  macOS hands it to Terminal. It needs its executable bit to stay set.
# ===========================================================================

# Finder starts a double-clicked .command in the user's home directory, not
# beside the script, so unimic.py would not be found without this.
cd "$(dirname "$0")" || exit 1

printf '\n  U N I M I C   -   macOS launcher\n\n'

ok()   { printf '  [ok] %s\n' "$1"; }
bad()  { printf '  [--] %s\n' "$1"; }
note() { printf '       %s\n' "$1"; }

# ask "question" Y|N  ->  0 when yes.  Defaults are the answer for someone
# who just hits return, and the answer when there is no terminal to ask.
ask() {
  local q="$1" def="$2" ans
  if [ ! -t 0 ]; then
    [ "$def" = "Y" ]
    return
  fi
  printf '\n  %s\n' "$q"
  if [ "$def" = "Y" ]; then printf '  [Y/n] '; else printf '  [y/N] '; fi
  read -r ans
  ans=$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]')
  case "$ans" in
    y|yes) return 0 ;;
    n|no)  return 1 ;;
    "")    [ "$def" = "Y" ]; return ;;
    *)     [ "$def" = "Y" ]; return ;;
  esac
}

# A double-click can leave a window that closes with the error still in it, so
# errors pause. Unlike cmd.exe there is no telling a double-click apart from a
# run in an existing tab -- Terminal gives both an identical login shell as the
# parent -- so this pauses for any terminal at all, and for none of the
# scripted runs where stdin is not one.
pause_on_error() {
  if [ -t 0 ]; then
    printf '\n  Press return to close.'
    read -r _
  fi
}

fail() { printf '\n'; pause_on_error; exit 1; }

# ---------------------------------------------------------------------------
#  1. Find a Python that actually runs.
# ---------------------------------------------------------------------------
#  macOS has its own version of the Microsoft Store stub problem: /usr/bin/
#  python3 exists on every Mac, but on a machine without the Xcode command
#  line tools it is a shim that pops a GUI installer dialog instead of running
#  anything. So candidates are proved by running them, and the shim is only
#  tried once xcode-select confirms the tools are really there.

PY=""

try_python() {
  local cand="$1" real
  real=$(command -v "$cand" 2>/dev/null) || return 1
  [ -x "$real" ] || return 1

  # Resolve symlinks before deciding whether this is the shim: a "python3" on
  # the PATH is very often /usr/bin/python3 wearing a different hat.
  local resolved
  resolved=$(cd "$(dirname "$real")" 2>/dev/null && pwd -P)/$(basename "$real")
  case "$resolved" in
    /usr/bin/python3)
      xcode-select -p >/dev/null 2>&1 || return 1
      ;;
  esac

  "$real" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    >/dev/null 2>&1 || return 1
  PY="$real"
  return 0
}

find_python() {
  local c
  for c in \
    python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
    /usr/bin/python3
  do
    try_python "$c" && return 0
  done
  return 1
}

BREW=""
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -x "$b" ] && { BREW="$b"; break; }
done
[ -n "$BREW" ] || BREW=$(command -v brew 2>/dev/null)

python_manual() {
  printf '\n'
  note 'Install it from https://www.python.org/downloads/macos/'
  note '(the universal2 installer), or with Homebrew:'
  printf '\n'
  note '    brew install python'
  fail
}

if ! find_python; then
  bad 'Python 3.8 or newer was not found.'
  printf '\n'
  note 'A "python3" command may still exist without Python being installed --'
  note 'macOS ships a placeholder that only offers to install the Xcode'
  note 'command line tools. This check ignores it.'
  printf '\n'

  if [ -n "$BREW" ]; then
    ask 'Install Python now with Homebrew?' Y || python_manual
    printf '\n  Installing Python...\n\n'
    "$BREW" install python || bad 'Homebrew could not install Python.'
  else
    note 'The Xcode command line tools include a working Python 3 and are the'
    note 'smallest way to get one without installing anything third-party.'
    ask 'Install the Xcode command line tools now?' Y || python_manual
    printf '\n  Accept the dialog macOS opens, wait for it to finish, then\n'
    printf '  come back here.\n\n'
    xcode-select --install 2>/dev/null
    if [ -t 0 ]; then
      printf '  Press return once the install has finished.'
      read -r _
    fi
  fi

  printf '\n'
  if ! find_python; then
    bad 'Python still is not runnable from here.'
    note 'It may only need a new terminal to appear on the PATH. Close this'
    note 'window, open a new one, and run unimic.command again.'
    fail
  fi
fi

ok "Python $("$PY" -c 'import platform; print(platform.python_version())')"

# ---------------------------------------------------------------------------
#  2. Ask UniMic itself whether the audio side is ready.
# ---------------------------------------------------------------------------
#  Rather than second-guessing the device list from shell, run the same
#  detection the server uses. --check exits 0 when a cable is present. The
#  user's own arguments go along for the ride so that --device is honoured --
#  someone running with "--device default" deliberately has no cable and
#  should not be nagged to install one.

cable_ready() {
  CHECK_OUT=$("$PY" unimic.py --check "$@" 2>&1)
  return $?
}

if cable_ready "$@"; then
  CABLE_OK=1
  ok "Virtual audio cable found"
else
  # check_setup always opens with this line. Anything else means the run never
  # reached the audio check at all -- a mistyped argument, most likely -- and
  # reporting that as a missing cable would send the user off to install a
  # driver they already have. Hand them the real error instead.
  case "$CHECK_OUT" in
    "UniMic setup check"*) ;;
    *)
      bad 'UniMic could not start:'
      printf '\n%s\n' "$CHECK_OUT"
      fail
      ;;
  esac
  CABLE_OK=0
  # The check prints its own reason; show that rather than guessing at one.
  printf '%s\n' "$CHECK_OUT" | grep '^  \[--\]' || bad 'No virtual audio cable is installed.'
fi

# ---------------------------------------------------------------------------
#  BlackHole, pinned.
# ---------------------------------------------------------------------------
#  Existential Audio publishes no installer on its GitHub releases, so the
#  only fixed URL is the versioned one Homebrew's cask uses. To move to a
#  newer BlackHole, bump all three of these together -- the checksum is what
#  makes the URL safe to fetch unattended.
BH_VERSION="0.7.1"
BH_URL="https://existential.audio/downloads/BlackHole2ch-${BH_VERSION}.pkg"
BH_SHA256="57b540f27a3e29c37e310e01bee0fdfab76733087e47f997ef9dccf851400dcf"
BH_TEAMID="Q5C99V536K"          # Existential Audio Inc.

cable_manual() {
  printf '\n'
  note 'To install it by hand:'
  note '  1. Download BlackHole from https://existential.audio/blackhole/'
  note '  2. Open the .pkg and follow the installer'
  note '  3. Reboot, or log out and back in, then run unimic.command again'
  printf '\n'
  note 'Loopback and Soundflower work too if you already have one.'
  printf '\n'
  note 'To try UniMic without installing a driver, run it with'
  note '"--device default" -- the phone comes out of your speakers instead.'
  note 'Useful for checking the phone half works, but no app can record it.'
  fail
}

recheck_cable() {
  printf '\n  Re-checking...\n'
  if cable_ready "$@"; then
    ok "Virtual audio cable found"
    return 0
  fi
  bad 'Still not detected.'
  note 'BlackHole is a CoreAudio plug-in and the audio daemon has to restart'
  note 'before it appears. Log out and back in -- or reboot -- and run'
  note 'unimic.command again.'
  fail
}

install_via_brew() {
  printf '\n  Installing BlackHole with Homebrew...\n'
  printf '  Homebrew will ask for your password: installing an audio driver\n'
  printf '  writes to /Library and that needs administrator rights.\n\n'
  "$BREW" install --cask blackhole-2ch || {
    bad 'Homebrew could not install BlackHole.'
    cable_manual
  }
}

install_via_download() {
  local tmp pkg sum sig
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/unimic-blackhole.XXXXXX") || cable_manual
  # Leaving a downloaded installer behind is untidy at best; clear it on every
  # exit from here, including the ones that go through cable_manual.
  trap 'rm -rf "$tmp"' EXIT
  pkg="$tmp/BlackHole2ch.pkg"

  printf '\n  Downloading BlackHole %s...\n' "$BH_VERSION"
  if ! curl -L --fail --silent --show-error -o "$pkg" "$BH_URL"; then
    bad 'Download failed.'
    cable_manual
  fi

  # The file came off the network a second ago. "It downloaded without error"
  # is not a reason to hand it administrator rights, so it has to match the
  # checksum this launcher was written against *and* still carry a valid
  # Existential Audio signature that Apple has notarised.
  printf '  Verifying the download...\n'
  sum=$(shasum -a 256 "$pkg" | awk '{print $1}')
  if [ "$sum" != "$BH_SHA256" ]; then
    bad 'The download does not match its expected checksum. Refusing to run it.'
    note "expected $BH_SHA256"
    note "got      $sum"
    cable_manual
  fi

  sig=$(pkgutil --check-signature "$pkg" 2>&1)
  if ! printf '%s' "$sig" | grep -q "($BH_TEAMID)"; then
    bad 'The installer is not signed by Existential Audio. Refusing to run it.'
    cable_manual
  fi
  if ! printf '%s' "$sig" | grep -q 'Notarization: trusted'; then
    bad 'The installer is not notarised by Apple. Refusing to run it.'
    cable_manual
  fi
  note "signed by: Existential Audio Inc. ($BH_TEAMID), notarised"

  printf '\n  Opening the installer -- click through it and approve the\n'
  printf '  administrator prompt when it asks.\n\n'
  open -W "$pkg"
}

if [ "$CABLE_OK" -eq 0 ]; then
  printf '\n'
  note 'macOS cannot present a microphone without a driver, so UniMic plays'
  note 'into a virtual cable and your apps record the other end.'
  note 'BlackHole is free, open source, about 100 KB, and the usual choice.'
  printf '\n'
  note 'Installing it needs administrator rights and you will be asked for'
  note 'your password. Nothing else on your system is changed.'

  if [ -n "$BREW" ]; then
    ask 'Install BlackHole with Homebrew now?' N || cable_manual
    install_via_brew
  else
    ask "Download and install BlackHole from existential.audio now?" N || cable_manual
    install_via_download
  fi

  recheck_cable "$@"
fi

# ---------------------------------------------------------------------------
#  3. Start the server.
# ---------------------------------------------------------------------------
printf '\n  Starting UniMic. Press Ctrl-C to stop.\n\n'
printf '  If your phone cannot reach the page, allow Python to accept\n'
printf '  incoming connections -- macOS asks only once.\n\n'

"$PY" unimic.py "$@"
RC=$?

if [ "$RC" -ne 0 ]; then
  printf '\n  UniMic exited with code %s.\n' "$RC"
  pause_if_finder
fi
exit "$RC"
