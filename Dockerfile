# OpenEMR production image for Railway.
#
# Uses the stock openemr/openemr image without overlaying repo source.
# Source overlays caused version mismatches between the image's bundled
# auto_configure.php and our newer Installer class. To ship code changes
# in the future, layer in selective `COPY` directives for
# /modules/custom_modules, /templates, /library/js, /interface/themes,
# and /public — those paths don't have signature dependencies on the
# image's core PHP.
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

CMD ["/usr/local/bin/init-sites-volume.sh"]

EXPOSE 80
