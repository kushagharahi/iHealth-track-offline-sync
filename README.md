# iHealth KN-550BT Data Puller

A Python script to securely authenticate and download offline blood pressure readings directly from an iHealth Track (KN-550BT) monitor over Bluetooth, bypassing the official app.

## Requirements
- Python 3.7+
- macOS/Linux/Windows with Bluetooth LE support

## Setup

1. Create and activate a Python virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. Install the required BLE library:
   ```bash
   pip install bleak
   ```

## Usage

1. **Wake the device:** Press the **M** (Memory) button on the blood pressure monitor to turn on the screen and activate Bluetooth.
2. **Run the script:**
   ```bash
   python pull_bp_data.py
   ```

*Note: On first run, the script will automatically scan for the monitor and save its UUID to `device_config.txt`. Subsequent runs will connect instantly.*

### Options
To view raw hex packet transmissions and debug the protocol:
```bash
python pull_bp_data.py --debug
```
