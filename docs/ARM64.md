# ARM64 (aarch64) Linux host

This page tells you how to build and run vphone-linux on an aarch64 Linux host.
The upstream project documents only x86_64 hosts.

## Tested host

- CPU: Ampere Altra, 128 Neoverse-N1 cores (ARMv8.2-A).
- GPU: NVIDIA RTX PRO 6000 Blackwell, driver 595.58.03.
- Host OS: Ubuntu 24.04, kernel 6.8.0-147-generic.
- Build and run environment: an Ubuntu 26.04 arm64 container (see below).

## How the guest runs

QEMU runs the iOS guest under TCG (software translation), as on x86_64.
KVM cannot run this guest on the tested host for two reasons:

- The guest needs Apple's GXF and SPRR extensions, which only QEMU models.
- The guest kernel is arm64e. It uses pointer authentication (PAC), and Neoverse-N1 does not have PAC.

Do not expect hardware acceleration of the CPU. The GPU work still goes to the
host GPU through reims-vgpu and metal2vulkan (Vulkan).

## What changed for aarch64

- `reims-vgpu` (fork `stevennevins/reims-vgpu`, branch `arm64-linux-host`):
  - The guest-memory atomic helpers assumed FEAT_LSE2. Neoverse-N1 has LSE and LRCPC, but not LSE2.
  - Without LSE2, a misaligned atomic faults and a `ldp`/`stp` pair is not one atomic access.
  - The helpers now detect LSE2 at run time. Without it, they do an `ldaxp`/`stlxp` read-modify-write of the aligned 16-byte granule.
  - The jemalloc allocator is now used on aarch64 Linux too.
- QEMU needs no source change. The x86-only fast paths (`host-fpcr.h`, `vec-f32-fast.h`, `TCG_TARGET_HAS_fp`) compile only on x86_64. On aarch64 every guest FP instruction uses softfloat.
- The glue scripts:
  - Use `Debug-linux-$(uname -m)` for the `linux-create` build output, not `Debug-linux-x86_64`.
  - `tools/check-host.sh` accepts aarch64 with LSE atomics and LRCPC.
  - `run-vphone.sh` also finds the NVIDIA and lavapipe Vulkan manifests at their aarch64 paths.

## The build container

`setup/setup.sh` needs packages that Ubuntu 24.04 does not have (for example
libtatsu and libimobiledevice-glue). `arm64/Dockerfile` makes an Ubuntu 26.04
arm64 image with the package list from `setup/lib/packages.sh`.
`arm64/vphone-container.sh` builds the image and runs one container.

The container is privileged. It shares these items with the host:

- `/dev`, for loop devices, USB/IP (`vhci-hcd`), `/dev/vhost-vsock` and the GPU.
- The network, the IPC namespace and the process ID namespace.
- `/lib/modules` (read-only) and `/tmp/.X11-unix`.

Note: a computer-use driver on the host finds a window by its process ID. The
shared process ID namespace gives QEMU the same process ID inside and outside
the container.

The NVIDIA container runtime gives the container the driver and its Vulkan
manifest, `/etc/vulkan/icd.d/nvidia_icd.json`.

### USB devices of the VM in the container

The restore connects to the VM as a USB device over USB/IP (`vhci-hcd`).
On an x86_64 host, `setup.sh` installs a udev rule that lets your user open
the VM's DFU and recovery devices. The container has no udev daemon, and the
host does not have the rule. Two changes replace the rule:

- `tools/usbip-autoattach.sh` sets mode 0666 on Apple DFU and recovery
  devices attached through `vhci-hcd`. It does this every 0.5 s. Other USB
  devices stay unchanged.
- The bundled libirecovery (`linux-create/edits/MobileRecoveryCore.txt`)
  retries the open of an arriving device for up to 5 s while access is
  denied. Without the retry, the restore misses the device that iBSS brings
  back in recovery mode.

