#!/usr/bin/env bash
# Run tools/ssh-ramdisk-inject.sh in a small Linux VM, so the host kernel
# never loads the out-of-tree APFS driver (linux-apfs-rw).
#
#   arm64/apfs-helper-vm.sh <apfs.img> <staging dir>
#
# The VM boots the host's own kernel image with an initramfs. The initramfs
# holds the APFS driver built for that kernel, libcrc32c, the staging tree,
# the inject script and the programs it uses. The APFS image is the VM's disk.
# The VM copies the disk into RAM, runs the inject script on the copy, and
# writes the copy back to the disk.
#
# Inputs:
#   VPHONE_APFS_HELPER_KERNEL  the kernel image (default
#                              .build/apfs-helper/vmlinuz-$(uname -r))
#   VPHONE_APFS_KO             the APFS driver (default
#                              .build/linux-apfs-rw/apfs.ko)
#
# The VM uses KVM when /dev/kvm is writable, else TCG. It only runs this job.
# docs/ARM64.md tells you how to get the kernel image and build the driver.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
IMG=${1:?usage: apfs-helper-vm.sh <apfs.img> <staging dir>}
STAGE=${2:?usage: apfs-helper-vm.sh <apfs.img> <staging dir>}
KREL=$(uname -r)
KERNEL=${VPHONE_APFS_HELPER_KERNEL:-$ROOT/.build/apfs-helper/vmlinuz-$KREL}
APFS_KO=${VPHONE_APFS_KO:-$ROOT/.build/linux-apfs-rw/apfs.ko}
CRC_KO=/lib/modules/$KREL/kernel/lib/libcrc32c.ko.zst
QEMU=${QEMU:-$ROOT/qemu/build/qemu-system-aarch64}

for f in "$IMG" "$KERNEL" "$APFS_KO" "$CRC_KO" "$QEMU"; do
  [ -e "$f" ] || { echo "missing $f (docs/ARM64.md, the SSH ramdisk without a host APFS driver)" >&2; exit 1; }
done
[ -d "$STAGE" ] || { echo "missing staging dir $STAGE" >&2; exit 1; }
vermagic=$(modinfo -F vermagic "$APFS_KO" | cut -d' ' -f1)
[ "$vermagic" = "$KREL" ] || { echo "$APFS_KO is for kernel $vermagic, not $KREL" >&2; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
R=$WORK/root
mkdir -p "$R"/{bin,dev,proc,sys,tmp,mnt,lib/modules,tools,stage}

# The programs that the init script and tools/ssh-ramdisk-inject.sh use, and their libraries.
progs=(bash cat cp mkdir rm chown df sync grep id tail dirname sleep tar mount umount mountpoint losetup insmod find cmp wc)
for p in "${progs[@]}"; do
  path=$(command -v "$p")
  cp -L "$path" "$R/bin/$p"
  ldd "$path" | grep -o '/[^ ]*' | while read -r lib; do
    mkdir -p "$R$(dirname "$lib")"
    cp -L --update=none "$lib" "$R$lib"
  done
done
ln -s bin "$R/sbin"
mkdir -p "$R/usr"
ln -s ../bin "$R/usr/bin"
ln -s ../bin "$R/usr/sbin"

cp "$APFS_KO" "$R/lib/modules/apfs.ko"
zstd -q -d "$CRC_KO" -o "$R/lib/modules/libcrc32c.ko"
cp "$ROOT/tools/ssh-ramdisk-inject.sh" "$R/tools/"
cp -a "$STAGE/." "$R/stage/"

cat > "$R/init" <<'EOF'
#!/bin/bash
export PATH=/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mount -t tmpfs -o size=1200m tmpfs /tmp
result() { echo "APFS-HELPER: $1"; sync; echo o > /proc/sysrq-trigger; sleep 30; }
insmod /lib/modules/libcrc32c.ko && insmod /lib/modules/apfs.ko || result "FAIL insmod"
cat /dev/vda > /tmp/apfs.img || result "FAIL read disk"
/tools/ssh-ramdisk-inject.sh /tmp/apfs.img /stage /mnt/sshramdisk || result "FAIL inject $?"
cat /tmp/apfs.img > /dev/vda && sync || result "FAIL write disk"
# Check the written disk: every staged file is on it unchanged, or the inject script dropped it.
mount -t apfs -o ro,vol=0 /dev/vda /mnt/sshramdisk || result "FAIL check mount"
cd /stage
find . -type f > /tmp/staged || result "FAIL find"
same=0 absent=0 differ=0
while IFS= read -r f; do
  if [ ! -e "/mnt/sshramdisk/$f" ]; then absent=$((absent + 1)); echo "=== not on the disk (dropped): $f"
  elif cmp -s "$f" "/mnt/sshramdisk/$f"; then same=$((same + 1))
  else differ=$((differ + 1)); echo "=== differs: $f"; fi
done < /tmp/staged
cd /
[ "$same" -gt 0 ] || differ=$((differ + 1))
echo "=== check: $same staged files match, $absent dropped, $differ differ"
[ -x /mnt/sshramdisk/usr/local/bin/dropbear ] || differ=$((differ + 1))
umount /mnt/sshramdisk
[ "$differ" = 0 ] || result "FAIL check"
result OK
EOF
chmod 755 "$R/init"
(cd "$R" && find . | cpio -o -H newc --quiet | gzip -1) > "$WORK/initrd.gz"

if [ -w /dev/kvm ]; then accel=(-accel kvm -cpu host); else accel=(-accel tcg -cpu max); fi
LOG=$WORK/console.log
echo "=== APFS helper VM (${accel[1]}): $IMG"
timeout 900 "$QEMU" -M virt,gic-version=max "${accel[@]}" -smp 2 -m 2048 \
  -kernel "$KERNEL" -initrd "$WORK/initrd.gz" \
  -append "console=ttyAMA0 rdinit=/init panic=-1 loglevel=4" \
  -drive "file=$IMG,format=raw,if=none,id=d0,cache=writethrough" -device virtio-blk-pci,drive=d0 \
  -display none -monitor none -serial "file:$LOG" -no-reboot || true
grep -a -v '^\[' "$LOG" | grep -a -E '^(===|APFS-HELPER|Filesystem|/dev|injected|run as|missing)' || true
grep -aq '^APFS-HELPER: OK' "$LOG" || { echo "the APFS helper VM failed; its console:" >&2; tail -40 "$LOG" >&2; exit 1; }
