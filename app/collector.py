import asyncio
import json
import os
import time
import logging
from datetime import timedelta
from app.database import save_traffic_increment, get_meta_value, set_meta_value
from app import timeutil
from app.config import INTERFACE

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def read_boot_id():
    try:
        with open(BOOT_ID_PATH) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def spread_over_hours(start, end, rx, tx):
    """Split rx/tx bytes over the hourly buckets between two panel-time datetimes, by elapsed time."""
    if end <= start:
        return [((end.year, end.month, end.day, end.hour), rx, tx)]
    segments = []
    cursor = start
    while cursor < end:
        boundary = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        segment_end = min(boundary, end)
        segments.append(((cursor.year, cursor.month, cursor.day, cursor.hour), (segment_end - cursor).total_seconds()))
        cursor = segment_end
    total = sum(seconds for _, seconds in segments)
    result, given_rx, given_tx = [], 0, 0
    for index, (bucket, seconds) in enumerate(segments):
        if index == len(segments) - 1:
            part_rx, part_tx = rx - given_rx, tx - given_tx
        else:
            part_rx, part_tx = int(rx * seconds / total), int(tx * seconds / total)
        given_rx += part_rx
        given_tx += part_tx
        result.append((bucket, part_rx, part_tx))
    return result

logger = logging.getLogger("uvicorn.error")

