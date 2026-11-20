"""One change path (D7): review_update compares a planned update with the
deployed stack parameters. Pure: no AWS."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from ecdl_link.change import (describe_review, effective_parameters,  # noqa: E402
                              parameter_drift, review_update)

SPEC = json.dumps({'tables': {'orders': {'columns': ['id', 'total']}}},
                  sort_keys=True, separators=(',', ':'))


def cdc_params(**over):
    p = {
        'SourceEngine': 'mysql', 'SourceDBHost': 'src.example', 'SourceDBPort': '3306',
        'SourceDBName': 'Inventory', 'SourceDBUser': 'cdc',
        'DBSecretArn': 'arn:secret:db', 'DBSecretJsonKey': 'password',
        'TargetHost': 'tgt.example', 'TargetPort': '6379', 'TargetCluster': 'true',
        'TargetSSL': 'true', 'Tables': 'Inventory.orders,Inventory.customers',
        'Streams': 'data:l.Inventory.orders,data:l.Inventory.customers',
        'TopicPrefix': 'l', 'OffsetKey': 'ecdl:l:offsets',
        'SchemaHistoryKey': 'ecdl:l:schema_history', 'KeyPrefix': '',
        'SinkWorkers': '8', 'DebeziumCpu': '2048', 'DebeziumDesiredCount': '1',
        'PgSlotName': '', 'MonitorSecurityGroupId': 'sg-mon',
        'PermissionsBoundaryArn': 'arn:boundary', 'TransformsJson': SPEC,
    }
    p.update(over)
    return p


DEFAULTS = {'cdc': {'AlarmEmail': '', 'CpuArchitecture': 'X86_64',
                    'DebeziumImage': 'repo/dbz@sha256:old', 'KeyPrefix': '',
                    'TransformsJson': ''}}
NEW_DEFAULTS = {'cdc': dict(DEFAULTS['cdc'], DebeziumImage='repo/dbz@sha256:new')}


def plan_with(**params):
    return {'steps': [{'kind': 'stack', 'id': 'cdc', 'parameters': cdc_params(**params),
                       'from_outputs': {'BrokerHost': ['broker', 'PrimaryEndpoint']}}]}


def review(deployed_over=None, planned_over=None, mode='initial_load_and_cdc',
           deployed_extra=('loader',)):
    deployed = {'cdc': dict(cdc_params(**(deployed_over or {})),
                            DebeziumImage='repo/dbz@sha256:old',
                            CpuArchitecture='X86_64', AlarmEmail='')}
    for comp in deployed_extra:
        deployed[comp] = {}
    return review_update(plan_with(**(planned_over or {})), {'name': 'l', 'mode': mode},
                         deployed, DEFAULTS, NEW_DEFAULTS)


def fields(r):
    return sorted(c['field'] for c in r['diff']['changes'])


class NoChangeTest(unittest.TestCase):
    def test_identical_update_is_no_change(self):
        r = review()
        self.assertFalse(r['diff']['changed'], describe_review(r))
        self.assertFalse(r['refuse'] or r['needs_ack'])
        self.assertEqual(describe_review(r), 'no change to the deployed link')

    def test_release_upgrade_of_a_default_image_is_not_a_change(self):
        # The plan sends no image; the stack held the old template default.
        self.assertEqual(review()['drift'], [])

    def test_kept_and_wired_parameters_are_ignored(self):
        # Debezium paused for a load (0) and planned 1; broker host from outputs.
        r = review(deployed_over={'DebeziumDesiredCount': '0', 'BrokerHost': 'b1',
                                  'PgSlotName': 'slot_a'})
        self.assertFalse(r['diff']['changed'], describe_review(r))

    def test_empty_control_plane_values_are_kept(self):
        # The CLI on a laptop knows neither value (lifecycle M1).
        r = review(planned_over={'MonitorSecurityGroupId': '', 'PermissionsBoundaryArn': ''})
        self.assertFalse(r['diff']['changed'], describe_review(r))


class EditorDroppedAdvancedTest(unittest.TestCase):
    """Lifecycle C1: the UI editor's document lacked advanced.*."""

    def setUp(self):
        self.r = review(deployed_over={'KeyPrefix': 'b', 'SourceDBUser': 'cdc'},
                        planned_over={'KeyPrefix': '', 'SourceDBUser': 'admin'})

    def test_every_dropped_setting_is_reported(self):
        self.assertEqual(fields(self.r), ['advanced.key_prefix', 'source.username'])

    def test_a_prefix_change_needs_acknowledgement(self):
        self.assertTrue(self.r['needs_ack'])
        self.assertFalse(self.r['refuse'])
        self.assertEqual(self.r['diff']['severity'], 'cleanup')
        text = describe_review(self.r)
        self.assertIn("SourceDBUser 'cdc' -> 'admin'", text)

    def test_a_removed_override_is_reported(self):
        # Deployed with an alarm email and arm64; the document drops both.
        r = review_update(plan_with(), {'name': 'l', 'mode': 'initial_load_and_cdc'},
                          {'cdc': dict(cdc_params(), AlarmEmail='ops@example.com',
                                       CpuArchitecture='ARM64',
                                       DebeziumImage='repo/dbz@sha256:old'),
                           'loader': {}},
                          DEFAULTS, NEW_DEFAULTS)
        self.assertEqual(fields(r), ['advanced.alarms', 'advanced.cpu_architecture'])
        self.assertFalse(r['refuse'] or r['needs_ack'])

    def test_a_custom_image_dropped_from_the_file_is_reported(self):
        r = review_update(plan_with(), {'name': 'l', 'mode': 'initial_load_and_cdc'},
                          {'cdc': dict(cdc_params(), DebeziumImage='mine/dbz:1',
                                       CpuArchitecture='X86_64', AlarmEmail=''),
                           'loader': {}},
                          DEFAULTS, NEW_DEFAULTS)
        self.assertEqual(fields(r), ['advanced.images'])


