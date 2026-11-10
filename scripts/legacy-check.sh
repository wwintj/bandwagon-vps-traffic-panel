#!/bin/bash
# Warns when the standalone cloudflare-ddns script (cf-ddns.timer) is still active.
# Its job is now built into the panel; running both makes two updaters edit the same record.
# Usage: legacy-check.sh          print a notice if found
#        legacy-check.sh remove   stop and remove the standalone script

has_legacy_ddns() {
    [ -f /etc/systemd/system/cf-ddns.timer ] || [ -f /etc/systemd/system/cf-ddns.service ] || [ -x /usr/local/bin/cf-ddns-update ]
}

remove_legacy_ddns() {
    systemctl disable --now cf-ddns.timer >/dev/null 2>&1 || true
    systemctl stop cf-ddns.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/cf-ddns.service /etc/systemd/system/cf-ddns.timer /usr/local/bin/cf-ddns-update
    systemctl daemon-reload
    echo "Removed the standalone cf-ddns script. Its config is kept in /etc/cf-ddns (delete it when no longer needed)."
}

case "${1:-}" in
    remove)
        if has_legacy_ddns; then
            remove_legacy_ddns
        else
            echo "Standalone cf-ddns script not found."
        fi
        ;;
    *)
        if has_legacy_ddns; then
            echo -e "\033[33m"
            echo "Notice: the standalone Cloudflare DDNS script (cf-ddns.timer) is installed on this server."
            echo "        The panel now does the same job (DDNS button in the panel). Running both means two"
            echo "        programs update the same DNS record. After enabling DDNS in the panel, remove the old one:"
            echo "            bash $(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/legacy-check.sh remove"
            echo -e "\033[0m"
        fi
        ;;
esac
exit 0
