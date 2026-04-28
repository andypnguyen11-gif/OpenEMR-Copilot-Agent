# OpenEMR production image for Railway.
#
# Uses the stock openemr/openemr image without overlaying repo source.
# Source overlays caused version mismatches between the image's bundled
# auto_configure.php and our newer Installer class. To ship code changes
# in the future, layer in selective COPYs for /modules/custom_modules,
# /templates, /library/js, /interface/themes, and /public — those paths
# don't have signature dependencies on the image's core PHP.
FROM openemr/openemr:latest

EXPOSE 80
