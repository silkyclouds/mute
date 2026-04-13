<div align="center">

<img width="256" height="256" alt="mute_text_logo" src="https://github.com/user-attachments/assets/8c2ab422-d2ff-4f68-b901-bddbd6840996" />

### Community-powered acoustic intelligence for everyone.

<p>
  <img src="https://img.shields.io/badge/Open%20Source-✓-5b9a9a?style=flat-square" alt="Open Source">
  <img src="https://img.shields.io/badge/Privacy--first-✓-5b9a9a?style=flat-square" alt="Privacy-first">
  <img src="https://img.shields.io/badge/Home%20Assistant-ready-5b9a9a?style=flat-square" alt="Home Assistant ready">
  <img src="https://img.shields.io/badge/Docker-supported-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker">
</p>

<p>
  <a href="https://dash.muteq.eu"><img src="https://img.shields.io/badge/📊_Dashboard-dash.muteq.eu-d95030?style=for-the-badge" alt="Dashboard"></a>
  <a href="https://muteq.eu"><img src="https://img.shields.io/badge/🌐_Website-muteq.eu-5b9a9a?style=for-the-badge" alt="Website"></a>
  <a href="https://discord.com/invite/m7RGZy6YmZ"><img src="https://img.shields.io/badge/💬_Discord-Join_Us-5865F2?style=for-the-badge" alt="Discord"></a>
</p>

</div>

---

## 🔗 Quick Links

