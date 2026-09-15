#!/usr/bin/env python3
from pathlib import Path
import re
import sys


def die(message: str) -> None:
    print(f"[o2micro-multiblock] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        die(f"{label}: expected 1 match, found {count}")
    path.write_text(text.replace(old, new, 1))
    print(f"[o2micro-multiblock] {label}")


def replace_exact(path: Path, old: str, new: str, expected: int, label: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != expected:
        die(f"{label}: expected {expected} matches, found {count}")
    path.write_text(text.replace(old, new))
    print(f"[o2micro-multiblock] {label}")


def regex_replace_once(path: Path, pattern: str, replacement: str, label: str) -> None:
    text = path.read_text()
    new_text, count = re.subn(pattern, lambda _m: replacement, text, count=1, flags=re.S)
    if count != 1:
        die(f"{label}: expected 1 match, found {count}")
    path.write_text(new_text)
    print(f"[o2micro-multiblock] {label}")


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} /path/to/VoodooSDHCI", file=sys.stderr)
        raise SystemExit(2)

    cpp = Path(sys.argv[1]).resolve() / "VoodooSDHC.cpp"
    if not cpp.is_file():
        die(f"missing {cpp}")

    replace_once(
        cpp,
        "#define USE_MULTIBLOCK\t0",
        "#define USE_MULTIBLOCK\t1",
        "enable multiblock PIO reads",
    )

    replace_once(
        cpp,
        "#define SDHCI_DATA_TIMEOUT_US       3000000U\n",
        "#define SDHCI_DATA_TIMEOUT_US       3000000U\n"
        "#define O2_PIO_MAX_BLOCKS           128U\n",
        "limit multiblock PIO chunks to 64 KiB",
    )

    multi_fn = r'''IOReturn VoodooSDHC::readBlockMulti_pio(IOMemoryDescriptor *buffer,
					UInt32 block, UInt32 nblks,
					UInt32 offset) {
	UInt8 buff[512];
	UInt32 *pBuff = (UInt32*)buff;
	IOReturn ret = kIOReturnError;
	UInt32 i = 0;
	UInt32 waited = 0;
	UInt16 nis = 0;
	UInt16 eis = 0;

	if (nblks == 0)
		return kIOReturnSuccess;
	if (!isCardPresent(0))
		return kIOReturnNoMedia;

#ifndef NO_RESET_WAR
	if (!Reset(0, CMD_RESET) || !Reset(0, DAT_RESET)) {
		IOLog("VoodooSDHCI/O2: PIO multi pre-read reset failed block=%u count=%u\n",
			block, nblks);
		return kIOReturnTimeout;
	}
#endif

	if (!isCardPresent(0))
		return kIOReturnNoMedia;

	this->PCIRegP[0]->BlockSize = 512;
	this->PCIRegP[0]->BlockCount = nblks;
	this->PCIRegP[0]->TransferMode = SDHCI_TRNS_READ | SDHCI_TRNS_MULTI |
		SDHCI_TRNS_BLK_CNT_EN | SDHCI_TRNS_ACMD12;
	this->PCIRegP[0]->NormalIntStatusEn = 0xffff;
	this->PCIRegP[0]->ErrorIntStatusEn = 0xffff;
	this->PCIRegP[0]->NormalIntStatus =
		BuffReadReady | XferComplete | CmdComplete | ErrorInterrupt;
	this->PCIRegP[0]->ErrorIntStatus = 0xffff;
	this->PCIRegP[0]->TimeoutControl = 0xe;
	::OSSynchronizeIO();

	if (!SDCommand(0, SD_READ_MULTIPLE_BLOCK, SDCR18,
			isHighCapacity ? block : block * 512)) {
		ret = isCardPresent(0) ? kIOReturnTimeout : kIOReturnNoMedia;
		IOLog("VoodooSDHCI/O2: CMD18 failed block=%u count=%u present=%u\n",
			block, nblks, isCardPresent(0) ? 1U : 0U);
		goto out;
	}

	for (i = 0; i < nblks; i++) {
		bool gotData = false;
		for (waited = 0; waited < SDHCI_DATA_TIMEOUT_US;
				waited += SDHCI_POLL_STEP_US) {
			if (!isCardPresent(0)) {
				IOLog("VoodooSDHCI/O2: card removed during CMD18 block=%u index=%u/%u\n",
					block, i, nblks);
				ret = kIOReturnNoMedia;
				goto out;
			}

			nis = this->PCIRegP[0]->NormalIntStatus;
			eis = this->PCIRegP[0]->ErrorIntStatus;
			if ((nis & ErrorInterrupt) || eis) {
				IOLog("VoodooSDHCI/O2: CMD18 data error block=%u index=%u/%u NIS=0x%04x EIS=0x%04x PS=0x%08x\n",
					block, i, nblks, nis, eis, this->PCIRegP[0]->PresentState);
				ret = kIOReturnTimeout;
				goto out;
			}

			if (nis & BuffReadReady) {
				this->PCIRegP[0]->NormalIntStatus = BuffReadReady;
				::OSSynchronizeIO();
				read_block_pio(&this->PCIRegP[0]->BufferDataPort, pBuff);
				buffer->writeBytes((offset + i) * 512, buff, 512);
				gotData = true;
				break;
			}
			IODelay(SDHCI_POLL_STEP_US);
		}

		if (!gotData) {
			IOLog("VoodooSDHCI/O2: CMD18 data-ready timeout block=%u index=%u/%u NIS=0x%04x EIS=0x%04x PS=0x%08x\n",
				block, i, nblks,
				this->PCIRegP[0]->NormalIntStatus,
				this->PCIRegP[0]->ErrorIntStatus,
				this->PCIRegP[0]->PresentState);
			ret = kIOReturnTimeout;
			goto out;
		}
	}

	for (waited = 0; waited < SDHCI_DATA_TIMEOUT_US;
			waited += SDHCI_POLL_STEP_US) {
		if (!isCardPresent(0)) {
			ret = kIOReturnNoMedia;
			goto out;
		}

		nis = this->PCIRegP[0]->NormalIntStatus;
		eis = this->PCIRegP[0]->ErrorIntStatus;
		if ((nis & ErrorInterrupt) || eis) {
			IOLog("VoodooSDHCI/O2: CMD18 completion error block=%u count=%u NIS=0x%04x EIS=0x%04x PS=0x%08x\n",
				block, nblks, nis, eis, this->PCIRegP[0]->PresentState);
			ret = kIOReturnTimeout;
			goto out;
		}
		if (nis & XferComplete) {
			this->PCIRegP[0]->NormalIntStatus = XferComplete;
			::OSSynchronizeIO();
			ret = kIOReturnSuccess;
			goto out;
		}
		IODelay(SDHCI_POLL_STEP_US);
	}

	IOLog("VoodooSDHCI/O2: CMD18 transfer-complete timeout block=%u count=%u NIS=0x%04x EIS=0x%04x PS=0x%08x\n",
		block, nblks,
		this->PCIRegP[0]->NormalIntStatus,
		this->PCIRegP[0]->ErrorIntStatus,
		this->PCIRegP[0]->PresentState);
	ret = kIOReturnTimeout;

out:
	this->PCIRegP[0]->NormalIntStatus =
		BuffReadReady | XferComplete | CmdComplete | ErrorInterrupt;
	this->PCIRegP[0]->ErrorIntStatus = 0xffff;
	::OSSynchronizeIO();

	if (ret != kIOReturnSuccess) {
		Reset(0, CMD_RESET);
		Reset(0, DAT_RESET);
	}
	return ret;
}'''

    regex_replace_once(
        cpp,
        r"IOReturn VoodooSDHC::readBlockMulti_pio\(IOMemoryDescriptor \*buffer,\n\s*UInt32 block, UInt32 nblks,\n\s*UInt32 offset\) \{.*?\n\}\n\n/\*\n \* sdma_access:",
        multi_fn + "\n\n/*\n * sdma_access:",
        "replace legacy CMD18 PIO path with bounded removal-aware implementation",
    )

    replace_exact(
        cpp,
        "\t\t\t\tint b = MIN(2048 /* should fit in sdma buff */, n);",
        "\t\t\t\tUInt32 b = MIN(O2_PIO_MAX_BLOCKS, n);",
        2,
        "chunk both multiblock callers at 128 sectors",
    )

    replace_once(
        cpp,
        "\tcase kIOReturnNoMedia:\n"
        "\tcase kIOReturnTimeout:\n"
        "\t\tcardPresence = kCardRemount;\n"
        "\t\tIOLog(\"VoodooSDHCI/O2: read path requires media remount ret=0x%x\\n\", ret);\n"
        "\t\tbreak;",
        "\tcase kIOReturnNoMedia:\n"
        "\tcase kIOReturnTimeout:\n"
        "\t\tif (cardPresence != kCardRemount)\n"
        "\t\t\tIOLog(\"VoodooSDHCI/O2: read path requires media remount ret=0x%x\\n\", ret);\n"
        "\t\tcardPresence = kCardRemount;\n"
        "\t\tif (timerSrc)\n"
        "\t\t\ttimerSrc->setTimeoutMS(10);\n"
        "\t\tbreak;",
        "kick media-state timer and suppress repeated remount logging",
    )

    print("[o2micro-multiblock] bounded CMD18 PIO performance hardening applied")


if __name__ == "__main__":
    main()
