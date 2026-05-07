#!/bin/sh

# Pre-push confirmation guard for OpenEMR Co-Pilot changes.
#
# Files under ``interface/copilot/`` and ``src/Services/Copilot/`` are
# part of the OpenEMR PHP image that Railway deploys to
# ``openemr-production-6c31.up.railway.app`` — the instance our testers
# are using. The Railway service watches the GitLab remote
# (``labs.gauntletai.com``) on the ``main`` branch — pushes there
# auto-deploy. Pushes to the GitHub mirror (``oe-fork``) are not
# wired to Railway and don't ship anything to testers; we let those
# pass through without prompting.
#
# This hook is wired in ``.pre-commit-config.yaml`` as a ``pre-push``
# stage with a ``files:`` regex that limits invocation to copilot PHP
# changes. The remote/branch gate below narrows further so pushes that
# can't possibly trigger a Railway deploy don't pop the prompt.
#
# It prompts on TTY and aborts on anything other than an explicit
# ``y`` / ``yes``. Bypass when scripting:
#
#     PREK_SKIP=copilot-prod-push-confirm git push
#
# or with ``git push --no-verify`` for full hook bypass.
#
# @package   OpenEMR
# @link      https://www.open-emr.org
# @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
# @copyright Copyright (c) 2026 Andy Nguyen
# @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3

set -eu

# Only the GitLab remote on the main branch deploys via Railway. Pre-commit
# and prek both export PRE_COMMIT_REMOTE_URL / PRE_COMMIT_REMOTE_BRANCH for
# pre-push hooks. If either is missing (older runner), fall through and
# prompt anyway — better to over-prompt than to miss a prod-bound push.
remote_url="${PRE_COMMIT_REMOTE_URL:-}"
remote_branch="${PRE_COMMIT_REMOTE_BRANCH:-}"

case "$remote_url" in
    "")
        # Unknown remote — fall through and prompt, defensively.
        ;;
    *labs.gauntletai.com*)
        # GitLab remote — Railway watches this. Continue to the branch check.
        ;;
    *)
        # Any other remote (GitHub mirror, etc.) — no prod deploy.
        echo "  (skipping copilot-prod-push-confirm — push target is not the GitLab prod remote)"
        exit 0
        ;;
esac

case "$remote_branch" in
    "" | refs/heads/main | main)
        # Main branch (or unknown) — proceed to prompt.
        ;;
    *)
        echo "  (skipping copilot-prod-push-confirm — push target is not the main branch)"
        exit 0
        ;;
esac

# Need a terminal to prompt. CI runners and other non-interactive
# environments must opt-in via PREK_SKIP rather than getting a silent
# pass — a "skip when no tty" branch would defeat the whole point of
# the gate the moment someone wired up an automated pipeline.
if ! [ -r /dev/tty ]; then
    echo "ERROR: copilot-prod-push-confirm needs a terminal." >&2
    echo "       For non-interactive use, set PREK_SKIP=copilot-prod-push-confirm." >&2
    exit 1
fi

cat <<'EOF'

────────────────────────────────────────────────────────────────────
  Co-Pilot prod-push confirmation
────────────────────────────────────────────────────────────────────
  This push includes changes under:
    interface/copilot/      and/or
    src/Services/Copilot/

  Railway auto-deploys GitLab/main pushes into the production
  OpenEMR service (openemr-production-6c31.up.railway.app), which
  is what the testers are using. Only continue if you intend to
  ship this.

  Skip next time:  PREK_SKIP=copilot-prod-push-confirm git push
EOF

printf "\n  Continue? [y/N] "

read -r reply < /dev/tty || reply=""
case "$reply" in
    [yY] | [yY][eE][sS])
        echo "  → Continuing with push."
        exit 0
        ;;
    *)
        echo "  → Aborted."
        exit 1
        ;;
esac