| | |
|---|---|
| 📊 **Dashboard** | [dash.muteq.eu](https://dash.muteq.eu) — Live noise monitoring & analytics |
| 🌐 **Website** | [muteq.eu](https://muteq.eu) — Learn more about MUTEq |
| 💬 **Discord** | [Join our community](https://discord.com/invite/m7RGZy6YmZ) — Get help & share your builds |
| 🔧 **Build Your Own** | [DIY Guide](#-build-your-own-mute-box) — Start building your Mute Box |

---

## 🎯 What is mute?

**mute** is a lightweight USB noise monitoring client that connects your DIY **Mute Box** to the official MUTEq dashboard. It's the heart of the MUTEq acoustic intelligence platform.

Connect any supported USB sound meter to your Raspberry Pi or computer, run the mute client via Docker, and instantly get:

- 📈 **Real-time noise level streaming** to the cloud dashboard
- 🌤️ **Weather data correlation** (temperature, humidity, conditions)
- 🏠 **Local Home Assistant integration** via MQTT
- 📊 **Advanced analytics**, alerts, and reporting

---

## ⚡ How It Works

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  🎤 USB Sound   │ ──▶ │  🐳 mute client │ ──▶ │  ☁️  MUTEq      │ ──▶ │  📊 Dashboard   │
│     Meter       │     │    (Docker)     │     │    Backend      │     │  dash.muteq.eu  │
└─────────────────┘     └─────────────────┘     └─────────────────┘     └─────────────────┘
                                │
                                │ (optional)
                                ▼
                        ┌─────────────────┐     ┌─────────────────┐
                        │  📡 MQTT        │ ──▶ │  🏠 Home        │
                        │    Broker       │     │    Assistant    │
                        └─────────────────┘     └─────────────────┘
```

---

## 🚀 Quick Start (Docker)

The recommended way to run mute is via Docker. **No API keys, no tokens, no manual device IDs!**

### Prerequisites

1. ✅ A supported USB sound meter connected to your device  
   → **Search for "volume meter HY1361" on Aliexpress — this model is tested and will be detected automatically by the mute script**

   <p align="center">
     <img src="https://github.com/user-attachments/assets/3148ad40-651c-4b55-9bf0-4b42e53e2c46" width="50%" />
     <br/>
     <em>this is what you are looking for on Aliexpress</em>
   </p>

3. ✅ Docker installed
4. ✅ A config directory on your host (e.g., `/home/pi/mute-config`)
5. ✅ A supported CPU architecture: `amd64`, `arm64`, or `arm/v7`

### Step 1: Run the Container (Universal Command)

This is the recommended command because it works with all supported meter families:
- classic HID meters (`0x16c0:0x05dc`)
- REED R8080 HID meter (`0x04d9:0xe000`)
- CH340/CH341 serial clones (`0x1a86:0x7523`)

```bash
docker run -d \
  --name mute-client \
  --restart unless-stopped \
  --privileged \
  -v /dev:/dev \
  -v /path/to/config:/config \
  meaning/mute-client:latest
```

> ⚠️ **Important:** The `-v /path/to/config:/config` volume mount is **required**. This is where the client stores your device registration and onboarding state. Replace `/path/to/config` with a real path on your host (e.g., `/home/pi/mute-config`).

Why this command is the safest default:
- `-v /dev:/dev` exposes both `/dev/bus/usb` and `/dev/ttyUSB*`
- `--privileged` avoids USB/serial permission edge cases across Linux distributions and Docker setups
- it works whether your sound meter is HID or a CH340 serial clone

### Step 1b: More restrictive alternatives

If you prefer to expose only the minimum required devices, use the variant that matches your hardware.

#### HID meter (`0x16c0:0x05dc` or `0x04d9:0xe000`)

```bash
docker run -d \
  --name mute-client \
  --restart unless-stopped \
  --device /dev/bus/usb:/dev/bus/usb \
  -v /path/to/config:/config \
  meaning/mute-client:latest
```

#### CH340 / CH341 serial clone (`0x1a86:0x7523`)

```bash
docker run -d \
  --name mute-client \
  --restart unless-stopped \
  --device /dev/bus/usb:/dev/bus/usb \
  --device /dev/ttyUSB0:/dev/ttyUSB0 \
  -e MUTE_SERIAL_PORT=/dev/ttyUSB0 \
  -v /path/to/config:/config \
  meaning/mute-client:latest
```

### Step 2: Get the Onboarding URL

After starting the container, check the logs to find your unique onboarding link:

```bash
docker logs mute-client
```

You'll see something like:

```
🌐 Enrollment URL: https://dash.muteq.eu/devices/my-muteq-sensor-XXXX/claim
```

### Step 3: Complete Onboarding

1. Click (or copy-paste) the onboarding URL into your browser
2. Enter your address — this is used to fetch local weather data
3. Click "Complete Setup"

That's it! The backend will:
- 🗺️ Geocode your address automatically
- 🆔 Assign your unique device ID
- 📡 Start receiving noise data from your Mute Box
- 📊 Show your device on the [dashboard](https://dash.muteq.eu)

---

## 🏠 Optional: Home Assistant Integration (MQTT)

If you want to integrate with Home Assistant, add the MQTT environment variables:

```bash
docker run -d \
  --name mute-client \
  --restart unless-stopped \
  --privileged \
  -v /dev:/dev \
  -v /path/to/config:/config \
  -e LOCAL_MQTT_ENABLED=true \
  -e LOCAL_MQTT_SERVER=192.168.1.100 \
  -e LOCAL_MQTT_PORT=1883 \
  -e LOCAL_MQTT_USER=mqtt-user \
  -e LOCAL_MQTT_PASS=mqtt-pass \
  -e LOCAL_MQTT_TLS=false \
  meaning/mute-client:latest
```

> 💡 **Note:** MQTT is completely optional. Your Mute Box will work perfectly fine without it — data always streams to the cloud dashboard.

### Environment Variables (All Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `LOCAL_MQTT_ENABLED` | `false` | Enable MQTT publishing for Home Assistant |
| `LOCAL_MQTT_SERVER` | — | MQTT broker IP address |
| `LOCAL_MQTT_PORT` | `1883` | MQTT broker port |
| `LOCAL_MQTT_USER` | — | MQTT username |
| `LOCAL_MQTT_PASS` | — | MQTT password |
| `LOCAL_MQTT_TLS` | `false` | Enable TLS for MQTT connection |

> 🚫 **No other configuration is needed.** There are no API keys, no tokens, no manual device IDs. Everything is automatic.

### CH340 offset note

For the `0x1a86:0x7523` CH340/CH341 devices, mute now reads the value as an **absolute SPL value by default**.

If you ever encounter a clone that still requires the historical `+30 dB` offset, you can force it:

```bash
docker run -d \
  --name mute-client \
  --restart unless-stopped \
  --privileged \
  -v /dev:/dev \
  -v /path/to/config:/config \
  -e MUTE_CH340_OFFSET_DB=30 \
  meaning/mute-client:latest
```

---

## 🏠 Home Assistant Integration

mute supports **MQTT auto-discovery** for seamless Home Assistant integration. When MQTT is configured, your sensor will automatically appear in Home Assistant!

### MQTT Topics

| Topic | Description |
|-------|-------------|
| `muteq/<device_id>/noise/realtime` | Current noise level in dB |
| `muteq/<device_id>/noise/threshold` | Threshold alert events |
| `muteq/<device_id>/availability` | Online/offline status |

### Example Home Assistant Automation

```yaml
automation:
  - alias: "Alert when noise exceeds 85 dB"
    trigger:
      - platform: numeric_state
        entity_id: sensor.mute_box_noise_level
        above: 85
    action:
      - service: notify.mobile_app
        data:
          message: "⚠️ Noise level is {{ states('sensor.mute_box_noise_level') }} dB!"
```

---

## 🎤 Supported USB Sound Meters

| Vendor ID | Product ID | Description |
|-----------|------------|-------------|
| `0x16c0` | `0x05dc` | Van Ooijen Technische Informatica HID meters |
| `0x04d9` | `0xe000` | REED R8080 (Holtek HID meter) |
| `0x1a86` | `0x7523` | CH340 / CH341 serial clone commonly found in DIY builds |

> **Have a different USB sound meter?** [Open an Issue](https://github.com/silkyclouds/mute/issues) to request support. Please include the vendor/product IDs and `lsusb` output.

---

## 📊 Dashboard Features

Everything you need to understand your acoustic environment at [dash.muteq.eu](https://dash.muteq.eu):

<table>
<tr>
<td width="50%">

### Real-time Metrics
- 📈 **Live SPL** — Sub-second updates
- 📊 **Daily averages** — Background level tracking
- 🔴 **Peak detection** — Catch the loudest moments
- 📉 **Event counts** — Threshold violations

</td>
<td width="50%">

### Advanced Analytics
- 🎯 **Quietness score** — Quality of life metric
- 🌤️ **Weather overlays** — Correlate with conditions
- 📍 **Sensor map** — Geographic visualization
- 📋 **Export reports** — PDF/CSV for evidence

</td>
</tr>
<tr>
<td>

### Visualization
- 📊 **Event timeline** — Spike charts
- 🥧 **Noise distribution** — dB range breakdown
- 📈 **Stack & quietness** — Stacked bar analysis
- 📉 **Statistical summary** — Median, std dev, peaks

</td>
<td>

### For Municipalities
- 💰 **Fine revenue calculator** — Estimate enforcement potential
- 📋 **Compliance reports** — Regulatory documentation
- 🗺️ **Multi-sensor networks** — City-wide deployment

</td>
</tr>
</table>

---

## 🎯 Who Is It For?

<table>
<tr>
<td align="center" width="33%">
<h3>🏠 Citizens</h3>
<p>Monitor noise in your neighborhood. Document disturbances. Take back your peace.</p>
</td>
<td align="center" width="33%">
<h3>🏛️ Municipalities</h3>
<p>Deploy city-wide sensor networks. Make data-driven noise policies.</p>
</td>
<td align="center" width="33%">
<h3>🎉 Event Organizers</h3>
<p>Stay compliant with noise limits. Real-time monitoring during events.</p>
</td>
</tr>
<tr>
<td align="center">
<h3>👮 Police & Enforcement</h3>
<p>Evidence-based enforcement. Timestamped data for legal proceedings.</p>
</td>
<td align="center">
<h3>🏢 Property Owners</h3>
<p>Document noise issues for tenant disputes. Monitor construction.</p>
</td>
<td align="center">
<h3>📋 Acoustic Consultants</h3>
<p>Professional-grade data at a fraction of the cost.</p>
</td>
</tr>
</table>

---

## 🔧 Build Your Own Mute Box

<div align="center">

### 100% Open Source · Free Dashboard Access · Works Instantly

Building your own Mute Box is easy and affordable. All you need is:

- A Raspberry Pi (or any Linux device)
- A supported USB sound meter
- Docker installed

</div>

---

## 🔒 Privacy First

| | |
|---|---|
| 🚫🎤 **No Audio Recordings** | Only dB levels are captured. Never actual sound content. |
| 👤 **No Personal Data** | No PII collected. Anonymous by design. |
| 🇪🇺 **GDPR Compliant** | Built in Europe with privacy at its core. |
| 🔐 **Local-first Option** | Use MQTT-only mode to keep data on your network. |

---

## 🤝 Contributing

We love contributions from the community! Here's how you can help:

- 🐛 **Report bugs** — Found an issue? Let us know!
- 💡 **Suggest features** — Have an idea? We're all ears.
- 🎤 **Add USB devices** — Help us support more sound meters.
- 📖 **Improve docs** — Documentation PRs are always welcome.
- 🌍 **Translations** — Help make mute accessible worldwide.

---

## 👤 Author

Developed with ❤️ by **Raphaël Vael**

---

## 📜 License

<a href="https://creativecommons.org/licenses/by-nc/4.0/"><img src="https://img.shields.io/badge/License-CC%20BY--NC%204.0-lightgrey.svg" alt="CC BY-NC 4.0"></a>

This project is licensed under the **Creative Commons Attribution-NonCommercial 4.0 International License** ([CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)).

**You are free to:**
- ✅ Share — copy and redistribute the material
- ✅ Adapt — remix, transform, and build upon the material

**Under the following terms:**
- 📛 **Attribution** — You must give appropriate credit
- 🚫 **NonCommercial** — Commercial use requires explicit approval from the author

> 💡 **Intellectual Property Notice:** The MUTEq concept is registered with the [Benelux Office for Intellectual Property (BOIP)](https://www.boip.int/).

---

<div align="center">

<img width128 height="128" alt="mute_text_logo" src="https://github.com/user-attachments/assets/8c2ab422-d2ff-4f68-b901-bddbd6840996" />

**MUTEq** — Acoustic intelligence for everyone.

Open-source · Community-powered · Privacy-first

<p>
  <a href="https://dash.muteq.eu">Dashboard</a> •
  <a href="https://muteq.eu">Website</a> •
  <a href="https://discord.com/invite/m7RGZy6YmZ">Discord</a> •
</p>

<sub>© 2024 Raphaël Vael · Licensed under CC BY-NC 4.0</sub>

</div>
