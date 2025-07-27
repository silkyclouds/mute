<img width="256" height="256" alt="muteq-512" src="https://github.com/user-attachments/assets/20b9eacd-a076-455a-a038-78b7ddb6e0b0" />


**Mute Client** is a lightweight Python agent that reads sound pressure levels from a USB or serial SPL meter and publishes them via MQTT for Home Assistant, Grafana, or any other integration you'd like. It's designed to be resilient, autonomous, and efficient on low-power devices.

---

## 🚀 Main Features

- Reads dB levels from USB HID or serial SPL meters
- Publishes two MQTT sensors:
  - `realtime_noise_level` (every few seconds)
  - `threshold_noise_level` (only when noise exceeds a threshold)
- Home Assistant auto-discovery support
- Offline queue via SQLite (WAL mode):
  - `realtime` messages kept for max 1 hour
  - `threshold` messages kept for max 48 hours
- Auto-restarts if no sound level is measured (watchdog)
- Timestamps use local timezone (RFC-3339)

---

## 🧰 Installation

1. **Requirements**:

   - Python ≥ 3.7
   - Install required packages:

     ```bash
     pip install paho-mqtt pyusb pytz pyserial
     ```

     *(serial support is optional if only using USB)*

2. **Launch the client**:

   ```bash
   python mute_client.py
   ```

   An interactive setup wizard will guide you through first-time configuration (MQTT, device address, etc.).

---

## ⚙️ Configuration

You **do not** need to manually edit `config.json`.  
If it’s missing, the script creates it via the interactive setup.

---

## 🧼 Generated Files

- `config.json` → configuration file (auto-created/updated)
- `mute_queue.db` → local SQLite message queue
- `mute_client.log` → log file (rotates automatically)

---

## 🧪 Debugging

Use `--debug` to enable verbose logging:

```bash
python mute_client.py --debug
```

---

## 🏠 Home Assistant Integration

The client publishes auto-discovery topics (`homeassistant/sensor/.../config`) so your sensors will appear automatically when using MQTT Discovery.

---

## 🔁 Automatic Restart

If no sound data is sent for 10+ seconds, a watchdog will restart the process using `os.execv()` to ensure stability.  
Fully compatible with `systemd`, `supervisord`, etc.

---

## 👤 Author

Developed by [Raphaël Vael](https://github.com/silkyclouds)  
License: [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)

---

## 📡 Example use cases:

- Noise pollution monitoring
- Live venue sound levels monitoring
- Logging city noise peaks
- Triggering automations based on sound levels

---

Enjoy the silence… or not. 🔊 

© 2025 Raphaël Vael – Commercial use forbidden without written permission.

