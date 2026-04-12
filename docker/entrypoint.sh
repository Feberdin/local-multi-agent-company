#!/bin/sh
# Register all paths as git safe.directory so workers can operate on mounted volumes
# regardless of PUID/PGID mismatch between the container user and the host file owner.
git config --global --replace-all safe.directory '*'
exec "$@"