class IdentityTest(unittest.TestCase):
    """Lifecycle H1: apply and /deploy changed the source or target in place."""

    def test_a_new_target_is_refused(self):
        r = review(planned_over={'TargetHost': 'other.example'})
        self.assertTrue(r['refuse'])
        self.assertEqual(fields(r), ['target'])
        self.assertIn('new link', r['diff']['changes'][0]['action'])

    def test_a_new_source_or_database_is_refused(self):
        for over in ({'SourceDBHost': 'clone.example'}, {'SourceDBName': 'Other'},
                     {'SourceEngine': 'postgresql'}, {'SourceDBPort': '3307'}):
            self.assertTrue(review(planned_over=over)['refuse'], over)

    def test_a_change_on_two_stacks_is_reported_once(self):
        plan = {'steps': [{'kind': 'stack', 'id': c, 'parameters': {'SourceDBName': 'Other'}}
                          for c in ('cdc', 'loader')]}
        r = review_update(plan, {'name': 'l', 'mode': 'initial_load_and_cdc'},
                          {'cdc': {'SourceDBName': 'Inventory'},
                           'loader': {'SourceDBName': 'Inventory'}}, {}, {})
        self.assertEqual(fields(r).count('source.database'), 1)
        self.assertEqual(len(r['drift']), 2)

    def test_an_unknown_parameter_change_is_refused(self):
        r = review(deployed_over={'SomethingNew': 'a'}, planned_over={'SomethingNew': 'b'})
        self.assertTrue(r['refuse'])
        self.assertEqual(r['diff']['changes'][0]['kind'], 'unexplained')

    def test_the_capture_paused_hours_are_a_setting(self):  # Jira story F103
        # A stack deployed before the parameter existed has no value for it;
        # the first update sends one. That must not be refused as unexplained.
        r = review(planned_over={'CapturePausedAlarmHours': '13'})
        self.assertFalse(r['refuse'], describe_review(r))
        self.assertEqual([(c['kind'], c['field']) for c in r['diff']['changes']],
                         [('setting', 'advanced.loader')])
        self.assertEqual(r['drift'], [{'stack': 'cdc', 'parameter': 'CapturePausedAlarmHours',
                                       'from': '', 'to': '13'}])
        self.assertFalse(review(deployed_over={'CapturePausedAlarmHours': '13'},
                                planned_over={'CapturePausedAlarmHours': '13'})['diff']['changed'])
    def test_a_changed_security_group_of_the_same_database_is_allowed(self):
        r = review(deployed_over={'AuroraSecurityGroupId': 'sg-1'},
                   planned_over={'AuroraSecurityGroupId': 'sg-2'})
        self.assertFalse(r['diff']['changed'])


