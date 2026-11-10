import asyncio
import os
import time
import logging
from app.database import save_traffic_increment
from app import timeutil
from app.config import INTERFACE

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

collector_instance = TrafficCollector()
