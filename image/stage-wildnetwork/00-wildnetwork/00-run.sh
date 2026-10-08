#!/bin/bash -e
# WildNetwork Base software: the same provisioning script as a live install, run inside the image chroot.

install -d "${ROOTFS_DIR}/tmp/wn-src"
cp -a files/src/. "${ROOTFS_DIR}/tmp/wn-src/"
on_chroot <<CHROOT
WN_USER="${FIRST_USER_NAME}" WN_SRC=/tmp/wn-src bash /tmp/wn-src/base/provision.sh --image
CHROOT
rm -rf "${ROOTFS_DIR}/tmp/wn-src"