class DataChangeTest(unittest.TestCase):
    def test_an_added_table_needs_a_backfill(self):
        r = review(planned_over={'Tables': 'Inventory.orders,Inventory.customers,Inventory.items'})
        self.assertTrue(r['needs_ack'])
        self.assertEqual(r['diff']['severity'], 'backfill')

    def test_a_removed_table_needs_a_cleanup(self):
        r = review(planned_over={'Tables': 'Inventory.orders'})
        self.assertEqual(r['diff']['severity'], 'cleanup')

    def test_a_changed_transform_needs_a_backfill(self):
        spec = json.dumps({'tables': {'orders': {'columns': ['id']}}})
        r = review(planned_over={'TransformsJson': spec})
        self.assertEqual([c['kind'] for c in r['diff']['changes']], ['transform_changed'])
        self.assertTrue(r['needs_ack'])

    def test_a_scaling_change_is_a_plain_deploy(self):
        r = review(planned_over={'SinkWorkers': '16'})
        self.assertEqual(fields(r), ['advanced.sink'])
        self.assertFalse(r['needs_ack'] or r['refuse'])

    def test_losing_cdc_is_refused(self):
        # Lifecycle H2: the update plan of an initial_load document has only
        # the loader stack, while CDC is deployed.
        plan = {'steps': [{'kind': 'stack', 'id': 'loader',
                           'parameters': {'Tables': 'orders,customers', 'KeyPrefix': ''}}]}
        r = review_update(plan, {'name': 'l', 'mode': 'initial_load'},
                          {'cdc': cdc_params(), 'loader': {'Tables': 'orders,customers',
                                                           'KeyPrefix': ''}},
                          {}, {})
        self.assertTrue(r['refuse'])
        self.assertIn('mode', fields(r))


class LoaderSourceTablesTest(unittest.TestCase):
    """Jira stories F060/F061: SourceTables and ProgressKeyBase are new; a loader stack
    deployed before them has neither."""
    def review_loader(self, deployed_loader, planned_loader):
        plan = {'steps': [{'kind': 'stack', 'id': 'loader', 'parameters': planned_loader}]}
        return review_update(plan, {'name': 'l', 'mode': 'initial_load'},
                             {'loader': deployed_loader}, {}, {})
    def test_adding_source_tables_to_an_old_stack_is_not_refused(self):
        r = self.review_loader({'Tables': 'orders', 'KeyPrefix': ''},
                               {'Tables': 'orders', 'SourceTables': 'Inventory.orders',
                                'KeyPrefix': ''})
        self.assertFalse(r['refuse'], describe_review(r))
        self.assertFalse(r['diff']['changed'], describe_review(r))
    def test_adding_the_progress_key_base_to_an_old_stack_is_not_refused(self):
        # Jira story F060: the parameter follows from the link name.
        r = self.review_loader({'Tables': 'orders', 'KeyPrefix': ''},
                               {'Tables': 'orders', 'KeyPrefix': '',
                                'ProgressKeyBase': 'ecdl:l:bulkload'})
        self.assertFalse(r['refuse'], describe_review(r))
        self.assertFalse(r['diff']['changed'], describe_review(r))
    def test_a_new_table_is_still_reported(self):
        r = self.review_loader({'Tables': 'orders', 'SourceTables': 'Inventory.orders',
                                'KeyPrefix': ''},
                               {'Tables': 'orders,items',
                                'SourceTables': 'Inventory.orders,Inventory.items',
                                'KeyPrefix': ''})
        self.assertIn('tables', fields(r))
class HelpersTest(unittest.TestCase):
    def test_effective_parameters_fill_template_defaults(self):
        eff = effective_parameters({'parameters': {'A': 1}}, {'A': '0', 'B': 'x'})
        self.assertEqual(eff, {'A': '1', 'B': 'x'})

    def test_drift_skips_parameters_the_plan_omits_at_their_default(self):
        step = {'parameters': {}}
        self.assertEqual(parameter_drift(step, {'Img': 'old'}, {'Img': 'old'}, {'Img': 'new'}), [])
        self.assertEqual(parameter_drift(step, {'Img': 'mine'}, {'Img': 'old'}, {'Img': 'new'}),
                         [('Img', 'mine', 'new')])



class ThresholdParameterTest(unittest.TestCase):  # Jira story F089
    def test_round_trip(self):
        from ecdl_link.change import INVERSE
        from ecdl_link.provision import table_stale_parameter
        path, convert = INVERSE['TableStaleSeconds']
        self.assertEqual(path, ('advanced', 'alarms', 'table_stale_seconds'))
        value = {'orders': 3600, 'items': 600}
        self.assertEqual(convert(table_stale_parameter(value)), value)
        self.assertIsNone(convert(''))
        self.assertIsNone(convert('orders=x'))


