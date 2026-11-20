# ═══════════════════════════════════════════════════════════════════════════
# ECDL — Debezium Server
#
# Upstream, plus this repo's configuration. The base image already contains
# everything executable we need -- verified by listing /debezium/lib in
# 3.0.6.Final: the mysql, postgres, sqlserver and oracle connectors are all
# bundled, and so is ojdbc11 (so Oracle needs no driver added, contrary to older
# guidance). We add no jars. We add one piece of code, broker-writer/ below: it
# replaces how the Redis sink delivers to the broker, and how offsets are kept.
#
# WHY THIS IMAGE EXISTS AT ALL. Debezium Server can be configured entirely from
# DEBEZIUM_* environment variables -- that was measured, not assumed, and it
# works. So this image is a choice, and the reasons are:
#
#   * It carries ~40 fixed engineering decisions (the redis sink, extended
#     message format, decimal.handling.mode=string, the offset and schema-history
#     store classes, the RegexRouter that names the streams). Those are ours, not
#     the operator's. As environment variables they would sit in a CloudFormation
#     template where anyone can change one and quietly break the sink contract.
#   * Config differs per engine in property NAMES, not just values
#     (database.include.list vs database.dbname vs database.names). Env-var-only
#     config means the template carries all four engines' property sets and
#     branches on Conditions.
#   * One image, four Quarkus profiles, one variable to pick between them.
#
# Environment variables still override anything here (SmallRye ordinal 300 takes precedence over
# a config file's ~260), so a one-off change needs no rebuild. See the precedence
# note in application.properties.
# ═══════════════════════════════════════════════════════════════════════════
#
# BASE IMAGE SOURCE. Debezium Server 3.0.6.Final is not on ECR Public, so
# `release.sh push` copies the upstream index (pinned by digest, both
# architectures; see DBZ_UPSTREAM in release.sh) into the publishing account's
# own ECR as ecdl/debezium-server-base:3.0.6.Final, and passes that copy here.
# Deliberately no default: a build without it fails on the FROM line rather than
# quietly pulling from an external registry.
#   docker build --build-arg DEBEZIUM_BASE=<acct>.dkr.ecr.<region>.amazonaws.com/ecdl/debezium-server-base:3.0.6.Final .
ARG DEBEZIUM_BASE
# The JDK is pinned by digest (Jira story F181), like DEBEZIUM_BASE.
ARG JDK_IMAGE=public.ecr.aws/docker/library/eclipse-temurin:21-jdk@sha256:db5cc6341a1d7bc40cdb51e1f0d9824f3bb2cc8107d29da421dc4df7d11f05f1
# The runtime. Upstream's image is built on Red Hat UBI 8 with Java 21.0.5,
# whose OS packages and JDK carry known vulnerabilities that no newer 3.0.6
# image fixes. The same /debezium tree runs on the current Temurin 21 JRE.
ARG JRE_IMAGE=public.ecr.aws/docker/library/eclipse-temurin:21-jre@sha256:ca73b4d3961703d6d7830a4361f49ef0478f8f28b39defc07bb72f4cc7f834f6

