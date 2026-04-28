# OpenEMR production image for Railway.
#
# Layers your repo source on top of the official openemr/openemr image so
# pushes to GitLab actually deploy your code changes. Vendor and node_modules
# are inherited from the base image (see .dockerignore) — rebuild the base
# image manually if you change composer.json or package.json.
FROM openemr/openemr:latest

COPY --chown=apache:apache . /var/www/localhost/htdocs/openemr/

EXPOSE 80
