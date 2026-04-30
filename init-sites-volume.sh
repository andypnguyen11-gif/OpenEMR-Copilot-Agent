#!/bin/sh
#
# First-boot volume initializer for the Railway openemr deploy.
#
# Shebang note: the openemr/openemr:latest image is Alpine-based; the
# `latest` tag's PATH at container-exec time does not resolve `bash`
# (`env: can't execute 'bash': No such file or directory` on every
# boot when we tried `#!/usr/bin/env bash`). POSIX `/bin/sh` is part
# of BusyBox and always present, so we use that and stick to POSIX
# constructs only — no `[[ ]]`, no `set -o pipefail`.
#
# Why this script exists: Railway mounts persistent volumes as empty
# bind mounts, which overlays (hides) the openemr image's bundled
# sites/default/. The upstream openemr.sh entrypoint does an
# unconditional require_once on sites/default/sqlconf.php (around line
# 729) under `set -euo pipefail`, so a missing file fatals the script
# before auto_configure.php can run. This wrapper copies the bundled
# sites/ template into the volume the first time we see an empty
# mount, then exec's the real entrypoint.
#
# After auto_configure.php (or a manual web install) lands a real
# sqlconf.php into the volume, every subsequent boot sees it and
# skips this restore.
set -eu

SITES_DIR="/var/www/localhost/htdocs/openemr/sites"
BUNDLED_DIR="/root/sites-bundled"

# We test for sqlconf.php specifically because:
#   - it ships in the bundled template (with $config=0), so its absence
#     proves the volume hasn't been initialized yet, and
#   - after install it stays present (with $config=1), so the guard
#     remains correct on every future boot.
if [ ! -f "$SITES_DIR/default/sqlconf.php" ]; then
    echo "[init-sites-volume] empty volume detected; restoring bundled sites/ from $BUNDLED_DIR"
    cp -a "$BUNDLED_DIR"/. "$SITES_DIR"/
    chown -R apache:apache "$SITES_DIR"
else
    echo "[init-sites-volume] $SITES_DIR/default/sqlconf.php exists; skipping restore"
fi

# Match the bundled image's CMD: ./openemr.sh from WORKDIR
# /var/www/localhost/htdocs/openemr (verified against openemr-devops
# docker/openemr/8.1.1/Dockerfile lines 233 and 331). openemr.sh's own
# shebang is `#!/usr/bin/env bash`, which somehow resolves at runtime
# inside the original CMD path (Docker may set up a richer PATH there).
# By the time openemr.sh runs, we have already done the volume-restore
# work that needed to happen first, so the bash-vs-sh asymmetry doesn't
# matter to us.
cd /var/www/localhost/htdocs/openemr
exec ./openemr.sh
