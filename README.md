# VoodooSDHCI O2Micro 1217:7120

Standalone patch/build/test project for the O2Micro SD Host Controller `1217:7120` on Mac OS X Snow Leopard.

The project was split out of the legacy-laptop installer repository so driver reverse engineering, build recipes and runtime experiments can evolve independently from OpenCore/installer tooling.

## Hardware-tested status

Confirmed on Acer Aspire 4310 hardware:

- controller: O2Micro `1217:7120`
- subsystem: `1025:012f`
- revision: `02`
- OS: Mac OS X Snow Leopard 10.6.8 / Darwin 10.8.0
- kernel: i386

The current development build is intentionally conservative:

- PIO data path
- 1-bit SD bus
- read-only
- bounded command/reset/data polling
- correct SDHCI command-complete handling
- corrected SDHCI v1/v2 clock divisor sequencing
- card power before startup clock
- hot-remove power/reset recovery
- cold retry after failed reinsertion
- experimental bounded CMD18 multi-block PIO reads, max 128 sectors / 64 KiB per transaction

Write support, 4-bit mode and DMA are **not yet considered hardware-validated**.

## Source lineage

The repository does not vendor the upstream VoodooSDHCI tree. The recipe pins:

```text
upstream: https://github.com/coolstar/VoodooSDHCI.git
commit:   be8dc240a3b979d629660daea0d59c108ea86311
```

`prepare_o2micro_source.sh` exports exactly that revision into ignored `output/`, then applies the deterministic local patch/hardening sequence.

Historical bring-up notes are kept in [`docs/BRINGUP.md`](docs/BRINGUP.md).

## Repository layout

```text
scripts/cardreader/
  apply_o2micro_7120_patch.py     baseline PCI-ID/fail-safe patch
  harden_o2micro_cardinit.py      command completion, clock and init sequencing
  harden_o2micro_runtime.py       hot-remove/reinsert and bounded PIO recovery
  harden_o2micro_multiblock.py    experimental bounded CMD18 PIO reads
  prepare_o2micro_source.sh       reproducible source generator
  build_snowleopard.sh            Xcode 3.2 / i386 build + kextutil validation
  install_opencore_snowleopard.sh OpenCore install/backup helper
  remove_opencore_snowleopard.sh  OpenCore rollback helper
  bootstrap_*.sh                  host bootstrap helpers

.github/workflows/source-patch.yml
  regenerates the patched source and checks the expected safety properties
```

Generated trees and build output are ignored:

```text
cache/
output/
backup/
```

## Prepare source

On a modern macOS/Linux host with Git, Python 3 and tar:

```bash
./scripts/cardreader/prepare_o2micro_source.sh
```

Generated source:

```text
output/cardreader/VoodooSDHCI-O2Micro-7120/
```

## Build on Snow Leopard

Install Xcode 3.2.x with the MacOSX10.6 SDK, then:

```bash
./scripts/cardreader/build_snowleopard.sh \
  ./output/cardreader/VoodooSDHCI-O2Micro-7120
```

Expected KEXT:

```text
output/cardreader/VoodooSDHCI-O2Micro-7120/build-o2micro/VoodooSDHC.kext
```

The build helper verifies:

- Mach-O i386 architecture
- valid `Info.plist`
- `0x71201217` PCI match
- Snow Leopard kext linkage with `kextutil -n -t`

## Install through OpenCore

Preview only:

```bash
./scripts/cardreader/install_opencore_snowleopard.sh \
  ./output/cardreader/VoodooSDHCI-O2Micro-7120/build-o2micro/VoodooSDHC.kext \
  --disable-sle-conflicts
```

Apply:

```bash
./scripts/cardreader/install_opencore_snowleopard.sh \
  ./output/cardreader/VoodooSDHCI-O2Micro-7120/build-o2micro/VoodooSDHC.kext \
  --efi-device disk0s1 \
  --disable-sle-conflicts \
  --apply
```

The installer backs up `config.plist`, preserves the previous VoodooSDHC bundle if present, limits the OpenCore entry to Darwin 10.x/i386, and falls back to `mount_msdos` when Snow Leopard's `diskutil mount` refuses a valid FAT EFI partition.

Rollback:

```bash
./scripts/cardreader/remove_opencore_snowleopard.sh --efi-device disk0s1
```

## Test

First boot a diagnostic build **without an SD card inserted**.

Verify the kext:

```bash
kextstat | grep -i VoodooSDHC
ioreg -p IOService -w0 | egrep 'pci1217,7120|VoodooSDHC|IOBlockStorageDriver'
```

Insert a known-good card:

```bash
diskutil list
sudo dmesg | egrep -i 'VoodooSDHCI|O2:|CMD|PIO|timeout|error'
```

Sequential read test, replacing `disk1` with the actual SD device:

```bash
sudo dd if=/dev/rdisk1 of=/dev/null bs=1m count=256
```

Hot-remove testing should make outstanding I/O fail promptly, mark media offline, reset/power down the slot, and allow clean reinitialization after reinsertion.

## Development rule

Do not enable write support, 4-bit bus mode and DMA in one change. Validate one layer at a time on disposable media, keep recovery bounded, and preserve a known-good read-only build for rollback.
