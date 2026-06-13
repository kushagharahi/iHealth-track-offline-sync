import asyncio
import os
import random
import time
from bleak import BleakClient

DEVICE_UUID = "7DB6E563-D922-93E5-8872-05C7D86C20F0"
WRITE_CHAR  = "7265632e-6a69-7561-6e2e-646576000000"
NOTIFY_CHAR = "7365642e-6a69-7561-6e2e-646576000000"
DEVICE_TYPE = 0xA1

KN550BT_STATIC_KEY = bytes([25, 1, 7, -106&0xFF, -14&0xFF, 35, 26, 104, -117&0xFF, 84, 52, 98, -116&0xFF, 87, -21&0xFF, 25])

def _to_uint32(n): return n & 0xFFFFFFFF

def xxtea_to_ints(data, include_length):
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

def xxtea_to_bytes(data, include_length):
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

def xxtea2_encrypt(data, key):
    if len(data) == 0: return data
    v = xxtea_to_ints(data, True)
    k_bytes = bytearray(16)
    for i in range(min(16, len(key))): k_bytes[i] = key[i]
    k = xxtea_to_ints(k_bytes, False)
    n = len(v) - 1
    if n >= 1:
        q = 6 + 52 // (n + 1)
        z = v[n]
        sum_val = 0
        delta = 0x9E3779B9
        for _ in range(q):
            sum_val = _to_uint32(sum_val + delta)
            e = (sum_val >> 2) & 3
            for p in range(n):
                y = v[p + 1]
                mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
                z = _to_uint32(v[p] + mx)
                v[p] = z
            p = n
            y = v[0]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
            z = _to_uint32(v[p] + mx)
            v[p] = z
    return xxtea_to_bytes(v, False)

SEQ_ID = 1

def package_data_v2(payload_bytes):
    global SEQ_ID
    size = len(payload_bytes)
    size2 = size + 5
    bArr = bytearray(size2)
    bArr[0] = 0xB0
    bArr[1] = (size + 2) & 0xFF
    bArr[2] = 0
    bArr[3] = SEQ_ID
    SEQ_ID = (SEQ_ID + 2) % 256
    
    for i in range(size):
        bArr[4 + i] = payload_bytes[i]
        
    checksum = sum(bArr[2:-1]) & 0xFF
    bArr[-1] = checksum
    
    mtu = 20
    chunks = []
    for i in range(0, len(bArr), mtu):
        chunks.append(bytes(bArr[i:i+mtu]))
        
    return chunks

def build_ack(state_id, sequence_id):
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
    packet[4] = DEVICE_TYPE
    packet[5] = sum(packet[2:-1]) & 0xFF
    return bytes(packet)

def build_time_sync():
    now = time.localtime()
    payload = [DEVICE_TYPE, 0x21, now.tm_year % 100, now.tm_mon,
               now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec]
    return bytes(payload)

def build_offline_data_num(user_id):
    return bytes([DEVICE_TYPE, 0x40, user_id, 0x00, 0x00])

def build_get_offline_data(user_id):
    return bytes([DEVICE_TYPE, 0x4A, user_id, 0x00, 0x00])

