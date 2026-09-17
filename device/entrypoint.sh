#!/bin/sh
#
# Copyright (C) 2026 tebbi
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Idle engine. pymobiledevice3 connects to devices on demand when the module
# runs idevice-tool inside this container (podman exec); nothing needs to listen.
mkdir -p /var/lib/lockdown /data/backups
echo "idevice-backup engine ready (pymobiledevice3 ${PMD3_VERSION:-?})" >&2
exec sleep infinity
