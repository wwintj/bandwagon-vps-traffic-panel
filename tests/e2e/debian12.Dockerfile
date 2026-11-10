# Debian 12 with systemd as PID 1, for running tests/e2e/run.sh in a container.
FROM debian:12
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq systemd systemd-sysv dbus curl ca-certificates openssl iproute2 python3 git procps sudo \
 && rm -rf /var/lib/apt/lists/* \
 && systemctl mask getty.target systemd-logind.service console-getty.service
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
