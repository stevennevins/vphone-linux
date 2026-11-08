#!/usr/bin/env bash
# Build the arm64/Dockerfile image and keep one container running for the
# build, the VM creation and the VM itself.
#
#   arm64/vphone-container.sh up        # build the image, start the container
#   arm64/vphone-container.sh shell     # a shell in it, as you
#   arm64/vphone-container.sh root      # a shell in it, as root
#   arm64/vphone-container.sh exec CMD  # run CMD in it, as you
#   arm64/vphone-container.sh down      # stop and remove the container
#
# The container is privileged and shares the host's /dev, network, process IDs
# and kernel modules: the VM creation needs loop devices and USB/IP (vhci-hcd),
# QEMU needs /dev/vhost-vsock and the GPU, and host tools that find a window by
# its process ID (a computer-use driver) need QEMU's host PID. The user is in
# the group that owns /dev/vhost-vsock on the host (kvm), as on the host. Your checkout is mounted
# at its own path. The container's home folder is $VPHONE_HOME (default
# ~/.cache/vphone-arm64-home), which keeps Swift, Rust and the firmware
# downloads between containers.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
name=${VPHONE_CONTAINER:-vphone-arm64}
image=${VPHONE_IMAGE:-vphone-linux-arm64:ubuntu26.04}
vhome=${VPHONE_HOME:-$HOME/.cache/vphone-arm64-home}
user=$(id -un) uid=$(id -u) gid=$(id -g)

[ "$(uname -m)" = aarch64 ] || echo "WARN: this container is for aarch64 hosts (this is $(uname -m))" >&2

up() {
  docker build -t "$image" --build-arg USER_NAME="$user" --build-arg USER_UID="$uid" \
    --build-arg USER_GID="$gid" --build-arg VSOCK_GID="$(stat -c %g /dev/vhost-vsock 2>/dev/null)" -f "$root/arm64/Dockerfile" "$root"
  if docker inspect "$name" >/dev/null 2>&1; then
    echo "container $name already exists (arm64/vphone-container.sh down to recreate it)"; return
  fi
  mkdir -p "$vhome" "$(dirname "$vhome")/vphone-arm64-x11"
  local gpu=()
  if docker info 2>/dev/null | grep -q 'Runtimes:.*nvidia'; then
    gpu=(--runtime nvidia --gpus all -e NVIDIA_DRIVER_CAPABILITIES=all)
  fi
  docker run -d --name "$name" --hostname "$name" --privileged --network host --ipc host --pid host \
    "${gpu[@]}" \
    -v /dev:/dev -v /lib/modules:/lib/modules:ro -v /usr/src:/usr/src:ro \
    -v /tmp/.X11-unix:/tmp/.X11-unix \
    -v "$vhome:/home/$user" -v "$root:$root" -w "$root" \
    -e HOME="/home/$user" -e USER="$user" \
    "$image" sleep infinity >/dev/null
  echo "container $name is running ($image); repo at $root"
}

case ${1:-} in
  up) up ;;
  shell) docker exec -it -u "$user" -w "$root" "$name" bash -l ;;
  root) docker exec -it -u 0 -w "$root" "$name" bash -l ;;
  exec) shift; docker exec -u "$user" -w "$root" "$name" bash -lc "$*" ;;
  rootexec) shift; docker exec -u 0 -w "$root" "$name" bash -lc "$*" ;;
  down) docker rm -f "$name" ;;
  *) sed -n '2,19p' "$0"; exit 2 ;;
esac
