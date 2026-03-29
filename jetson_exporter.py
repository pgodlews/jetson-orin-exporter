#!/usr/bin/env python3
"""Prometheus exporter for NVIDIA Jetson boards via jetson-stats (jtop)."""

import time
from prometheus_client import start_http_server, Gauge, Info
from jtop import jtop

PORT = 9101

# GPU
g_gpu_usage    = Gauge('jetson_gpu_usage_percent',   'GPU utilization %')
g_gpu_freq     = Gauge('jetson_gpu_freq_hertz',      'GPU frequency Hz')
g_gpu_mem_used = Gauge('jetson_gpu_memory_used_bytes',  'GPU/unified memory used bytes')
g_gpu_mem_free = Gauge('jetson_gpu_memory_free_bytes',  'GPU/unified memory free bytes')
g_gpu_mem_tot  = Gauge('jetson_gpu_memory_total_bytes', 'GPU/unified memory total bytes')

# CPU (per-core)
g_cpu_usage = Gauge('jetson_cpu_usage_percent', 'CPU core utilization %',  ['core'])
g_cpu_freq  = Gauge('jetson_cpu_freq_hertz',    'CPU core frequency Hz',   ['core'])

# Temperatures
g_temp = Gauge('jetson_temperature_celsius', 'Component temperature celsius', ['zone'])

# Power
g_power_total = Gauge('jetson_power_total_watts', 'Total board power watts')
g_power_rail  = Gauge('jetson_power_rail_watts',  'Power rail watts', ['rail'])

# Swap memory
g_swap_used  = Gauge('jetson_swap_used_bytes',  'Swap used bytes')
g_swap_total = Gauge('jetson_swap_total_bytes', 'Swap total bytes')

# EMC (External Memory Controller) frequency
g_emc_freq = Gauge('jetson_emc_freq_hertz', 'EMC frequency Hz')

# Fan
g_fan_rpm   = Gauge('jetson_fan_rpm',           'Fan speed RPM',   ['fan', 'index'])
g_fan_speed = Gauge('jetson_fan_speed_percent', 'Fan duty cycle %', ['fan', 'index'])

# Hardware engines (DLA, NVENC, NVDEC, etc.)
g_engine_freq   = Gauge('jetson_engine_freq_hertz', 'Hardware engine frequency Hz',  ['engine', 'unit'])
g_engine_online = Gauge('jetson_engine_online',     'Hardware engine online status', ['engine', 'unit'])

# Disk
g_disk_total     = Gauge('jetson_disk_total_bytes',     'Disk total bytes')
g_disk_used      = Gauge('jetson_disk_used_bytes',      'Disk used bytes')
g_disk_available = Gauge('jetson_disk_available_bytes', 'Disk available bytes')

# jetson_clocks
g_jetson_clocks = Gauge('jetson_clocks_active', 'jetson_clocks enabled (1=on, 0=off)')

# NV power profile
g_nvpmodel = Info('jetson_nvpmodel', 'Active NV power model/profile')

# Board info (static, set once)
g_board_info = Info('jetson_board', 'Jetson board information')


