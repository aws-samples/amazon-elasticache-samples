# ═══════════════════════════════════════════════════════════════════════════
# ECDL — Debezium Server
#
# Upstream, plus this repo's configuration. The base image already contains
# everything executable we need -- verified by listing /debezium/lib in
# 3.0.6.Final: the mysql, postgres, sqlserver and oracle connectors are all
# bundled, and so is ojdbc11 (so Oracle needs no driver added, contrary to older
# guidance). We add no jars and no code.
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
FROM ${DEBEZIUM_BASE}

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
ENTRYPOINT ["/ecdl-entrypoint.sh"]

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -f http://localhost:8080/q/health || exit 1
