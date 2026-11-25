"""Two links must not write the same keys into the same cache."""
import unittest

from ecdl_link.conflicts import describe_conflict, key_conflicts, key_prefixes, \
    resolution_conflicts

HOST = 'clustercfg.shared.abc.use2.cache.amazonaws.com'


class FakeCfn(object):
    def __init__(self, stacks):
        # stacks: {name: params}
        self.stacks = stacks

    def list_stacks(self, **kwargs):
        return {'StackSummaries': [{'StackName': n, 'StackStatus': 'CREATE_COMPLETE'}
                                   for n in self.stacks]}

    def describe_stacks(self, StackName=None):
        params = self.stacks[StackName]
        return {'Stacks': [{'Parameters': [{'ParameterKey': k, 'ParameterValue': v}
                                           for k, v in params.items()]}]}


class KeyPrefixTest(unittest.TestCase):
    def test_bare_and_qualified_tables(self):
        self.assertEqual(key_prefixes('Inventory.products, orders'),
                         {'products:', 'orders:'})

    def test_link_prefix(self):
        self.assertEqual(key_prefixes('Inventory.products', 'v2:'), {'v2:products:'})


class ConflictTest(unittest.TestCase):
    def cfn(self, **extra):
        stacks = {
            'ecdl-cxtest-cdc': {'TargetHost': HOST, 'Tables': 'Inventory.products'},
            'ecdl-cxtest-loader': {'TargetHost': HOST, 'Tables': 'products'},
            'ecdl-other-cdc': {'TargetHost': 'clustercfg.elsewhere', 'Tables': 'x.products'},
            'ecdl-control-plane': {'AllowedCIDR': '1.2.3.4/32'},
        }
        stacks.update(extra)
        return FakeCfn(stacks)

    def test_same_table_same_cache_conflicts(self):
        found = key_conflicts(self.cfn(), 'demo', HOST, ['products:'])
        self.assertEqual(found, [{'link': 'cxtest', 'key_prefixes': ['products:']}])
        self.assertIn("'cxtest' already writes products:*", describe_conflict(found[0], HOST))

    def test_host_comparison_ignores_case(self):
        self.assertTrue(key_conflicts(self.cfn(), 'demo', HOST.upper(), ['products:']))

    def test_redeploying_the_same_link_is_not_a_conflict(self):
        self.assertEqual(key_conflicts(self.cfn(), 'cxtest', HOST, ['products:']), [])

    def test_a_key_prefix_separates_them(self):
        self.assertEqual(key_conflicts(self.cfn(), 'demo', HOST, ['v2:products:']), [])

    def test_other_cache_or_other_table_is_fine(self):
        self.assertEqual(key_conflicts(self.cfn(), 'demo', 'clustercfg.mine', ['products:']), [])
        self.assertEqual(key_conflicts(self.cfn(), 'demo', HOST, ['orders:']), [])

    def test_existing_link_with_a_prefix(self):
        cfn = self.cfn(**{'ecdl-v2-cdc': {'TargetHost': HOST, 'Tables': 'a.products',
                                          'KeyPrefix': 'v2'}})
        self.assertEqual(key_conflicts(cfn, 'demo', HOST, ['v2:products:']),
                         [{'link': 'v2', 'key_prefixes': ['v2:products:']}])

    def test_nested_prefixes_conflict_both_ways(self):
        # cxtest's table products owns products:*, which holds every key of a
        # link whose key_prefix is products.
        self.assertEqual(key_conflicts(self.cfn(), 'demo', HOST, ['products:orders:']),
                         [{'link': 'cxtest', 'key_prefixes': ['products:']}])
        cfn = self.cfn(**{'ecdl-shop-cdc': {'TargetHost': HOST, 'Tables': 'a.orders',
                                            'KeyPrefix': 'shop'}})
        self.assertEqual(key_conflicts(cfn, 'demo', HOST, ['shop:']),
                         [{'link': 'shop', 'key_prefixes': ['shop:orders:']}])

    def test_a_shared_start_without_the_separator_is_fine(self):
        # products: and products2: never share a key.
        self.assertEqual(key_conflicts(self.cfn(), 'demo', HOST, ['products2:']), [])

    def test_from_a_resolution(self):
        resolution = {'link': {'name': 'demo'}, 'target': {'endpoint': HOST},
                      'tables': [{'key_prefix': 'products:'}]}
        self.assertEqual([c['link'] for c in resolution_conflicts(self.cfn(), resolution)],
                         ['cxtest'])


if __name__ == '__main__':
    unittest.main()
