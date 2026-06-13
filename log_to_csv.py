import asyncio
import argparse
import csv
import os
from kn550bt import KN550BT_Client

CSV_FILE = "blood_pressure_log.csv"

def load_existing_timestamps():
    existing = set()
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='r', newline='') as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            for row in reader:
                if row:
                    # Timestamp is in the first column
                    existing.add(row[0])
    return existing

async def main():
    parser = argparse.ArgumentParser(description="Log KN-550BT data to CSV.")
    parser.add_argument("--debug", action="store_true", help="Enable raw packet debugging output")
    args = parser.parse_args()

    print("==============================================")
    print("🔄 Connecting to KN-550BT Blood Pressure Monitor")
    print("==============================================\n")

    client = KN550BT_Client(debug=args.debug)
    records = await client.get_offline_data()

    if not records:
        print("\n✅ Sync Complete. No new records found.")
        return

    print(f"\n📥 Retrieved {len(records)} record(s) from device memory.")
    
    existing_timestamps = load_existing_timestamps()
    
    new_records = []
    for r in records:
        ts_str = r.timestamp.strftime('%Y-%m-%d %H:%M:%S')
        if ts_str not in existing_timestamps:
            new_records.append(r)

    if not new_records:
        print(f"✅ All retrieved records are already in {CSV_FILE}. Nothing to add.")
        return

    print(f"📝 Appending {len(new_records)} new record(s) to {CSV_FILE}...")
    
    file_exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, mode='a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Datetime", "Systolic", "Diastolic", "HeartRate", "Arrhythmia"])
            
        for r in new_records:
            arr_str = "Yes" if r.arrhythmia else "No"
            writer.writerow([
                r.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                r.systolic,
                r.diastolic,
                r.heart_rate,
                arr_str
            ])
            print(f"   + {r.timestamp.strftime('%Y-%m-%d %H:%M:%S')} - {r.systolic}/{r.diastolic} mmHg, {r.heart_rate} bpm")
            
    print("\n✅ Log updated successfully!")

if __name__ == "__main__":
    asyncio.run(main())
