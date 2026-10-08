#!/bin/bash -e

if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi

# Low disk builds (image/build.sh sets this when asked): the earlier stages' root file systems are not needed
# once copied here, and this stage holds the only image that is exported.
if [ "${WN_PRUNE_WORK:-0}" = "1" ]; then
	rm -rf "${WORK_DIR}/stage0/rootfs" "${WORK_DIR}/stage1/rootfs" "${WORK_DIR}/stage2/rootfs"
fi
