# Copyright (c) 2013 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import datetime
import time
import uuid

import mock
from oslo_utils import timeutils
from pymongo import cursor
import pymongo.errors
import six
from testtools import matchers

from zaqar import bootstrap
from zaqar.openstack.common.cache import cache as oslo_cache
from zaqar import storage
from zaqar.storage import errors
from zaqar.storage import mongodb
from zaqar.storage.mongodb import controllers
from zaqar.storage.mongodb import options
from zaqar.storage.mongodb import utils
from zaqar.storage import pooling
from zaqar import tests as testing
from zaqar.tests.unit.storage import base


class MongodbSetupMixin(object):
    def _purge_databases(self):
        if isinstance(self.driver, mongodb.DataDriver):
            databases = (self.driver.message_databases +
                         [self.control.queues_database,
                          self.driver.subscriptions_database])
        else:
            databases = [self.driver.queues_database]

        for db in databases:
            self.driver.connection.drop_database(db)

    def _prepare_conf(self):
        if options.MESSAGE_MONGODB_GROUP in self.conf:
            self.config(options.MESSAGE_MONGODB_GROUP,
                        database=uuid.uuid4().hex)

        if options.MANAGEMENT_MONGODB_GROUP in self.conf:
            self.config(options.MANAGEMENT_MONGODB_GROUP,
                        database=uuid.uuid4().hex)


class MongodbUtilsTest(MongodbSetupMixin, testing.TestBase):

    config_file = 'wsgi_mongodb.conf'

    def setUp(self):
        super(MongodbUtilsTest, self).setUp()

        self.conf.register_opts(options.MESSAGE_MONGODB_OPTIONS,
                                group=options.MESSAGE_MONGODB_GROUP)

        self.mongodb_conf = self.conf[options.MESSAGE_MONGODB_GROUP]

        MockDriver = collections.namedtuple('MockDriver', 'mongodb_conf')

        self.driver = MockDriver(self.mongodb_conf)
        self.control_driver = MockDriver(self.mongodb_conf)

    def test_scope_queue_name(self):
        self.assertEqual(utils.scope_queue_name('my-q'), '/my-q')
        self.assertEqual(utils.scope_queue_name('my-q', None), '/my-q')
        self.assertEqual(utils.scope_queue_name('my-q', '123'), '123/my-q')

        self.assertEqual(utils.scope_queue_name(None), '/')
        self.assertEqual(utils.scope_queue_name(None, '123'), '123/')

    def test_descope_queue_name(self):
        self.assertEqual(utils.descope_queue_name('/'), None)
        self.assertEqual(utils.descope_queue_name('/some-pig'), 'some-pig')
        self.assertEqual(utils.descope_queue_name('radiant/some-pig'),
                         'some-pig')

    def test_calculate_backoff(self):
        sec = utils.calculate_backoff(0, 10, 2, 0)
        self.assertEqual(sec, 0)

        sec = utils.calculate_backoff(9, 10, 2, 0)
        self.assertEqual(sec, 1.8)

        sec = utils.calculate_backoff(4, 10, 2, 0)
        self.assertEqual(sec, 0.8)

        sec = utils.calculate_backoff(4, 10, 2, 1)
        if sec != 0.8:
            self.assertThat(sec, matchers.GreaterThan(0.8))
            self.assertThat(sec, matchers.LessThan(1.8))

        self.assertRaises(ValueError, utils.calculate_backoff, 0, 10, -2, -1)
        self.assertRaises(ValueError, utils.calculate_backoff, 0, 10, -2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, 0, 10, 2, -1)

        self.assertRaises(ValueError, utils.calculate_backoff, -2, -10, 2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, 2, -10, 2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, -2, 10, 2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, -1, 10, 2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, 10, 10, 2, 0)
        self.assertRaises(ValueError, utils.calculate_backoff, 11, 10, 2, 0)

    def test_retries_on_autoreconnect(self):
        num_calls = [0]

        @utils.retries_on_autoreconnect
        def _raises_autoreconnect(self):
            num_calls[0] += 1
            raise pymongo.errors.AutoReconnect()

        self.assertRaises(pymongo.errors.AutoReconnect,
                          _raises_autoreconnect, self)
        self.assertEqual(num_calls, [self.mongodb_conf.max_reconnect_attempts])

    def test_retries_on_autoreconnect_neg(self):
        num_calls = [0]

        @utils.retries_on_autoreconnect
        def _raises_autoreconnect(self):
            num_calls[0] += 1

            # NOTE(kgriffs): Don't exceed until the last attempt
            if num_calls[0] < self.mongodb_conf.max_reconnect_attempts:
                raise pymongo.errors.AutoReconnect()

        # NOTE(kgriffs): Test that this does *not* raise AutoReconnect
        _raises_autoreconnect(self)

        self.assertEqual(num_calls, [self.mongodb_conf.max_reconnect_attempts])


