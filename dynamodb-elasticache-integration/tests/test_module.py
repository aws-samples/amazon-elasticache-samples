import pytest
from ddbcache.cache_client import CacheClient
import boto3
from redis import Redis
import time
from decimal import Decimal
from boto3.dynamodb.types import Binary
from boto3.dynamodb.conditions import Attr, Key
from collections import Counter


REGION = "us-west-2"
REDIS_HOST = "localhost"

# Testing will construct a variety of DynamoDB tables with a variety of schemas
# in the given region.

# Each run uses a different namespace to simulate starting with an empty cache.

@pytest.fixture(scope='module')
def random_namespace():
    # Time in milliseconds as a string
    yield str(int(round(time.time() * 1000)))

@pytest.fixture(scope='module')
def clients(random_namespace):
    redis_client = Redis(host=REDIS_HOST, port=6379, decode_responses=True, ssl=True)

    real_client = boto3.client("dynamodb", region_name=REGION)
    cache_client = CacheClient(real_client, redis_client, ttl=60, namespace=random_namespace)

    real_resource = boto3.resource('dynamodb', region_name=REGION)
    cache_resource = boto3.resource('dynamodb', region_name=REGION)
    cache_resource.meta.client = CacheClient(cache_resource.meta.client, redis_client, ttl=60, namespace=random_namespace)

    yield (real_client, cache_client, real_resource, cache_resource)

def table(pktype, sktype):
    dynamodb = boto3.resource('dynamodb', region_name=REGION)

    table_name = "CacheTest" + pktype + (sktype if sktype else "")
    table_list = [table.name for table in dynamodb.tables.all()]
    if table_name in table_list:
        print(f"Table {table_name} already exists. Skipping creation.")
    else:
        key_schema = [{'AttributeName': 'pk', 'KeyType': 'HASH'}]
        attribute_definitions = [{'AttributeName': 'pk', 'AttributeType': pktype}]
        if sktype:
            key_schema.append({'AttributeName': 'sk', 'KeyType': 'RANGE'})
            attribute_definitions.append({'AttributeName': 'sk', 'AttributeType': sktype})
        # Make a GSI with the same schema as the base table for use with query and scan calls
        global_secondary_indexes = [
            {
                'IndexName': 'GSI1',
                'KeySchema': key_schema,
                'Projection': {
                    'ProjectionType': 'ALL'
                }
            }
        ]
        try:
            table = dynamodb.create_table(
                TableName=table_name,
                KeySchema=key_schema,
                AttributeDefinitions=attribute_definitions,
                GlobalSecondaryIndexes=global_secondary_indexes,
                BillingMode='PAY_PER_REQUEST'
            )

            # Wait for the table to be created
            print(f"Creating table {table_name} in on-demand mode. Please wait...")
            table.meta.client.get_waiter('table_exists').wait(TableName=table_name)
            print(f"Table {table_name} is now active and ready for use.")

        except Exception as e:
            print(f"Error creating table: {e}")

    return table_name
    #dynamodb_client.delete_table(TableName=table_name)

@pytest.fixture(scope='module')
def table_ss():
    yield table("S", "S")

@pytest.fixture(scope='module')
def table_bn():
    yield table("B", "N")

@pytest.fixture(scope='module')
def table_nb():
    yield table("N", "B")

@pytest.fixture(scope='module')
def table_s():
    yield table("S", None)

@pytest.fixture(scope='module')
def table_ss_loaded(table_ss):
    dynamodb = boto3.resource('dynamodb', region_name=REGION)
    table = dynamodb.Table(table_ss)
    table.put_item(Item={
        "pk": "queryme",
        "sk": "x",
        "Attribute1": "Hello",
        "Attribute2": "World"
    })
    table.put_item(Item={
        "pk": "queryme",
        "sk": "y",
        "Attribute1": "Hello",
        "Attribute2": "Amazon"
    })
    table.put_item(Item={
        "pk": "queryme2",
        "sk": "x",
        "Attribute1": "Hello",
        "Attribute2": "World"
    })
    table.put_item(Item={
        "pk": "queryme2",
        "sk": "y",
        "Attribute1": "Hello",
        "Attribute2": "Amazon"
    })
    yield table_ss

@pytest.fixture(scope='module')
def table_s_loaded(table_s):
    dynamodb = boto3.resource('dynamodb', region_name=REGION)
    table = dynamodb.Table(table_s)
    table.put_item(Item={
        "pk": "cat",
        "Attribute1": "Hello",
        "Attribute2": "World"
    })
    table.put_item(Item={
        "pk": "dog",
        "Attribute1": "Hello",
        "Attribute2": "Amazon"
    })
    table.put_item(Item={
        "pk": "cat2",
        "Attribute1": "Hello",
        "Attribute2": "World"
    })
    table.put_item(Item={
        "pk": "dog2",
        "Attribute1": "Hello",
        "Attribute2": "Amazon"
    })
    '''
    haveWeAlreadyDoneTheBigLoad = table.get_item(
        Key={ "pk": "1" },
        ProjectionExpression="pk"
    )
    if 'Item' not in haveWeAlreadyDoneTheBigLoad:
        # Save some huge items
        payload = "x" * 398000
        with table.batch_writer() as batch:
            for i in range(0, 100):
                batch.put_item(Item={
                    "pk": str(i),
                    "Payload": payload
                })
    '''
    yield table_s



