# A disposable "database host" for trying the tool end to end: PostgreSQL 16 and sshd in one
# container, with a continuous read-heavy shop workload. The tool reaches it the way it reaches
# a real host: over SSH, running psql on the host. See README.md.
#
# Only sshd is meant to be published, and only on 127.0.0.1 (up.sh does that). PostgreSQL
# listens on localhost inside the container and is never published.
FROM public.ecr.aws/docker/library/postgres:16

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server sudo python3 procps iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd \
 && sed -i 's/^AcceptEnv/# AcceptEnv/' /etc/ssh/sshd_config \
 && useradd --create-home --shell /bin/bash apcat \
 && passwd -l apcat \
 && echo 'apcat ALL=(postgres) NOPASSWD: ALL' > /etc/sudoers.d/apcat \
 && chmod 0440 /etc/sudoers.d/apcat

# Key-only SSH, no root login, no forwarding.
RUN printf '%s\n' \
      'PasswordAuthentication no' 'KbdInteractiveAuthentication no' 'PermitRootLogin no' \
      'PubkeyAuthentication yes' 'AllowUsers apcat' 'AllowTcpForwarding no' \
      'X11Forwarding no' 'PermitTunnel no' \
      > /etc/ssh/sshd_config.d/apcat.conf

COPY schema.sql /docker-entrypoint-initdb.d/10-schema.sql
COPY load/ /opt/load/
COPY entrypoint.sh /usr/local/bin/apcat-entrypoint.sh
RUN chmod 0755 /usr/local/bin/apcat-entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/apcat-entrypoint.sh"]
CMD ["postgres", "-c", "listen_addresses=localhost", \
     "-c", "shared_preload_libraries=pg_stat_statements", \
     "-c", "pg_stat_statements.track=top", "-c", "pg_stat_statements.max=5000", \
     "-c", "track_io_timing=on", "-c", "shared_buffers=48MB", "-c", "work_mem=1MB", \
     "-c", "max_connections=60"]