@testing.requires_mongodb
class MongodbDriverTest(MongodbSetupMixin, testing.TestBase):

    config_file = 'wsgi_mongodb.conf'

    def setUp(self):
        super(MongodbDriverTest, self).setUp()

        self.conf.register_opts(bootstrap._GENERAL_OPTIONS)
        self.config(unreliable=False)

    def test_db_instance(self):
        self.config(unreliable=True)
        cache = oslo_cache.get_cache()
        control = mongodb.ControlDriver(self.conf, cache)
        data = mongodb.DataDriver(self.conf, cache, control)

        for db in data.message_databases:
            self.assertThat(db.name, matchers.StartsWith(
                data.mongodb_conf.database))

    def test_version_match(self):
        self.config(unreliable=True)
        cache = oslo_cache.get_cache()

        with mock.patch('pymongo.MongoClient.server_info') as info:
            info.return_value = {'version': '2.1'}
            self.assertRaises(RuntimeError, mongodb.DataDriver,
                              self.conf, cache,
                              mongodb.ControlDriver(self.conf, cache))

            info.return_value = {'version': '2.11'}

            try:
                mongodb.DataDriver(self.conf, cache,
                                   mongodb.ControlDriver(self.conf, cache))
            except RuntimeError:
                self.fail('version match failed')

    def test_replicaset_or_mongos_needed(self):
        cache = oslo_cache.get_cache()

        with mock.patch('pymongo.MongoClient.nodes') as nodes:
            nodes.__get__ = mock.Mock(return_value=[])
            with mock.patch('pymongo.MongoClient.is_mongos') as is_mongos:
                is_mongos.__get__ = mock.Mock(return_value=False)
                self.assertRaises(RuntimeError, mongodb.DataDriver,
                                  self.conf, cache,
                                  mongodb.ControlDriver(self.conf, cache))

    def test_using_replset(self):
        cache = oslo_cache.get_cache()

        with mock.patch('pymongo.MongoClient.nodes') as nodes:
            nodes.__get__ = mock.Mock(return_value=['node1', 'node2'])

            with mock.patch('pymongo.MongoClient.write_concern') as wc:
                write_concern = pymongo.WriteConcern(w=2)
                wc.__get__ = mock.Mock(return_value=write_concern)
                mongodb.DataDriver(self.conf, cache,
                                   mongodb.ControlDriver(self.conf, cache))

    def test_using_mongos(self):
        cache = oslo_cache.get_cache()

        with mock.patch('pymongo.MongoClient.is_mongos') as is_mongos:
            is_mongos.__get__ = mock.Mock(return_value=True)

            with mock.patch('pymongo.MongoClient.write_concern') as wc:
                write_concern = pymongo.WriteConcern(w=2)
                wc.__get__ = mock.Mock(return_value=write_concern)
                mongodb.DataDriver(self.conf, cache,
                                   mongodb.ControlDriver(self.conf, cache))

    def test_write_concern_check_works(self):
        cache = oslo_cache.get_cache()

        with mock.patch('pymongo.MongoClient.is_mongos') as is_mongos:
            is_mongos.__get__ = mock.Mock(return_value=True)

            with mock.patch('pymongo.MongoClient.write_concern') as wc:
                write_concern = pymongo.WriteConcern(w=1)
                wc.__get__ = mock.Mock(return_value=write_concern)
                self.assertRaises(RuntimeError, mongodb.DataDriver,
                                  self.conf, cache,
                                  mongodb.ControlDriver(self.conf, cache))

                write_concern = pymongo.WriteConcern(w=2)
                wc.__get__ = mock.Mock(return_value=write_concern)
                mongodb.DataDriver(self.conf, cache,
                                   mongodb.ControlDriver(self.conf, cache))

    def test_write_concern_is_set(self):
        cache = oslo_cache.get_cache()

        with mock.patch('pymongo.MongoClient.is_mongos') as is_mongos:
            is_mongos.__get__ = mock.Mock(return_value=True)
            self.config(unreliable=True)
            driver = mongodb.DataDriver(self.conf, cache,
                                        mongodb.ControlDriver
                                        (self.conf, cache))

            driver.server_version = (2, 6)

            for db in driver.message_databases:
                wc = db.write_concern

                self.assertEqual(wc.document['w'], 'majority')
                self.assertEqual(wc.document['j'], False)


