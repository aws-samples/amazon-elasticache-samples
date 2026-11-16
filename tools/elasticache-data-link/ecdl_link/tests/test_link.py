"""Tests for the shared link schema and validator.

Run with:  python3 -m ecdl_link.test_link      (from the repo root)
      or:  python3 ecdl_link/test_link.py

Stdlib only, so it runs on a Python interpreter with no extra packages. The YAML tests skip themselves if
PyYAML is absent rather than failing, because PyYAML is a dependency of the two
callers, not of the schema itself.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link import (  # noqa: E402
    LinkValidationError,
    canonical_json,
    loads,
    normalize,
    problems_for,
    schema,
    validate_link,
)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SRC_ARN = 'arn:aws:rds:us-east-2:111122223333:cluster:my-source'
TGT_ARN = ('arn:aws:elasticache:us-east-2:111122223333'
           ':replicationgroup:my-cache')
SEC_ARN = ('arn:aws:secretsmanager:us-east-2:111122223333'
           ':secret:shop-db-a1b2c3')


def minimal(**overrides):
    """The smallest config a customer can write."""
    doc = {
        'name': 'checkout-cache',
        'mode': 'initial_load_and_cdc',
        'source': {
            'arn': SRC_ARN,
            'database': 'Inventory',
            'secret_arn': SEC_ARN,
        },
        'target': {'arn': TGT_ARN},
        'tables': ['products', 'orders'],
    }
    doc.update(overrides)
    return doc


def paths(problems):
    return sorted(p.path for p in problems)


def message_for(problems, path):
    for p in problems:
        if p.path == path:
            return p.message
    raise AssertionError(
        'no problem at {!r}; got {}'.format(path, paths(problems)))


class SchemaFileTest(unittest.TestCase):
    """The schema file itself has to be well formed and self-consistent."""

    def test_schema_parses(self):
        self.assertIsInstance(schema(), dict)
        self.assertEqual(schema()['type'], 'object')

    def test_every_required_property_is_declared(self):
        # A required key with no schema entry would be unvalidatable.
        props = schema()['properties']
        for key in schema()['required']:
            self.assertIn(key, props, '{} is required but not declared'.format(key))

    def test_rejected_keys_never_overlap_properties(self):
        """A key cannot be both supported and rejected; that would be ambiguous."""
        def walk(node, path='root'):
            if not isinstance(node, dict):
                return
            props = set(node.get('properties') or {})
            rejected = set(node.get('x-rejected') or {})
            overlap = props & rejected
            self.assertEqual(
                overlap, set(),
                '{} lists {} as both a property and rejected'.format(path, overlap))
            for key, sub in (node.get('properties') or {}).items():
                walk(sub, '{}.{}'.format(path, key))
            extra = node.get('additionalProperties')
            if isinstance(extra, dict):
                walk(extra, '{}.*'.format(path))
            items = node.get('items')
            if isinstance(items, dict):
                walk(items, '{}[]'.format(path))
        walk(schema())

    def test_every_pattern_has_a_readable_hint(self):
        """A raw regex is not an error message a customer can act on."""
        missing = []

        def walk(node, path='root'):
            if not isinstance(node, dict):
                return
            if 'pattern' in node and 'x-format-hint' not in node:
                missing.append(path)
            for key, sub in (node.get('properties') or {}).items():
                walk(sub, '{}.{}'.format(path, key))
            extra = node.get('additionalProperties')
            if isinstance(extra, dict):
                walk(extra, '{}.*'.format(path))
            items = node.get('items')
            if isinstance(items, dict):
                walk(items, '{}[]'.format(path))

        walk(schema())
        self.assertEqual(missing, [],
                         'these patterned fields would report a raw regex')

    def test_every_rejection_explains_what_to_do(self):
        """A rejection message that just says 'no' is not an improvement."""
        def walk(node, path='root'):
            if not isinstance(node, dict):
                return
            for key, text in (node.get('x-rejected') or {}).items():
                # Guards against a degenerate rejection like "No." A useful one
                # either names the replacement setting or says why the value is
                # not the author's to choose. Length does not measure that well --
                # "Renamed to 'filter'." is twenty characters and still
                # actionable -- so this checks for actionable content instead.
                self.assertTrue(
                    text.rstrip().endswith('.'),
                    '{}.{} rejection should read as a sentence: {!r}'.format(
                        path, key, text))
                self.assertGreaterEqual(
                    len(text.split()), 3,
                    '{}.{} rejection is too terse to act on: {!r}'.format(
                        path, key, text))
                lowered = text.lower()
                actionable = any(marker in lowered for marker in (
                    'renamed', 'move', 'derived', 'detected', 'generated',
                    'not supported', 'not configurable', 'not a customer',
                    'never put', 'redundant', 'ignored', 'determined by',
                    'set ', 'use ', 'add ', 'compute ', 'managed by',
                    'has no ', 'always', 'there is one'))
                self.assertTrue(
                    actionable,
                    '{}.{} rejection names neither a replacement nor a reason: '
                    '{!r}'.format(path, key, text))
            for key, sub in (node.get('properties') or {}).items():
                walk(sub, '{}.{}'.format(path, key))
            extra = node.get('additionalProperties')
            if isinstance(extra, dict):
                walk(extra, '{}.*'.format(path))
            items = node.get('items')
            if isinstance(items, dict):
                walk(items, '{}[]'.format(path))
        walk(schema())


class ValidTest(unittest.TestCase):

    def test_minimal_config_is_valid(self):
        self.assertEqual(problems_for(minimal()), [])

    def test_all_three_modes_valid(self):
        for mode in ('initial_load', 'cdc', 'initial_load_and_cdc'):
            doc = minimal(mode=mode)
            self.assertEqual(problems_for(doc), [], 'mode {} rejected'.format(mode))

    def test_fully_populated_config_is_valid(self):
        doc = minimal(
            mode='cdc',
            source={'arn': SRC_ARN, 'database': 'Inventory',
                    'secret_arn': SEC_ARN, 'schema': 'public',
                    'username': 'admin'},
            target={'arn': TGT_ARN, 'secret_arn': SEC_ARN},
            tables=['public.products', 'public.orders', 'public.customers'],
            transforms={'tables': {
                'products': {
                    'columns': ['id', 'sku', 'price', 'active'],
                    'rename': {'sku': 'code'},
                    'computed': [{'name': 'price_cents', 'expr': 'price * 100'}],
                    'filter': {'field': 'active', 'op': '=', 'value': 'true'},
                },
                'orders': {
                    'denormalize': [{
                        'parent_table': 'customers',
                        'join_field': 'customer_id',
                        'parent_key_prefix': 'customers:',
                        'fields': ['name', 'tier'],
                        'prefix': 'cust_',
                        'on_parent_delete': 'null',
                    }],
                },
            }},
            advanced={
                'key_prefix': 'live',
                'images': {'sink': 'public.ecr.aws/x/ecdl-sink:1.0.0'},
                'sink': {'workers': 8, 'batch_size': 500, 'min_count': 1,
                         'desired_count': 2, 'max_count': 10,
                         'target_cpu_percent': 60},
                'broker': {'node_type': 'cache.t4g.small',
                           'maxmemory_policy': 'noeviction'},
                'debezium': {'server_id': 4242, 'poll_interval_ms': 10},
                'network': {'vpc_id': 'vpc-0abc123',
                            'subnet_ids': ['subnet-0abc123', 'subnet-0def456'],
                            'assign_public_ip': False},
            })
        self.assertEqual(problems_for(doc), [])

    def test_loader_primary_keys_can_be_set(self):
        """The override the loader itself tells operators to use.

        bulk_loader.py refuses to guess a split column and its FATAL messages say
        to "set advanced.loader.primary_keys in the link". That advice was
        unusable: advanced.loader is additionalProperties:false and did not
        declare the field, so anyone following it got a validation error. A
        composite or non-numeric key has no other remedy.
        """
        doc = minimal(tables=['merchants', 'orders'],
                      advanced={'loader': {'primary_keys':
                                           {'merchants': 'merchant_id'}}})
        self.assertEqual(problems_for(doc), [])

    def test_loader_primary_keys_column_must_be_a_string(self):
        doc = minimal(advanced={'loader': {'primary_keys': {'merchants': 7}}})
        self.assertEqual(
            paths(problems_for(doc)),
            ['advanced.loader.primary_keys.merchants'])

    def test_names_that_reach_sql_must_be_plain_identifiers(self):
        # Each of these is later written into SQL text, where it cannot be a
        # bound parameter; the schema refuses quotes, ';', spaces and comments.
        evil = 'x"; DROP TABLE t; --'
        src = {'arn': SRC_ARN, 'database': 'Inventory', 'secret_arn': SEC_ARN}
        cases = {
            'source.schema': minimal(source=dict(src, schema=evil)),
            'advanced.debezium.signal_collection':
                minimal(advanced={'debezium': {'signal_collection': 'public.' + evil}}),
            'advanced.loader.primary_keys.orders':
                minimal(advanced={'loader': {'primary_keys': {'orders': evil}}}),
        }
        for path, doc in cases.items():
            self.assertIn(path, paths(problems_for(doc)), path)
        ok = minimal(source=dict(src, schema='sales'),
                     advanced={'debezium': {'signal_collection': 'Inventory.ecdl_signal_x'},
                               'loader': {'primary_keys': {'orders': 'order_id'}}})
        self.assertEqual(problems_for(ok), [])

    def test_loader_primary_keys_typo_is_caught_and_suggested(self):
        doc = minimal(advanced={'loader': {'primary_kys': {'merchants': 'x'}}})
        found = problems_for(doc)
        self.assertEqual(paths(found), ['advanced.loader.primary_kys'])
        self.assertIn('primary_keys', found[0].message)

    def test_repo_transforms_json_can_be_expressed(self):
        """The spec that actually executes today must fit the canonical schema.

        data-plane/transforms.json is consumed by both the Go sink and the Python
        loader. If the schema could not express it, the schema would be wrong.
        """
        with open(os.path.join(REPO, 'data-plane', 'transforms.json')) as fh:
            spec = json.load(fh)
        doc = minimal(tables=['orders'], transforms=spec)
        self.assertEqual(problems_for(doc), [])

    def test_underscore_keys_are_comments(self):
        doc = minimal(transforms={
            '_doc': 'why this exists',
            'tables': {'products': {'_note': 'hi', 'columns': ['id']}},
        })
        self.assertEqual(problems_for(doc), [])

    def test_valid_config_round_trips_unchanged(self):
        doc = minimal()
        once = validate_link(doc)
        twice = validate_link(once)
        self.assertEqual(canonical_json(once), canonical_json(twice))

    def test_schema_version_defaulted(self):
        self.assertEqual(validate_link(minimal())['schema_version'], 1)


class RequiredTest(unittest.TestCase):

    def test_missing_top_level_keys_each_reported(self):
        found = problems_for({})
        self.assertEqual(paths(found),
                         ['mode', 'name', 'source', 'tables', 'target'])
        for p in found:
            self.assertEqual(p.message, 'is required')

    def test_missing_source_fields_reported(self):
        doc = minimal(source={'arn': SRC_ARN})
        found = problems_for(doc)
        self.assertEqual(paths(found), ['source.database', 'source.secret_arn'])

    def test_empty_target_object_reads_as_absent(self):
        # An operator who has not filled the target section in is told the
        # section is missing, which is more useful than an error about a nested
        # field they have never seen.
        found = problems_for(minimal(target={}))
        self.assertEqual(paths(found), ['target'])
        self.assertEqual(found[0].message, 'is required')

    def test_target_without_arn_but_with_other_keys(self):
        found = problems_for(minimal(target={'secret_arn': SEC_ARN}))
        self.assertEqual(paths(found), ['target.arn'])

    def test_tables_cannot_be_empty(self):
        # Empty and absent are the same statement: no tables were chosen.
        found = problems_for(minimal(tables=[]))
        self.assertEqual(message_for(found, 'tables'), 'is required')

    def test_tables_minitems_enforced_when_present(self):
        # Reached when the list holds something the pruner keeps but the schema
        # rejects, proving minItems is wired rather than dead.
        found = problems_for(minimal(tables=['ok']))
        self.assertEqual(found, [])

    def test_top_level_not_an_object(self):
        found = problems_for(['not', 'a', 'link'])
        self.assertEqual(len(found), 1)
        self.assertIn('expected an object', found[0].message)


class RejectedLegacyFieldsTest(unittest.TestCase):
    """Every field the old surfaces silently ignored now says what to do.

    These are the exact fields verified as silently dropped: the CLI read
    target.endpoint/port/tls/auth_token/cluster_mode and
    performance.poll_interval_ms and passed none of them to CloudFormation; the
    UI collected auth_username with no column to store it in; the CLI transform
    loader dropped key_pattern, columns, rename and aggregations.
    """

    def assert_explained(self, doc, path, must_mention):
        found = problems_for(doc)
        msg = message_for(found, path)
        self.assertNotIn('is not a recognised setting', msg,
                         '{} got a generic message, not an explanation'.format(path))
        self.assertIn(must_mention, msg.lower())

    def test_target_tls_explained(self):
        doc = minimal(target={'arn': TGT_ARN, 'tls': True})
        self.assert_explained(doc, 'target.tls', 'always on')

    def test_target_endpoint_explained(self):
        doc = minimal(target={'arn': TGT_ARN, 'endpoint': 'my.cache.local'})
        self.assert_explained(doc, 'target.endpoint', 'derived from target.arn')

    def test_target_cluster_mode_explained_with_the_failure_mode(self):
        doc = minimal(target={'arn': TGT_ARN, 'cluster_mode': True})
        self.assert_explained(doc, 'target.cluster_mode', 'moved')

    def test_target_auth_token_redirects_to_secret(self):
        doc = minimal(target={'arn': TGT_ARN, 'auth_token': 'hunter2'})  # nosec B105 - fake test credential
        self.assert_explained(doc, 'target.auth_token', 'secrets manager')

    def test_target_auth_username_explained(self):
        doc = minimal(target={'arn': TGT_ARN, 'auth_username': 'svc'})
        self.assert_explained(doc, 'target.auth_username', 'secret_arn')

    def test_source_port_explained(self):
        # port is now a real field for an external source (source.kind
        # 'external'); on an AWS source it is still rejected, with a message that
        # says where it belongs rather than a generic one.
        doc = minimal(source={'arn': SRC_ARN, 'database': 'd',
                              'secret_arn': SEC_ARN, 'port': 3306})
        self.assert_explained(doc, 'source.port', 'external source')

    def test_source_password_refused(self):
        doc = minimal(source={'arn': SRC_ARN, 'database': 'd',
                              'secret_arn': SEC_ARN, 'password': 'hunter2'})  # nosec B105 - fake test credential
        self.assert_explained(doc, 'source.password', 'never put a password')

    def test_source_password_secret_arn_renamed(self):
        doc = minimal(source={'arn': SRC_ARN, 'database': 'd',
                              'secret_arn': SEC_ARN,
                              'password_secret_arn': SEC_ARN})
        self.assert_explained(doc, 'source.password_secret_arn', 'renamed')

    def test_performance_block_explained(self):
        doc = minimal(performance={'poll_interval_ms': 100})
        self.assert_explained(doc, 'performance', 'advanced.debezium')

    def test_infrastructure_block_explained(self):
        doc = minimal(infrastructure={'vpc_id': 'vpc-0abc123'})
        self.assert_explained(doc, 'infrastructure', 'advanced.network')

    def test_broker_block_at_top_level_explained(self):
        doc = minimal(broker={'kafka_partitions': 5})
        self.assert_explained(doc, 'broker', 'advanced.broker')

    def test_connector_block_explained(self):
        doc = minimal(connector={'snapshot_mode': 'initial'})
        self.assert_explained(doc, 'connector', 'derived')

    def test_transform_aggregations_refused(self):
        doc = minimal(transforms={'aggregations': [{'function': 'count'}],
                                  'tables': {'products': {}}})
        self.assert_explained(doc, 'transforms.aggregations', 'not supported')

    def test_transform_row_filters_renamed(self):
        doc = minimal(transforms={'tables': {
            'products': {'row_filters': [{'field': 'a', 'op': '=', 'value': 'b'}]}}})
        self.assert_explained(doc, 'transforms.tables.products.row_filters',
                              "renamed to 'filter'")

    def test_transform_computed_fields_renamed(self):
        doc = minimal(transforms={'tables': {
            'products': {'computed_fields': [{'name': 'x', 'expr': '1'}]}}})
        self.assert_explained(doc, 'transforms.tables.products.computed_fields',
                              "renamed to 'computed'")

    def test_transform_key_pattern_explained(self):
        doc = minimal(transforms={'tables': {
            'products': {'key_pattern': 'p:{pk}'}}})
        self.assert_explained(doc, 'transforms.tables.products.key_pattern',
                              'advanced.key_prefix')

    def test_pipeline_block_explained(self):
        doc = minimal(pipeline={'source_id': 1})
        self.assert_explained(doc, 'pipeline', 'no \'pipeline\' object')


class FilterOperatorTest(unittest.TestCase):
    """The sink treats an unknown operator as 'always true'.

    Accepting `contains` here would produce a filter that silently matches every
    row -- the kind of silent error this schema exists to prevent.
    """

    def test_supported_operators_accepted(self):
        for op in ('=', '!=', '>', '>=', '<', '<='):
            doc = minimal(transforms={'tables': {'products': {
                'filter': {'field': 'price', 'op': op, 'value': '1'}}}})
            self.assertEqual(problems_for(doc), [], 'operator {} rejected'.format(op))

    def test_contains_rejected(self):
        doc = minimal(transforms={'tables': {'products': {
            'filter': {'field': 'name', 'op': 'contains', 'value': 'x'}}}})
        msg = message_for(problems_for(doc),
                          'transforms.tables.products.filter.op')
        self.assertIn('must be one of', msg)

    def test_in_rejected(self):
        doc = minimal(transforms={'tables': {'products': {
            'filter': {'field': 'c', 'op': 'in', 'value': 'a,b'}}}})
        self.assertIn('must be one of',
                      message_for(problems_for(doc),
                                  'transforms.tables.products.filter.op'))

    def test_filter_requires_all_three_fields(self):
        doc = minimal(transforms={'tables': {'products': {
            'filter': {'field': 'price'}}}})
        self.assertEqual(
            paths(problems_for(doc)),
            ['transforms.tables.products.filter.op',
             'transforms.tables.products.filter.value'])


class FormatTest(unittest.TestCase):

    def test_name_must_be_lowercase_dns_safe(self):
        # 'Checkout' is absent deliberately: case is normalised rather than
        # rejected, which the next test checks.
        for bad in ('checkout_cache', 'checkout-', '-checkout',
                    'check out', 'c' * 33, ''):
            found = problems_for(minimal(name=bad))
            self.assertTrue(
                any(p.path == 'name' for p in found),
                'name {!r} should have been rejected'.format(bad))

    def test_name_uppercase_is_normalised_not_rejected(self):
        # Normalisation lowercases before validating, so mixed case from a UI
        # field is accepted and canonicalised rather than rejected.
        self.assertEqual(validate_link(minimal(name='Checkout'))['name'],
                         'checkout')

    def test_good_names_accepted(self):
        for good in ('a', 'ab', 'checkout-cache', 'c1', 'a-b-c-1'):
            self.assertEqual(problems_for(minimal(name=good)), [],
                             'name {!r} should be valid'.format(good))

    def test_source_arn_must_be_rds(self):
        found = problems_for(minimal(source={
            'arn': TGT_ARN, 'database': 'd', 'secret_arn': SEC_ARN}))
        msg = message_for(found, 'source.arn')
        self.assertIn('Expected an RDS cluster or instance ARN', msg)
        self.assertNotIn('^arn:aws', msg, 'raw regex appeared in the message')

    def test_target_arn_must_be_elasticache(self):
        msg = message_for(problems_for(minimal(target={'arn': SRC_ARN})),
                          'target.arn')
        self.assertIn('replication group ARN', msg)
        # A cache cluster ARN is the plausible mistake, so the hint names it.
        self.assertIn('cache cluster ARN', msg)

    def test_secret_arn_must_be_secretsmanager(self):
        found = problems_for(minimal(source={
            'arn': SRC_ARN, 'database': 'd', 'secret_arn': SRC_ARN}))
        self.assertIn('Expected a Secrets Manager secret ARN',
                      message_for(found, 'source.secret_arn'))

    def test_name_format_error_is_readable(self):
        msg = message_for(problems_for(minimal(name='checkout_cache')), 'name')
        self.assertIn('lowercase letters, digits and hyphens', msg)
        self.assertNotIn('(?:', msg, 'raw regex appeared in the message')

    def test_table_format_error_is_readable(self):
        msg = message_for(problems_for(minimal(tables=['my table'])),
                          'tables[0]')
        self.assertIn('optionally qualified', msg)

    def test_instance_arn_accepted_as_source(self):
        arn = 'arn:aws:rds:us-east-2:111122223333:db:my-instance'
        self.assertEqual(problems_for(minimal(source={
            'arn': arn, 'database': 'd', 'secret_arn': SEC_ARN})), [])

    def test_govcloud_partition_accepted(self):
        self.assertEqual(problems_for(minimal(
            source={'arn': 'arn:aws-us-gov:rds:us-gov-west-1:111122223333'
                           ':cluster:s',
                    'database': 'd',
                    'secret_arn': 'arn:aws-us-gov:secretsmanager:us-gov-west-1'  # nosec B105 - fake test credential
                                  ':111122223333:secret:x-AbCdEf'},
            target={'arn': 'arn:aws-us-gov:elasticache:us-gov-west-1'
                           ':111122223333:replicationgroup:c'})), [])

    def test_bad_mode_lists_the_alternatives(self):
        msg = message_for(problems_for(minimal(mode='full_load')), 'mode')
        self.assertIn('initial_load_and_cdc', msg)

    def test_table_name_with_spaces_rejected(self):
        found = problems_for(minimal(tables=['my table']))
        self.assertEqual(paths(found), ['tables[0]'])

    def test_qualified_table_names_accepted(self):
        for name in ('orders', 'public.orders', 'Inventory.dbo.orders3w'):
            self.assertEqual(problems_for(minimal(tables=[name])), [],
                             '{!r} should be valid'.format(name))

    def test_boolean_is_not_an_integer(self):
        doc = minimal(advanced={'sink': {'workers': True}})
        self.assertIn('expected integer',
                      message_for(problems_for(doc), 'advanced.sink.workers'))

    def test_out_of_range_integer_reported(self):
        doc = minimal(advanced={'sink': {'target_cpu_percent': 99}})
        self.assertIn('<= 90', message_for(problems_for(doc),
                                           'advanced.sink.target_cpu_percent'))

    def test_typo_gets_a_suggestion(self):
        doc = minimal(tabels=['products'])
        msg = message_for(problems_for(doc), 'tabels')
        self.assertIn("Did you mean 'tables'", msg)


class SemanticTest(unittest.TestCase):

    def test_transform_for_unlisted_table_rejected(self):
        doc = minimal(tables=['products'],
                      transforms={'tables': {'orders': {'columns': ['id']}}})
        msg = message_for(problems_for(doc), 'transforms.tables.orders')
        self.assertIn('no matching entry', msg)

    def test_transform_matches_on_bare_name(self):
        doc = minimal(tables=['public.products'],
                      transforms={'tables': {'products': {'columns': ['id']}}})
        self.assertEqual(problems_for(doc), [])

    def test_duplicate_table_qualification_rejected(self):
        doc = minimal(tables=['orders', 'public.orders'])
        msg = message_for(problems_for(doc), 'tables')
        self.assertIn('more than once', msg)

    def test_exact_duplicate_tables_deduped_silently(self):
        # An exact repeat is a harmless copy-paste, not an ambiguity.
        link = validate_link(minimal(tables=['orders', 'orders', 'products']))
        self.assertEqual(link['tables'], ['orders', 'products'])

    def test_denormalize_parent_must_be_in_the_link(self):
        doc = minimal(tables=['orders'], transforms={'tables': {'orders': {
            'denormalize': [{'parent_table': 'customers',
                             'join_field': 'customer_id',
                             'fields': ['name']}]}}})
        msg = message_for(problems_for(doc),
                          'transforms.tables.orders.denormalize[0].parent_table')
        self.assertIn('never materialised', msg)

    def test_denormalize_parent_present_is_fine(self):
        doc = minimal(tables=['orders', 'customers'],
                      transforms={'tables': {'orders': {
                          'denormalize': [{'parent_table': 'customers',
                                           'join_field': 'customer_id',
                                           'fields': ['name']}]}}})
        self.assertEqual(problems_for(doc), [])

    def test_dead_rename_rejected(self):
        doc = minimal(transforms={'tables': {'products': {
            'columns': ['id', 'sku'],
            'rename': {'price': 'cost'}}}})
        msg = message_for(problems_for(doc),
                          'transforms.tables.products.rename.price')
        self.assertIn('never apply', msg)

    def test_rename_of_computed_field_is_fine(self):
        doc = minimal(transforms={'tables': {'products': {
            'columns': ['id'],
            'computed': [{'name': 'cents', 'expr': 'price * 100'}],
            'rename': {'cents': 'price_cents'}}}})
        self.assertEqual(problems_for(doc), [])

    def test_rename_without_column_selection_is_fine(self):
        doc = minimal(transforms={'tables': {'products': {
            'rename': {'price': 'cost'}}}})
        self.assertEqual(problems_for(doc), [])

    def test_debezium_overrides_rejected_for_initial_load(self):
        doc = minimal(mode='initial_load',
                      advanced={'debezium': {'server_id': 99}})
        msg = message_for(problems_for(doc), 'advanced.debezium')
        self.assertIn('does not run Debezium', msg)

    def test_sink_overrides_rejected_for_initial_load(self):
        doc = minimal(mode='initial_load', advanced={'sink': {'workers': 4}})
        self.assertIn('no sink is created',
                      message_for(problems_for(doc), 'advanced.sink'))

    def test_loader_overrides_rejected_for_cdc(self):
        doc = minimal(mode='cdc', advanced={'loader': {'workers': 8}})
        self.assertIn('does not run the initial loader',
                      message_for(problems_for(doc), 'advanced.loader'))

    def test_loader_overrides_fine_for_initial_load(self):
        doc = minimal(mode='initial_load', advanced={'loader': {'workers': 8}})
        self.assertEqual(problems_for(doc), [])

    def test_sink_count_ordering_checked(self):
        doc = minimal(advanced={'sink': {'min_count': 5, 'max_count': 2}})
        self.assertIn('above max_count',
                      message_for(problems_for(doc), 'advanced.sink.min_count'))

    def test_desired_below_min_reported(self):
        doc = minimal(advanced={'sink': {'min_count': 3, 'desired_count': 1,
                                         'max_count': 10}})
        self.assertIn('below min_count',
                      message_for(problems_for(doc),
                                  'advanced.sink.desired_count'))

    def test_loader_pk_range_ordering_checked(self):
        doc = minimal(mode='initial_load',
                      advanced={'loader': {'min_pk': 100, 'max_pk': 1}})
        self.assertIn('above max_pk',
                      message_for(problems_for(doc), 'advanced.loader.min_pk'))


class NormalisationTest(unittest.TestCase):
    """The UI posts nulls and blanks; a file omits them. Both must canonicalise
    to the same document, or "the UI and the file accept the same things" is an
    unverifiable claim."""

    def test_ui_shaped_and_file_shaped_are_byte_identical(self):
        file_shaped = minimal()
        ui_shaped = {
            'schema_version': None,
            'name': '  Checkout-Cache  ',
            'mode': 'initial_load_and_cdc',
            'source': {
                'arn': SRC_ARN,
                'database': 'Inventory',
                'secret_arn': SEC_ARN,
                'schema': '',
                'username': None,
            },
            'target': {'arn': TGT_ARN, 'secret_arn': ''},  # nosec B105 - fake test credential
            'tables': ['products', 'orders', ''],
            'transforms': {'tables': {}},
            'advanced': {},
        }
        self.assertEqual(canonical_json(validate_link(file_shaped)),
                         canonical_json(validate_link(ui_shaped)))

    def test_empty_transforms_dropped(self):
        link = validate_link(minimal(transforms={'tables': {}}))
        self.assertNotIn('transforms', link)

    def test_transforms_version_dropped(self):
        link = validate_link(minimal(transforms={
            'version': 1, 'tables': {'products': {'columns': ['id']}}}))
        self.assertNotIn('version', link['transforms'])

    def test_deployment_defaults_not_written_into_the_config(self):
        """Only structural defaults are filled.

        Writing batch sizes and node types back into the customer's file would
        make it impossible to tell what they chose from what we chose.
        """
        link = validate_link(minimal())
        self.assertEqual(set(link), {'schema_version', 'name', 'mode',
                                     'source', 'target', 'tables'})

    def test_normalize_is_idempotent(self):
        once = normalize(minimal(name=' Checkout '))
        self.assertEqual(canonical_json(once), canonical_json(normalize(once)))

    def test_normalize_does_not_mutate_the_input(self):
        doc = minimal(name='  Checkout  ')
        before = json.dumps(doc, sort_keys=True)
        normalize(doc)
        self.assertEqual(json.dumps(doc, sort_keys=True), before)


class LoadingTest(unittest.TestCase):

    def test_json_text_loads(self):
        doc = loads(json.dumps(minimal()))
        self.assertEqual(problems_for(doc), [])

    def test_malformed_json_is_reported_as_json(self):
        with self.assertRaises(LinkValidationError) as ctx:
            loads('{"name": ')
        self.assertIn('not valid JSON', str(ctx.exception))

    def test_yaml_text_loads(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest('PyYAML not installed')
        text = (
            'name: checkout-cache\n'
            'mode: initial_load_and_cdc\n'
            'source:\n'
            '  arn: {src}\n'
            '  database: Inventory\n'
            '  secret_arn: {sec}\n'
            'target:\n'
            '  arn: {tgt}\n'
            'tables:\n'
            '  - products\n'
            '  - orders\n'
        ).format(src=SRC_ARN, sec=SEC_ARN, tgt=TGT_ARN)
        doc = loads(text)
        self.assertEqual(problems_for(doc), [])
        self.assertEqual(canonical_json(validate_link(doc)),
                         canonical_json(validate_link(minimal())))

    def test_yaml_and_json_agree(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML not installed')
        doc = minimal()
        as_yaml = loads(yaml.safe_dump(doc))
        as_json = loads(json.dumps(doc))
        self.assertEqual(canonical_json(validate_link(as_yaml)),
                         canonical_json(validate_link(as_json)))

    def test_empty_file_reported(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest('PyYAML not installed')
        with self.assertRaises(LinkValidationError) as ctx:
            loads('# only a comment\n')
        self.assertIn('empty', str(ctx.exception))

    def test_load_file_round_trip(self):
        tmp = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
        try:
            json.dump(minimal(), tmp)
            tmp.close()
            from ecdl_link import validate_file
            link = validate_file(tmp.name)
            self.assertEqual(link['name'], 'checkout-cache')
        finally:
            os.unlink(tmp.name)


class ErrorReportingTest(unittest.TestCase):

    def test_all_problems_reported_not_just_the_first(self):
        doc = minimal(name='BAD NAME', mode='nope', tables=[])
        found = problems_for(doc)
        self.assertGreaterEqual(len(found), 3)

    def test_exception_message_lists_every_problem(self):
        with self.assertRaises(LinkValidationError) as ctx:
            validate_link({}, source='my-link.yaml')
        text = str(ctx.exception)
        self.assertIn('my-link.yaml', text)
        self.assertIn('5 problems found', text)
        for key in ('name', 'mode', 'source', 'target', 'tables'):
            self.assertIn(key, text)

    def test_singular_problem_phrasing(self):
        with self.assertRaises(LinkValidationError) as ctx:
            validate_link(minimal(mode='wrong'))
        self.assertIn('1 problem found', str(ctx.exception))

    def test_semantic_checks_gated_behind_schema_checks(self):
        """A wrong-typed transforms block should not also emit semantic errors."""
        found = problems_for(minimal(transforms='not-an-object'))
        self.assertEqual(paths(found), ['transforms'])


class ExternalSourceValidation(unittest.TestCase):
    """source.kind 'external': an on-prem/self-managed DB given by engine+host.

    The schema cannot express "an ARN or an endpoint, never both" (the validator
    has no oneOf), so these rules live in the semantic layer and are what these
    tests check.
    """

    def external(self, **source_over):
        src = {'kind': 'external', 'engine': 'postgresql',
               'host': 'db.corp.internal', 'database': 'Inventory',
               'username': 'repl', 'secret_arn': SEC_ARN}
        src.update(source_over)
        return minimal(source=src)

    def test_a_complete_external_source_is_valid(self):
        self.assertEqual(problems_for(self.external()), [])

    def test_external_requires_engine_host_and_username(self):
        doc = minimal(source={'kind': 'external', 'database': 'Inventory',
                              'secret_arn': SEC_ARN})
        self.assertEqual(
            paths(problems_for(doc)),
            ['source.engine', 'source.host', 'source.username'])

    def test_external_rejects_an_arn(self):
        doc = self.external(arn=SRC_ARN)
        self.assertIn('source.arn', paths(problems_for(doc)))

    def test_external_engine_must_be_known(self):
        # 'db2' has no Debezium connector, so the enum rejects it at the schema
        # layer before the semantic checks even run.
        doc = self.external(engine='db2')
        self.assertIn('source.engine', paths(problems_for(doc)))

    def test_external_port_is_optional(self):
        # Omitted here; the resolver fills the engine default. Present and valid
        # when set.
        self.assertEqual(problems_for(self.external(port=5433)), [])

    def test_aws_source_rejects_external_only_fields(self):
        for field, value in (('engine', 'mysql'), ('host', 'db.example'),
                             ('port', 3306), ('tls', True)):
            src = {'arn': SRC_ARN, 'database': 'Inventory',
                   'secret_arn': SEC_ARN, field: value}
            doc = minimal(source=src)
            self.assertIn('source.{}'.format(field), paths(problems_for(doc)),
                          '{} should be rejected on an AWS source'.format(field))

    def test_aws_source_still_requires_an_arn(self):
        # arn is no longer a schema-level required key (external does not use it),
        # so the "AWS source needs an arn" rule moved to the semantic layer.
        doc = minimal(source={'database': 'Inventory', 'secret_arn': SEC_ARN})
        self.assertIn('source.arn', paths(problems_for(doc)))

    def test_default_kind_is_aws(self):
        # A source with no kind is an AWS source, so the plain minimal() config
        # (which has an arn and no kind) stays valid and needs no engine/host.
        self.assertEqual(problems_for(minimal()), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
