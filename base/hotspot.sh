#!/usr/bin/env bash
# Start the Wildlife Network Base hotspot now (for example over a cable, or to set the Base up again).
# The hotspot is normally started by wn-net.service (base/wnnet.py) only when home Wi-Fi is not available.
# --force is accepted for older instructions; it is the same as no option. This drops a Wi-Fi SSH session.
set -euo pipefail
exec python3 /opt/wnbase/wnnet.py --config "${WNBASE_CONFIG:-/etc/wnbase/wnbase.ini}" --hotspot
