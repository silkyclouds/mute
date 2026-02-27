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

# REED R8080 identifiers (Holtek Semiconductor)
R8080_VENDOR_ID = 0x04D9
R8080_PRODUCT_ID = 0xE000

# Known SPL meter devices: (VID, PID, device_type)
KNOWN_DEVICES = [
    (DEFAULT_VENDOR_ID, DEFAULT_PRODUCT_ID, "hy1361"),
    (R8080_VENDOR_ID, R8080_PRODUCT_ID, "r8080"),
]


def convert_raw_to_spl(raw: bytes) -> float:
    """
    Convert raw bytes from the HY1361 SPL meter into a dB SPL value.
    Formula derived from the device specification.
    """
    if len(raw) < 2:
        return 0.0
    value = (raw[0] + ((raw[1] & 3) * 256)) * 0.1 + 30
    return round(float(value), 1)


class R8080Device:
    """
    REED R8080 Sound Level Meter driver.

    Protocol discovered by reverse engineering HTUSB.dll:
    - Commands use STX/ETX framing: [0x02, CMD, D1, D2, D3, D4, 0x03]
    - HTUSB WriteCmd header sent via SET_REPORT(Output): [0x43, 0x01, len, 0, 0, 0, 0, 0]
    - Command data sent on Interrupt OUT with length prefix: [count, data...]
    - HTUSB ReadData header: [0x43, 0x04, len, 0, 0, 0, 0, 0]
    - Response read from Interrupt IN: [count, data...]
    - Acquire command ('A' = 0x41) returns live measurement
    - dB value = (response[5] * 256 + response[6]) / 10.0
    """

    EP_IN = 0x81
    EP_OUT = 0x02

    def __init__(self, logger):
        self.dev = None
        self.logger = logger

    def connect(self):
        self.dev = usb.core.find(idVendor=R8080_VENDOR_ID, idProduct=R8080_PRODUCT_ID)
        if not self.dev:
            raise RuntimeError("R8080 not found")
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except Exception:
            pass
        self.dev.set_configuration()

    def _drain(self):
        while True:
            try:
                self.dev.read(self.EP_IN, 32, timeout=100)
            except Exception:
                break

    def _send_header(self, cmd_type, length):
        self.dev.ctrl_transfer(
            0x21, 0x09, 0x0200, 0,
            bytes([0x43, cmd_type, length & 0xFF, (length >> 8) & 0xFF, 0, 0, 0, 0]),
            timeout=1000,
        )

    def _reset(self):
        self.dev.reset()
        time.sleep(0.6)
        self.connect()

    def read_spl(self) -> Optional[float]:
        """Read current dB level from the R8080. Returns float or None."""
        try:
            # WriteCmd header (may STALL on subsequent cycles, that's OK)
            try:
                self._send_header(0x01, 7)
            except Exception:
                pass

            # Acquire command with length prefix on interrupt OUT
            self.dev.write(
                self.EP_OUT,
                bytes([0x07, 0x02, 0x41, 0x00, 0x00, 0x00, 0x00, 0x03]),
                timeout=1000,
            )
            self._drain()

            # ReadData header
            self._send_header(0x04, 32)

            # Read response
            for _ in range(5):
                try:
                    d = bytes(self.dev.read(self.EP_IN, 32, timeout=1500))
                    cnt = d[0]
                    data = d[1 : cnt + 1]
                    if len(data) > 6:
                        db_val = (data[5] * 256 + data[6]) / 10.0
                        self._reset()
                        return round(db_val, 1)
                    break
                except Exception:
                    break

            self._reset()
            return None

        except Exception as exc:
            self.logger.warning(f"R8080 read failed: {exc}")
            try:
                self._reset()
            except Exception:
                pass
            return None


def find_usb_device(vendor_id: Optional[int], product_id: Optional[int], logger):
    """
    Locate the SPL USB device. If specific VID/PID are given, use those.
    Otherwise auto-detect from known devices. Exits the process if not found.
    """
    if usb is None:
        logger.error("pyusb is not installed. Cannot read SPL meter.")
        sys.exit(1)

    # If user specified VID/PID, use those directly
    if vendor_id is not None and product_id is not None:
        # Check if this is an R8080
        if vendor_id == R8080_VENDOR_ID and product_id == R8080_PRODUCT_ID:
            r8080 = R8080Device(logger)
            try:
                r8080.connect()
                logger.info(f"REED R8080 connected (VID=0x{vendor_id:04X}, PID=0x{product_id:04X})")
                return r8080
            except Exception as exc:
                logger.error(f"R8080 connection failed: {exc}")
                sys.exit(1)

        # Otherwise treat as HY1361-compatible
        dev = usb.core.find(idVendor=vendor_id, idProduct=product_id)
        if dev is None:
            logger.error(f"SPL meter not found (VID=0x{vendor_id:04X}, PID=0x{product_id:04X}). Exiting.")
            sys.exit(1)
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass
        return dev

    # Auto-detect: try each known device
    for vid, pid, device_type in KNOWN_DEVICES:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is not None:
            if device_type == "r8080":
                r8080 = R8080Device(logger)
                try:
                    r8080.connect()
                    logger.info(f"Auto-detected REED R8080 (VID=0x{vid:04X}, PID=0x{pid:04X})")
                    return r8080
                except Exception as exc:
                    logger.warning(f"R8080 detected but connection failed: {exc}")
                    continue
            else:
                try:
                    if dev.is_kernel_driver_active(0):
                        dev.detach_kernel_driver(0)
                except Exception:
                    pass
                logger.info(f"Auto-detected SPL meter (VID=0x{vid:04X}, PID=0x{pid:04X})")
                return dev

    # Nothing found
    logger.error("No supported SPL meter found. Exiting.")
    sys.exit(1)


def read_spl_value(device, logger) -> Optional[float]:
    """
    Read a single SPL value from the USB device.
    Supports both HY1361 (pyusb device) and R8080 (R8080Device wrapper).
    Returns None on transient failures.
    """
    if isinstance(device, R8080Device):
        return device.read_spl()

    # Original HY1361 path
    try:
        data = device.ctrl_transfer(0xC0, 4, 0, 0, 200)
        return convert_raw_to_spl(bytes(data))
    except Exception as exc:
        logger.warning(f"USB read failed: {exc}")
        time.sleep(0.1)
        return None