def test_my_function():
    assert "Expected Result" == 'Expected Result'

def _compare_serialization(value):
    value_after = CacheClient._json_loads(CacheClient._json_dumps(value))
    assert value_after == value
    if isinstance(value, Decimal):
        assert isinstance(value_after, Decimal)
    elif isinstance(value, Binary):
        assert isinstance(value_after, Binary)
    elif isinstance(value, bytes):
        assert isinstance(value_after, bytes)


def test_cache_serialization():
    for val in (42, 'a', 3.14, True, None, b'abc',
                b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81',
                Binary(b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'),
                Decimal('3.9'), [1, 2, 3], {'a': 1, 'b': 2}, [b'a', b'b', b'c'],
                set(['a', 'b', 'c']), set([1, 2, 3]), set([Decimal('1.1'), Decimal('2.2'), Decimal('3.3')])):
        _compare_serialization(val)


def _compare_results(real_response, cache_miss_response, cache_hit_response, cache_sc_response):
    if 'Item' in real_response:
        #print("real:", real_response['Item'])
        #print("miss:", cache_miss_response['Item'])
        #print("hit :", cache_hit_response['Item'])
        #print("sc  :", cache_sc_response['Item'])
        assert real_response['Item'] == cache_miss_response['Item'] == cache_hit_response['Item'] == cache_sc_response['Item']
    else:
        assert 'Item' not in real_response
        assert 'Item' not in cache_miss_response
        assert 'Item' not in cache_hit_response
        assert 'Item' not in cache_sc_response
    if 'ConsumedCapacity' in real_response:
        assert real_response['ConsumedCapacity'] == cache_miss_response['ConsumedCapacity']
        assert real_response['ConsumedCapacity']['CapacityUnits'] * 2 == cache_sc_response['ConsumedCapacity']['CapacityUnits']
        assert real_response['ConsumedCapacity'] != cache_hit_response['ConsumedCapacity']
        assert real_response['ConsumedCapacity']["CapacityUnits"] == cache_miss_response["ConsumedCapacity"]["CapacityUnits"] > 0
        assert cache_hit_response["ConsumedCapacity"]["CapacityUnits"] == 0.0
    assert 'ResponseMetadata' in real_response
    assert 'ResponseMetadata' in cache_miss_response
    assert 'ResponseMetadata' not in cache_hit_response
    assert 'ResponseMetadata' in cache_sc_response
    assert 'CacheMetadata' not in real_response
    assert 'CacheMetadata' not in cache_miss_response
    assert 'CacheMetadata' in cache_hit_response
    assert 'CacheMetadata' not in cache_sc_response
    assert 'CacheHit' in cache_hit_response['CacheMetadata']
    assert 'CachedTime' in cache_hit_response['CacheMetadata']
    assert 'Client' in cache_hit_response['CacheMetadata']


def _client_get_item_series(dynamodb_client, cache_client, table_name, key, return_consumed_capacity):
    real_response = dynamodb_client.get_item(
        TableName=table_name,
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity
    )
    cache_miss_response = cache_client.get_item(
        TableName=table_name,
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity
    )
    cache_hit_response = cache_client.get_item(
        TableName=table_name,
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity
    )
    cache_sc_response = cache_client.get_item(
        ConsistentRead=True,
        TableName=table_name,
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity
    )
    _compare_results(real_response, cache_miss_response, cache_hit_response, cache_sc_response)

    # Do it with a projection expression also

    real_response = dynamodb_client.get_item(
        TableName=table_name,
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity,
        ProjectionExpression="pk"
    )
    cache_miss_response = cache_client.get_item(
        TableName=table_name,
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity,
        ProjectionExpression="pk"
    )
    cache_hit_response = cache_client.get_item(
        TableName=table_name,
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity,
        ProjectionExpression="pk"
    )
    cache_sc_response = cache_client.get_item(
        ConsistentRead=True,
        TableName=table_name,
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity,
        ProjectionExpression="pk"
    )
    _compare_results(real_response, cache_miss_response, cache_hit_response, cache_sc_response)


def _resource_get_item_series(real_table, cache_table, key, return_consumed_capacity):
    real_response = real_table.get_item(
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity
    )
    cache_miss_response = cache_table.get_item(
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity
    )
    cache_hit_response = cache_table.get_item(
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity
    )
    cache_sc_response = cache_table.get_item(
        ConsistentRead=True,
        Key=key,
        ReturnConsumedCapacity=return_consumed_capacity
    )
    _compare_results(real_response, cache_miss_response, cache_hit_response, cache_sc_response)


def test_client_get_item_SS(clients, table_ss):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_ss

    sk = "x" + str(time.time()) # we want different on each run to avoid db side effects
    item = {
        "pk": {"S": "a"},
        "sk": {"S":  sk},
        "Attribute1": {"N": "42"},
        "Attribute2": {"S": "Hello"},
        "Attribute3": {"N": "3.9"},
        "Attribute4": {"BOOL": True},
        "Attribute5": {"NULL": True},
        "Attribute6": {"L": [{"N": "1"}, {"N": "2"}]},
        "Attribute7": {"M": {"a": {"N": "1"}, "b": {"N": "2"}}},
        "Attribute8": {"SS": ["a", "b", "c"]},
        "Attribute9": {"NS": ["1", "2", "3"]},
        "Attribute10": {"BS": [b"a", b"b", b"c"]},
        "Attribute11": {"S": "a" * 10},
        "Attribute12": {"B": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'}
    }
    key = {
        "pk": {"S": "a"},
        "sk": {"S": sk}
    }

    # Test in series: item missing, put, updated, batch put, tx updated, deleted, tx put, tx deleted
    # After each one we confirm the get-item was invalidated with a cache miss followed by hit

    _client_get_item_series(real_client, cache_client, tablename, key, "TOTAL")

    cache_client.put_item(
        TableName=tablename,
        Item=item
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "INDEXES")

    cache_client.update_item(
        TableName=tablename,
        Key=key,
        UpdateExpression="SET extraAttribute = :a",
        ExpressionAttributeValues={":a": {"N": "99"}}
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "NONE")

    cache_client.batch_write_item(
        RequestItems={tablename: [{"PutRequest": {"Item": item}}]}
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "NONE")

    cache_client.transact_write_items(

        TransactItems=[
            {"Update": {"TableName": tablename, "Key": key,
                        "UpdateExpression": "SET extraAttribute = :a", "ExpressionAttributeValues": {":a": {"N": "99"}}}}
        ]
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "NONE")

    cache_client.delete_item(
        TableName=tablename,
        Key=key
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "NONE")

    cache_client.transact_write_items(
        TransactItems=[
            {"Put": {"TableName": tablename, "Item": item, "ConditionExpression": "attribute_not_exists(pk)"}} # condition just for fun
        ]
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "NONE")

    cache_client.transact_write_items(
        TransactItems=[
            {"Delete": {"TableName": tablename, "Key": key}}
        ]
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "NONE")



def test_resource_get_item_SS(clients, table_ss):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_ss
    sk = "x" + str(time.time()) # we want different on each run to avoid db side effects
    item = {
        "pk": "b",
        "sk": sk,
        "Attribute1": 42,
        "Attribute2": "Hello",
        "Attribute3": Decimal('3.9'),
        "Attribute4": True,
        "Attribute5": None,
        'Attribute6': [1, 2],
        'Attribute7': {'a': 1, 'b': 2},
        'Attribute8': set(['a', 'b', 'c']),
        'Attribute9': set([1, 2, 3]),
        "Attribute10": set([b'a', b'b', b'c']),
        'Attribute11': 'a' * 10,
        'Attribute12': b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'
    }
    key = {
        "pk": "b",
        "sk": sk
    }

    real_table = real_resource.Table(tablename)
    cache_table = cache_resource.Table(tablename)

    _resource_get_item_series(real_table, cache_table, key, "TOTAL")

    cache_table.put_item(Item=item)

    _resource_get_item_series(real_table, cache_table, key, "INDEXES")

    cache_table.update_item(
        Key=key,
        UpdateExpression="SET extraAttribute = :a",
        ExpressionAttributeValues={":a": 99}
    )

    _resource_get_item_series(real_table, cache_table, key, "NONE")

    cache_table.delete_item(
        Key=key
    )

    _resource_get_item_series(real_table, cache_table, key, "NONE")

    with cache_table.batch_writer() as batch:
        batch.put_item(Item=item)

    _resource_get_item_series(real_table, cache_table, key, "NONE")

    with cache_table.batch_writer() as batch:
        batch.delete_item(Key=key)

    _resource_get_item_series(real_table, cache_table, key, "NONE")



def test_client_get_item_BN(clients, table_bn):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_bn

    sk = time.time() # a float
    item = {
        "pk": {"B": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'},
        "sk": {"N": str(sk)},
        "Attribute1": {"S": "extra"}
    }
    key = {
        "pk": {"B": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'},
        "sk": {"N": str(sk)}
    }

    _client_get_item_series(real_client, cache_client, tablename, key, "TOTAL")

    cache_client.put_item(
        TableName=tablename,
        Item=item
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "INDEXES")

    cache_client.update_item(
        TableName=tablename,
        Key=key,
        UpdateExpression="SET extraAttribute = :a",
        ExpressionAttributeValues={":a": {"N": "99"}}
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "NONE")

    cache_client.delete_item(
        TableName=tablename,
        Key=key
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "NONE")

def test_resource_get_item_BN(clients, table_bn):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_bn

    sk = time.time()
    item = {
        "pk": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81',
        "sk": Decimal(sk),
        "Attribute1": "extra"
    }
    key = {
        "pk": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81',
        "sk": Decimal(sk),
    }

    real_table = real_resource.Table(tablename)
    cache_table = cache_resource.Table(tablename)

    _resource_get_item_series(real_table, cache_table, key, "TOTAL")

    cache_table.put_item(Item=item)

    _resource_get_item_series(real_table, cache_table, key, "INDEXES")

    cache_table.update_item(
        Key=key,
        UpdateExpression="SET extraAttribute = :a",
        ExpressionAttributeValues={":a": 99}
    )

    _resource_get_item_series(real_table, cache_table, key, "NONE")

    cache_table.delete_item(
        Key=key
    )

    _resource_get_item_series(real_table, cache_table, key, "NONE")


def test_client_get_item_NB(clients, table_nb):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_nb

    sk = time.time() # a float
    item = {
        "pk": {"N": str(sk)},
        "sk": {"B": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'},
        "Attribute1": {"S": "extra"}
    }
    key = {
        "pk": {"N": str(sk)},
        "sk": {"B": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'}
    }

    _client_get_item_series(real_client, cache_client, tablename, key, "TOTAL")

    cache_client.put_item(
        TableName=tablename,
        Item=item
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "INDEXES")

def test_resource_get_item_NB(clients, table_nb):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_nb

    sk = time.time()
    item = {
        "pk": Decimal(sk),
        "sk": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81',
        "Attribute1": "extra"
    }
    key = {
        "pk": Decimal(sk),
        "sk": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'
    }

    real_table = real_resource.Table(tablename)
    cache_table = cache_resource.Table(tablename)

    _resource_get_item_series(real_table, cache_table, key, "TOTAL")

    cache_table.put_item(Item=item)

    _resource_get_item_series(real_table, cache_table, key, "INDEXES")


def test_client_get_item_S(clients, table_s):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_s

    sk = time.time() # a float
    item = {
        "pk": {"S": '\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'},
        "Attribute1": {"S": "extra"}
    }
    key = {
        "pk": {"S": '\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'}
    }

    _client_get_item_series(real_client, cache_client, tablename, key, "TOTAL")

    cache_client.put_item(
        TableName=tablename,
        Item=item
    )

    _client_get_item_series(real_client, cache_client, tablename, key, "INDEXES")


def test_resource_get_item_S(clients, table_s):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_s

    sk = time.time()
    item = {
        "pk": '\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81',
        "Attribute1": "extra"
    }
    key = {
        "pk": '\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'
    }

    real_table = real_resource.Table(tablename)
    cache_table = cache_resource.Table(tablename)

    _resource_get_item_series(real_table, cache_table, key, "TOTAL")

    cache_table.put_item(Item=item)

    _resource_get_item_series(real_table, cache_table, key, "INDEXES")

def test_mixed_get_item_BN(clients, table_bn):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_bn

    pk = b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'
    sk = Decimal(time.time()) # a float
    item = {
        "pk": {"B": pk},
        "sk": {"N": str(sk)},
        "Attribute1": {"S": "extra"}
    }
    key = {
        "pk": {"B": pk},
        "sk": {"N": str(sk)}
    }
    itemResource = {
        "pk": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81',
        "sk": sk,
        "Attribute1": "extra"
    }
    keyResource = {
        "pk": b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81',
        "sk": sk,
    }

    real_table = real_resource.Table(tablename)
    cache_table = cache_resource.Table(tablename)

    # Prime the pump by putting the item

    cache_table.put_item(Item=itemResource)

    # Now the test is that the next client call is a hit, that the caches are shared

    cache_client_response = cache_client.get_item(
        TableName=tablename,
        Key=key
    )
    cache_client_response = cache_client.get_item(
        TableName=tablename,
        Key=key
    )
    cache_resource_response = cache_table.get_item(
        Key=keyResource
    )
    cache_resource_response = cache_table.get_item(
        Key=keyResource
    )
    assert 'CacheMetadata' in cache_client_response
    assert 'CacheMetadata' in cache_resource_response

    # Now delete the item and make sure it invalidated both client and resource versions

    cache_table.delete_item(Key=keyResource)

    cache_client_response = cache_client.get_item(
        TableName=tablename,
        Key=key
    )
    cache_resource_response = cache_table.get_item(
        Key=keyResource
    )
    assert 'CacheMetadata' not in cache_resource_response
    assert 'CacheMetadata' not in cache_client_response


#   ----------------------------------------------------

def _compare_query_results(real_response, cache_miss_response, cache_hit_response):
    #print("real:", real_response)
    #print("miss:", cache_miss_response)
    #print("hit :", cache_hit_response)

    assert real_response['Count'] == cache_miss_response['Count'] == cache_hit_response['Count']
    assert real_response['Items'] == cache_miss_response['Items'] == cache_hit_response['Items']
    assert real_response['ScannedCount'] == cache_miss_response['ScannedCount'] == cache_hit_response['ScannedCount']
    if 'LastEvaluatedKey' in real_response:
        assert real_response['LastEvaluatedKey'] == cache_miss_response['LastEvaluatedKey'] == cache_hit_response['LastEvaluatedKey']

    if 'ConsumedCapacity' in real_response:
        assert real_response['ConsumedCapacity'] == cache_miss_response['ConsumedCapacity']
        assert real_response['ConsumedCapacity'] != cache_hit_response['ConsumedCapacity']
        assert real_response['ConsumedCapacity']["CapacityUnits"] == cache_miss_response["ConsumedCapacity"]["CapacityUnits"] > 0
        assert cache_hit_response["ConsumedCapacity"]["CapacityUnits"] == 0.0
        if 'Table' in real_response['ConsumedCapacity']:
            assert real_response['ConsumedCapacity']['Table'] == cache_miss_response['ConsumedCapacity']['Table']
            assert cache_hit_response["ConsumedCapacity"]["Table"]["CapacityUnits"] == 0.0
        if 'GlobalSecondaryIndexes' in real_response['ConsumedCapacity']:
            assert real_response['ConsumedCapacity']['GlobalSecondaryIndexes'] == cache_miss_response['ConsumedCapacity']['GlobalSecondaryIndexes']
            inx = cache_hit_response['ConsumedCapacity']['GlobalSecondaryIndexes']
            assert all(inx[key]["CapacityUnits"] == 0.0 for key in inx), "Not all hit GSI CapacityUnits are 0.0"
        if 'LocalSecondaryIndexes' in real_response['ConsumedCapacity']:
            assert real_response['ConsumedCapacity']['LocalSecondaryIndexes'] == cache_miss_response['ConsumedCapacity']['LocalSecondaryIndexes']
            inx = cache_hit_response['ConsumedCapacity']['LocalSecondaryIndexes']
            assert all(inx[key]["CapacityUnits"] == 0.0 for key in inx), "Not all hit LSI CapacityUnits are 0.0"


    assert 'ResponseMetadata' in real_response
    assert 'ResponseMetadata' in cache_miss_response
    assert 'ResponseMetadata' not in cache_hit_response
    assert 'CacheMetadata' not in real_response
    assert 'CacheMetadata' not in cache_miss_response
    assert 'CacheMetadata' in cache_hit_response
    assert 'CacheHit' in cache_hit_response['CacheMetadata']
    assert 'CachedTime' in cache_hit_response['CacheMetadata']
    assert 'Client' in cache_hit_response['CacheMetadata']


def _client_query_series(dynamodb_client, cache_client, core_values):
    real_response = dynamodb_client.query(
        **core_values
    )
    cache_miss_response = cache_client.query(
        **core_values
    )
    cache_hit_response = cache_client.query(
        **core_values
    )
    _compare_query_results(real_response, cache_miss_response, cache_hit_response)

    if 'LastEvaluatedKey' in real_response:
        lek = real_response['LastEvaluatedKey']
        real_response = dynamodb_client.query(
            **core_values,
            ExclusiveStartKey = lek
        )
        cache_miss_response = cache_client.query(
            **core_values,
            ExclusiveStartKey = lek
        )
        cache_hit_response = cache_client.query(
            **core_values,
            ExclusiveStartKey = lek
        )
        _compare_query_results(real_response, cache_miss_response, cache_hit_response)

def test_client_query_SS(clients, table_ss_loaded):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_ss_loaded

    core_values = dict(
        TableName = tablename,
        KeyConditionExpression = "pk = :value",
        ExpressionAttributeValues = {':value': {'S': 'queryme'}},
        ReturnConsumedCapacity = "INDEXES"
    )

    _client_query_series(real_client, cache_client, core_values)

    # Do with a GSI too
    core_values["IndexName"] = "GSI1"

    _client_query_series(real_client, cache_client, core_values)

    # Add more args
    core_values["Limit"] = 1
    core_values["ScanIndexForward"] = False

    _client_query_series(real_client, cache_client, core_values)

    # Add a projection
    core_values["ProjectionExpression"] = "pk"

    _client_query_series(real_client, cache_client, core_values)

    # Add a filter
    core_values["FilterExpression"] = "Attribute1 > :val"
    core_values["ExpressionAttributeValues"][":val"] = {"S": "A"} # syntax adds to the existing dict

    _client_query_series(real_client, cache_client, core_values)

#   ----------------------------------------------------


def _resource_query_series(real_table, cache_table, core_values):
    real_response = real_table.query(
        **core_values
    )
    cache_miss_response = cache_table.query(
        **core_values
    )
    cache_hit_response = cache_table.query(
        **core_values
    )
    _compare_query_results(real_response, cache_miss_response, cache_hit_response)

    if 'LastEvaluatedKey' in real_response:
        lek = real_response['LastEvaluatedKey']
        real_response = real_table.query(
            **core_values,
            ExclusiveStartKey = lek
        )
        cache_miss_response = cache_table.query(
            **core_values,
            ExclusiveStartKey = lek
        )
        cache_hit_response = cache_table.query(
            **core_values,
            ExclusiveStartKey = lek
        )
        _compare_query_results(real_response, cache_miss_response, cache_hit_response)

def test_resource_query_SS(clients, table_ss_loaded):
    real_client, cache_client, real_resource, cache_resource = clients
    real_table = real_resource.Table(table_ss_loaded)
    cache_table = cache_resource.Table(table_ss_loaded)

    core_values = dict(
        KeyConditionExpression=Key('pk').eq('queryme'),
        ReturnConsumedCapacity = "INDEXES"
    )

    _resource_query_series(real_table, cache_table, core_values)

    # Do with a GSI too
    core_values["IndexName"] = "GSI1"

    _resource_query_series(real_table, cache_table, core_values)

    # Add more args
    core_values["Limit"] = 1
    core_values["ScanIndexForward"] = False

    _resource_query_series(real_table, cache_table, core_values)

    # Add a projection
    core_values["ProjectionExpression"] = "pk"

    _resource_query_series(real_table, cache_table, core_values)

    # Add a filter
    core_values["FilterExpression"] = Attr("Attribute1").gt("A")

    _resource_query_series(real_table, cache_table, core_values)

    # Throw a wide variety of conditions that need to be correctly serialized
    core_values["FilterExpression"] = Attr('score').gt(50)
    _resource_query_series(real_table, cache_table, core_values)
    core_values["FilterExpression"] = Attr('date').lte('2022-12-31')
    _resource_query_series(real_table, cache_table, core_values)
    core_values["FilterExpression"] = Attr('age').between(20, 30)
    _resource_query_series(real_table, cache_table, core_values)
    core_values["FilterExpression"] = Attr('name').begins_with('J')
    _resource_query_series(real_table, cache_table, core_values)
    core_values["FilterExpression"] = Attr('description').contains('keyword')
    _resource_query_series(real_table, cache_table, core_values)
    core_values["FilterExpression"] = Attr('status').is_in(['new', 'in_progress', 'done'])
    _resource_query_series(real_table, cache_table, core_values)
    core_values["FilterExpression"] = Attr('x').ne('value')
    _resource_query_series(real_table, cache_table, core_values)
    core_values["FilterExpression"] = Attr('x').eq('value')
    _resource_query_series(real_table, cache_table, core_values)
    core_values["FilterExpression"] = Attr('isActive').eq(True)
    _resource_query_series(real_table, cache_table, core_values)
    core_values["FilterExpression"] = Attr('category').eq('books') & Attr('category').eq('electronics')
    _resource_query_series(real_table, cache_table, core_values)
    core_values["FilterExpression"] = Attr('category').eq('books') | Attr("Attribute1").gt("B")
    _resource_query_series(real_table, cache_table, core_values)


#   ----------------------------------------------------



# Scans have the purgatory system so it's two misses before the hit
def _compare_scan_results(real_response, cache_miss_response, cache_purgatory_response, cache_hit_response):
    #print("real:", real_response)
    #print("miss:", cache_miss_response)
    #print("purg:", cache_purgatory_response)
    #print("hit :", cache_hit_response)

    assert real_response['Count'] == cache_miss_response['Count'] == cache_purgatory_response['Count'] == cache_hit_response['Count']
    assert real_response['Items'] == cache_miss_response['Items'] == cache_purgatory_response['Items'] == cache_hit_response['Items']
    assert real_response['ScannedCount'] == cache_miss_response['ScannedCount'] == cache_purgatory_response['ScannedCount'] == cache_hit_response['ScannedCount']
    if 'LastEvaluatedKey' in real_response:
        assert real_response['LastEvaluatedKey'] == cache_miss_response['LastEvaluatedKey'] == cache_purgatory_response['LastEvaluatedKey'] == cache_hit_response['LastEvaluatedKey']

    if 'ConsumedCapacity' in real_response:
        assert real_response['ConsumedCapacity'] == cache_miss_response['ConsumedCapacity'] == cache_purgatory_response['ConsumedCapacity']
        assert real_response['ConsumedCapacity'] != cache_hit_response['ConsumedCapacity']
        assert real_response['ConsumedCapacity']["CapacityUnits"] == cache_miss_response["ConsumedCapacity"]["CapacityUnits"] == cache_purgatory_response["ConsumedCapacity"]["CapacityUnits"] > 0
        assert cache_hit_response["ConsumedCapacity"]["CapacityUnits"] == 0.0

    assert 'ResponseMetadata' in real_response
    assert 'ResponseMetadata' in cache_miss_response
    assert 'ResponseMetadata' in cache_purgatory_response
    assert 'ResponseMetadata' not in cache_hit_response
    assert 'CacheMetadata' not in real_response
    assert 'CacheMetadata' not in cache_miss_response
    assert 'CacheMetadata' not in cache_purgatory_response
    assert 'CacheMetadata' in cache_hit_response
    assert 'CacheHit' in cache_hit_response['CacheMetadata']
    assert 'CachedTime' in cache_hit_response['CacheMetadata']
    assert 'Client' in cache_hit_response['CacheMetadata']


def _client_scan_series(dynamodb_client, cache_client, core_values):
    real_response = dynamodb_client.scan(
        **core_values
    )
    cache_miss_response = cache_client.scan(
        **core_values
    )
    cache_purgatory_response = cache_client.scan(
        **core_values
    )
    cache_hit_response = cache_client.scan(
        **core_values
    )
    _compare_scan_results(real_response, cache_miss_response, cache_purgatory_response, cache_hit_response)

    if 'LastEvaluatedKey' in real_response:
        lek = real_response['LastEvaluatedKey']
        real_response = dynamodb_client.scan(
            **core_values,
            ExclusiveStartKey = lek
        )
        cache_miss_response = cache_client.scan(
            **core_values,
            ExclusiveStartKey = lek
        )
        cache_purgatory_response = cache_client.scan(
            **core_values,
            ExclusiveStartKey = lek
        )
        cache_hit_response = cache_client.scan(
            **core_values,
            ExclusiveStartKey = lek
        )
        _compare_scan_results(real_response, cache_miss_response, cache_purgatory_response, cache_hit_response)

def test_client_scan_SS(clients, table_ss_loaded):
    real_client, cache_client, real_resource, cache_resource = clients
    tablename = table_ss_loaded

    core_values = dict(
        TableName = tablename,
        ReturnConsumedCapacity = "INDEXES"
    )

    _client_scan_series(real_client, cache_client, core_values)

    # Do with a GSI too
    core_values["IndexName"] = "GSI1"

    _client_scan_series(real_client, cache_client, core_values)

    # Add more args
    core_values["Limit"] = 1

    _client_scan_series(real_client, cache_client, core_values)

    # Add a projection
    core_values["ProjectionExpression"] = "pk"

    _client_scan_series(real_client, cache_client, core_values) # this one is the one dying

    # Add a filter
    core_values["FilterExpression"] = "Attribute1 > :val"
    core_values["ExpressionAttributeValues"] = {':val': {"S": "A"}}

    _client_scan_series(real_client, cache_client, core_values)


def _resource_scan_series(real_table, cache_table, core_values):
    real_response = real_table.scan(
        **core_values
    )
    cache_miss_response = cache_table.scan(
        **core_values
    )
    cache_purgatory_response = cache_table.scan(
        **core_values
    )
    cache_hit_response = cache_table.scan(
        **core_values
    )
    _compare_scan_results(real_response, cache_miss_response, cache_purgatory_response, cache_hit_response)

    if 'LastEvaluatedKey' in real_response:
        lek = real_response['LastEvaluatedKey']
        real_response = real_table.scan(
            **core_values,
            ExclusiveStartKey = lek
        )
        cache_miss_response = cache_table.scan(
            **core_values,
            ExclusiveStartKey = lek
        )
        cache_purgatory_response = cache_table.scan(
            **core_values,
            ExclusiveStartKey = lek
        )
        cache_hit_response = cache_table.scan(
            **core_values,
            ExclusiveStartKey = lek
        )
        _compare_scan_results(real_response, cache_miss_response, cache_purgatory_response, cache_hit_response)



def test_resource_scan_SS(clients, table_ss_loaded):
    real_client, cache_client, real_resource, cache_resource = clients
    real_table = real_resource.Table(table_ss_loaded)
    cache_table = cache_resource.Table(table_ss_loaded)

    core_values = dict(
        ReturnConsumedCapacity = "INDEXES"
    )

    _resource_scan_series(real_table, cache_table, core_values)

    # Do with a GSI too
    core_values["IndexName"] = "GSI1"

    _resource_scan_series(real_table, cache_table, core_values)

    # Add more args
    core_values["Limit"] = 1

    _resource_scan_series(real_table, cache_table, core_values)

    # Add a projection
    core_values["ProjectionExpression"] = "pk"

    _resource_scan_series(real_table, cache_table, core_values)

    # Add a filter
    core_values["FilterExpression"] = Attr("Attribute1").gt("A")

    _resource_scan_series(real_table, cache_table, core_values)


def deep_compare(obj1, obj2):
    """
    Recursively compares two objects (dicts, lists, or primitive data types).
    Dicts are compared regardless of key order, lists are compared as sets if they contain unhashable types (like dicts).
    """
    if isinstance(obj1, dict) and isinstance(obj2, dict):
        if obj1.keys() != obj2.keys():
            return False
        return all(deep_compare(obj1[key], obj2[key]) for key in obj1)
    elif isinstance(obj1, list) and isinstance(obj2, list):
        if len(obj1) != len(obj2):
            return False
        # Convert lists to sets of tuples if they contain only hashable types
        try:
            return set(obj1) == set(obj2)
        except TypeError:  # Contains unhashable types, likely dicts
            return all(any(deep_compare(item1, item2) for item2 in obj2) for item1 in obj1)
    else:
        return obj1 == obj2




def test_client_batch_get_item_with_consumed(clients, table_ss_loaded, table_s_loaded, table_bn, table_nb):
    real_client, cache_client, real_resource, cache_resource = clients

    # Get items from three different types of tables
    # Include a mix of items that exist and ones that don't
    request_items = {
        table_ss_loaded: {
            'Keys': [
                {'pk': {'S': 'queryme'}, 'sk': {'S': 'x'}}, # present
                {'pk': {'S': 'queryme'}, 'sk': {'S': 'y'}}, # present
                {'pk': {'S': 'queryme'}, 'sk': {'S': 'z'}}  # absent
            ]
        },
        table_s_loaded: {
            'Keys': [
                {'pk': {'S': 'fox'}}, # absent
                {'pk': {'S': 'cat'}}, # present
                {'pk': {'S': 'dog'}}  # present
            ],
            'ProjectionExpression': "pk"
        },
        table_bn: {
            'Keys': [
                {'pk': {'B': b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'}, 'sk': {'N': str(time.time())}} # absent
            ],
            'ConsistentRead': True
        }
    }

    real_response = real_client.batch_get_item(
        RequestItems=request_items,
        ReturnConsumedCapacity="INDEXES"
    )

    cache_miss_response = cache_client.batch_get_item(
        RequestItems=request_items,
        ReturnConsumedCapacity="INDEXES"
    )

    cache_hit_response = cache_client.batch_get_item(
        RequestItems=request_items,
        ReturnConsumedCapacity="INDEXES"
    )

    assert 'ResponseMetadata' in real_response
    assert 'ResponseMetadata' not in cache_miss_response
    assert 'ResponseMetadata' not in cache_hit_response
    assert 'CacheMetadata' not in real_response
    assert 'CacheMetadata' in cache_miss_response
    assert 'CacheMetadata' in cache_hit_response

    assert 'Responses' in real_response
    assert 'Responses' in cache_miss_response
    assert 'Responses' in cache_hit_response
    assert deep_compare(real_response['Responses'], cache_miss_response['Responses'])
    assert deep_compare(real_response['Responses'], cache_hit_response['Responses'])

    assert deep_compare(real_response['UnprocessedKeys'], cache_miss_response['UnprocessedKeys'])
    assert deep_compare(real_response['UnprocessedKeys'], cache_hit_response['UnprocessedKeys'])
    assert deep_compare(real_response['ConsumedCapacity'], cache_miss_response['ConsumedCapacity'])
    assert deep_compare(cache_hit_response['ConsumedCapacity'], [{'TableName': 'CacheTestSS', 'CapacityUnits': 0.0, 'Table': {'CapacityUnits': 0.0}}, {'TableName': 'CacheTestS', 'CapacityUnits': 0.0, 'Table': {'CapacityUnits': 0.0}}, {'TableName': 'CacheTestBN', 'CapacityUnits': 1.0, 'Table': {'CapacityUnits': 1.0}}])

    assert cache_miss_response['CacheMetadata']['CacheMissCount'] == 6
    assert cache_miss_response['CacheMetadata']['CacheHitCount'] == 0
    assert cache_miss_response['CacheMetadata']['StronglyConsistentCount'] == 1
    assert cache_hit_response['CacheMetadata']['CacheMissCount'] == 0
    assert cache_hit_response['CacheMetadata']['CacheHitCount'] == 6
    assert cache_hit_response['CacheMetadata']['StronglyConsistentCount'] == 1


def test_client_batch_get_item_without_consumed(clients, table_ss_loaded, table_s_loaded, table_bn):
    real_client, cache_client, real_resource, cache_resource = clients

    # Same as above but for present ones add "2" so we don't get cache misses from the above run
    request_items = {
        table_ss_loaded: {
            'Keys': [
                {'pk': {'S': 'queryme2'}, 'sk': {'S': 'x'}}, # present
                {'pk': {'S': 'queryme2'}, 'sk': {'S': 'y'}}, # present
                {'pk': {'S': 'queryme2'}, 'sk': {'S': 'z'}}  # absent
            ]
        },
        table_s_loaded: {
            'Keys': [
                {'pk': {'S': 'fox2'}}, # absent
                {'pk': {'S': 'cat2'}}, # present
                {'pk': {'S': 'dog2'}}  # present
            ],
            'ProjectionExpression': "pk"
        },
        table_bn: {
            'Keys': [
                {'pk': {'B': b'\x48\x65\x6c\x6c\x6f\x00\x01\x02\xfe\xff\x48\x69\x20\xF0\x9F\x98\x81'}, 'sk': {'N': str(time.time())}} # absent
            ],
            'ConsistentRead': True
        }
    }

    real_response = real_client.batch_get_item(
        RequestItems=request_items
    )

    cache_miss_response = cache_client.batch_get_item(
        RequestItems=request_items
    )

    cache_hit_response = cache_client.batch_get_item(
        RequestItems=request_items
    )

    assert 'ResponseMetadata' in real_response
    assert 'ResponseMetadata' not in cache_miss_response
    assert 'ResponseMetadata' not in cache_hit_response
    assert 'CacheMetadata' not in real_response
    assert 'CacheMetadata' in cache_miss_response
    assert 'CacheMetadata' in cache_hit_response

    assert 'Responses' in real_response
    assert 'Responses' in cache_miss_response
    assert 'Responses' in cache_hit_response
    assert deep_compare(real_response['Responses'], cache_miss_response['Responses'])
    assert deep_compare(real_response['Responses'], cache_hit_response['Responses'])

    assert deep_compare(real_response['UnprocessedKeys'], cache_miss_response['UnprocessedKeys'])
    assert deep_compare(real_response['UnprocessedKeys'], cache_hit_response['UnprocessedKeys'])
    assert 'ConsumedCapacity' not in real_response
    assert 'ConsumedCapacity' not in cache_miss_response
    assert 'ConsumedCapacity' not in cache_hit_response

    assert cache_miss_response['CacheMetadata']['CacheMissCount'] == 6
    assert cache_miss_response['CacheMetadata']['CacheHitCount'] == 0
    assert cache_miss_response['CacheMetadata']['StronglyConsistentCount'] == 1
    assert cache_hit_response['CacheMetadata']['CacheMissCount'] == 0
    assert cache_hit_response['CacheMetadata']['CacheHitCount'] == 6
    assert cache_hit_response['CacheMetadata']['StronglyConsistentCount'] == 1
