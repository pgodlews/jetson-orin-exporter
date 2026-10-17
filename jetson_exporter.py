#!/usr/bin/env python3
"""Prometheus exporter for NVIDIA Jetson boards via jetson-stats (jtop)."""

import sys
import time
from prometheus_client import start_http_server, Gauge, Info
from jtop import jtop

PORT = 9101

# jtop reports disk sizes in GiB (bytes / 1024**3), despite labelling them "GB"
GIB = 1024 ** 3

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

# Labelled gauges whose series are dropped when jtop stops reporting them
# (core offline, rail offline, zone inactive, ...) instead of freezing at the last value.
LABELLED_GAUGES = (g_cpu_usage, g_cpu_freq, g_temp, g_power_rail,
                   g_fan_rpm, g_fan_speed, g_engine_freq, g_engine_online)

# Label sets exported in the previous cycle, per gauge
_exported = {}


def num(value):
    """Return value as a number, or None if jtop reported no reading (None, missing, etc.)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def set_labelled(seen, gauge, value, *labels):
    """Set a labelled gauge and remember the label set as seen this cycle."""
    labels = tuple(str(label) for label in labels)
    gauge.labels(*labels).set(value)
    seen.setdefault(gauge, set()).add(labels)


def prune_stale(seen):
    """Remove series that were exported last cycle but not this one."""
    for gauge in LABELLED_GAUGES:
        current = seen.get(gauge, set())
        for labels in _exported.get(gauge, set()) - current:
            try:
                gauge.remove(*labels)
            except KeyError:
                pass
        _exported[gauge] = current


def collect_gpu(jetson, seen):
    gpu = jetson.gpu
    if gpu:
        for name, data in gpu.items():
            load = num(data.get('status', {}).get('load'))
            if load is not None:
                g_gpu_usage.set(load)
            cur = num(data.get('freq', {}).get('cur'))
            if cur is not None:
                # jtop freq in kHz -> Hz
                g_gpu_freq.set(cur * 1e3)


def collect_memory(jetson, seen):
    # Unified memory (jtop reports in kB -> bytes)
    mem = jetson.memory
    if mem:
        if 'RAM' in mem:
            ram = mem['RAM']
            used = (num(ram.get('used')) or 0) * 1024
            tot  = (num(ram.get('tot')) or 0) * 1024
            g_gpu_mem_used.set(used)
            g_gpu_mem_tot.set(tot)
            g_gpu_mem_free.set(tot - used)
        if 'SWAP' in mem:
            swap = mem['SWAP']
            g_swap_used.set((num(swap.get('used')) or 0) * 1024)
            g_swap_total.set((num(swap.get('tot')) or 0) * 1024)
        if 'EMC' in mem:
            emc = mem['EMC']
            cur = num(emc.get('cur'))
            if emc.get('online') and cur and cur > 0:
                # jtop EMC freq in kHz -> Hz
                g_emc_freq.set(cur * 1e3)


def collect_cpu(jetson, seen):
    cpu = jetson.cpu
    if cpu and 'cpu' in cpu:
        for i, core in enumerate(cpu['cpu']):
            # Offline cores have no usage/frequency; let their series drop out
            if not core or not core.get('online', True):
                continue
            idle = num(core.get('idle'))
            # jtop returns all-zero usage when no ticks elapsed; that is not 100% busy
            busy = sum(num(core.get(k)) or 0 for k in ('user', 'nice', 'system'))
            if idle is not None and idle + busy > 0:
                # 100 - idle captures user+system+nice+iowait in one number.
                # Intentionally kept simple — no per-mode breakdown like node_exporter.
                set_labelled(seen, g_cpu_usage, 100.0 - idle, i)
            cur = num(core.get('freq', {}).get('cur'))
            if cur is not None:
                # jtop freq in kHz -> Hz
                set_labelled(seen, g_cpu_freq, cur * 1e3, i)


def collect_fan(jetson, seen):
    fan = jetson.fan
    if fan:
        for fan_name, fan_data in fan.items():
            for i, rpm in enumerate(fan_data.get('rpm', [])):
                if num(rpm) is not None:
                    set_labelled(seen, g_fan_rpm, rpm, fan_name, i)
            for i, speed in enumerate(fan_data.get('speed', [])):
                if num(speed) is not None:
                    set_labelled(seen, g_fan_speed, speed, fan_name, i)


def collect_nvpmodel(jetson, seen):
    nvpmodel = jetson.nvpmodel
    if nvpmodel:
        g_nvpmodel.info({'profile': str(nvpmodel)})


def collect_temperature(jetson, seen):
    # Skip inactive zones (jtop marks them offline and reports -256)
    temps = jetson.temperature
    if temps:
        for zone, val in temps.items():
            if isinstance(val, dict):
                if not val.get('online', True):
                    continue
                val = val.get('temp')
            val = num(val)
            if val is not None and val > -200:
                set_labelled(seen, g_temp, val, zone)


def collect_power(jetson, seen):
    # jtop reports in mW -> watts
    power = jetson.power
    if power:
        total = num((power.get('tot') or {}).get('power'))
        if total is not None:
            g_power_total.set(total / 1000)
        for rail_name, rail_data in power.get('rail', {}).items():
            rail_power = num(rail_data.get('power'))
            if rail_data.get('online') and rail_power is not None:
                set_labelled(seen, g_power_rail, rail_power / 1000, rail_name)


def collect_engines(jetson, seen):
    # jtop freq in kHz -> Hz
    engines = jetson.engine
    if engines:
        for eng_name, eng_units in engines.items():
            for unit_name, unit_data in eng_units.items():
                set_labelled(seen, g_engine_online, 1 if unit_data.get('online') else 0, eng_name, unit_name)
                # Engines absent on the board can report cur=None
                cur = num(unit_data.get('cur'))
                if cur is not None:
                    set_labelled(seen, g_engine_freq, cur * 1e3, eng_name, unit_name)


def collect_disk(jetson, seen):
    # jtop reports in GiB -> bytes
    disk = jetson.disk
    if disk:
        g_disk_total.set((num(disk.get('total')) or 0) * GIB)
        g_disk_used.set((num(disk.get('used')) or 0) * GIB)
        g_disk_available.set((num(disk.get('available')) or 0) * GIB)


def collect_jetson_clocks(jetson, seen):
    g_jetson_clocks.set(1 if jetson.jetson_clocks else 0)


COLLECTORS = (collect_gpu, collect_memory, collect_cpu, collect_fan, collect_nvpmodel,
              collect_temperature, collect_power, collect_engines, collect_disk,
              collect_jetson_clocks)


def collect(jetson):
    seen = {}
    # Collect each section independently so one bad value doesn't drop every metric after it
    for collector in COLLECTORS:
        try:
            collector(jetson, seen)
        except Exception as e:
            print(f"{collector.__name__} error: {e!r}")
    prune_stale(seen)


def main():
    start_http_server(PORT)
    print(f"Jetson exporter running on :{PORT}")

    with jtop() as jetson:
        # Set static board info once
        try:
            hw   = jetson.board.get('hardware', {})
            libs = jetson.board.get('libraries', {})
            g_board_info.info({
                'model':  str(hw.get('Model') or 'unknown'),
                'module': str(hw.get('Module') or 'unknown'),
                'l4t':    str(hw.get('L4T') or 'unknown'),
                'cuda':   str(libs.get('CUDA') or 'unknown'),
            })
        except Exception as e:
            print(f"board info error: {e!r}")

        while jetson.ok():
            collect(jetson)
            time.sleep(5)

    # jetson.ok() returns False when jtop stops sending data. Exit non-zero so
    # systemd restarts us instead of treating it as a clean, intentional stop.
    sys.exit("jtop stopped sending data, exiting")


if __name__ == '__main__':
    main()
