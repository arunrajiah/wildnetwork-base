#!/bin/bash -e
# BirdNET-Go for offline first boot: the arm64 container as a docker-load tarball, and its default config.
# Runs in the build container (not the chroot) and needs no Docker daemon.

# shellcheck source=/dev/null
source files/VERSIONS
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

arch="$(uname -m)"; [ "${arch}" = aarch64 ] && arch=arm64
sum_var="CRANE_SHA256_Linux_${arch}"
curl -fsSL "https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/go-containerregistry_Linux_${arch}.tar.gz" -o "${work}/crane.tgz"
echo "${!sum_var}  ${work}/crane.tgz" | sha256sum -c -
tar -xzf "${work}/crane.tgz" -C "${work}" crane

digest="$("${work}/crane" digest "${BIRDNET_GO_IMAGE}")"
if [ "${digest}" != "${BIRDNET_GO_DIGEST}" ]; then
	echo "BirdNET-Go ${BIRDNET_GO_IMAGE} is now ${digest}, expected ${BIRDNET_GO_DIGEST}"
	exit 1
fi
install -d "${ROOTFS_DIR}/var/lib/wnbase" "${ROOTFS_DIR}/etc/wnbase" "${ROOTFS_DIR}/opt/wnbase"
"${work}/crane" pull --platform linux/arm64 "${BIRDNET_GO_IMAGE}" "${ROOTFS_DIR}/var/lib/wnbase/birdnet-go.tar"
echo "${BIRDNET_GO_IMAGE}" > "${ROOTFS_DIR}/etc/wnbase/birdnet-go-image"
install -m 644 files/VERSIONS "${ROOTFS_DIR}/etc/wnbase/VERSIONS"

# Default config of that release, with the clip folder made absolute as BirdNET-Go's installer does.
curl -fsSL "${BIRDNET_GO_CONFIG_URL}" -o "${work}/config.yaml"
sed -i -E 's|^([[:space:]]*path:[[:space:]]*)clips/([[:space:]].*)?$|\1/data/clips/\2|' "${work}/config.yaml"
grep -q 'path: /data/clips/' "${work}/config.yaml"
install -m 644 "${work}/config.yaml" "${ROOTFS_DIR}/opt/wnbase/birdnet-go-config.yaml"
