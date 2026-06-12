#!/bin/sh
set -e

# Bind-mounted host dirs are often root-owned; zzk (uid 1000) must own them.
for dir in /app/data /app/recordings; do
    mkdir -p "$dir"
    chown -R zzk:zzk "$dir" 2>/dev/null || chmod -R u+rwX,g+rwX "$dir" 2>/dev/null || true
done

exec su-exec zzk "$@"