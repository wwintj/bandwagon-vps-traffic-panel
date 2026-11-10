"""Host information for the server card."""
import os
import platform
from datetime import timedelta

import psutil

from app import bandwagon, net, util


def get_load_average():
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def build_system_info(client_ip):
    """Blocking: public IP lookups, ip-api calls and a 0.1s CPU sample. Run via run_blocking."""
    ip, ip_info = net.get_cached_public_ip_info()
    client_ip_info = net.lookup_ip_info(client_ip)

    uptime = "Unknown"
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.readline().split()[0])
            uptime = str(timedelta(seconds=int(uptime_seconds)))
    except Exception:
        pass

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')

    return {
        "public_ip": ip,
        "client_ip": client_ip,
        "client_ip_location": net.format_ip_location(client_ip_info),
        "client_ip_lat": client_ip_info.get("lat"),
        "client_ip_lon": client_ip_info.get("lon"),
        "client_ip_timezone": client_ip_info.get("timezone") or "Unknown",
        "client_ip_isp": client_ip_info.get("isp") or "Unknown",
        "client_ip_org": client_ip_info.get("org") or "Unknown",
        "client_ip_access_type": net.classify_access_network(client_ip_info),
        "ip_location": net.format_ip_location(ip_info),
        "ip_isp": ip_info.get("isp") or "Unknown",
        "ip_org": ip_info.get("org") or "Unknown",
        "ip_as": ip_info.get("as") or "Unknown",
        "ip_type": net.classify_ip_info(ip_info),
        "bandwagon": bandwagon.bandwagon_plan_summary(),
        "hostname": platform.node(),
        "uptime": uptime,
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "load_avg": get_load_average(),
        "memory_total": mem.total,
        "memory_used": mem.used,
        "memory_percent": mem.percent,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_percent": disk.percent,
        "os_version": util.get_os_pretty_name(),
        "kernel_release": platform.release(),
        "kernel_version": platform.version(),
    }