# ── ECDL broker writer (broker-writer/) ─────────────────────────────────────
# Replaces Debezium 3.0.6's Redis sink delivery: see
# broker-writer/src/main/java/io/debezium/server/redis/EcdlStreamWriter.java.
# Compiled against the exact jars in the base image, tested, and placed in
# /debezium/config/lib, which run.sh puts ahead of lib/ on the classpath.
# Class files are the same on every architecture, so this stage runs on the
# build host's own platform, not under emulation.
# checkov:skip=CKV_DOCKER_7: DEBEZIUM_BASE has no default on purpose; release.sh always passes upstream's image by digest (DBZ_UPSTREAM).
FROM --platform=$BUILDPLATFORM ${DEBEZIUM_BASE} AS base
FROM --platform=$BUILDPLATFORM ${JDK_IMAGE} AS writer
COPY --from=base /debezium /debezium
COPY broker-writer /src
# The replacement class is written against this exact upstream jar. Any other
# version (a base image upgrade) fails the build here, rather than mixing an
# old copy of the class with a new Debezium.
# The offset store (EcdlOffsetBackingStore) is a new class, not a replacement,
# but it reads upstream's offset settings with upstream's config class and keeps
# offsets in the hash layout upstream's RedisOffsetBackingStore wrote, so the
# storage jar is pinned the same way. The schema history (EcdlSchemaHistory)
# does the same with upstream's history settings and stream layout.
# EcdlOffsetStoreIT and EcdlSchemaHistoryIT need a real Valkey (and
# PostgreSQL) and are not run here; see the comments at their tops.
RUN echo "ec5187d52c6762434a72f4f4d8ca7f56741c6d32a4c3b1e31d4210e058cc2d39  /debezium/lib/debezium-server-redis-3.0.6.Final.jar" \
      | sha256sum -c - \
 && echo "f22ff883a6e7ece15416d5303be0a5931e2075a76de226db5e55778da6d6362a  /debezium/lib/debezium-storage-redis-3.0.6.Final.jar" \
      | sha256sum -c - \
 && CP="$(ls /debezium/debezium-server-*runner.jar):/debezium/lib/*" \
 && mkdir -p /out /test \
 && javac --release 21 -Xlint:all,-processing,-serial,-path,-classfile -cp "$CP" -d /out \
      $(find /src/src/main -name '*.java') \
 && javac --release 21 -cp "/out:$CP" -d /test $(find /src/src/test -name '*.java') \
 && java -cp "/test:/out:$CP" io.debezium.server.redis.EcdlStreamWriterTest \
 && java -cp "/test:/out:$CP" io.debezium.server.redis.EcdlOffsetBackingStoreTest \
 && java -cp "/test:/out:$CP" io.debezium.server.redis.EcdlSchemaHistoryTest \
 && if jar tf "$(ls /debezium/debezium-server-*runner.jar)" | grep -q 'io/debezium/server/redis/RedisStreamChangeConsumer.class'; then \
      echo 'the runner jar contains the Redis sink class, so config/lib would not take precedence' >&2; exit 1; fi

FROM ${JRE_IMAGE}
# OS fixes published since the base image was built. bash and curl, which
# run.sh and the ECS health check use, are part of the image.
RUN apt-get update \
 && apt-get -y --no-install-recommends upgrade \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r -u 185 -g 0 -d /debezium -s /usr/sbin/nologin jboss
# Upstream's tree, unchanged: the runner, every connector jar and run.sh. Owned
# by the same uid upstream runs as (185); nothing in it needs root.
COPY --from=base --chown=185:0 /debezium /debezium
ENV SERVER_HOME=/debezium DEBEZIUM_VERSION=3.0.6.Final
WORKDIR /debezium
COPY --from=writer --chown=185:0 /out /debezium/config/lib

# /debezium/config is where Debezium Server looks; the error message when it
# cannot find a config names that path explicitly. NOT /debezium/conf.
#
# application.properties is the shared base. The four application-<engine>.
# properties files are Quarkus profile overlays: setting QUARKUS_PROFILE=<engine>
# loads the base and then that file on top. The profile names are exactly the
# engine_family values in ecdl_link/resolve.py, so the resolver's output needs no
# translating.
COPY application.properties            /debezium/config/application.properties
COPY application-mysql.properties      /debezium/config/application-mysql.properties
COPY application-postgresql.properties /debezium/config/application-postgresql.properties
COPY application-sqlserver.properties  /debezium/config/application-sqlserver.properties
COPY application-oracle.properties     /debezium/config/application-oracle.properties

# The upstream image ships *.example config files that Quarkus scans and then
# warns about ("Unrecognized configuration file ... .example"). Harmless, but the
# warnings are the first thing in the log on every start and they lead people
# to look for a problem that does not exist.
RUN rm -f /debezium/config/*.example

# QUARKUS_PROFILE has no safe default -- guessing could point, for example, a MySQL
# connector at a PostgreSQL port. Without it the container does still fail, but
# with "SRCFG00014: debezium.source.connector.class is required", which leads
# people to inspect a properties file that is correct. This wrapper names the actual
# cause and the actual fix, then runs the upstream launcher unchanged.
# --chmod on COPY rather than a RUN chmod, so the image's own USER never has to
# be known or switched. Needs BuildKit, which is what release.sh uses.
COPY --chmod=0555 entrypoint.sh /ecdl-entrypoint.sh
USER 185
ENTRYPOINT ["/ecdl-entrypoint.sh"]

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -f http://localhost:8080/q/health || exit 1