class SecretJsonKeyRoundTripTest(unittest.TestCase):  # Jira story F015

    def test_a_deployed_key_the_file_would_not_produce_is_written_back(self):
        from ecdl_link.change import INVERSE, INVERSE_IF_DRIFTED, SECTION, apply_settings
        self.assertEqual(INVERSE['DBSecretJsonKey'][0], ('source', 'secret_json_key'))
        self.assertIn('DBSecretJsonKey', INVERSE_IF_DRIFTED)
        self.assertEqual(SECTION['DBSecretJsonKey'], 'source.secret_json_key')
        doc = {'source': {'arn': 'a'}}
        # A plain rebuild does not write a planner-derived value...
        self.assertEqual(apply_settings(doc, {'DBSecretJsonKey': 'db_pass'}, {}), [])
        # ...the drift fix-up does, so the next plan sends the same key.
        apply_settings(doc, {'DBSecretJsonKey': 'db_pass'}, {'DBSecretJsonKey': ''},
                       only={'DBSecretJsonKey'})
        self.assertEqual(doc['source']['secret_json_key'], 'db_pass')

    def test_a_changed_key_is_a_setting_change_not_a_refusal(self):
        deployed = {'cdc': cdc_params(DBSecretJsonKey='')}
        plan = {'mode': 'cdc', 'steps': [{'id': 'cdc', 'kind': 'stack',
                                          'parameters': cdc_params(DBSecretJsonKey='db_pass')}]}
        review = review_update(plan, {'name': 'l', 'mode': 'cdc'}, deployed, DEFAULTS, DEFAULTS)
        changes = [c for c in review['diff']['changes'] if c.get('parameter') == 'DBSecretJsonKey']
        self.assertEqual(len(changes), 1, review['diff']['changes'])
        self.assertEqual(changes[0]['kind'], 'setting')
        self.assertEqual(changes[0]['field'], 'source.secret_json_key')
        self.assertFalse(review['refuse'])


class NewSettingsTest(unittest.TestCase):  # Jira story F129
    def test_node_type_change_is_a_setting_not_unexplained(self):
        step = {'kind': 'stack', 'id': 'broker',
                'parameters': {'NodeType': 'cache.r7g.large'}, 'from_outputs': {}}
        r = review_update({'steps': [step]}, {'name': 'l', 'mode': 'cdc'},
                          {'broker': {'NodeType': 'cache.t4g.small'}},
                          {'broker': {'NodeType': 'cache.t4g.small'}},
                          {'broker': {'NodeType': 'cache.t4g.small'}})
        change = next(c for c in r['diff']['changes'] if c.get('parameter') == 'NodeType')
        self.assertEqual((change['field'], change['severity']), ('advanced.broker', 'deploy'))
        self.assertFalse(r['refuse'])

    def test_reconstruct_maps_loader_image_and_node_type(self):
        from ecdl_link.change import apply_settings
        doc = {}
        apply_settings(doc, {'LoaderImage': 'acct/l:2', 'NodeType': 'cache.r7g.large'},
                       {'LoaderImage': 'acct/l:1', 'NodeType': 'cache.t4g.small'},
                       only={'LoaderImage', 'NodeType'})
        self.assertEqual(doc, {'advanced': {'images': {'loader': 'acct/l:2'},
                                            'broker': {'node_type': 'cache.r7g.large'}}})
        doc = {}
        apply_settings(doc, {'LoaderImage': 'acct/l:1'}, {'LoaderImage': 'acct/l:1'},
                       only={'LoaderImage'})
        self.assertEqual(doc, {})


if __name__ == '__main__':
    unittest.main()


class BrokerSizingDriftTest(unittest.TestCase):  # Jira stories F023, F024
    def broker_review(self, planned, keep):
        plan = {'steps': [{'kind': 'stack', 'id': 'broker', 'parameters': planned,
                           'keep_previous': keep}]}
        return review_update(plan, {'name': 'l', 'mode': 'cdc'},
                             {'broker': {'NodeType': 'cache.t4g.small', 'RestoreSnapshotName': ''}, 'cdc': {}},
                             {'broker': {'NodeType': 'cache.t4g.small', 'NodeCount': ''}},
                             {'broker': {'NodeType': 'cache.t4g.small', 'NodeCount': ''}})

    def test_kept_sizing_is_no_change(self):
        r = self.broker_review({'NodeType': 'cache.m7g.large', 'NodeCount': '3',
                                'RestoreSnapshotName': ''},
                               ['RestoreSnapshotName', 'NodeType', 'NodeCount'])
        self.assertFalse(r['diff']['changed'], r['diff']['changes'])

    def test_set_sizing_is_a_setting_change(self):
        r = self.broker_review({'NodeType': 'cache.m7g.large', 'NodeCount': '3',
                                'RestoreSnapshotName': ''}, ['RestoreSnapshotName'])
        self.assertFalse(r['refuse'])
        self.assertEqual(sorted((c['parameter'], c['field'], c['severity']) for c in r['diff']['changes']),
                         [('NodeCount', 'advanced.broker', 'deploy'),
                          ('NodeType', 'advanced.broker', 'deploy')])

    def test_a_new_restore_snapshot_is_still_refused(self):
        r = self.broker_review({'NodeType': 'cache.t4g.small', 'RestoreSnapshotName': 'ecdl-x'},
                               ['RestoreSnapshotName', 'NodeType', 'NodeCount'])
        self.assertTrue(r['refuse'])
        self.assertEqual(r['diff']['changes'][0]['kind'], 'create_only')