@testing.requires_mongodb
class MongodbQueueTests(MongodbSetupMixin, base.QueueControllerTest):

    driver_class = mongodb.ControlDriver
    config_file = 'wsgi_mongodb.conf'
    controller_class = controllers.QueueController
    control_driver_class = mongodb.ControlDriver

    def test_indexes(self):
        collection = self.controller._collection
        indexes = collection.index_information()
        self.assertIn('p_q_1', indexes)

    def test_raises_connection_error(self):

        with mock.patch.object(cursor.Cursor,
                               'next' if six.PY2 else '__next__',
                               autospec=True) as method:
            error = pymongo.errors.ConnectionFailure()
            method.side_effect = error

            queues = next(self.controller.list())
            self.assertRaises(storage.errors.ConnectionError,
                              queues.next)


@testing.requires_mongodb
class MongodbMessageTests(MongodbSetupMixin, base.MessageControllerTest):

    driver_class = mongodb.DataDriver
    config_file = 'wsgi_mongodb.conf'
    controller_class = controllers.MessageController
    control_driver_class = mongodb.ControlDriver

    # NOTE(kgriffs): MongoDB's TTL scavenger only runs once a minute
    gc_interval = 60

    def test_indexes(self):
        for collection in self.controller._collections:
            indexes = collection.index_information()
            self.assertIn('active', indexes)
            self.assertIn('claimed', indexes)
            self.assertIn('queue_marker', indexes)
            self.assertIn('counting', indexes)

    def test_message_counter(self):
        queue_name = self.queue_name
        iterations = 10

        seed_marker1 = self.queue_controller._get_counter(queue_name,
                                                          self.project)
        self.assertEqual(seed_marker1, 1, 'First marker is 1')

        for i in range(iterations):
            self.controller.post(queue_name, [{'ttl': 60}],
                                 'uuid', project=self.project)

            marker1 = self.queue_controller._get_counter(queue_name,
                                                         self.project)
            marker2 = self.queue_controller._get_counter(queue_name,
                                                         self.project)
            marker3 = self.queue_controller._get_counter(queue_name,
                                                         self.project)

            self.assertEqual(marker1, marker2)
            self.assertEqual(marker2, marker3)
            self.assertEqual(marker1, i + 2)

        new_value = self.queue_controller._inc_counter(queue_name,
                                                       self.project)
        self.assertIsNotNone(new_value)

        value_before = self.queue_controller._get_counter(queue_name,
                                                          project=self.project)
        new_value = self.queue_controller._inc_counter(queue_name,
                                                       project=self.project)
        self.assertIsNotNone(new_value)
        value_after = self.queue_controller._get_counter(queue_name,
                                                         project=self.project)
        self.assertEqual(value_after, value_before + 1)

        value_before = value_after
        new_value = self.queue_controller._inc_counter(queue_name,
                                                       project=self.project,
                                                       amount=7)
        value_after = self.queue_controller._get_counter(queue_name,
                                                         project=self.project)
        self.assertEqual(value_after, value_before + 7)
        self.assertEqual(value_after, new_value)

        reference_value = value_after

        unchanged = self.queue_controller._inc_counter(queue_name,
                                                       project=self.project,
                                                       window=10)
        self.assertIsNone(unchanged)

        timeutils.set_time_override()
        timeutils.advance_time_delta(datetime.timedelta(seconds=10))

        changed = self.queue_controller._inc_counter(queue_name,
                                                     project=self.project,
                                                     window=5)
        self.assertEqual(changed, reference_value + 1)

        timeutils.clear_time_override()


