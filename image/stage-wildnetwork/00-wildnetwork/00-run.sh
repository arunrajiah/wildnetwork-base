#!/bin/bash -e
# WildNetwork Base software: the same provisioning script as a live install, run inside the image chroot.
# Not under /tmp: pi-gen mounts a fresh tmpfs there inside the chroot.

install -d "${ROOTFS_DIR}/usr/local/src/wn-src"
cp -a files/src/. "${ROOTFS_DIR}/usr/local/src/wn-src/"
on_chroot <<CHROOT
WN_USER="${FIRST_USER_NAME}" WN_SRC=/usr/local/src/wn-src bash /usr/local/src/wn-src/base/provision.sh --image
CHROOT
rm -rf "${ROOTFS_DIR}/usr/local/src/wn-src"