Note: `tools/usbip-autoattach.sh` loads the host kernel modules `vhci-hcd`
and `usbip-core` (in-tree modules) the first time it runs.

### Restore fixes

These fixes are not specific to aarch64. The restore on the tested host found them:

- `tools/vusb-usbip.py` read and wrote each USB/IP unlink as 24 bytes. Every USB/IP command and reply header is 48 bytes. The bridge then read the padding as a new command ("unsupported USB/IP command 0"), and the restore stopped with "Could not read data (-256)".
- After the bridge answered an unlink with status 0, it sent a RET_SUBMIT for the same USB request block (URB). `vhci-hcd` had already given that URB back. It logged "cannot find a urb of seqnum" and disconnected the device. Now a URB gets its RET_SUBMIT before the unlink answer (status -ENOENT), or no RET_SUBMIT.
- The bridge failed a bulk IN URB when the guest sent nothing for `VUSB_URB_TIMEOUT` seconds (300 s from `tools/dfu-up.sh`). Under TCG the RestoreOS can be quiet for longer, for example while it unmounts its filesystems after the system image. The failed URB had status -EIO, and usbmuxd removed the device ("unrecognised urb status -5"). Now a bulk IN URB waits with no time limit, as on a real host controller. A bulk OUT URB that times out now has status -ETIMEDOUT.
- `setup/create-vm.sh` now blanks `Disk.img`, `nvram.bin` and `SEPStorage` before each restore attempt. An old `nvram.bin` can stop the restore at "Waiting for device to enter restore mode". An old APFS container on `Disk.img` makes `format_media` fail ("Resource busy").

Two tests check the bridge:

```bash
python3 -I setup/test/vusb-usbip-unlink.py tools/vusb-usbip.py
```

```bash
python3 -I setup/test/vusb-usbip-timeout.py tools/vusb-usbip.py
```

### Device profile and firmware

`create-vm.sh --device iPhone18,1 --ios 26.6.2` is the iPhone 17 Pro entry in `compatibility.json`. Its firmware is `iPhone17,3_26.6.2_23G90_Restore.ipsw` from Apple's public servers. vphone-cli reports the guest as "iPhone 16 (iPhone17,3), display 1290x2796 @ 460 ppi".

## Procedure

1. Start the container:

   ```bash
   arm64/vphone-container.sh up
   ```

2. Build the Rust parts, QEMU and the firmware tool:

   ```bash
   arm64/vphone-container.sh exec 'make -C reims-vgpu'
   ```

   ```bash
   arm64/vphone-container.sh exec 'cd qemu && mkdir -p build && cd build && PKG_CONFIG_PATH=$PWD/../../reims-vgpu/target/dist-vulkan/lib/pkgconfig ../configure --target-list=aarch64-softmmu --enable-pvg --enable-lzfse --enable-slirp --enable-libssh --enable-zstd --enable-nettle --enable-gnutls --enable-gtk --enable-sdl --disable-werror --disable-qom-cast-debug && ninja qemu-system-aarch64'
   ```

   ```bash
   arm64/vphone-container.sh exec 'linux-create/build.sh'
   ```

3. Make the VM with firmware from Apple's public servers:

   ```bash
   arm64/vphone-container.sh exec './setup/create-vm.sh --device iPhone18,1 --ios 26.6.2 -y'
   ```

   The last step of `create-vm.sh` starts the VM in a window. Set `DISPLAY` in the command if the container has no default display.

4. To start the VM again later, start an X server on the host on a free display, for example `:91`. Then run the VM in the container:

   ```bash
   arm64/vphone-container.sh exec 'DISPLAY=:91 GFX=1 WINDOW=1 ./run-vphone.sh ~/vphone-setup-work/devices/vphone-iPhone18-1-2662'
   ```

CAUTION: do not run `setup/setup.sh` host settings on a shared host. They make `/dev/vhost-vsock` writable by all users.
The container user is in the host group that owns `/dev/vhost-vsock` (on Ubuntu, `kvm`). Add your host user to that group instead.
