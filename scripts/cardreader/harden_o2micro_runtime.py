#!/usr/bin/env python3
from pathlib import Path
import re
import sys


def die(message: str) -> None:
    print(f"[o2micro-runtime] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        die(f"{label}: expected 1 match, found {count}")
    path.write_text(text.replace(old, new, 1))
    print(f"[o2micro-runtime] {label}")


def regex_replace_once(path: Path, pattern: str, replacement: str, label: str) -> None:
    text = path.read_text()
    new_text, count = re.subn(pattern, lambda _m: replacement, text, count=1, flags=re.S)
    if count != 1:
        die(f"{label}: expected 1 match, found {count}")
    path.write_text(new_text)
    print(f"[o2micro-runtime] {label}")


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} /path/to/VoodooSDHCI", file=sys.stderr)
        raise SystemExit(2)

    cpp = Path(sys.argv[1]).resolve() / "VoodooSDHC.cpp"
    if not cpp.is_file():
        die(f"missing {cpp}")

    # Keep the normal build quiet. The previous diagnostic build logged every
    # CMD17 and every completed block, which makes Finder/Preview stress tests
    # needlessly expensive on the single-core Acer. Error/recovery logs below
    # remain unconditional.
    replace_once(
        cpp,
        "#define __DEBUG__\t1",
        "//#define __DEBUG__\t1",
        "disable per-block diagnostic spam",
    )

    # Bound the PIO data phase separately from command completion. The original
    # single-block path could spin for roughly 11 seconds per 512-byte request.
    replace_once(
        cpp,
        "#define SDHCI_INT_TIMEOUT_US        5000000U\n",
        "#define SDHCI_INT_TIMEOUT_US        5000000U\n"
        "#define SDHCI_DATA_TIMEOUT_US       3000000U\n",
        "add bounded PIO data timeout",
    )

    # Give a reinserted card a real power-off interval. This is deliberately
    # conservative; startup is not performance-sensitive.
    replace_once(
        cpp,
        "\tthis->PCIRegP[slot]->PowerControl = 0;\n"
        "\t::OSSynchronizeIO();\n"
        "\tIODelay(1000);\n"
        "\tthis->PCIRegP[slot]->PowerControl = voltage | SDPower;",
        "\tthis->PCIRegP[slot]->PowerControl = 0;\n"
        "\t::OSSynchronizeIO();\n"
        "\tIODelay(10000);\n"
        "\tthis->PCIRegP[slot]->PowerControl = voltage | SDPower;",
        "extend SD power-cycle off time",
    )

    read_single_fn = r'''IOReturn VoodooSDHC::readBlockSingle_pio(UInt8 *buff, UInt32 block) {
	UInt32 *pBuff = (UInt32*)buff;
	IOReturn ret = kIOReturnError;

	if (!isCardPresent(0))
		return kIOReturnNoMedia;

#ifndef NO_RESET_WAR
	if (!Reset(0, CMD_RESET) || !Reset(0, DAT_RESET)) {
		IOLog("VoodooSDHCI/O2: PIO pre-read reset failed block=%u\n", block);
		return kIOReturnTimeout;
	}
#endif

	if (!isCardPresent(0))
		return kIOReturnNoMedia;

	this->PCIRegP[0]->TransferMode = BIT4;
	this->PCIRegP[0]->BlockSize = 512;
	this->PCIRegP[0]->BlockCount = 1;
	this->PCIRegP[0]->NormalIntStatusEn = 0xffff;
	this->PCIRegP[0]->ErrorIntStatusEn = 0xffff;
	this->PCIRegP[0]->NormalIntStatus =
		BuffReadReady | XferComplete | CmdComplete | ErrorInterrupt;
	this->PCIRegP[0]->ErrorIntStatus = 0xffff;
	this->PCIRegP[0]->TimeoutControl = 0xe;
	::OSSynchronizeIO();

	if (!SDCommand(0, SD_READ_SINGLE_BLOCK, SDCR17,
			isHighCapacity ? block : block * 512)) {
		ret = isCardPresent(0) ? kIOReturnTimeout : kIOReturnNoMedia;
		IOLog("VoodooSDHCI/O2: CMD17 failed block=%u present=%u\n",
			block, isCardPresent(0) ? 1U : 0U);
		goto out;
	}

	for (UInt32 waited = 0; waited < SDHCI_DATA_TIMEOUT_US;
			waited += SDHCI_POLL_STEP_US) {
		if (!isCardPresent(0)) {
			IOLog("VoodooSDHCI/O2: card removed while waiting for PIO data block=%u\n", block);
			ret = kIOReturnNoMedia;
			goto out;
		}

		UInt16 nis = this->PCIRegP[0]->NormalIntStatus;
		UInt16 eis = this->PCIRegP[0]->ErrorIntStatus;
		if ((nis & ErrorInterrupt) || eis) {
			IOLog("VoodooSDHCI/O2: PIO data error block=%u NIS=0x%04x EIS=0x%04x PS=0x%08x\n",
				block, nis, eis, this->PCIRegP[0]->PresentState);
			ret = kIOReturnTimeout;
			goto out;
		}
		if (nis & BuffReadReady) {
			this->PCIRegP[0]->NormalIntStatus = BuffReadReady;
			::OSSynchronizeIO();
			read_block_pio(&this->PCIRegP[0]->BufferDataPort, pBuff);
			goto wait_complete;
		}
		IODelay(SDHCI_POLL_STEP_US);
	}

	IOLog("VoodooSDHCI/O2: PIO data-ready timeout block=%u NIS=0x%04x EIS=0x%04x PS=0x%08x\n",
		block,
		this->PCIRegP[0]->NormalIntStatus,
		this->PCIRegP[0]->ErrorIntStatus,
		this->PCIRegP[0]->PresentState);
	ret = kIOReturnTimeout;
	goto out;

wait_complete:
	for (UInt32 waited = 0; waited < SDHCI_DATA_TIMEOUT_US;
			waited += SDHCI_POLL_STEP_US) {
		if (!isCardPresent(0)) {
			IOLog("VoodooSDHCI/O2: card removed before transfer complete block=%u\n", block);
			ret = kIOReturnNoMedia;
			goto out;
		}
		UInt16 nis = this->PCIRegP[0]->NormalIntStatus;
		UInt16 eis = this->PCIRegP[0]->ErrorIntStatus;
		if ((nis & ErrorInterrupt) || eis) {
			IOLog("VoodooSDHCI/O2: PIO completion error block=%u NIS=0x%04x EIS=0x%04x PS=0x%08x\n",
				block, nis, eis, this->PCIRegP[0]->PresentState);
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

	IOLog("VoodooSDHCI/O2: PIO transfer-complete timeout block=%u NIS=0x%04x EIS=0x%04x PS=0x%08x\n",
		block,
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
        r"IOReturn VoodooSDHC::readBlockSingle_pio\(UInt8 \*buff, UInt32 block\) \{.*?\n\}\n\n/\*\n \* writeBlockMulti_pio:",
        read_single_fn + "\n\n/*\n * writeBlockMulti_pio:",
        "replace unbounded single-block PIO polling",
    )

    # A timed-out read should force the same offline/remount sequence as a
    # physical removal. Do not treat generic kIOReturnError this way because the
    # read-only build intentionally returns that for write attempts.
    old_switch = '''\tout:\n\tswitch (ret) {\n\tcase kIOReturnSuccess:\n\t\tbreak;\n\tcase kIOReturnNoMedia:\n\t\t/* require remount */\n\t\tcardPresence = kCardRemount;\n\t\tIOLog("VoodooSDHCI: media not present, require remount\\n");\n\t\tbreak;\n\t}\n'''.replace("\tout:", "out:")
    new_switch = '''out:\n\tswitch (ret) {\n\tcase kIOReturnSuccess:\n\t\tbreak;\n\tcase kIOReturnNoMedia:\n\tcase kIOReturnTimeout:\n\t\tcardPresence = kCardRemount;\n\t\tIOLog("VoodooSDHCI/O2: read path requires media remount ret=0x%x\\n", ret);\n\t\tbreak;\n\t}\n'''
    replace_once(cpp, old_switch, new_switch, "remount after PIO timeout/removal")

    report_media_fn = r'''IOReturn VoodooSDHC::reportMediaState(bool *mediaPresent, bool *changedState)
{
	IOLockLock(mediaStateLock);

	bool presence = isCardPresent(0);

	if (!presence) {
		bool hadMedia = (cardPresence != kCardNotPresent);
		if (hadMedia) {
			IOLog("VoodooSDHCI/O2: physical removal; resetting and powering slot down\n");
			Reset(0, FULL_RESET);
			this->PCIRegP[0]->ClockControl = 0;
			this->PCIRegP[0]->PowerControl = 0;
			this->PCIRegP[0]->NormalIntStatus = 0xffff;
			this->PCIRegP[0]->ErrorIntStatus = 0xffff;
			::OSSynchronizeIO();
			IODelay(10000);
		}
		cardPresence = kCardNotPresent;
		*mediaPresent = false;
		*changedState = hadMedia;
		IOLockUnlock(mediaStateLock);
		return kIOReturnSuccess;
	}

	if (cardPresence == kCardInitFailed) {
		*mediaPresent = false;
		*changedState = false;
		IOLockUnlock(mediaStateLock);
		return kIOReturnSuccess;
	}

	if (cardPresence == kCardRemount) {
		*mediaPresent = false;
		*changedState = true;
		cardPresence = kCardNotPresent;
		IOLockUnlock(mediaStateLock);
		return kIOReturnSuccess;
	}

	if (cardPresence == kCardIsPresent) {
		*mediaPresent = true;
		*changedState = false;
		IOLockUnlock(mediaStateLock);
		return kIOReturnSuccess;
	}

	bool initialized = false;
	if (Reset(0, FULL_RESET))
		initialized = cardInit(0);

	if (!initialized && isCardPresent(0)) {
		IOLog("VoodooSDHCI/O2: first reinitialization failed; forcing cold slot retry\n");
		this->PCIRegP[0]->ClockControl = 0;
		this->PCIRegP[0]->PowerControl = 0;
		this->PCIRegP[0]->NormalIntStatus = 0xffff;
		this->PCIRegP[0]->ErrorIntStatus = 0xffff;
		::OSSynchronizeIO();
		IODelay(20000);
		if (Reset(0, FULL_RESET))
			initialized = cardInit(0);
	}

	if (!initialized) {
		IOLog("VoodooSDHCI/O2: card initialization FAILED; media kept offline until removal\n");
		Reset(0, FULL_RESET);
		cardPresence = kCardInitFailed;
		*mediaPresent = false;
		*changedState = false;
		IOLockUnlock(mediaStateLock);
		return kIOReturnSuccess;
	}

	cardPresence = kCardIsPresent;
	*mediaPresent = true;
	*changedState = true;
	IOLockUnlock(mediaStateLock);
	return kIOReturnSuccess;
}'''
    regex_replace_once(
        cpp,
        r"IOReturn VoodooSDHC::reportMediaState\(bool \*mediaPresent, bool \*changedState\)\n\{.*?\n\}\n\n#ifndef __LP64__",
        report_media_fn + "\n\n#ifndef __LP64__",
        "power down on removal and cold-retry reinsert",
    )

    print("[o2micro-runtime] runtime PIO/removal/reinsert hardening applied")


if __name__ == "__main__":
    main()
