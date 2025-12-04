import sys
import time
from typing import Optional

try:
    import usb.core
    import usb.util
except ImportError:  # pragma: no cover - dependency is installed in the image
    usb = None  # type: ignore


DEFAULT_VENDOR_ID = 0x16C0
DEFAULT_PRODUCT_ID = 0x05DC


def convert_raw_to_spl(raw: bytes) -> float:
    """
    Convert raw bytes from the SPL meter into a dB SPL value.
    Formula derived from the device specification.
    """
    if len(raw) < 2:
        return 0.0
    value = (raw[0] + ((raw[1] & 3) * 256)) * 0.1 + 30
    return round(float(value), 1)


def find_usb_device(vendor_id: Optional[int], product_id: Optional[int], logger):
    """
    Locate the SPL USB device. Exits the process if not found.
    """
    if usb is None:
        logger.error("pyusb is not installed. Cannot read SPL meter.")
        sys.exit(1)
    vid = vendor_id or DEFAULT_VENDOR_ID
    pid = product_id or DEFAULT_PRODUCT_ID
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        logger.error(f"SPL meter not found (VID=0x{vid:04X}, PID=0x{pid:04X}). Exiting.")
        sys.exit(1)
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except Exception:
        pass
    return dev


def read_spl_value(device, logger) -> Optional[float]:
    """
    Read a single SPL value from the USB device.
    Returns None on transient failures.
    """
    try:
        data = device.ctrl_transfer(0xC0, 4, 0, 0, 200)
        return convert_raw_to_spl(bytes(data))
    except Exception as exc:
        logger.warning(f"USB read failed: {exc}")
        time.sleep(0.1)
        return None
