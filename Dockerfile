# OpenEMR production image for Railway.
#
# Stock openemr/openemr base + a narrow overlay for the Clinical Co-Pilot
# (M3) feature. The previous full-source overlay broke the image's
# bundled auto_configure.php / Installer class — this overlay
# deliberately stays out of those paths and only writes to:
#
#   * /src/Services/Copilot/                            (new namespace, PSR-4)
#   * /apis/routes/_rest_routes_copilot.inc.php         (new gateway routes)
#   * /apis/routes/_rest_routes_standard.inc.php        (modified — adds the copilot include)
#   * /interface/copilot/                                (new — chat / daily brief / side panel)
#   * /public/copilot/                                  (new — JS/CSS)
#   * /interface/main/tabs/menu/menus/standard.json     (modified — top-nav entry)
#
# None of these paths are touched by the installer or by openemr.sh's
# bootstrap, so they're safe to layer on top of the stock image.
FROM openemr/openemr:latest

# Stash the image's bundled sites/ tree so we can restore it onto an
# empty Railway volume mount on first boot. Railway bind-mount volumes
# start empty, which overlays (hides) the bundled sites/default/ that
# openemr.sh requires — without it, the entrypoint fatals on a missing
# sqlconf.php (line 729 in upstream openemr.sh, unconditional require_once
# under `set -euo pipefail`) before auto_configure.php can ever run.
# The image's built-in /swarm-pieces feature only restores the directory
# structure, not the bundled config templates, so we keep our own copy.
# `cp -a` preserves the apache:apache ownership and the 666 mode on
# sqlconf.php that auto_configure.php expects to be able to overwrite.
USER root
RUN cp -a /var/www/localhost/htdocs/openemr/sites /root/sites-bundled

# Wrap the bundled openemr.sh entrypoint to populate an empty volume
# on first boot, then hand off unchanged.
COPY init-sites-volume.sh /usr/local/bin/init-sites-volume.sh
RUN chmod +x /usr/local/bin/init-sites-volume.sh

# --- Clinical Co-Pilot (M3) overlay ---------------------------------------
ARG OPENEMR_ROOT=/var/www/localhost/htdocs/openemr

COPY --chown=apache:apache src/Services/Copilot/ \
     ${OPENEMR_ROOT}/src/Services/Copilot/
COPY --chown=apache:apache apis/routes/_rest_routes_copilot.inc.php \
     ${OPENEMR_ROOT}/apis/routes/_rest_routes_copilot.inc.php
COPY --chown=apache:apache apis/routes/_rest_routes_standard.inc.php \
     ${OPENEMR_ROOT}/apis/routes/_rest_routes_standard.inc.php
COPY --chown=apache:apache interface/copilot/ \
     ${OPENEMR_ROOT}/interface/copilot/
COPY --chown=apache:apache public/copilot/ \
     ${OPENEMR_ROOT}/public/copilot/
COPY --chown=apache:apache interface/main/tabs/menu/menus/standard.json \
     ${OPENEMR_ROOT}/interface/main/tabs/menu/menus/standard.json

# Refresh the Composer classmap so PSR-4 picks up the new
# OpenEMR\Services\Copilot\* classes even if the base image was built
# with --classmap-authoritative. Best-effort: skipped if composer was
# stripped from the production image (PSR-4 fallback usually still
# works without it).
RUN if command -v composer >/dev/null 2>&1; then \
        cd ${OPENEMR_ROOT} && composer dump-autoload --no-dev -o; \
    else \
        echo "composer not present in image; relying on PSR-4 fallback"; \
    fi
# --- end overlay ----------------------------------------------------------

CMD ["/usr/local/bin/init-sites-volume.sh"]

EXPOSE 80