@testing.requires_mongodb
class MongodbFIFOMessageTests(MongodbSetupMixin, base.MessageControllerTest):

    driver_class = mongodb.FIFODataDriver
    config_file = 'wsgi_fifo_mongodb.conf'
    controller_class = controllers.FIFOMessageController
    control_driver_class = mongodb.ControlDriver

    # NOTE(kgriffs): MongoDB's TTL scavenger only runs once a minute
    gc_interval = 60

    def test_race_condition_on_post(self):
        queue_name = self.queue_name

        expected_messages = [
            {
                'ttl': 60,
                'body': {
                    'event': 'BackupStarted',
                    'backupId': 'c378813c-3f0b-11e2-ad92-7823d2b0f3ce',
                },
            },
            {
                'ttl': 60,
                'body': {
                    'event': 'BackupStarted',
                    'backupId': 'd378813c-3f0b-11e2-ad92-7823d2b0f3ce',
                },
            },
            {
                'ttl': 60,
                'body': {
                    'event': 'BackupStarted',
                    'backupId': 'e378813c-3f0b-11e2-ad92-7823d2b0f3ce',
                },
            },
        ]

        uuid = '97b64000-2526-11e3-b088-d85c1300734c'

        # NOTE(kgriffs): Patch _inc_counter so it is a noop, so that
        # the second time we post, we will get a collision. This simulates
        # what happens when we have parallel requests and the "winning"
        # requests hasn't gotten around to calling _inc_counter before the
        # "losing" request attempts to insert it's batch of messages.
        with mock.patch.object(mongodb.queues.QueueController,
                               '_inc_counter', autospec=True) as method:

            method.return_value = 2
            messages = expected_messages[:1]
            created = list(self.controller.post(queue_name, messages,
                                                uuid, project=self.project))
            self.assertEqual(len(created), 1)

            # Force infinite retries
            method.return_value = None

            with testing.expect(errors.MessageConflict):
                self.controller.post(queue_name, messages,
                                     uuid, project=self.project)

        created = list(self.controller.post(queue_name,
                                            expected_messages[1:],
                                            uuid, project=self.project))

        self.assertEqual(len(created), 2)

        expected_ids = [m['body']['backupId'] for m in expected_messages]

        interaction = self.controller.list(queue_name, client_uuid=uuid,
                                           echo=True, project=self.project)

        actual_messages = list(next(interaction))
        self.assertEqual(len(actual_messages), len(expected_messages))
        actual_ids = [m['body']['backupId'] for m in actual_messages]

        self.assertEqual(actual_ids, expected_ids)


@testing.requires_mongodb
class MongodbClaimTests(MongodbSetupMixin, base.ClaimControllerTest):

    driver_class = mongodb.DataDriver
    config_file = 'wsgi_mongodb.conf'
    controller_class = controllers.ClaimController
    control_driver_class = mongodb.ControlDriver

    def test_claim_doesnt_exist(self):
        """Verifies that operations fail on expired/missing claims.

        Methods should raise an exception when the claim doesn't
        exists and/or has expired.
        """
        epoch = '000000000000000000000000'
        self.assertRaises(storage.errors.ClaimDoesNotExist,
                          self.controller.get, self.queue_name,
                          epoch, project=self.project)

        claim_id, messages = self.controller.create(self.queue_name,
                                                    {'ttl': 1, 'grace': 0},
                                                    project=self.project)

        # Lets let it expire
        time.sleep(1)
        self.assertRaises(storage.errors.ClaimDoesNotExist,
                          self.controller.update, self.queue_name,
                          claim_id, {'ttl': 1, 'grace': 0},
                          project=self.project)

        self.assertRaises(storage.errors.ClaimDoesNotExist,
                          self.controller.update, self.queue_name,
                          claim_id, {'ttl': 1, 'grace': 0},
                          project=self.project)


