# Build and run environment for an ARM64 (aarch64) Linux host whose distro is
# older than setup.sh supports (for example Ubuntu 24.04: no libtatsu or
# libimobiledevice-glue). The image is Ubuntu 26.04 arm64 with the same package
# list as setup.sh (setup/lib/packages.sh), plus the tools the headless test
# needs (Xvfb, xdotool, ffmpeg).
#
# arm64/vphone-container.sh builds this image and starts it. Your checkout is
# mounted at the same path, so paths in the scripts and docs stay the same.
FROM ubuntu:26.04

ARG USER_NAME=vphone
ARG USER_UID=1000
ARG USER_GID=1000
# The host group that owns /dev/vhost-vsock (kvm). QEMU opens it as the user.
ARG VSOCK_GID=

ENV DEBIAN_FRONTEND=noninteractive
COPY setup/lib/packages.sh /tmp/packages.sh
RUN bash -c 'source /tmp/packages.sh && apt-get update && \
    apt-get install -y --no-install-recommends "${DEBIAN_PKGS[@]}" \
      linux-tools-generic kmod sudo ca-certificates file procps iproute2 less \
      xvfb xdotool x11-utils x11-xserver-utils ffmpeg imagemagick openbox \
      python3-pip python3-venv python3-numpy e2fsprogs dosfstools bzip2 cpio && \
    rm -rf /var/lib/apt/lists/*'
# Ubuntu ships usbip in linux-tools for one kernel version; link the newest copy.
RUN for t in usbip usbipd; do u=$(find /usr/lib -path '*/linux-tools-*' -name "$t" | sort -V | tail -1); \
      [ -n "$u" ] && ln -sf "$u" "/usr/local/bin/$t"; done
# NVIDIA's container runtime (NVIDIA_DRIVER_CAPABILITIES=all) mounts the driver and its Vulkan
# manifest, /etc/vulkan/icd.d/nvidia_icd.json, into the container.
RUN (getent group "$USER_GID" >/dev/null || groupadd -g "$USER_GID" "$USER_NAME") && \
    (id -u "$USER_UID" >/dev/null 2>&1 && userdel -r "$(id -nu "$USER_UID")" || true) && \
    useradd -m -u "$USER_UID" -g "$USER_GID" -s /bin/bash "$USER_NAME" && \
    if [ -n "$VSOCK_GID" ]; then (getent group "$VSOCK_GID" >/dev/null || groupadd -g "$VSOCK_GID" vsockhost) && \
      usermod -aG "$(getent group "$VSOCK_GID" | cut -d: -f1)" "$USER_NAME"; fi && \
    echo "$USER_NAME ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/vphone && chmod 440 /etc/sudoers.d/vphone
USER $USER_NAME
