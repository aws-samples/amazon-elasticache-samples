import boto3
from redis import Redis
import hashlib
import base64
from typing import List
import logging
from datetime import datetime
import time
import botocore.exceptions
from decimal import Decimal
from boto3.dynamodb.types import Binary
import copy
import json

class CacheClient:

    @classmethod
    def _custom_encoder(cls, obj):
        if isinstance(obj, bytes):
            # Use 'B' for plain bytes
            return {'__t__': 'B', 'd': base64.b64encode(obj).decode('ascii')}
        elif isinstance(obj, Binary):
            # Use 'BIN' for Binary instances
            return {'__t__': 'BIN', 'd': base64.b64encode(obj.value).decode('ascii')}
        elif isinstance(obj, Decimal):
            return {'__t__': 'N', 'd': str(obj)}
        elif isinstance(obj, set):
            if not obj:  # DynamoDB doesn't support empty sets, but let's handle it generically
                return {'__t__': 'EmptySet', 'd': []}
            sample_element = next(iter(obj))
            # Repeat the checks for bytes and Binary within sets
            if isinstance(sample_element, str):
                return {'__t__': 'SS', 'd': list(obj)}
            elif isinstance(sample_element, (int, float, Decimal)):
                return {'__t__': 'NS', 'd': [str(item) for item in obj]}
            elif isinstance(sample_element, bytes):
                return {'__t__': 'BS', 'd': [base64.b64encode(item).decode('ascii') for item in obj]}
            elif isinstance(sample_element, Binary):
                return {'__t__': 'BSBIN', 'd': [base64.b64encode(item.value).decode('ascii') for item in obj]}
            else:
                raise TypeError(f"Set contains unsupported type for DynamoDB: {type(sample_element).__name__}")

        # The following helps us get a string version of calls like KeyConditionExpression=Key('pk').eq('X')
        # for cache key calculation. It depends on ConditionExpressionBuilder.
        # https://stackoverflow.com/questions/59216070/convert-boto3-basecondition-to-its-string-representation
        elif (isinstance(obj, (
                               boto3.dynamodb.conditions.AttributeType,
                               boto3.dynamodb.conditions.BeginsWith,
                               boto3.dynamodb.conditions.Between,
                               boto3.dynamodb.conditions.Contains,
                               boto3.dynamodb.conditions.Equals, # eq
                               boto3.dynamodb.conditions.AttributeExists, # exists
                               boto3.dynamodb.conditions.GreaterThan, # gt
                               boto3.dynamodb.conditions.GreaterThanEquals, # gte
                               boto3.dynamodb.conditions.In, # is_in
                               boto3.dynamodb.conditions.LessThan, # lt
                               boto3.dynamodb.conditions.LessThanEquals, # lte
                               boto3.dynamodb.conditions.NotEquals, # ne
                               boto3.dynamodb.conditions.AttributeNotExists, # not_exists()
                               boto3.dynamodb.conditions.Size,
                               boto3.dynamodb.conditions.ComparisonCondition, # needed?
                               boto3.dynamodb.conditions.And,
                               boto3.dynamodb.conditions.Or,
                               boto3.dynamodb.conditions.Not,
                               boto3.dynamodb.conditions.Key
                               ))):
            return boto3.dynamodb.conditions.ConditionExpressionBuilder().build_expression(obj)
        raise TypeError(f'Object of type {obj.__class__.__name__} is not JSON serializable')

    @classmethod
    def _custom_decoder(cls, obj):
        if '__t__' in obj:
            type_ = obj['__t__']
            data = obj['d']
            if type_ == 'B':
                return base64.b64decode(data)
            elif type_ == 'BIN':
                # Decode and wrap in Binary for 'BIN'
                return Binary(base64.b64decode(data))
            elif type_ == 'N':
                return Decimal(data)
            elif type_ == 'SS':
                return set(data)
            elif type_ == 'NS':
                return set(Decimal(item) for item in data)
            elif type_ == 'BS':
                return set(base64.b64decode(item) for item in data)
            elif type_ == 'BSBIN':
                # Decode each item and wrap in Binary for 'BSBIN'
                return set(Binary(base64.b64decode(item)) for item in data)
        return obj

    @classmethod
    def _json_dumps(cls, obj):
        return json.dumps(obj, default=cls._custom_encoder)

    @classmethod
    def _json_loads(cls, obj):
        return json.loads(obj, object_hook=cls._custom_decoder)

    @classmethod
    def _convert_bytes_to_str(cls, bytes_obj):
        return bytes_obj.decode('latin-1').encode('unicode-escape').decode('ascii')

    # If Redis ever generates an error, we want to continue using the database without the cache
    # This class wraps the Redis client to catch exceptions and log them, returning None on failure
    class UnfailingRedis:
        def __init__(self, redis):
            self.redis = redis
            self.logger = logger = logging.getLogger(__name__)

        def __getattr__(self, name):
            method = getattr(self.redis, name)

            def wrapper(*args, **kwargs):
                try:
                    # Try calling the method with given arguments and keyword arguments
                    return method(*args, **kwargs)
                except Exception as e:
                    # If any exception occurs, log it and return None
                    self.logger.error(f"Redis operation failed: {e}")
                    return None

            return wrapper

    # Create a class-level logger
    logger = logging.getLogger(__name__)
    logger.debug("This is a debug message")
    logger.info("This is a info message")
    logger.warning("This is a warning message")

    def __init__(self, dynamodb_client, redis_client, ttl=60*60, ttl_config=None, namespace="CC"):
        """
        Initializes the class with DynamoDB and Redis clients, optional TTL configurations, and an optional namespace.

        :param dynamodb_client: The DynamoDB client instance
        :param redis_client: The Redis client instance
        :param ttl: The default TTL for cache entries, in seconds, defaults to 3600 (one hour)
        :param ttl_configs: A dictionary of TTL configurations for fine-grained control:
            - 'item': TTL for the item cache
            - 'query': TTL for the query cache
            - 'scan': TTL for the scan cache
            - 'item_negative': TTL for the item cache, when the item is absent in the database
        :param namespace: An optional namespace for keys.
        """

        self.dynamodb_client = dynamodb_client
        self.ttl = ttl
        self.namespace = namespace
        self.schema_cache = {}
        self.__version__='0.0.1' # formalize this somehow later

        # Validate we received only understandable TTL keys
        if not isinstance(ttl, int) or ttl <= 0:
            raise ValueError(f"Default ttl must be a positive integer: {ttl}")

        valid_ttl_keys = {'item', 'query', 'scan', 'item_negative'}
        if ttl_config:
            unknown_keys = set(ttl_config.keys()) - valid_ttl_keys
            if unknown_keys:
                raise ValueError(f"Unknown ttl_config keys: {unknown_keys}")

        self.ttl_item = ttl_config.get('item', ttl) if ttl_config else ttl
        self.ttl_query = ttl_config.get('query', ttl) if ttl_config else ttl
        self.ttl_scan = ttl_config.get('scan', ttl) if ttl_config else ttl
        self.ttl_item_negative = ttl_config.get('item_negative', ttl) if ttl_config else ttl
        for attr in ['ttl_item', 'ttl_query', 'ttl_scan', 'ttl_item_negative']:
            if not isinstance(getattr(self, attr), int) or getattr(self, attr) <= 0:
                raise ValueError(f"{attr} must be a positive integer")

        # Check if the Redis client is connected, so we can fail fast if required
        try:
            redis_client.ping()
        except Exception as e:
            raise Exception(f"Could not connect to Redis {redis_client}") from e
        # We confirmed Redis is live now, but if it dies later under use, we don't want to pop exceptions
        self.redis_client = self.UnfailingRedis(redis_client)

        # Adjust the user-agent to append the fact we're using this caching client
        # Boto3 has a user_agent_extra feature but by the time the client is created, it's too late to set it
        def custom_user_agent_modifier(request, **kwargs):
            # The user-agent comes as bytes, so we need to decode it to a string, then later to be polite encode it back
            original_user_agent = (request.headers['User-Agent'].decode('utf-8') if isinstance(request.headers['User-Agent'], bytes) else request.headers['User-Agent'])
            new_user_agent = original_user_agent + ' CacheClient-Python/' + self.__version__
            request.headers['User-Agent'] = new_user_agent.encode('utf-8')
        self.dynamodb_client.meta.events.register('before-send.*.*', custom_user_agent_modifier)

    def __getattr__(self, name):
        # Forward any attribute or method call to the DynamoDB client
        if hasattr(self.dynamodb_client, name):
            return getattr(self.dynamodb_client, name)
        else:
            raise AttributeError(f"'CacheClient' object has no attribute '{name}'")

    # Returns a 64 character string hash based on the passed-in string array
    def _compute_hash(self, *values):
        separator = "###"
        input_string = separator.join([self.__class__._json_dumps(value) if isinstance(value, dict) else str(value) for value in values])

        # Calculate the SHA-256 hash of the input string
        hash_object = hashlib.sha256(input_string.encode())

        # hash_base64 = base64.b64encode(hash_object.digest()).decode()

        # Get the hexadecimal representation of the hash
        return hash_object.hexdigest()

    def _compute_hash_of_keys(self, kwargs, keys_to_include):
        # Hash using the specified keys in kwargs
        filtered_kwargs = {key: kwargs[key] for key in keys_to_include if key in kwargs}
        return self._compute_hash(filtered_kwargs)

    def _is_operation_successful(self, response):
        return response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200

    def _get_primary_key_names_using_describe_table(self, table_name) -> List[str]:
        # Use describe_table to get the table schema
        self.logger.debug(f"Making a describe_table call for table {table_name}")
        response = self.dynamodb_client.describe_table(TableName=table_name)

        # Extract primary key attribute names and types from the table schema
        table_description = response.get("Table")
        key_schema = table_description.get("KeySchema")

        hash_key = None
        range_key = ""

        for key in key_schema:
            if key["KeyType"] == "HASH":
                hash_key = key["AttributeName"]
            elif key["KeyType"] == "RANGE":
                range_key = key["AttributeName"]


        return hash_key, range_key

    def _get_primary_key_names(self, table_name) -> List[str]:
        # Do we have a copy in our SDK-side cache?
        if table_name in self.schema_cache:
            return self.schema_cache[table_name].split(".")

        # We don't, so let's check if Redis already knows
        # We store schemas in Redis and the value is separated with a dot like "pk.sk"
        key = f"{self.namespace}:PKNAMES#table:{table_name}"
        names = self.redis_client.get(key)
        if names:
            self.schema_cache[table_name] = names
            return names.split(".")

        names = self._get_primary_key_names_using_describe_table(table_name)

        # Cache the result
        self.redis_client.set(key, ".".join(names), ex=24*60*60) # daily
        self.schema_cache[table_name] = ".".join(names)
        return names

    def _get_item_identifier(self, table_name, item):
        names = self._get_primary_key_names(table_name)
        if len(names) == 2:
            pk = item.get(names[0])
            sk = item.get(names[1])
        elif len(names) == 1:
            pk = item.get(names[0])
            sk = ""

        # The pk and sk may be dicts, and we want to focus on the value
        if (isinstance(pk, dict)):
            pk = next(iter(pk.values()))
        if (isinstance(sk, dict)):
            sk = next(iter(sk.values()))

        pkvalue = pk if isinstance(pk, str) \
            else str(pk) if isinstance(pk, (int, float, Decimal)) \
            else self.__class__._convert_bytes_to_str(pk.value) if isinstance(pk, Binary) \
            else self.__class__._convert_bytes_to_str(pk) if isinstance(pk, bytes) \
            else next(iter(pk.values())) # shouldn't happen

        skvalue = "" if sk is None \
            else sk if isinstance(sk, str) \
            else str(sk) if isinstance(sk, (int, float, Decimal)) \
            else self.__class__._convert_bytes_to_str(sk.value) if isinstance(sk, Binary) \
            else self.__class__._convert_bytes_to_str(sk) if isinstance(sk, bytes) \
            else next(iter(sk.values())) # shouldn't happen

        pk = str(pkvalue)
        sk = str(skvalue)

        return f"table:{table_name}:pk:{pk}:sk:{sk}"

    # Takes an Item or Key
    def _invalidate(self, table_name, item):
        summary = self._get_item_identifier(table_name, item)

        lookup = f"{self.namespace}:INVALIDATION:{summary}"

        # Get the set of keys to invalidate
        self.logger.debug(f"Fetching invalidation keys from Redis for key {lookup}")
        invalidation_keys = self.redis_client.smembers(lookup)
        self.logger.debug(f"Gathered {len(invalidation_keys)} invalidation keys from Redis")

        # Iterate through the set and delete each key and remove the key from the set
        # This design should limit the chance for race conditions if others are doing gets
        for invalidation_key in invalidation_keys:
            self.redis_client.delete(invalidation_key)
            self.redis_client.srem(lookup, invalidation_key)

    def _adjust_consumed_capacity(self, response):
        # Scan of a GSI with ReturnConsumedCapacity=TOTAL
        #   'ConsumedCapacity': {'TableName': 'CacheTest', 'CapacityUnits': 2.0}
        # Scan of a GSI with ReturnConsumedCapacity=INDEXES
        #   'ConsumedCapacity': { 'TableName': 'CacheTest', 'CapacityUnits': 2.0, 'Table': {'CapacityUnits': 0.0}, 'GlobalSecondaryIndexes': {'gsi1': {'CapacityUnits': 2.0}}}

        # Adjust the ConsumedCapacity to reflect the cache hit by zeroing the costs
        if "ConsumedCapacity" in response:
            original = copy.deepcopy(response["ConsumedCapacity"])
            self.logger.debug(f"Returned consumed capacity {response['ConsumedCapacity']}")
            response["ConsumedCapacity"]["CapacityUnits"] = 0.0
            if "Table" in response["ConsumedCapacity"]:
                response["ConsumedCapacity"]["Table"]["CapacityUnits"] = 0.0
            if "LocalSecondaryIndexes" in response["ConsumedCapacity"]:
                for index in response["ConsumedCapacity"]["LocalSecondaryIndexes"]:
                    response["ConsumedCapacity"]["LocalSecondaryIndexes"][index]["CapacityUnits"] = 0.0
            if "GlobalSecondaryIndexes" in response["ConsumedCapacity"]:
                for index in response["ConsumedCapacity"]["GlobalSecondaryIndexes"]:
                    response["ConsumedCapacity"]["GlobalSecondaryIndexes"][index]["CapacityUnits"] = 0.0
            return original
        else:
            return None

    def _unadjust_consumed_capacity(self, response, original):
        if original:
            response["ConsumedCapacity"] = original

    # Cached respones should skip the ResponseMetadata, and substitute a CacheMetadata
    def _add_cache_metadata_and_remove_response_metadata(self, response):
        response["CacheMetadata"] = {
            "CacheHit": True,
            "CachedTime": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%SZ'),
            "Client": f"Python {self.__version__}"
        }
        response.pop("ResponseMetadata")
        return response

    def _perform_dynamodb_mutation_operation(self, operation, **kwargs):
        # Make the database call, let exceptions propagate
        response = operation(**kwargs)

        # Check if the operation failed
        if not self._is_operation_successful(response):
            return response

        # Extract table name and key
        table_name = kwargs.get("TableName")
        key = kwargs.get("Key") if "Key" in kwargs else kwargs.get("Item")

        # Invalidate any cache entries
        self._invalidate(table_name, key)

        return response

    # We need to cache low-level and high-level responses differently because
    # they're formatted differently.  Both calls go through our same code. So
    # for example we might get a no-arg scan() call and need to know if we
    # should return a low-level or high-level version of the cached data. Both
    # layers call our same scan code but the high-level Resource does parameter
    # and output massaging via event hooks. The goal then is to determine if
    # we're being used by low-level or high-level client and cache differently.  

    # This can be determined by peeking at the user_agent_extra metadata. The
    # "Resource" client always adds "Resource" there. Different clients might
    # in the future use other strings.

    # Here instead of hard-coding a dependency on the string "Resource", we
    # take whatever UA extra string we get and use that as part of the cache
    # lookup key so different UAE's will result in different cache entries.

    # Important: For item cache invalidation tracking we don't differentiate!
    # Invalidations go low-level and high-level both.

    def _smart_prefix(self, prefix):
        user_agent_extra = getattr(getattr(self.dynamodb_client.meta, '_client_config', None), 'user_agent_extra', None)
        if user_agent_extra:
            return f"{prefix}:{user_agent_extra}"
        else:
            return prefix

    def put_item(self, **kwargs):
        return self._perform_dynamodb_mutation_operation(self.dynamodb_client.put_item, **kwargs)

    def update_item(self, **kwargs):
        return self._perform_dynamodb_mutation_operation(self.dynamodb_client.update_item, **kwargs)

    def delete_item(self, **kwargs):
        return self._perform_dynamodb_mutation_operation(self.dynamodb_client.delete_item, **kwargs)

    def batch_write_item(self, **kwargs):
        # Make the database call, let exceptions propagate
        response = self.dynamodb_client.batch_write_item(**kwargs)
        if not self._is_operation_successful(response):
            return response

        unprocessed = response.get("UnprocessedItems", {})
        items = kwargs.get("RequestItems")

        for table_name, requests in items.items():
            for request in requests:
                if "PutRequest" in request:
                    item = request["PutRequest"]["Item"]
                elif "DeleteRequest" in request:
                    item = request["DeleteRequest"]["Key"]
                else:
                    raise Exception("Invalid batch write request")

                # Check if the item is in the UnprocessedItems
                if table_name in unprocessed and request in unprocessed[table_name]:
                    self.logger.debug(f"Skipping invalidation for unprocessed item {item}")
                    continue  # Skip invalidation for unprocessed items

                # Invalidate any cache entries
                self._invalidate(table_name, item)

        return response

    def transact_write_items(self, **kwargs):
        # Make the database call, let exceptions propagate
        response = self.dynamodb_client.transact_write_items(**kwargs)
        if not self._is_operation_successful(response):
            return response

        items = kwargs.get("TransactItems")

        for request in items:
            if "Put" in request:
                item = request["Put"]["Item"]
                table_name = request["Put"]["TableName"]
            elif "Delete" in request:
                item = request["Delete"]["Key"]
                table_name = request["Delete"]["TableName"]
            elif "Update" in request:
                item = request["Update"]["Key"]
                table_name = request["Update"]["TableName"]
            elif "ConditionCheck" in request:
                continue
            else:
                raise Exception("Invalid transaction write request")

            # Invalidate any cache entries
            self._invalidate(table_name, item)

        return response

    def get_item(self, **kwargs):
        # If this is a SC read, hit the database directly, cache isn't involved, let exceptions propagate
        if kwargs.get("ConsistentRead") == True:
            return self.dynamodb_client.get_item(**kwargs)

        # Get a summary of the item (table name + primary keys)
        table_name = kwargs.get("TableName")
        key = kwargs.get("Key")

        self.logger.debug(f"get_item: table_name {table_name} and key {key}")

        summary = self._get_item_identifier(table_name, key)

        # Then get a hash unique to the particular representation of this get
        hash = self._compute_hash_of_keys(kwargs, [
            "AttributesToGet",
            "ProjectionExpression",
            "ExpressionAttributeNames"
        ])

        # The item cache lookup key is a combination of the summary and the hash
        lookup = f"{self.namespace}:{self._smart_prefix('ITEM')}:{summary}:{hash}"

        self.logger.debug(f"get_item: fetching item cache entry from Redis for key {lookup}")
        lookupResponse = self.redis_client.get(lookup)

        # Note on negative caching:
        # A request for a missing item still gets a happy cacheable response, just no Item inside, so cache it

        if lookupResponse:
            val = self.__class__._json_loads(lookupResponse)
            self.logger.debug(f"get_item: cache hit for key {lookup}: {val}")
            return val

        self.logger.debug(f"get_item: cache miss for key {lookup}")

        # Print the kwargs to see what's being passed in
        response = self.dynamodb_client.get_item(**kwargs)
        self.logger.debug(f"get_item client {self.dynamodb_client} and kwargs {kwargs} returned {response}")

        # Skip all caching if unsuccessful
        if not self._is_operation_successful(response):
            self.logger.debug(f"get_item: Unsuccessful get_item call {response}")
            return response

        # Cache the successful response, and record that it's a hit and the time we're saving it
        cachedResponse = response.copy() # shallow copy
        self._add_cache_metadata_and_remove_response_metadata(cachedResponse)
        original = self._adjust_consumed_capacity(cachedResponse)
        dumped = self.__class__._json_dumps(cachedResponse)#, cls=DecimalEncoder)
        self._unadjust_consumed_capacity(response, original)
        # Figure out if this is a positive or negative cache and adjust TTL accordingly
        has_item_key = "Item" in cachedResponse
        use_ttl = self.ttl_item if has_item_key else self.ttl_item_negative
        self.logger.debug(f"get_item: set cache {lookup}: {dumped} for {use_ttl} seconds")
        self.redis_client.set(lookup, dumped, px=use_ttl*1000)

        # Note the entry in the invalidation list
        invalidation_key = f"{self.namespace}:INVALIDATION:{summary}"
        self.logger.debug(f"get_item: tracking for later invalidation {invalidation_key}: {lookup}")
        self.redis_client.sadd(invalidation_key, lookup)

        return response

    # Notes:
    # - The SC/EC is dictated per table, as are other things like ProjectionExpression
    # - The ReturnConsumedCapacity is on the overall request
    # - Logic here loops get_item calls and build up a response, letting those being read-through cache calls
    # - One could potentially improve this by multi-threading the calls to make them parallel
    # - The ConsumedCapacity is built by summing up the underlying get_item calls
    # - The CacheMetadata includes the count of cache hits, misses, and uncacheable strongly consistent reads
    def batch_get_item(self, **kwargs):

        doingConsumedCapacity = "ReturnConsumedCapacity" in kwargs and kwargs.get("ReturnConsumedCapacity") != "NONE"

        # Build a synthetic response
        constructedResponse = {
            "Responses": {},
            "UnprocessedKeys": {}
        }
        if doingConsumedCapacity:
            constructedResponse["ConsumedCapacity"] = []

        # Track consumed capacity per table in a hash
        capacityTracker = {}
        cacheHitTracker = 0
        cacheMissTracker = 0
        cacheStronglyConsistentTracker = 0

        items = kwargs.get("RequestItems")


        # Loop over each table / key combo and make a get_item call.
        # This call either quickly returns a cached value or makes a db call and caches the result for later.
        # Then glue all responses together into a singular response object, with UnprocessedItems for any problems
        for table_name, spec in items.items():
            for key in spec["Keys"]:
                localSpec = spec.copy()
                localSpec.pop("Keys")
                # Add the table name and keys to the localSpec
                localSpec["TableName"] = table_name
                localSpec["Key"] = key
                if "ReturnConsumedCapacity" in kwargs:
                    localSpec["ReturnConsumedCapacity"] = kwargs.get("ReturnConsumedCapacity")
                try:
                    response = self.get_item(**localSpec)
                    if table_name not in constructedResponse["Responses"]:
                        constructedResponse["Responses"][table_name] = []
                    if ("Item" in response):
                        constructedResponse["Responses"][table_name].append(response.get("Item")) # real call puts [] if missing
                    if doingConsumedCapacity and 'ConsumedCapacity' in response:
                        # Track the consumed capacity, note https://github.com/aws/aws-sdk-java/issues/1986 is better than docs at what you get
                        consumed = response.get("ConsumedCapacity")
                        consumedTable = consumed.get("TableName")
                        consumedValue = consumed.get("CapacityUnits")
                        if consumedTable and consumedValue is not None:
                            if consumedTable not in capacityTracker:
                                capacityTracker[consumedTable] = 0
                            capacityTracker[consumedTable] += consumedValue
                    # Track cache hit ratios
                    if "CacheMetadata" in response:
                        cacheHitTracker += 1
                    elif "ConsistentRead" in localSpec and localSpec["ConsistentRead"] == True:
                        cacheStronglyConsistentTracker += 1
                    else:
                        cacheMissTracker += 1

                except self.dynamodb_client.exceptions.ResourceNotFoundException as e:
                    raise # The real call errors out on RNFE with a batch call, so we should too
                except Exception as e:
                    self.logger.error(f"Error getting item {key} from table {table_name}: {e}")
                    if table_name not in constructedResponse["UnprocessedKeys"]:
                        constructedResponse["UnprocessedKeys"][table_name] = {'Keys': [key]}
                        localSpec.pop("TableName")
                        localSpec.pop("Key")
                        # Add localSpec to the UnprocessedKeys
                        constructedResponse["UnprocessedKeys"][table_name].update(localSpec) # only once for however many keys
                    else:
                        constructedResponse["UnprocessedKeys"][table_name]['Keys'].append(key)

        # Build out ConsumedCapacity
        # Here's a sample of the format we need to copy:
        # [{'TableName': 'CacheTestBN', 'CapacityUnits': 0.5, 'Table': {'CapacityUnits': 0.5}}, 
        #  {'TableName': 'CacheTestS', 'CapacityUnits': 1.5, 'Table': {'CapacityUnits': 1.5}},
        #  {'TableName': 'CacheTestSS', 'CapacityUnits': 1.5, 'Table': {'CapacityUnits': 1.5}}]
        if doingConsumedCapacity:
            for table, value in capacityTracker.items():
                constructedResponse["ConsumedCapacity"].append({
                    "TableName": table,
                    "CapacityUnits": value,
                    "Table": {"CapacityUnits": value}
                })

        # Build out CacheMetadata
        constructedResponse["CacheMetadata"] = {
            "CacheHitCount": cacheHitTracker,
            "CacheMissCount": cacheMissTracker,
            "StronglyConsistentCount": cacheStronglyConsistentTracker,
            "Time": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%SZ'),
            "Client": f"Python {self.__version__}"
        }

        return constructedResponse

    def query(self, **kwargs):
        # If this is a SC read, hit the database directly, cache isn't involved
        if kwargs.get("ConsistentRead") == True:
            return self.dynamodb_client.query(**kwargs)

        # These are the query args we'll use to compute the unique hash of the request
        hash = self._compute_hash_of_keys(kwargs, [
            "TableName",
            "IndexName",
            "Select",
            "AttributesToGet",
            "Limit",
            "KeyConditions",
            "QueryFilter",
            "ConditionalOperator",
            "ScanIndexForward",
            "ExclusiveStartKey",
            "ProjectionExpression",
            "FilterExpression",
            "KeyConditionExpression",
            "ExpressionAttributeNames",
            "ExpressionAttributeValues"
        ])

        # The item cache lookup key (could later add the table/index name to be human friendly)
        lookup = f"{self.namespace}:{self._smart_prefix('QUERY')}:{hash}"

        self.logger.debug(f"query: fetching query cache entry from Redis for key {lookup}")
        lookupResponse = self.redis_client.get(lookup)

        if lookupResponse:
            val = self.__class__._json_loads(lookupResponse)
            self.logger.debug(f"query: cache hit for key {lookup}: {val}")
            return val

        self.logger.debug(f"query: cache miss for key {lookup}")

        # Make the database call, let exceptions propagate
        response = self.dynamodb_client.query(**kwargs)

        # Skip all caching if unsuccessful
        if not self._is_operation_successful(response):
            self.logger.debug(f"query: Unsuccessful query call {response}")
            return response

        # Cache the successful response, and record that it's a hit and the time we're saving it
        cachedResponse = response.copy()
        self._add_cache_metadata_and_remove_response_metadata(cachedResponse)
        original = self._adjust_consumed_capacity(cachedResponse)
        dumped = self.__class__._json_dumps(cachedResponse)#, cls=DecimalEncoder)
        self._unadjust_consumed_capacity(response, original)
        self.logger.debug(f"query: set cache {lookup}: {dumped} for {self.ttl_query} seconds")
        self.redis_client.set(lookup, dumped, px=self.ttl_query*1000)

        return response

    def scan(self, **kwargs):

        # If this is a SC read, hit the database directly, cache isn't involved
        if kwargs.get("ConsistentRead") == True:
            return self.dynamodb_client.scan(**kwargs)

        # These are the query args we'll use to compute the unique hash of the request
        hash = self._compute_hash_of_keys(kwargs, [
            "TableName",
            "IndexName",
            "AttributesToGet",
            "Limit",
            "Select",
            "ScanFilter",
            "KeyConditions",
            "ConditionalOperator",
            "ExclusiveStartKey",
            "TotalSegments",
            "Segments",
            "ProjectionExpression",
            "FilterExpression",
            "KeyConditionExpression",
            "ExpressionAttributeNames",
            "ExpressionAttributeValues"
        ])

        # The item cache lookup key (could later add the table/index name to be human friendly)
        lookup = f"{self.namespace}:{self._smart_prefix('SCAN')}:{hash}"

        #self.logger.debug(f"scan: fetching scan cache entry from Redis for key {lookup}")
        lookupResponse = self.redis_client.get(lookup)

        # Check for a cache hit of a full scan response (a digit means a purgatory response)
        if lookupResponse and not lookupResponse.isdigit():
            val = self.__class__._json_loads(lookupResponse)
            self.logger.debug(f"scan: cache hit for key {lookup}: {val}")
            return val

        # Check for a cache hit of a purgatory value
        purgatorySatisfied = False
        if lookupResponse and lookupResponse.isdigit():
            val = int(lookupResponse)
            self.logger.debug(f"scan: cache hit (but purgatory) for key {lookup}: {val}")
            purgatorySatisfied = True
        else:
            self.logger.debug(f"scan: cache miss for key {lookup}")

        response = self.dynamodb_client.scan(**kwargs)

        # Skip all caching if unsuccessful
        if not self._is_operation_successful(response):
            self.logger.debug(f"scan: Unsuccessful scan call {response}")
            return response

        if purgatorySatisfied:
            # Cache the successful response, and record that it's a hit and the time we're saving it
            cachedResponse = response.copy()
            self._add_cache_metadata_and_remove_response_metadata(cachedResponse)
            original = self._adjust_consumed_capacity(cachedResponse)
            dumped = self.__class__._json_dumps(cachedResponse)#, cls=DecimalEncoder)
            self._unadjust_consumed_capacity(response, original)
            self.logger.debug(f"scan: set cache entry {lookup}: {dumped} for {self.ttl_scan} seconds")
            self.redis_client.set(lookup, dumped, px=self.ttl_scan*1000)
        else:
            # Cache the purgatory value
            self.logger.debug(f"scan: set cache as purgatory {lookup}: {self.ttl_scan} seconds")
            self.redis_client.set(lookup, 1, px=self.ttl_scan*1000)

        return response
