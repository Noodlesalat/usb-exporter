"""
Prometheus Exporter für USB-Monitoring mit usbmon
Version 6: 
- Unbuffered Reading
- Split Errors
- Extended Device Info (Serial, Version, Driver, Class) aus SysFS
"""

import time
import re
import os
import sys
from collections import defaultdict, Counter
from prometheus_client import start_http_server, Gauge, Counter as PromCounter, REGISTRY
from threading import Thread, Lock
import logging

# Logging konfigurieren
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class USBMonitorExporter:
    def __init__(self, port=8000, usbmon_path="/sys/kernel/debug/usb/usbmon"):
        self.port = port
        self.usbmon_path = usbmon_path
        self.lock = Lock()

        # Metriken definieren
        self.setup_metrics()

        # USB-Geräte-Cache
        self.usb_devices = {}
        self.device_lock = Lock()

    def setup_metrics(self):
        """Prometheus-Metriken initialisieren"""

        self.usb_data_sent_bytes = PromCounter(
            'usb_data_sent_bytes_total',
            'Total amount of USB data sent in bytes',
            ['bus', 'device', 'endpoint', 'type', 'vendor', 'product', 'serial']
        )

        self.usb_data_received_bytes = PromCounter(
            'usb_data_received_bytes_total',
            'Total amount of USB data received in bytes',
            ['bus', 'device', 'endpoint', 'type', 'vendor', 'product', 'serial']
        )

        self.usb_transfer_errors_sent = PromCounter(
            'usb_transfer_errors_sent_total',
            'Total number of USB transfer errors during sending',
            ['bus', 'device', 'endpoint', 'type', 'error_code']
        )

        self.usb_transfer_errors_received = PromCounter(
            'usb_transfer_errors_received_total',
            'Total number of USB transfer errors during receiving',
            ['bus', 'device', 'endpoint', 'type', 'error_code']
        )

        self.usb_device_speed = Gauge(
            'usb_device_speed_mbps',
            'USB device speed in Mbps',
            ['bus', 'device', 'vendor_id', 'product_id', 'vendor_name', 'product_name', 'serial']
        )

        self.usb_device_power = Gauge(
            'usb_device_power_ma',
            'USB device power consumption in mA',
            ['bus', 'device', 'vendor_id', 'product_id', 'vendor_name', 'product_name', 'serial']
        )

        # ERWEITERT: Mehr Labels für detaillierte Infos
        self.usb_device_info = Gauge(
            'usb_device_info',
            'USB device information',
            ['bus', 'device', 'vendor_id', 'product_id', 'vendor_name', 'product_name', 
             'serial', 'version', 'class_id', 'driver', 'speed']
        )

    def parse_usbmon_line(self, line):
        """Parse eine usbmon-Zeile im 1u-Format"""
        try:
            parts = line.strip().split()
            if len(parts) < 6:
                return None

            event_type = parts[2]
            address = parts[3]
            status = parts[4]

            address_parts = address.split(':')
            if len(address_parts) < 4:
                return None

            direction_type = address_parts[0]
            bus = address_parts[1]
            device = str(int(address_parts[2]))
            endpoint = address_parts[3]

            direction = 'in' if len(direction_type) > 1 and direction_type[1] == 'i' else 'out'
            transfer_type = self.get_transfer_type(direction_type[0])

            result = {
                'event_type': event_type,
                'bus': bus,
                'device': device,
                'endpoint': endpoint,
                'direction': direction,
                'transfer_type': transfer_type,
                'status': status,
                'data_length': 0
            }

            try:
                if '=' in parts:
                    idx = parts.index('=')
                    if idx > 0 and parts[idx-1].lstrip('-').isdigit():
                        result['data_length'] = int(parts[idx-1])
                else:
                    for i in range(5, len(parts)):
                        p = parts[i]
                        if p.lstrip('-').isdigit():
                            val = int(p)
                            if val >= 0 and val < 100000000: 
                                result['data_length'] = val
                                break
            except ValueError:
                pass

            return result

        except Exception:
            return None

    def get_transfer_type(self, type_char):
        types = {'C': 'control', 'Z': 'isochronous', 'I': 'interrupt', 'B': 'bulk'}
        return types.get(type_char, 'unknown')

    def get_driver_name(self, device_path):
        """Versucht den Treiber des ersten Interfaces zu ermitteln"""
        # USB Treiber binden an Interfaces (z.B. 1-1:1.0), nicht an das Device (1-1)
        try:
            # Suche nach Verzeichnissen, die wie Interfaces aussehen (z.B. "1-1:1.0")
            # Wir nehmen einfach das erste gefundene Interface als "Haupttreiber"
            for item in os.listdir(device_path):
                if re.match(r'.*:\d+\.\d+$', item):
                    interface_path = os.path.join(device_path, item)
                    driver_link = os.path.join(interface_path, 'driver')
                    if os.path.exists(driver_link):
                        # Der Link zeigt auf /sys/bus/usb/drivers/TREIBERNAME
                        driver_path = os.readlink(driver_link)
                        return os.path.basename(driver_path)
        except Exception:
            pass
        return "none"

    def update_device_info(self):
        """USB-Geräteinformationen aus SysFS aktualisieren"""
        try:
            devices_path = "/sys/bus/usb/devices/"
            if not os.path.exists(devices_path):
                return

            for device_dir in os.listdir(devices_path):
                # Akzeptiere N-N (Device) und usbN (Root Hub)
                if not (re.match(r'^\d+-\d+(\.\d+)*$', device_dir) or re.match(r'^usb\d+$', device_dir)):
                    continue

                full_path = os.path.join(devices_path, device_dir)

                try:
                    bus_id = self.read_sysfs_file(f"{full_path}/busnum")
                    dev_id = self.read_sysfs_file(f"{full_path}/devnum")

                    if not bus_id or not dev_id:
                        continue

                    bus_id = str(int(bus_id))
                    dev_id = str(int(dev_id))

                    vendor_id = self.read_sysfs_file(f"{full_path}/idVendor", "unknown")
                    product_id = self.read_sysfs_file(f"{full_path}/idProduct", "unknown")

                    # NEU: Zusätzliche statische Infos
                    serial = self.read_sysfs_file(f"{full_path}/serial", "unknown")
                    version = self.read_sysfs_file(f"{full_path}/version", "unknown") # USB Version (2.00 etc)
                    class_id = self.read_sysfs_file(f"{full_path}/bDeviceClass", "00")
                    driver = self.get_driver_name(full_path)

                    speed = self.parse_speed(self.read_sysfs_file(f"{full_path}/speed", "0"))
                    max_power = self.parse_power(self.read_sysfs_file(f"{full_path}/bMaxPower", "0"))
                    vendor_name = self.clean_string(self.read_sysfs_file(f"{full_path}/manufacturer", "unknown"))
                    product_name = self.clean_string(self.read_sysfs_file(f"{full_path}/product", "unknown"))

                    device_key = f"{bus_id}:{dev_id}"

                    with self.device_lock:
                        self.usb_devices[device_key] = {
                            'vendor_id': vendor_id.lower(),
                            'product_id': product_id.lower(),
                            'vendor_name': vendor_name,
                            'product_name': product_name,
                            'serial': serial,       # Neu
                            'version': version,     # Neu
                            'class_id': class_id,   # Neu
                            'driver': driver,       # Neu
                            'speed': speed,
                            'max_power': max_power,
                            'bus': bus_id,
                            'device': dev_id
                        }

                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Globaler Fehler bei Geräte-Update: {e}")

    def clean_string(self, text):
        if text == "unknown" or not text:
            return "unknown"
        cleaned = re.sub(r'[^\x20-\x7E]', '', text).strip()
        return cleaned if cleaned else "unknown"

    def read_sysfs_file(self, filepath, default=None):
        try:
            if not os.path.exists(filepath):
                return default
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read().strip()
                return content if content else default
        except:
            return default

    def parse_speed(self, speed_str):
        try:
            return float(speed_str)
        except ValueError:
            return 0.0

    def parse_power(self, power_str):
        try:
            if 'mA' in power_str:
                return int(re.search(r'(\d+)', power_str).group(1))
            return int(power_str) * 2
        except:
            return 0

    def get_device_info(self, bus, device):
        key = f"{bus}:{device}"
        with self.device_lock:
            return self.usb_devices.get(key, {
                'vendor_id': 'unknown', 'product_id': 'unknown',
                'vendor_name': 'unknown', 'product_name': 'unknown',
                'serial': 'unknown',
                'speed': 0, 'max_power': 0
            })

    def process_usbmon_data(self, line):
        """Verarbeitet eine einzelne Zeile"""
        parsed = self.parse_usbmon_line(line)
        if not parsed:
            return

        if parsed['event_type'] != 'C':
            return

        # Fehler zählen
        if parsed['status'] != '0':
            if parsed['direction'] == 'out':
                self.usb_transfer_errors_sent.labels(
                    bus=parsed['bus'],
                    device=parsed['device'],
                    endpoint=parsed['endpoint'],
                    type=parsed['transfer_type'],
                    error_code=parsed['status']
                ).inc()
            else:
                self.usb_transfer_errors_received.labels(
                    bus=parsed['bus'],
                    device=parsed['device'],
                    endpoint=parsed['endpoint'],
                    type=parsed['transfer_type'],
                    error_code=parsed['status']
                ).inc()
            return

        # Bytes zählen
        if parsed['data_length'] > 0:
            device_info = self.get_device_info(parsed['bus'], parsed['device'])

            if parsed['direction'] == 'out':
                self.usb_data_sent_bytes.labels(
                    bus=parsed['bus'],
                    device=parsed['device'],
                    endpoint=parsed['endpoint'],
                    type=parsed['transfer_type'],
                    vendor=device_info['vendor_name'],
                    product=device_info['product_name'],
                    serial=device_info['serial'] # Serial auch hier nützlich für Aggregation
                ).inc(parsed['data_length'])
            else:
                self.usb_data_received_bytes.labels(
                    bus=parsed['bus'],
                    device=parsed['device'],
                    endpoint=parsed['endpoint'],
                    type=parsed['transfer_type'],
                    vendor=device_info['vendor_name'],
                    product=device_info['product_name'],
                    serial=device_info['serial']
                ).inc(parsed['data_length'])

    def update_derived_metrics(self):
        """Aktualisiert statische Metriken"""
        self.update_device_info()

        with self.device_lock:
            for info in self.usb_devices.values():
                self.usb_device_speed.labels(
                    bus=info['bus'],
                    device=info['device'],
                    vendor_id=info['vendor_id'],
                    product_id=info['product_id'],
                    vendor_name=info['vendor_name'],
                    product_name=info['product_name'],
                    serial=info['serial']
                ).set(info['speed'])

                self.usb_device_power.labels(
                    bus=info['bus'],
                    device=info['device'],
                    vendor_id=info['vendor_id'],
                    product_id=info['product_id'],
                    vendor_name=info['vendor_name'],
                    product_name=info['product_name'],
                    serial=info['serial']
                ).set(info['max_power'])

                # Hier sind die neuen Felder:
                self.usb_device_info.labels(
                    bus=info['bus'],
                    device=info['device'],
                    vendor_id=info['vendor_id'],
                    product_id=info['product_id'],
                    vendor_name=info['vendor_name'],
                    product_name=info['product_name'],
                    serial=info['serial'],
                    version=info['version'],
                    class_id=info['class_id'],
                    driver=info['driver'],
                    speed=str(info['speed'])
                ).set(1)

    def monitor_usbmon(self, bus="0u"):
        usbmon_file = f"{self.usbmon_path}/{bus}"
        if not os.path.exists(usbmon_file):
            logger.error(f"File not found: {usbmon_file}")
            return

        logger.info(f"Starte Live-Monitoring auf {usbmon_file}")

        try:
            with open(usbmon_file, 'rb', buffering=0) as f:
                while True:
                    line_bin = f.readline()
                    if not line_bin:
                        break
                    try:
                        line = line_bin.decode('utf-8', errors='replace')
                        self.process_usbmon_data(line)
                    except Exception as e:
                        logger.error(f"Parsing Error: {e}")
        except Exception as e:
            logger.error(f"Fataler Fehler im Monitoring Thread: {e}")

    def run(self):
        if not os.path.exists(self.usbmon_path):
            logger.error(f"usbmon Pfad nicht gefunden.")
            sys.exit(1)

        start_http_server(self.port)
        logger.info(f"Exporter läuft auf Port {self.port}")

        monitor_thread = Thread(target=self.monitor_usbmon, args=("0u",), daemon=True)
        monitor_thread.start()

        self.update_device_info()

        try:
            while True:
                time.sleep(10)
                self.update_derived_metrics()
        except KeyboardInterrupt:
            logger.info("Beende Exporter...")

if __name__ == '__main__':
    exporter = USBMonitorExporter()
    exporter.run()