def collect(jetson):
    # GPU
    gpu = jetson.gpu
    if gpu:
        for name, data in gpu.items():
            if 'status' in data:
                g_gpu_usage.set(data['status'].get('load', 0))
            if 'freq' in data:
                # jtop freq in kHz -> Hz
                g_gpu_freq.set(data['freq'].get('cur', 0) * 1e3)

    # Unified memory (jtop reports in kB -> bytes)
    mem = jetson.memory
    if mem:
        if 'RAM' in mem:
            ram = mem['RAM']
            used = ram.get('used', 0) * 1024
            tot  = ram.get('tot',  0) * 1024
            g_gpu_mem_used.set(used)
            g_gpu_mem_tot.set(tot)
            g_gpu_mem_free.set(tot - used)
        if 'SWAP' in mem:
            swap = mem['SWAP']
            g_swap_used.set(swap.get('used', 0) * 1024)
            g_swap_total.set(swap.get('tot',  0) * 1024)
        if 'EMC' in mem:
            emc = mem['EMC']
            if emc.get('online') and emc.get('cur', 0) > 0:
                # jtop EMC freq in kHz -> Hz
                g_emc_freq.set(emc['cur'] * 1e3)

    # CPU
    cpu = jetson.cpu
    if cpu and 'cpu' in cpu:
        for i, core in enumerate(cpu['cpu']):
            if core:
                # 100 - idle captures user+system+nice+iowait in one number.
                # Intentionally kept simple — no per-mode breakdown like node_exporter.
                g_cpu_usage.labels(core=i).set(100.0 - core.get('idle', 100.0))
                freq = core.get('freq', {})
                if freq:
                    # jtop freq in kHz -> Hz
                    g_cpu_freq.labels(core=i).set(freq.get('cur', 0) * 1e3)

    # Fan
    fan = jetson.fan
    if fan:
        for fan_name, fan_data in fan.items():
            for i, rpm in enumerate(fan_data.get('rpm', [])):
                g_fan_rpm.labels(fan=fan_name, index=i).set(rpm)
            for i, speed in enumerate(fan_data.get('speed', [])):
                g_fan_speed.labels(fan=fan_name, index=i).set(speed)

    # NV power profile
    nvpmodel = jetson.nvpmodel
    if nvpmodel:
        g_nvpmodel.info({'profile': str(nvpmodel)})

    # Temperatures (skip inactive zones reporting -256)
    temps = jetson.temperature
    if temps:
        for zone, val in temps.items():
            if isinstance(val, (int, float)) and val > -200:
                g_temp.labels(zone=zone).set(val)
            elif isinstance(val, dict) and 'temp' in val and val['temp'] > -200:
                g_temp.labels(zone=zone).set(val['temp'])

    # Power (jtop reports in mW -> watts)
    power = jetson.power
    if power:
        if 'tot' in power:
            g_power_total.set(power['tot'].get('power', 0) / 1000)
        for rail_name, rail_data in power.get('rail', {}).items():
            if rail_data.get('online'):
                g_power_rail.labels(rail=rail_name).set(rail_data.get('power', 0) / 1000)

    # Hardware engines (jtop freq in kHz -> Hz)
    engines = jetson.engine
    if engines:
        for eng_name, eng_units in engines.items():
            for unit_name, unit_data in eng_units.items():
                g_engine_online.labels(engine=eng_name, unit=unit_name).set(
                    1 if unit_data.get('online') else 0)
                if 'cur' in unit_data:
                    g_engine_freq.labels(engine=eng_name, unit=unit_name).set(
                        unit_data['cur'] * 1e3)

    # Disk (jtop reports in GB -> bytes)
    disk = jetson.disk
    if disk:
        g_disk_total.set(disk.get('total', 0) * 1e9)
        g_disk_used.set(disk.get('used', 0) * 1e9)
        g_disk_available.set(disk.get('available', 0) * 1e9)

    # jetson_clocks
    try:
        g_jetson_clocks.set(1 if jetson.jetson_clocks else 0)
    except Exception:
        pass


def main():
    start_http_server(PORT)
    print(f"Jetson exporter running on :{PORT}")

    with jtop() as jetson:
        # Set static board info once
        try:
            hw   = jetson.board.get('hardware', {})
            libs = jetson.board.get('libraries', {})
            g_board_info.info({
                'model':  hw.get('Model',  'unknown'),
                'module': hw.get('Module', 'unknown'),
                'l4t':    hw.get('L4T',    'unknown'),
                'cuda':   libs.get('CUDA', 'unknown'),
            })
        except Exception:
            pass

        while jetson.ok():
            try:
                collect(jetson)
            except Exception as e:
                print(f"collect error: {e}")
            time.sleep(5)


if __name__ == '__main__':
    main()
