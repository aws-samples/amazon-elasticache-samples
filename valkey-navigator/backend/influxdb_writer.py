import os
import logging
from datetime import datetime
from typing import Dict, Any, Optional

try:
    import influxdb_client
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS
    HAS_INFLUX = True
except Exception:
    HAS_INFLUX = False
    InfluxDBClient = None  # type: ignore
    Point = None  # type: ignore
    WritePrecision = None  # type: ignore

logger = logging.getLogger(__name__)

allowed_string_fields = [
    'server.redis_version',
    'server.redis_mode',
    'server.os',
    'cluster.role'
]

class InfluxConfig:
    influxEndpoint = None
    influxPort = 8086
    influxToken = None
    influxBucket = None
    influxOrg = None

    def __init__(self, endpoint, port, token, bucket, org):
        self.influxEndpoint = endpoint
        self.influxPort = port
        self.influxToken = token
        self.influxBucket = bucket
        self.influxOrg = org

class InfluxWriter:
    _client: Optional["InfluxDBClient"] = None
    _write_api = None

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
            logger.warning("influxdb-client not installed; metrics will not be written")
            return False
        if cls._client is not None and cls._write_api is not None:
            return True
        env = cls._get_env()
        if not env["url"] or not env["token"] or not env["org"] or not env["bucket"]:
            logger.warning("InfluxDB env not fully configured (INFLUXDB_URL/ENDPOINT+PORT, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET). Skipping writes.")
            return False
        try:
            cls._client = InfluxDBClient(url=env["url"], token=env["token"], org=env["org"])  # type: ignore
            cls._write_api = cls._client.write_api(write_options=SYNCHRONOUS)  # type: ignore
            logger.info(f"Connected InfluxDB client at {env['url']} org={env['org']} bucket={env['bucket']}")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize InfluxDB client: {e}")
            return False

    @classmethod
    def write_valkey_metrics(cls,
        vc, metrics: Dict[str, Any],
        config: InfluxConfig,
        timestamp: Optional[datetime] = None
        ):
        """
        Write all metrics to InfluxDB as one Point with multiple fields.
        Tags: name, host, port.
        Fields: flattened metrics dict.
        """

        influxEndpoint = config.influxEndpoint
        influxPort = config.influxPort
        influxToken = config.influxToken
        influxBucket = config.influxBucket
        influxOrg = config.influxOrg

        print(f' ----- INFLUX ENDPOINT FROM CONFIG IS [{influxEndpoint}]')
        print(f' ----- INFLUX PORT FROM CONFIG IS {influxPort}')
        print(f' ----- INFLUX TOKEN FROM CONFIG IS {influxToken}')
        print(f' ----- INFLUX BUCKET FROM CONFIG IS {influxBucket}')
        print(f' ----- INFLUX ORG FROM CONFIG IS {influxOrg}')

        try:

            if influxEndpoint is None or influxEndpoint is '':
                influxEndpoint = os.getenv("INFLUXDB_ENDPOINT")
                influxPort = os.getenv("INFLUXDB_PORT")
                influxToken = os.getenv("INFLUXDB_TOKEN")
                influxBucket = os.getenv("INFLUXDB_BUCKET")
                influxOrg = os.getenv("INFLUXDB_ORG")
                print('======= No valid Influx Settings from UI, using environment variables')
                print(f' ----- INFLUX ENDPOINT FROM CONFIG IS [{influxEndpoint}]')
                print(f' ----- INFLUX PORT FROM CONFIG IS {influxPort}')
                print(f' ----- INFLUX TOKEN FROM CONFIG IS {influxToken}')
                print(f' ----- INFLUX BUCKET FROM CONFIG IS {influxBucket}')
                print(f' ----- INFLUX ORG FROM CONFIG IS {influxOrg}')
            else:
                print('---   Valid Influx provided from UI')

        except Exception as e:
            logger.error(f"Failed to set environment for InfluxDB: {e}")

        print('==== Finished getting Influx Settings')

        if not cls._ensure_client():
            return
        env = cls._get_env()
        ts = timestamp or datetime.utcnow()

        # Flatten metrics into simple key->value fields (numbers only where possible)
        def flatten(prefix: str, obj: Any, out: Dict[str, Any]):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
            elif isinstance(obj, list):
                # store size and maybe stringified sample
                out[f"{prefix}.count"] = len(obj)
            else:
                # only store primitive types; stringify others
                if isinstance(obj, (int, float, bool)):
                    out[prefix] = obj
                else:
                    # store as string field
                    out[prefix] = str(obj)

        fields: Dict[str, Any] = {}
        for section, data in metrics.items():
            if section in ("collection_info", "collected_at"):
                continue
            flatten(section, data, fields)

        try:

            token = influxToken # os.environ.get("INFLUXDB_TOKEN")
            bucket = influxBucket # os.environ.get("INFLUXDB_BUCKET")
            org = influxOrg # "wwso-ssa"
            url = 'https://' + influxEndpoint + ':' + str(influxPort) # "https://nitro-fleet-xvhurycie5d7sy.us-east-1.timestream-influxdb.amazonaws.com:8086"

            print(f'URL === {url}')

            client = influxdb_client.InfluxDBClient(url=url, token=token, org=org)

            write_api = client.write_api(write_options=SYNCHRONOUS)

            point = Point("valkey_metrics") \
                .tag("name", getattr(vc, "name", "unknown")) \
                .tag("host", getattr(vc, "host", "unknown")) \
                .tag("port", str(getattr(vc, "port", "0")))
            for k, v in fields.items():
                # InfluxDB accepts different types; write generically
                # print(f'{k} - {type(v)} - {v}')
                # Force all values to be float. InfluxDB cannot change type once ingested


                ignore = False
                field_processed = False
                if 'timestamp' in k:
                    ignore = True
                    field_processed = True


                if k in allowed_string_fields:
                    point = point.field(k, v)
                    field_processed = True

                if field_processed is False:
                    try:
                        fvalue = float(v)
                        point = point.field(k, fvalue)
                    except:
                        field_added = True

                if field_processed is False:
                    try:
                        sv = v.replace('M','')
                        fvalue = float(v) * 1000000.0
                        point = point.field(k, fvalue)
                    except:
                        field_processed = True

                if field_processed is False:
                    try:
                        sv = v.replace('K','')
                        fvalue = float(v) * 1000.0
                        point = point.field(k, fvalue)
                    except:
                        field_processed = True

                if field_processed is False:
                    try:
                        sv = v.replace('B','')
                        fvalue = float(v)
                        point = point.field(k, fvalue)
                    except:
                        field_processed = True

                if field_processed is False:
                    try:
                        sv = v.replace('%','')
                        fvalue = float(v)
                        point = point.field(k, fvalue)
                    except:
                        field_processed = True

                if field_processed is False:
                    print(f'WARNING - {k} - type "{type(v)}" - {v} -- cannot be converted to float')

#             point = (
#                 Point("debug")
#                 .tag("tagname1", "tagvalue1")
#                 .field("field1", 100)
#             )
#
#             print(point)
            point = point.time(ts, WritePrecision.NS)  # type: ignore
#            cls._write_api.write(bucket=env["bucket"], org=env["org"], record=point)  # type: ignore
            write_api.write(bucket=bucket, org="wwso-ssa", record=point)
            logger.debug(f"Wrote metrics to InfluxDB for {getattr(vc,'host','unknown')}:{getattr(vc,'port','')}")
        except Exception as e:
            logger.error(f"Failed to write metrics to InfluxDB: {e}")

# Convenience function
write_valkey_metrics = InfluxWriter.write_valkey_metrics