class TrafficCollector:
    def __init__(self):
        self.interface = INTERFACE or self._get_default_interface()
        self.running = False
        self.interface_status = False
        
        self.last_rx = 0
        self.last_tx = 0
        self.current_rx_speed = 0
        self.current_tx_speed = 0
        
        self.pending_buckets = {}
        
        self.flush_interval = 60
        self.last_flush = time.time()
        self.last_log_time = 0

    def _get_default_interface(self):
        try:
            with os.popen("ip route show default") as f:
                for line in f:
                    parts = line.strip().split()
                    if "dev" in parts:
                        iface = parts[parts.index("dev") + 1]
                        if iface:
                            return iface
        except Exception:
            pass
        return self._get_active_interface() or "eth0"

    def _read_all_interfaces(self):
        interfaces = {}
        try:
            with open('/proc/net/dev', 'r') as f:
                for line in f:
                    if ':' not in line:
                        continue
                    name, data = line.split(':', 1)
                    name = name.strip()
                    parts = data.split()
                    if len(parts) >= 16:
                        interfaces[name] = (int(parts[0]), int(parts[8]))
        except Exception:
            pass
        return interfaces

    def _get_active_interface(self):
        ignored_prefixes = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap")
        candidates = {
            name: counters
            for name, counters in self._read_all_interfaces().items()
            if not name.startswith(ignored_prefixes)
        }
        if not candidates:
            return ""
        return max(candidates.items(), key=lambda item: item[1][0] + item[1][1])[0]

    def _read_net_dev(self):
        interfaces = self._read_all_interfaces()
        if self.interface in interfaces:
            rx, tx = interfaces[self.interface]
            if not INTERFACE and rx == 0 and tx == 0:
                active_iface = self._get_active_interface()
                if active_iface and active_iface != self.interface:
                    self.interface = active_iface
                    rx, tx = interfaces.get(self.interface, (rx, tx))
            self.interface_status = True
            return rx, tx

        detected_iface = self._get_default_interface() if not INTERFACE else ""
        if detected_iface in interfaces:
            self.interface = detected_iface
            self.interface_status = True
            return interfaces[detected_iface]
            
        if time.time() - self.last_log_time >= 60:
            logger.warning(f"Interface '{self.interface}' dropped or not found in /proc/net/dev")
            self.last_log_time = time.time()
            
        self.interface_status = False
        return 0, 0

    async def start(self):
        self.running = True
        self.last_rx, self.last_tx = self._read_net_dev()
        if self.interface_status:
            try:
                self.recover_downtime(self.last_rx, self.last_tx)
            except Exception as exc:
                logger.warning("Could not recover traffic from while the panel was stopped: %s", exc)
        
        try:
            while self.running:
                await asyncio.sleep(1)
                prev_interface = self.interface
                prev_status = self.interface_status
                curr_rx, curr_tx = self._read_net_dev()

                if not self.interface_status:
                    self.current_rx_speed = 0
                    self.current_tx_speed = 0
                    continue

                # After switching interfaces, or when an interface comes back, the counters belong to a
                # different baseline. Re-anchor instead of counting everything since boot as new traffic.
                if self.interface != prev_interface or not prev_status:
                    self.last_rx = curr_rx
                    self.last_tx = curr_tx
                    self.current_rx_speed = 0
                    self.current_tx_speed = 0
                    continue

                rx_diff = curr_rx - self.last_rx if curr_rx >= self.last_rx else curr_rx
                tx_diff = curr_tx - self.last_tx if curr_tx >= self.last_tx else curr_tx
                
                self.current_rx_speed = rx_diff
                self.current_tx_speed = tx_diff
                
                now = timeutil.now()
                bucket_key = (now.year, now.month, now.day, now.hour)
                if bucket_key not in self.pending_buckets:
                    self.pending_buckets[bucket_key] = {"rx": 0, "tx": 0}
                    
                self.pending_buckets[bucket_key]["rx"] += rx_diff
                self.pending_buckets[bucket_key]["tx"] += tx_diff
                
                self.last_rx = curr_rx
                self.last_tx = curr_tx
                
                if time.time() - self.last_flush >= self.flush_interval:
                    self.flush_to_db()
                    
        except asyncio.CancelledError:
            pass
        finally:
            self.flush_to_db()

    def snapshot_pending(self):
        """Copy of the unflushed buckets, safe to hand to a worker thread."""
        return {key: dict(value) for key, value in self.pending_buckets.items()}

    def flush_to_db(self):
        buckets_to_flush = self.pending_buckets
        self.pending_buckets = {}
        self.last_flush = time.time()
        
        for (y, m, d, h), traffic in buckets_to_flush.items():
            if traffic["rx"] > 0 or traffic["tx"] > 0:
                save_traffic_increment(traffic["rx"], traffic["tx"], y, m, d, h)
        self.save_counter_state()

    def save_counter_state(self):
        """Remember the kernel counters that are already accounted for, so traffic that happens while
        the panel is stopped (crash, update, manual stop) can be recovered on the next start."""
        if not self.interface_status:
            return
        set_meta_value("collector_state", json.dumps({
            "boot_id": read_boot_id(),
            "iface": self.interface,
            "rx": self.last_rx,
            "tx": self.last_tx,
            "at": time.time(),
        }))

    def recover_downtime(self, rx, tx):
        """Add traffic counted by the kernel since the last saved state, if the machine has not
        rebooted (same boot id) and the interface is the same. Returns (rx, tx) recovered."""
        try:
            state = json.loads(get_meta_value("collector_state", "") or "{}")
        except ValueError:
            return 0, 0
        boot_id = read_boot_id()
        if not state or not boot_id or state.get("boot_id") != boot_id or state.get("iface") != self.interface:
            return 0, 0
        gap_rx, gap_tx = rx - int(state.get("rx", 0)), tx - int(state.get("tx", 0))
        if gap_rx < 0 or gap_tx < 0 or (gap_rx == 0 and gap_tx == 0):
            return 0, 0
        start = timeutil.from_unix(state.get("at", time.time()))
        for (y, m, d, h), part_rx, part_tx in spread_over_hours(start, timeutil.now(), gap_rx, gap_tx):
            if part_rx or part_tx:
                save_traffic_increment(part_rx, part_tx, y, m, d, h)
        logger.info("Recovered %s down / %s up bytes counted while the panel was stopped", gap_rx, gap_tx)
        return gap_rx, gap_tx

collector_instance = TrafficCollector()
