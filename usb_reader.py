import glob
import os
import sys
import time
from typing import Optional

try:
    import usb.core
    import usb.util
except ImportError:  # pragma: no cover - dependency is installed in the image
    usb = None  # type: ignore

try:
    import serial
except ImportError:  # pragma: no cover - dependency is installed in the image
    serial = None  # type: ignore


DEFAULT_VENDOR_ID = 0x16C0
DEFAULT_PRODUCT_ID = 0x05DC
R8080_VENDOR_ID = 0x04D9
R8080_PRODUCT_ID = 0xE000
CH340_VENDOR_ID = 0x1A86
CH340_PRODUCT_ID = 0x7523
CH340_BAUDRATE = 115200
CH340_FRAME_LEN = 6
SERIAL_PORT_ENV = "MUTE_SERIAL_PORT"
CH340_OFFSET_ENV = "MUTE_CH340_OFFSET_DB"
CH340_OFFSET_DB = float(os.environ.get(CH340_OFFSET_ENV, "0.0"))
SERIAL_PORT_GLOBS = (
    "/dev/serial/by-id/*",
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
)


def convert_raw_to_spl(raw: bytes) -> float:
    """
    Convert raw bytes from the SPL meter into a dB SPL value.
    Formula derived from the device specification.
    """
    if len(raw) < 2:
        return 0.0
    value = (raw[0] + ((raw[1] & 3) * 256)) * 0.1 + 30
    return round(float(value), 1)


def _is_ch340_meter(vendor_id: int, product_id: int) -> bool:
    return vendor_id == CH340_VENDOR_ID and product_id == CH340_PRODUCT_ID


def _is_r8080_meter(vendor_id: int, product_id: int) -> bool:
    return vendor_id == R8080_VENDOR_ID and product_id == R8080_PRODUCT_ID


def convert_ch340_frame_to_spl(frame: bytes) -> float:
    """
    Convert a CH340 serial frame to a dB SPL value.
    Observed frames are shaped like: 55 HH LL 01 01 aa
    """
    if len(frame) != CH340_FRAME_LEN:
        raise ValueError(f"Invalid CH340 frame length: {len(frame)}")
    if frame[0] != 0x55 or frame[-1] != 0xAA:
        raise ValueError(f"Invalid CH340 frame markers: {frame.hex()}")
    raw_value = (frame[1] << 8) | frame[2]
    return round(float(raw_value) * 0.1 + CH340_OFFSET_DB, 1)


def _extract_ch340_frame(buffer: bytearray) -> Optional[bytes]:
    while len(buffer) >= CH340_FRAME_LEN:
        start_idx = buffer.find(0x55)
        if start_idx < 0:
            buffer.clear()
            return None
        if start_idx > 0:
            del buffer[:start_idx]
        if len(buffer) < CH340_FRAME_LEN:
            return None
        candidate = bytes(buffer[:CH340_FRAME_LEN])
        if candidate[0] == 0x55 and candidate[3] == 0x01 and candidate[4] == 0x01 and candidate[5] == 0xAA:
            del buffer[:CH340_FRAME_LEN]
            return candidate
        del buffer[0]
    return None


class SerialSPLDevice:
    def __init__(self, port: str, baudrate: int, vendor_id: int, product_id: int):
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self.port = port
        self.baudrate = baudrate
        self.vendor_id = vendor_id
        self.product_id = product_id
        self._serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.25,
        )
        self._buffer = bytearray()
        try:
            self._serial.reset_input_buffer()
        except Exception:
            pass

    def close(self):
        try:
            if self._serial and self._serial.is_open:
                self._serial.close()
        except Exception:
            pass

    def read_value(self) -> Optional[float]:
        read_size = max(32, getattr(self._serial, "in_waiting", 0) or 0)
        chunk = self._serial.read(read_size)
        if chunk:
            self._buffer.extend(chunk)
            if len(self._buffer) > 512:
                del self._buffer[:-256]
        value = None
        while True:
            frame = _extract_ch340_frame(self._buffer)
            if frame is None:
                break
            value = convert_ch340_frame_to_spl(frame)
        return value

    def __str__(self) -> str:
        return (
            f"CH340 serial SPL meter on {self.port} "
            f"(VID=0x{self.vendor_id:04X}, PID=0x{self.product_id:04X}, baud={self.baudrate}, "
            f"offset_db={CH340_OFFSET_DB:.1f})"
        )


class R8080Device:
    """
    REED R8080 USB sound level meter.

    Protocol derived from the vendor Windows tooling:
    - command frames use STX/ETX framing
    - the command channel is primed through a HID SET_REPORT header
    - the live reading is returned through interrupt IN
    - the SPL value is encoded as (payload[5] * 256 + payload[6]) / 10.0
    """

    EP_IN = 0x81
    EP_OUT = 0x02

    def __init__(self, logger):
        self.dev = None
        self.logger = logger

    def connect(self):
        if usb is None:
            raise RuntimeError("pyusb is not installed")
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

    def _send_header(self, cmd_type: int, length: int):
        self.dev.ctrl_transfer(
            0x21,
            0x09,
            0x0200,
            0,
            bytes([0x43, cmd_type, length & 0xFF, (length >> 8) & 0xFF, 0, 0, 0, 0]),
            timeout=1000,
        )

    def _reset(self):
        self.dev.reset()
        time.sleep(0.6)
        self.connect()

    def read_value(self) -> Optional[float]:
        try:
            try:
                self._send_header(0x01, 7)
            except Exception:
                pass

            self.dev.write(
                self.EP_OUT,
                bytes([0x07, 0x02, 0x41, 0x00, 0x00, 0x00, 0x00, 0x03]),
                timeout=1000,
            )
            self._drain()
            self._send_header(0x04, 32)

            for _ in range(5):
                try:
                    data = bytes(self.dev.read(self.EP_IN, 32, timeout=1500))
                    count = data[0]
                    payload = data[1 : count + 1]
                    if len(payload) > 6:
                        db_value = (payload[5] * 256 + payload[6]) / 10.0
                        self._reset()
                        return round(db_value, 1)
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


