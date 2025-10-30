import os
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from influxdb_writer import InfluxConfig

try:
    import influxdb_client
    from influxdb_client import InfluxDBClient
    HAS_INFLUX = True
except Exception:
    HAS_INFLUX = False
    InfluxDBClient = None  # type: ignore

logger = logging.getLogger(__name__)

class InfluxQuery:
    _client: Optional["InfluxDBClient"] = None

    @classmethod
    def _get_env(cls) -> dict:
        return {
            "endpoint": os.getenv("INFLUXDB_ENDPOINT") or '',
            "port": os.getenv("INFLUXDB_PORT") or 8086,
            "url": os.getenv("INFLUXDB_URL") or cls._build_url(),
            "token": os.getenv("INFLUXDB_TOKEN"),
            "org": os.getenv("INFLUXDB_ORG"),
            "bucket": os.getenv("INFLUXDB_BUCKET", "valkey_metrics"),
        }

    @staticmethod
    def _build_url() -> Optional[str]:
        endpoint = os.getenv("INFLUXDB_ENDPOINT")
        port = os.getenv("INFLUXDB_PORT")
        scheme = os.getenv("INFLUXDB_SCHEME", "http")
        if endpoint and port:
            return f"{scheme}://{endpoint}:{port}"
        if endpoint:
            return f"{scheme}://{endpoint}"
        return None

    @classmethod
    def _ensure_client(cls) -> bool:
        if not HAS_INFLUX:
            logger.warning("influxdb-client not installed; queries disabled")
            return False
        if cls._client is not None:
            return True
        env = cls._get_env()
        if not env["url"] or not env["token"] or not env["org"]:
            logger.warning("InfluxDB read env not fully configured")
            return False
        try:
            cls._client = InfluxDBClient(url=env["url"], token=env["token"], org=env["org"])  # type: ignore
            logger.info(f"InfluxDB query client ready: {env['url']}")
            return True
        except Exception as e:
            logger.error(f"Failed to init InfluxDB query client: {e}")
            return False

    @classmethod
    def query_metric(
        cls,
        metric_field: str,
        config: InfluxConfig,
        start: str,
        stop: Optional[str],
        redis_endpoint: Optional[str],
        measurement: str = "valkey_metrics",
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query a single field (metric_field) from measurement within time range.
        metric_field should match how fields were flattened, e.g., "server.system_cpu_percentage".
        start/stop must be RFC3339 timestamps e.g., 2025-09-16T13:00:00Z
        """

        print("+++++++++++++++++++++++++++++")
        print("")
        print(f'redis_endpoint:{redis_endpoint}')
        print("")
        print("+++++++++++++++++++++++++++++")


        if not cls._ensure_client():
            return []
        env = cls._get_env()
        org = env["org"]
        bucket = env["bucket"]

        try:
            influxBucket = config.influxBucket
            influxOrg = config.influxOrg

            print(f' ----- INFLUX BUCKET FROM CONFIG IS {influxBucket}')

            org = influxOrg
            bucket = influxBucket

        except Exception as e:
            logger.error(f"influxBucket and Org failed, taking from environments variables: {e}")


        flux = [
            f'from(bucket: "{bucket}")',
            f'|> range(start: time(v: "{start}"), stop: time(v: "{stop}") )' if stop else f'|> range(start: time(v: "{start}") )',
            f'|> filter(fn: (r) => r._measurement == "{measurement}")',
            f'|> filter(fn: (r) => r._field == "{metric_field}")',
        ]
        if redis_endpoint:
            # We tag host=vc.host in writer; the frontend passes redisEndpoint which is host
            flux.append(f'|> filter(fn: (r) => r.host == "{redis_endpoint}")')
        flux.append('|> keep(columns: ["_time", "_value", "host", "port", "name", "_field"])')
        if limit:
            flux.append(f'|> limit(n: {int(limit)})')
        query = "\n".join(flux)

        print(f'FLUX:{query}')

        try:
            influxEndpoint = config.influxEndpoint
            influxPort = config.influxPort
            influxToken = config.influxToken
            influxBucket = config.influxBucket
            influxOrg = config.influxOrg

            print(f' ----- INFLUX ENDPOINT FROM CONFIG IS {influxEndpoint}')
            print(f' ----- INFLUX PORT FROM CONFIG IS {influxPort}')
            print(f' ----- INFLUX TOKEN FROM CONFIG IS {influxToken}')
            print(f' ----- INFLUX BUCKET FROM CONFIG IS {influxBucket}')
            print(f' ----- INFLUX ORG FROM CONFIG IS {influxOrg}')

            token = os.environ.get("INFLUXDB_TOKEN")
            bucket = os.environ.get("INFLUXDB_BUCKET")
            org = os.environ.get("INFLUXDB_ORG")
            url = "https:" + os.environ.get("INFLUXDB_ENDPOINT") + ":" + str(os.environ.get("INFLUXDB_PORT"))

#            if 'amazonaws.com' in influxEndpoint:
            token = influxToken
            bucket = influxBucket
            org = influxOrg
            url = 'https://' + influxEndpoint + ':' + influxPort

            print("------ Executing query --------------")
            print(f'{url}-{token}-{org}')
            client = influxdb_client.InfluxDBClient(url=url, token=token, org=org)
            query_api = client.query_api()
            tables = query_api.query(query, org=org)
            print("------ Finish Executing query -------")

            # tables = cls._client.query_api().query(query, org=org)  # type: ignore
            points: List[Dict[str, Any]] = []
            for table in tables:
                for rec in table.records:
                    # print(rec)
                    points.append({
                        "time": rec.get_time().isoformat(),
                        "value": rec.get_value(),
                        "host": rec.values.get("host"),
                        "port": rec.values.get("port"),
                        "name": rec.values.get("name"),
                        "field": rec.values.get("_field"),
                    })
            return points
        except Exception as e:
            logger.error(f"Influx query failed: {e}")
            return []

# convenience
query_metric = InfluxQuery.query_metric
