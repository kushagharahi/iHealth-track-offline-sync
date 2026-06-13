import asyncio
import os
import random
import time
from dataclasses import dataclass
from typing import List
from datetime import datetime
from bleak import BleakClient, BleakScanner

@dataclass
class BloodPressureRecord:
    timestamp: datetime
    systolic: int
    diastolic: int
    heart_rate: int
    arrhythmia: bool

class KN550BT_Client:
    WRITE_CHAR = "7265632e-6a69-7561-6e2e-646576000000"
    NOTIFY_CHAR = "7365642e-6a69-7561-6e2e-646576000000"
    DEVICE_TYPE = 0xA1
    STATIC_KEY = bytes([25, 1, 7, -106&0xFF, -14&0xFF, 35, 26, 104, -117&0xFF, 84, 52, 98, -116&0xFF, 87, -21&0xFF, 25])
    CONFIG_FILE = "device_config.txt"

    def __init__(self, debug: bool = False):
        self.debug = debug
        self.seq_id = 1
        self.offline_buffer = bytearray()
        self.records: List[BloodPressureRecord] = []
        self.auth_done = asyncio.Event()
        self.sync_done = asyncio.Event()
        self.tx_lock = asyncio.Lock()
        self.client = None

    def _debug_print(self, msg: str):
        if self.debug:
            print(msg)

    @staticmethod
    def _to_uint32(n): return n & 0xFFFFFFFF

    @classmethod
    def _xxtea_to_ints(cls, data, include_length):
        length = len(data)
        n = length >> 2
        if length & 3 != 0: n += 1
        if include_length:
            res = [0] * (n + 1)
            res[n] = length
        else:
            res = [0] * n
        for i in range(length):
            res[i >> 2] |= (data[i] & 0xFF) << ((i & 3) << 3)
        return res

    @classmethod
    def _xxtea_to_bytes(cls, data, include_length):
        n = len(data)
        if n == 0: return b""
        if include_length:
            n -= 1
            length = data[n]
            if length < n * 4 - 3 or length > n * 4: return None
        else:
            length = n * 4
        res = bytearray(length)
        for i in range(length):
            res[i] = (data[i >> 2] >> ((i & 3) << 3)) & 0xFF
        return bytes(res)

    @classmethod
    def _xxtea2_encrypt(cls, data, key):
        if len(data) == 0: return data
        v = cls._xxtea_to_ints(data, True)
        k_bytes = bytearray(16)
        for i in range(min(16, len(key))): k_bytes[i] = key[i]
        k = cls._xxtea_to_ints(k_bytes, False)
        n = len(v) - 1
        if n >= 1:
            q = 6 + 52 // (n + 1)
            z = v[n]
            sum_val = 0
            delta = 0x9E3779B9
            for _ in range(q):
                sum_val = cls._to_uint32(sum_val + delta)
                e = (sum_val >> 2) & 3
                for p in range(n):
                    y = v[p + 1]
                    mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
                    z = cls._to_uint32(v[p] + mx)
                    v[p] = z
                p = n
                y = v[0]
                mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
                z = cls._to_uint32(v[p] + mx)
                v[p] = z
        return cls._xxtea_to_bytes(v, False)

    def _package_data(self, payload_bytes):
        size = len(payload_bytes)
        size2 = size + 5
        bArr = bytearray(size2)
        bArr[0] = 0xB0
        bArr[1] = (size + 2) & 0xFF
        bArr[2] = 0
        bArr[3] = self.seq_id
        self.seq_id = (self.seq_id + 2) % 256
        
        for i in range(size):
            bArr[4 + i] = payload_bytes[i]
            
        checksum = sum(bArr[2:-1]) & 0xFF
        bArr[-1] = checksum
        
        mtu = 20
        chunks = []
        for i in range(0, len(bArr), mtu):
            chunks.append(bytes(bArr[i:i+mtu]))
        return chunks

    def _build_ack(self, state_id, sequence_id):
        off = state_id & 0x0F
        ack_state_id = 0xA0 + off
        seq = sequence_id & 0xFF
        tempask = 255 if seq == 0 else seq - 1
        ack_seq = (tempask + 2) & 0xFF
        packet = bytearray(6)
        packet[0] = 0xB0
        packet[1] = 0x03
        packet[2] = ack_state_id
        packet[3] = ack_seq
        packet[4] = self.DEVICE_TYPE
        packet[5] = sum(packet[2:-1]) & 0xFF
        return bytes(packet)

    def _build_time_sync(self):
        now = time.localtime()
        return bytes([self.DEVICE_TYPE, 0x21, now.tm_year % 100, now.tm_mon,
                      now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec])

    def _build_offline_data_num(self):
        return bytes([self.DEVICE_TYPE, 0x40, 1, 0x00, 0x00])

    def _build_get_offline_data(self):
        return bytes([self.DEVICE_TYPE, 0x4A, 1, 0x00, 0x00])

    async def _get_or_create_device_uuid(self):
        if os.path.exists(self.CONFIG_FILE):
            with open(self.CONFIG_FILE, "r") as f:
                uuid = f.read().strip()
                if uuid:
                    self._debug_print(f"Found saved UUID: {uuid}")
                    print("👉 Please ensure the monitor is awake (Press 'M' button) to connect...")
                    return uuid

        print(f"\n[{self.CONFIG_FILE} not found] -> Scanning for KN-550BT...")
        print("👉 Please ensure the monitor is awake (Press 'M' button once) to pair...")
        
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: d.name and ("Track" in d.name or "KN-550" in d.name),
            timeout=15.0
        )
        if not device:
            raise RuntimeError("Pairing failed: Monitor not found.")
        
        with open(self.CONFIG_FILE, "w") as f:
            f.write(device.address)
            
        print(f"Paired and saved UUID [{device.address}] to {self.CONFIG_FILE}.\n")
        return device.address

    async def _send_command(self, cmd_payload):
        async with self.tx_lock:
            for pkt in self._package_data(cmd_payload):
                self._debug_print(f"  TX: {pkt.hex().upper()}")
                try:
                    if self.client:
                        await self.client.write_gatt_char(self.WRITE_CHAR, pkt, response=False)
                except Exception as e:
                    self._debug_print(f"⚠️ Failed to write chunk: {e}")
                await asyncio.sleep(0.05)

    def _notification_handler(self, sender, data):
        self._debug_print(f"📡 RX: {data.hex().upper()}")
        if not data or len(data) < 6: return
            
        header = data[0]
        if header == 0xA0:
            state_id = data[2]
            seq_id = data[3]
            
            if (state_id & 0xF0) != 0xF0:
                ack = self._build_ack(state_id, seq_id)
                async def send_ack():
                    async with self.tx_lock:
                        try:
                            if self.client:
                                await self.client.write_gatt_char(self.WRITE_CHAR, ack, response=False)
                        except Exception:
                            pass
                        await asyncio.sleep(0.05)
                asyncio.create_task(send_ack())
                
                current_bag = state_id & 0x0F
                expected_id = (seq_id + current_bag * 2) & 0xFF
                self.seq_id = (expected_id + 2) & 0xFF

            payload = data[4:-1]
            if len(payload) < 2: return
                
            cmd_id = payload[1]
            
            if cmd_id == 0xFB:
                self._debug_print(f"\n🔐 Received 0xFB challenge")
                fb_data = payload[2:]
                r2_stroke = fb_data[0:16]
                device_id = fb_data[36:52]
                ka = self._xxtea2_encrypt(device_id, self.STATIC_KEY)
                r2 = self._xxtea2_encrypt(r2_stroke, ka)
                fc_cmd = bytes([self.DEVICE_TYPE, 0xFC]) + r2
                asyncio.create_task(self._send_command(fc_cmd))
                    
            elif cmd_id == 0xFD:
                self._debug_print("\n✅ Authentication SUCCESSFUL!")
                self.auth_done.set()
                
            elif cmd_id == 0xFE:
                self._debug_print("\n❌ Authentication FAILED! Device rejected the response.")
                self.auth_done.set()
                
            elif cmd_id == 0x40:
                offline_num = payload[3]
                self._debug_print(f"\n📊 Device reports {offline_num} offline records!")
                if offline_num > 0:
                    asyncio.create_task(self._send_command(self._build_get_offline_data()))
                else:
                    self.sync_done.set()
                    
            elif cmd_id == 0x4A:
                if len(payload) <= 2:
                    self.sync_done.set()
                    return
            
                chunk_data = payload[4:]
                self.offline_buffer.extend(chunk_data)
                
                for i in range(0, len(self.offline_buffer), 11):
                    record_bytes = self.offline_buffer[i:i+11]
                    if len(record_bytes) == 11:
                        year = record_bytes[0] & 0xFF
                        month = record_bytes[1] & 0xFF
                        day = record_bytes[2] & 0xFF
                        hour = record_bytes[3] & 0xFF
                        minute = record_bytes[4] & 0xFF
                        second = record_bytes[5] & 0xFF
                        
                        offset = record_bytes[6] & 0xFF
                        dia = record_bytes[7] & 0xFF
                        sys = dia + offset
                        heart_rate = record_bytes[8] & 0xFF
                        arrhythmia = (record_bytes[10] & 0x80) != 0
                        
                        try:
                            dt = datetime(2000 + year, month, day, hour, minute, second)
                            record = BloodPressureRecord(
                                timestamp=dt,
                                systolic=sys,
                                diastolic=dia,
                                heart_rate=heart_rate,
                                arrhythmia=arrhythmia
                            )
                            self.records.append(record)
                        except ValueError:
                            pass
                
                self.offline_buffer.clear()
                
                if payload[2] != 0:
                    asyncio.create_task(self._send_command(self._build_get_offline_data()))
                else:
                    self.sync_done.set()

    async def get_offline_data(self) -> List[BloodPressureRecord]:
        self.records = []
        self.offline_buffer.clear()
        self.auth_done.clear()
        self.sync_done.clear()
        self.seq_id = 1
        
        try:
            device_uuid = await self._get_or_create_device_uuid()
        except Exception as e:
            print(f"Error resolving UUID: {e}")
            return []

        try:
            async with BleakClient(device_uuid) as client:
                self.client = client
                self._debug_print(f"🔌 Connected to [{device_uuid}]!")
                await client.start_notify(self.NOTIFY_CHAR, self._notification_handler)
                
                r1 = bytes([random.randint(0, 127) for _ in range(16)])
                fa_cmd = bytes([self.DEVICE_TYPE, 0xFA]) + r1
                self._debug_print("\n── Step 1: Sending Identify (0xFA) ──")
                await self._send_command(fa_cmd)
                
                async def monitor_connection():
                    while not self.auth_done.is_set():
                        if not client.is_connected:
                            self._debug_print("\n❌ Device disconnected unexpectedly.")
                            self.auth_done.set()
                            break
                        await asyncio.sleep(0.5)
                        
                monitor_task = asyncio.create_task(monitor_connection())
                
                try:
                    await asyncio.wait_for(self.auth_done.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    self._debug_print("\n⏱️ Authentication timed out.")
                    
                monitor_task.cancel()
                
                if not self.auth_done.is_set() or not client.is_connected:
                    self.client = None
                    return []
                    
                self._debug_print("\nSyncing time...")
                await self._send_command(self._build_time_sync())
                await asyncio.sleep(0.5)
                
                self._debug_print("Querying offline data...")
                await self._send_command(self._build_offline_data_num())
                
                try:
                    await asyncio.wait_for(self.sync_done.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    self._debug_print("\n⏱️ Data sync timed out.")
                    
                await client.stop_notify(self.NOTIFY_CHAR)
                self.client = None
        except Exception as e:
            print(f"❌ Failed to connect to monitor. Is it awake? (Error: {e})")
            return []
            
        return self.records
