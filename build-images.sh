#!/bin/bash

#
# Copyright (C) 2026 tebbi
# SPDX-License-Identifier: GPL-3.0-or-later
#

set -e

images=()
repobase="${REPOBASE:-ghcr.io/tebbiworld}"
reponame="idevice-backup"

# The engine image bundles pymobiledevice3 (pinned). The auto-release workflow
# bumps this reference when a newer pymobiledevice3 tag appears on GitHub.
pmd3_ref="github.com/doronz88/pymobiledevice3:11.14.0"
pmd3_version="${pmd3_ref##*:}"

app_image="${repobase}/idevice-backup-app:${IMAGETAG:-latest}"

echo "Build the pymobiledevice3 ${pmd3_version} engine image..."
podman build --force-rm --build-arg "PMD3_VERSION=${pmd3_version}" -t "${app_image}" -f device/Containerfile device/
podman tag "${app_image}" "${repobase}/idevice-backup-app"
images+=("${repobase}/idevice-backup-app")

runtime_images=(
    "${app_image}"
)

container=$(buildah from scratch)

if ! buildah containers --format "{{.ContainerName}}" | grep -q nodebuilder-idevice; then
    echo "Pulling NodeJS runtime..."
    buildah from --name nodebuilder-idevice -v "${PWD}:/usr/src:Z" docker.io/library/node:24.16.0-slim
fi

echo "Build static UI files with node..."
buildah run \
    --workingdir=/usr/src/ui \
    --env="NODE_OPTIONS=--openssl-legacy-provider" \
    nodebuilder-idevice \
    sh -c "yarn install && yarn build"

buildah add "${container}" imageroot /imageroot
buildah add "${container}" ui/dist /ui
# The cluster-admin UI needs no route, but the self-service web app does: it
# demands one TCP port (published on the node loopback for Traefik) and the
# routeadm authorization to publish/remove its Traefik route. The engine still
# reaches devices by outbound TCP only.
# The bulk-data volumes can be placed on an additional disk at install time.
buildah config --entrypoint=/ \
    --label="org.nethserver.rootfull=0" \
    --label="org.nethserver.tcp-ports-demand=1" \
    --label="org.nethserver.authorizations=traefik@node:routeadm node:portsadm" \
    --label="org.nethserver.images=${runtime_images[*]}" \
    --label="org.nethserver.volumes=idevice-data" \
    "${container}"
buildah commit "${container}" "${repobase}/${reponame}"

images+=("${repobase}/${reponame}")

if [[ -n "${CI}" ]]; then
    printf "images=%s\n" "${images[*],,}" >> "${GITHUB_OUTPUT}"
else
    printf "Publish the images with:\n\n"
    printf "  buildah push %s docker://%s\n" "${app_image,,}" "${app_image,,}"
    printf "  buildah push %s docker://%s:%s\n" "${repobase,,}/${reponame,,}" "${repobase,,}/${reponame,,}" "${IMAGETAG:-latest}"
    printf "\n"
fi