async def pull_bp_records():
    print(f"\n==============================================")
    print(f"🔄 Connecting to KN-550BT Blood Pressure Monitor")
    print(f"==============================================\n")
    
    auth_done = asyncio.Event()
    sync_done = asyncio.Event()
    tx_lock = asyncio.Lock()
    r1 = bytes([random.randint(0, 127) for _ in range(16)])
    
    current_user_id = 1
    offline_buffer = bytearray()

    async def send_command(client, cmd_payload):
        async with tx_lock:
            for pkt in package_data_v2(cmd_payload):
                print(f"  TX: {pkt.hex().upper()}")
                try:
                    await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
                except Exception as e:
                    print(f"⚠️ Failed to write chunk: {e}")
                await asyncio.sleep(0.05)

    def notification_handler(sender, data):
        global SEQ_ID
        nonlocal offline_buffer, current_user_id
        
        print(f"📡 RX: {data.hex().upper()}")
        
        if not data or len(data) < 6: return
            
        header = data[0]
        if header == 0xA0:
            state_id = data[2]
            seq_id = data[3]
            
            # Send ACK for received packet
            if (state_id & 0xF0) != 0xF0:
                ack = build_ack(state_id, seq_id)
                async def send_ack():
                    async with tx_lock:
                        try:
                            await client.write_gatt_char(WRITE_CHAR, ack, response=False)
                        except Exception as e:
                            pass
                        await asyncio.sleep(0.05)
                asyncio.create_task(send_ack())
                
                current_bag = state_id & 0x0F
                expected_id = (seq_id + current_bag * 2) & 0xFF
                SEQ_ID = (expected_id + 2) & 0xFF

            payload = data[4:-1]
            if len(payload) < 2: return
                
            cmd_id = payload[1]
            
            if cmd_id == 0xFB:
                print(f"\n🔐 Received 0xFB challenge")
                # Handle Challenge
                fb_data = payload[2:]
                r2_stroke = fb_data[0:16]
                device_id = fb_data[36:52]
                
                ka = xxtea2_encrypt(device_id, KN550BT_STATIC_KEY)
                r2 = xxtea2_encrypt(r2_stroke, ka)
                
                fc_cmd = bytes([DEVICE_TYPE, 0xFC]) + r2
                asyncio.create_task(send_command(client, fc_cmd))
                    
            elif cmd_id == 0xFD:
                print("\n✅ Authentication SUCCESSFUL!")
                # Auth Success
                auth_done.set()
                
            elif cmd_id == 0xFE:
                print("\n❌ Authentication FAILED! Device rejected the response.")
                auth_done.set()
                
            elif cmd_id == 0x40:
                # Query offline data count
                offline_num = payload[3]
                print(f"\n📊 Device reports {offline_num} offline records!")
                if offline_num > 0:
                    asyncio.create_task(send_command(client, build_get_offline_data(current_user_id)))
                else:
                    print("\n✅ Sync Complete. No new records found.")
                    sync_done.set()
                    
            elif cmd_id == 0x4A:
                if len(payload) <= 2:
                    print("\n✅ Sync Complete.")
                    sync_done.set()
                    return
            
                chunk_data = payload[4:]
                offline_buffer.extend(chunk_data)
                
                print(f"Parsing records...")
                # Parse the chunk we just got
                for i in range(0, len(offline_buffer), 11):
                    record = offline_buffer[i:i+11]
                    if len(record) == 11:
                        year = record[0] & 0xFF
                        month = record[1] & 0xFF
                        day = record[2] & 0xFF
                        hour = record[3] & 0xFF
                        minute = record[4] & 0xFF
                        second = record[5] & 0xFF
                        
                        offset = record[6] & 0xFF
                        dia = record[7] & 0xFF
                        sys = dia + offset
                        heart_rate = record[8] & 0xFF
                        
                        arrhythmia = (record[10] & 0x80) != 0
                        arrhythmia_str = "Yes" if arrhythmia else "No"
                        
                        print(f"🩺 Date: 20{year:02d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}")
                        print(f"   Blood Pressure: {sys}/{dia} mmHg")
                        print(f"   Heart Rate:     {heart_rate} bpm")
                        print(f"   Arrhythmia:     {arrhythmia_str}\n")
                
                offline_buffer.clear()
                
                if payload[2] != 0:
                    asyncio.create_task(send_command(client, build_get_offline_data(current_user_id)))
                else:
                    print("✅ Sync Complete.")
                    sync_done.set()
            else:
                pass

    try:
        async with BleakClient(DEVICE_UUID) as client:
            print(f"🔌 Connected to [{DEVICE_UUID}]!")
            await client.start_notify(NOTIFY_CHAR, notification_handler)
            
            # Step 1: Send Identify (0xFA)
            print("\n── Step 1: Sending Identify (0xFA) ──")
            fa_cmd = bytes([DEVICE_TYPE, 0xFA]) + r1
            await send_command(client, fa_cmd)
            
            print("\nWaiting for authentication...")
            async def monitor_connection():
                while not auth_done.is_set():
                    if not client.is_connected:
                        print("\n❌ Device disconnected unexpectedly.")
                        auth_done.set()
                        break
                    await asyncio.sleep(0.5)
                    
            monitor_task = asyncio.create_task(monitor_connection())
            
            try:
                await asyncio.wait_for(auth_done.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                print("\n⏱️ Authentication timed out.")
                
            monitor_task.cancel()
            
            if not auth_done.is_set() or not client.is_connected:
                return
                
            print("\nSyncing time...")
            # Step 2: Sync Time
            await send_command(client, build_time_sync())
            await asyncio.sleep(0.5)
            
            print("Querying offline data...")
            # Step 3: Query offline data for User 1
            await send_command(client, build_offline_data_num(current_user_id))
            
            try:
                await asyncio.wait_for(sync_done.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                print("\n⏱️ Data sync timed out.")
                
            await client.stop_notify(NOTIFY_CHAR)
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(pull_bp_records())
