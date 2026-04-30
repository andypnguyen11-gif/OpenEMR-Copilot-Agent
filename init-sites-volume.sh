#!/bin/bash
#
# First-boot volume initializer for the Railway openemr deploy.
#
# Why this exists: Railway mounts persistent volumes as empty bind mounts,
# which overlays (hides) the openemr image's bundled sites/default/. The
# upstream openemr.sh entrypoint does an unconditional require_once on
# sites/default/sqlconf.php (around line 729) under `set -euo pipefail`,
# so a missing file fatals the script before auto_configure.php can run.
# This wrapper copies the bundled sites/ template into the volume the
# first time we see an empty mount, then exec's the real entrypoint.
#
# After auto_configure.php runs (or after a manual web install), the
# populated volume persists across redeploys — every subsequent boot
# sees sqlconf.php and skips this restore.
set -euo pipefail

SITES_DIR="/var/www/localhost/htdocs/openemr/sites"
BUNDLED_DIR="/root/sites-bundled"

# We test for sqlconf.php specifically because:
#   - it ships in the bundled template (with $config=0), so its absence
#     proves the volume hasn't been initialized yet, and
#   - after install it stays present (with $config=1), so the guard
#     remains correct on every future boot.
if [[ ! -f "$SITES_DIR/default/sqlconf.php" ]]; then
    echo "[init-sites-volume] empty volume detected; restoring bundled sites/ from $BUNDLED_DIR"
    cp -a "$BUNDLED_DIR"/. "$SITES_DIR"/
    chown -R apache:apache "$SITES_DIR"
else
    echo "[init-sites-volume] $SITES_DIR/default/sqlconf.php exists; skipping restore"
fi

# Match the bundled image's CMD: ./openemr.sh from WORKDIR
# /var/www/localhost/htdocs/openemr (verified against openemr-devops
# docker/openemr/8.1.1/Dockerfile lines 233 and 331).
cd /var/www/localhost/htdocs/openemr
exec ./openemr.sh
