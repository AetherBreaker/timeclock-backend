# The tunnel endpoint the clocks dial so Coolify can reach their hosts (timeclock-system, Decided 29).
# An unlisted compose service: not devkit-container's, so it keeps its own Dockerfile.
FROM debian:trixie-slim

# The password field is set to '*' (not '!'): sshd treats a '!'-locked account as locked even for keys.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server \
  && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
  && useradd --create-home --shell /usr/sbin/nologin tctunnel \
  && usermod -p '*' tctunnel \
  && mkdir -p /run/sshd

COPY docker/tunnel/sshd_config /etc/ssh/sshd_config
COPY docker/tunnel/authorized_keys /etc/ssh/tunnel_authorized_keys
COPY docker/tunnel/entrypoint.sh /entrypoint.sh
RUN chmod 644 /etc/ssh/sshd_config /etc/ssh/tunnel_authorized_keys && chmod 755 /entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