@testing.requires_mongodb
class MongodbSubscriptionTests(MongodbSetupMixin,
                               base.SubscriptionControllerTest):
    driver_class = mongodb.DataDriver
    config_file = 'wsgi_mongodb.conf'
    controller_class = controllers.SubscriptionController
    control_driver_class = mongodb.ControlDriver


#
# TODO(kgriffs): Do these need database purges as well as those above?
#

@testing.requires_mongodb
class MongodbPoolsTests(base.PoolsControllerTest):
    config_file = 'wsgi_mongodb.conf'
    driver_class = mongodb.ControlDriver
    controller_class = controllers.PoolsController
    control_driver_class = mongodb.ControlDriver

    def setUp(self):
        super(MongodbPoolsTests, self).setUp()

    def tearDown(self):
        super(MongodbPoolsTests, self).tearDown()

    def test_delete_pool_used_by_flavor(self):
        self.flavors_controller.create('durable', self.pool_group,
                                       project=self.project,
                                       capabilities={})

        with testing.expect(errors.PoolInUseByFlavor):
            self.pools_controller.delete(self.pool)

    def test_mismatching_capabilities_fifo(self):
        with testing.expect(errors.PoolCapabilitiesMismatch):
            self.pools_controller.create(str(uuid.uuid1()),
                                         100, 'mongodb.fifo://localhost',
                                         group=self.pool_group,
                                         options={})


@testing.requires_mongodb
class MongodbCatalogueTests(base.CatalogueControllerTest):
    driver_class = mongodb.ControlDriver
    controller_class = controllers.CatalogueController
    control_driver_class = mongodb.ControlDriver

    def setUp(self):
        super(MongodbCatalogueTests, self).setUp()
        self.load_conf('wsgi_mongodb.conf')

    def tearDown(self):
        self.controller.drop_all()
        super(MongodbCatalogueTests, self).tearDown()


@testing.requires_mongodb
class PooledMessageTests(base.MessageControllerTest):
    config_file = 'wsgi_mongodb_pooled.conf'
    controller_class = pooling.MessageController
    driver_class = pooling.DataDriver
    control_driver_class = mongodb.ControlDriver
    controller_base_class = storage.Message

    # NOTE(kgriffs): MongoDB's TTL scavenger only runs once a minute
    gc_interval = 60


@testing.requires_mongodb
class PooledClaimsTests(base.ClaimControllerTest):
    config_file = 'wsgi_mongodb_pooled.conf'
    controller_class = pooling.ClaimController
    driver_class = pooling.DataDriver
    control_driver_class = mongodb.ControlDriver
    controller_base_class = storage.Claim

    def test_delete_message_expired_claim(self):
        # NOTE(flaper87): The pool tests uses sqlalchemy
        # as one of the pools, which causes this test to fail.
        # Several reasons to do this:
        # The sqla driver is deprecated
        # It's not optimized
        # mocking utcnow mocks the driver too, which
        # requires to put sleeps in the test
        self.skip("Fix sqlalchemy driver")


@testing.requires_mongodb
class MongodbFlavorsTest(base.FlavorsControllerTest):
    driver_class = mongodb.ControlDriver
    controller_class = controllers.FlavorsController
    control_driver_class = mongodb.ControlDriver

    def setUp(self):
        super(MongodbFlavorsTest, self).setUp()
        self.load_conf('wsgi_mongodb.conf')
        self.addCleanup(self.controller.drop_all)
