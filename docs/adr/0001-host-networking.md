# ADR 0001: Run every container with `network_mode: host`

## Status

Accepted.

## Context

LiveKit negotiates WebRTC media over a 10,000-port UDP range (`50000-60000`)
and the SIP bridge needs the same for RTP. Publishing that range through the
Docker bridge network costs one iptables/NAT rule per port, makes container
start take minutes, and adds a userspace hop on every media packet. The lane
hosts are single-tenant boxes that run nothing but this stack.

## Decision

[`deploy/docker-compose.yml`](../../deploy/docker-compose.yml) defines seven
services. Six of them — `redis`, `livekit`, `sip`, `caddy`, `worker`, and
`scheduler` — run with `network_mode: host`. The seventh, `autoheal`, runs
with `network_mode: none`: it only needs the Docker socket, so it gets no
network at all. Nothing is published through Docker's port mapping; the cloud
firewall (`deploy/provisioning/firewall-setup.sh`) is the only thing between
the public internet and a listening socket.

Because host networking removes the container boundary as a network boundary,
every internal listener must bind to loopback explicitly:

* Redis: `--bind 127.0.0.1`.
* Worker Docker health server: `127.0.0.1:8080` (`/health`, `/ready`, `/live`).
* LiveKit worker HTTP: `127.0.0.1:8082`.

## Consequences

* Media latency and start-up time are those of a native process.
* A service that forgets to bind loopback is exposed to the internet the
  moment the firewall has a hole. Every new listener is reviewed for its bind
  address; `tests/test_f_g5_health_logger.py` pins the health server default.
* Only one instance of the stack can run per host, since ports are global.
* Container-to-container DNS does not exist; services find each other on
  `127.0.0.1` with fixed ports, which is why ports are documented in the
  README and this ADR rather than discovered.