def _discover_serial_port(logger) -> Optional[str]:
    preferred = os.environ.get(SERIAL_PORT_ENV, "").strip()
    if preferred:
        if os.path.exists(preferred):
            return preferred
        logger.warning(f"Preferred serial port {preferred} does not exist")

    seen = set()
    candidates = []
    for pattern in SERIAL_PORT_GLOBS:
        for path in sorted(glob.glob(pattern)):
            real_path = os.path.realpath(path)
            if real_path in seen:
                continue
            seen.add(real_path)
            candidates.append(path)
    if len(candidates) > 1:
        logger.warning(f"Multiple serial ports detected, selecting the first one: {', '.join(candidates)}")
    return candidates[0] if candidates else None


def _open_ch340_device(vendor_id: int, product_id: int, logger):
    port = _discover_serial_port(logger)
    if not port:
        logger.error(
            "CH340 serial meter detected but no /dev/ttyUSB* or /dev/ttyACM* device is available. "
            "Map the serial device into Docker, e.g. --device /dev/ttyUSB0:/dev/ttyUSB0."
        )
        sys.exit(1)
    try:
        return SerialSPLDevice(port, CH340_BAUDRATE, vendor_id, product_id)
    except Exception as exc:
        logger.error(f"Unable to open serial SPL meter on {port}: {exc}")
        sys.exit(1)


def _open_r8080_device(vendor_id: int, product_id: int, logger):
    r8080 = R8080Device(logger)
    try:
        r8080.connect()
        logger.info(f"REED R8080 connected (VID=0x{vendor_id:04X}, PID=0x{product_id:04X})")
        return r8080
    except Exception as exc:
        logger.error(f"Unable to open R8080 meter: {exc}")
        sys.exit(1)


def find_usb_device(vendor_id: Optional[int], product_id: Optional[int], logger):
    """
    Locate the SPL USB device. Exits the process if not found.
    """
    if vendor_id is not None or product_id is not None:
        vid = vendor_id or DEFAULT_VENDOR_ID
        pid = product_id or DEFAULT_PRODUCT_ID
        if _is_ch340_meter(vid, pid):
            return _open_ch340_device(vid, pid, logger)
        if _is_r8080_meter(vid, pid):
            return _open_r8080_device(vid, pid, logger)
        if usb is None:
            logger.error("pyusb is not installed. Cannot read USB HID SPL meter.")
            sys.exit(1)
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

    if usb is None:
        port = _discover_serial_port(logger)
        if port:
            return _open_ch340_device(CH340_VENDOR_ID, CH340_PRODUCT_ID, logger)
        logger.error("pyusb is not installed and no serial SPL meter is available.")
        sys.exit(1)

    dev = usb.core.find(idVendor=DEFAULT_VENDOR_ID, idProduct=DEFAULT_PRODUCT_ID)
    if dev is not None:
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass
        return dev

    r8080_dev = usb.core.find(idVendor=R8080_VENDOR_ID, idProduct=R8080_PRODUCT_ID)
    if r8080_dev is not None:
        return _open_r8080_device(R8080_VENDOR_ID, R8080_PRODUCT_ID, logger)

    serial_dev = usb.core.find(idVendor=CH340_VENDOR_ID, idProduct=CH340_PRODUCT_ID)
    if serial_dev is not None:
        return _open_ch340_device(CH340_VENDOR_ID, CH340_PRODUCT_ID, logger)

    logger.error(
        "SPL meter not found. Tried HY1361 VID=0x%04X PID=0x%04X, "
        "R8080 VID=0x%04X PID=0x%04X and CH340 VID=0x%04X PID=0x%04X. Exiting."
        % (
            DEFAULT_VENDOR_ID,
            DEFAULT_PRODUCT_ID,
            R8080_VENDOR_ID,
            R8080_PRODUCT_ID,
            CH340_VENDOR_ID,
            CH340_PRODUCT_ID,
        )
    )
    sys.exit(1)


def read_spl_value(device, logger) -> Optional[float]:
    """
    Read a single SPL value from the USB device.
    Returns None on transient failures.
    """
    if isinstance(device, R8080Device):
        return device.read_value()
    if isinstance(device, SerialSPLDevice):
        try:
            return device.read_value()
        except Exception as exc:
            logger.warning(f"Serial read failed: {exc}")
            time.sleep(0.1)
            return None
    try:
        data = device.ctrl_transfer(0xC0, 4, 0, 0, 200)
        return convert_raw_to_spl(bytes(data))
    except Exception as exc:
        logger.warning(f"USB read failed: {exc}")
        time.sleep(0.1)
        return None
