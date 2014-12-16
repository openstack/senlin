#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import functools
import json
import os

import eventlet
from oslo.config import cfg
from oslo import messaging
from oslo.serialization import jsonutils
from oslo.utils import timeutils
from osprofiler import profiler
import requests
import six
import warnings
import webob

from senlin.common import context
from senlin.common import exception
from senlin.common.i18n import _
from senlin.common.i18n import _LE
from senlin.common.i18n import _LI
from senlin.common.i18n import _LW
from senlin.common import identifier
from senlin.common import messaging as rpc_messaging
# Just for test, need be repalced with real db api implementation
from senlin.db import api_sim as db_api
from senlin.engine import action
from senlin.engine import cluster
from senlin.engine import member
from senlin.engine import profile
from senlin.openstack.common import log as logging
from senlin.openstack.common import service
from senlin.openstack.common import threadgroup
from senlin.openstack.common import uuidutils
from senlin.rpc import api as rpc_api

LOG = logging.getLogger(__name__)


def request_context(func):
    @functools.wraps(func)
    def wrapped(self, ctx, *args, **kwargs):
        if ctx is not None and not isinstance(ctx, context.RequestContext):
            ctx = context.RequestContext.from_dict(ctx.to_dict())
        try:
            return func(self, ctx, *args, **kwargs)
        except exception.HeatException:
            raise messaging.rpc.dispatcher.ExpectedException()
    return wrapped


class ThreadGroupManager(object):

    def __init__(self):
        super(ThreadGroupManager, self).__init__()
        self.groups = {}
        self.events = collections.defaultdict(list)

        # Create dummy service task, because when there is nothing queued
        # on self.tg the process exits
        self.add_timer(cfg.CONF.periodic_interval, self._service_task)

    def _service_task(self):
        """
        This is a dummy task which gets queued on the service.Service
        threadgroup.  Without this service.Service sees nothing running
        i.e has nothing to wait() on, so the process exits..
        This could also be used to trigger periodic non-cluster-specific
        housekeeping tasks
        """
        pass

    def _serialize_profile_info(self):
        prof = profiler.get()
        trace_info = None
        if prof:
            trace_info = {
                "hmac_key": prof.hmac_key,
                "base_id": prof.get_base_id(),
                "parent_id": prof.get_id()
            }
        return trace_info

    def _start_with_trace(self, trace, func, *args, **kwargs):
        if trace:
            profiler.init(**trace)
        return func(*args, **kwargs)

    def start(self, cluster_id, func, *args, **kwargs):
        """
        Run the given method in a sub-thread.
        """
        if cluster_id not in self.groups:
            self.groups[cluster_id] = threadgroup.ThreadGroup()
        return self.groups[cluster_id].add_thread(self._start_with_trace,
                                                self._serialize_profile_info(),
                                                func, *args, **kwargs)

    def start_with_lock(self, cnxt, cluster, engine_id, func, *args, **kwargs):
        """
        Try to acquire a cluster lock and, if successful, run the given
        method in a sub-thread.  Release the lock when the thread
        finishes.

        :param cnxt: RPC context
        :param cluster: Cluster to be operated on
        :type cluster: heat.engine.parser.Stack
        :param engine_id: The UUID of the engine/worker acquiring the lock
        :param func: Callable to be invoked in sub-thread
        :type func: function or instancemethod
        :param args: Args to be passed to func
        :param kwargs: Keyword-args to be passed to func.
        """
        lock = cluster_lock.ClusterLock(cnxt, cluster, engine_id)
        with lock.thread_lock(cluster.id):
            th = self.start_with_acquired_lock(cluster, lock,
                                               func, *args, **kwargs)
            return th

    def start_with_acquired_lock(self, cluster, lock, func, *args, **kwargs):
        """
        Run the given method in a sub-thread and release the provided lock
        when the thread finishes.

        :param cluster: Cluster to be operated on
        :type cluster: heat.engine.parser.Stack
        :param lock: The acquired cluster lock
        :type lock: heat.engine.cluster_lock.ClusterLock
        :param func: Callable to be invoked in sub-thread
        :type func: function or instancemethod
        :param args: Args to be passed to func
        :param kwargs: Keyword-args to be passed to func

        """
        def release(gt, *args):
            """
            Callback function that will be passed to GreenThread.link().
            """
            lock.release(*args)

        th = self.start(cluster.id, func, *args, **kwargs)
        th.link(release, cluster.id)
        return th

    def add_timer(self, cluster_id, func, *args, **kwargs):
        """
        Define a periodic task, to be run in a separate thread, in the cluster
        threadgroups.  Periodicity is cfg.CONF.periodic_interval
        """
        if cluster_id not in self.groups:
            self.groups[cluster_id] = threadgroup.ThreadGroup()
        self.groups[cluster_id].add_timer(cfg.CONF.periodic_interval,
                                        func, *args, **kwargs)

    def add_event(self, cluster_id, event):
        self.events[cluster_id].append(event)

    def remove_event(self, gt, cluster_id, event):
        for e in self.events.pop(cluster_id, []):
            if e is not event:
                self.add_event(cluster_id, e)

    def stop_timers(self, cluster_id):
        if cluster_id in self.groups:
            self.groups[cluster_id].stop_timers()

    def stop(self, cluster_id, graceful=False):
        '''Stop any active threads on a cluster.'''
        if cluster_id in self.groups:
            self.events.pop(cluster_id, None)
            threadgroup = self.groups.pop(cluster_id)
            threads = threadgroup.threads[:]

            threadgroup.stop(graceful)
            threadgroup.wait()

            # Wait for link()ed functions (i.e. lock release)
            links_done = dict((th, False) for th in threads)

            def mark_done(gt, th):
                links_done[th] = True

            for th in threads:
                th.link(mark_done, th)
            while not all(links_done.values()):
                eventlet.sleep()

    def send(self, cluster_id, message):
        for event in self.events.pop(cluster_id, []):
            event.send(message)


@profiler.trace_cls("rpc")
class EngineService(service.Service):
    """
    Manages the running instances from creation to destruction.
    All the methods in here are called from the RPC backend.  This is
    all done dynamically so if a call is made via RPC that does not
    have a corresponding method here, an exception will be thrown when
    it attempts to call into this class.  Arguments to these methods
    are also dynamically added and will be named as keyword arguments
    by the RPC caller.
    """

    RPC_API_VERSION = '1.2'

    def __init__(self, host, topic, manager=None):
        super(EngineService, self).__init__()
        resources.initialise()
        self.host = host
        self.topic = topic

        # The following are initialized here, but assigned in start() which
        # happens after the fork when spawning multiple worker processes
        self.engine_id = None
        self.thread_group_mgr = None
        self.target = None

        if cfg.CONF.instance_user:
            warnings.warn('The "instance_user" option in heat.conf is '
                          'deprecated and will be removed in the Juno '
                          'release.', DeprecationWarning)

        if cfg.CONF.trusts_delegated_roles:
            warnings.warn('The default value of "trusts_delegated_roles" '
                          'option in heat.conf is changed to [] in Kilo '
                          'and heat will delegate all roles of trustor. '
                          'Please keep the same if you do not want to '
                          'delegate subset roles when upgrading.',
                          Warning)

    def start(self):
        self.engine_id = cluster_lock.ClusterLock.generate_engine_id()
        self.thread_group_mgr = ThreadGroupManager()
        LOG.debug("Starting engine worker with engine_id %s" % self.engine_id)
        target = messaging.Target(
            version=self.RPC_API_VERSION, server=self.host,
            topic=self.topic)
        self.target = target
        server = rpc_messaging.get_rpc_server(target, self)
        server.start()

        super(EngineService, self).start()

    def stop(self):
        # Stop rpc connection at first for preventing new requests
        LOG.info(_LI("Attempting to stop engine service..."))
        try:
            self.conn.close()
        except Exception:
            pass

        # Wait for all active threads to be finished
        for cluster_id in self.thread_group_mgr.groups.keys():
            # Ignore dummy service task
            if cluster_id == cfg.CONF.periodic_interval:
                continue
            LOG.info(_LI("Waiting cluster %s processing to be finished"),
                     cluster_id)
            # Stop threads gracefully
            self.thread_group_mgr.stop(cluster_id, True)
            LOG.info(_LI("cluster %s processing was finished"), cluster_id)

        # Terminate the engine process
        LOG.info(_LI("All threads were gone, terminating engine"))
        super(EngineService, self).stop()

    @request_context
    def identify_cluster(self, cnxt, cluster_name):
        """
        The identify_cluster method returns the full cluster identifier for a
        single, live cluster given the cluster name.

        :param cnxt: RPC context.
        :param cluster_name: Name or UUID of the cluster to look up.
        """
        # Just for test
        if uuidutils.is_uuid_like(cluster_name):
            c = db_api.cluster_get(cnxt, cluster_name, show_deleted=True)
            # may be the name is in uuid format, so if get by id returns None,
            # we should get the info by name again
            if not c:
                c = db_api.cluster_get_by_name(cnxt, cluster_name)
        else:
            c = db_api.cluster_get_by_name(cnxt, cluster_name)
        if c:
            return dict(c['uuid'])
        else:
            raise exception.ClusterNotFound(cluster_name=cluster_name)

    def _get_cluster(self, cnxt, cluster_identity, show_deleted=False):
        identity = identifier.SenlinIdentifier(**cluster_identity)

        c = db_api.cluster_get(cnxt, identity.cluster_id,
                             show_deleted=show_deleted,
                             eager_load=True)

        if c is None:
            raise exception.ClusterNotFound(cluster_name=identity.cluster_name)

        if cnxt.tenant_id not in (identity.tenant, s.cluster_user_project_id):
            # The DB API should not allow this, but sanity-check anyway..
            raise exception.InvalidTenant(target=identity.tenant,
                                          actual=cnxt.tenant_id)

        if identity.path or s.name != identity.cluster_name:
            raise exception.ClusterNotFound(cluster_name=identity.cluster_name)

        return c

    @request_context
    def show_cluster(self, cnxt, cluster_identity):
        """
        Return detailed information about one or all clusters.

        :param cnxt: RPC context.
        :param cluster_identity: Name of the cluster you want to show, or None
            to show all
        """
        if cluster_identity is not None:
            clusters = self._get_cluster(cnxt, cluster_identity, show_deleted=True)
        else:
            clusters = db_api.cluster_get_all(cnxt)

        return clusters

    @request_context
    def list_clusters(self, cnxt, limit=None, marker=None, sort_keys=None,
                    sort_dir=None, filters=None, tenant_safe=True,
                    show_deleted=False, show_nested=False):
        """
        The list_clusters method returns attributes of all clusters.  It supports
        pagination (``limit`` and ``marker``), sorting (``sort_keys`` and
        ``sort_dir``) and filtering (``filters``) of the results.

        :param cnxt: RPC context
        :param limit: the number of clusters to list (integer or string)
        :param marker: the ID of the last item in the previous page
        :param sort_keys: an array of fields used to sort the list
        :param sort_dir: the direction of the sort ('asc' or 'desc')
        :param filters: a dict with attribute:value to filter the list
        :param tenant_safe: if true, scope the request by the current tenant
        :param show_deleted: if true, show soft-deleted clusters
        :param show_nested: if true, show nested clusters
        :returns: a list of formatted clusters
        """
        clusters = db_api.cluster_get_all(cnxt)
        return clusters

    @request_context
    def create_cluster(self, cnxt, cluster_name, size, profile,
                     owner_id=None, nested_depth=0, user_creds_id=None,
                     cluster_user_project_id=None):
        """
        The create_cluster method creates a new cluster using the template
        provided.
        Note that at this stage the template has already been fetched from the
        heat-api process if using a template-url.

        :param cnxt: RPC context.
        :param cluster_name: Name of the cluster you want to create.
        :param size: Size of cluster you want to create.
        :param profile: Profile used to create cluster nodes.
        :param owner_id: parent cluster ID for nested clusters, only expected when
                         called from another heat-engine (not a user option)
        :param nested_depth: the nested depth for nested clusters, only expected
                         when called from another heat-engine
        :param user_creds_id: the parent user_creds record for nested clusters
        :param cluster_user_project_id: the parent cluster_user_project_id for
                         nested clusters
        """
        LOG.info(_LI('Creating cluster %s'), cluster_name)
        cluster = cluster.Cluster(name, size, profile, **kwargs)
        action = ClusterAction(cnxt, cluster, 'CREATE', **kwargs)

        self.thread_group_mgr.start_with_lock(self.engine_id, action.execute)

        return cluster['uuid']

    @request_context
    def update_cluster(self, cnxt, cluster_identity, size, profile):
        """
        The update_cluster method updates an existing cluster based on the
        provided template and parameters.
        Note that at this stage the template has already been fetched from the
        heat-api process if using a template-url.

        :param cnxt: RPC context.
        :param cluster_identity: Name of the cluster you want to create.
        :param size: Size of cluster you want to create.
        :param profile: Profile used to create cluster nodes.
        """
        # Get the database representation of the existing cluster
        cluster = self._get_cluster(cnxt, cluster_identity)
        LOG.info(_LI('Updating cluster %s'), db_cluster.name)

        if cluster.action == cluster.SUSPEND:
            msg = _('Updating a cluster when it is suspended')
            raise exception.NotSupported(feature=msg)

        if cluster.action == cluster.DELETE:
            msg = _('Updating a cluster when it is deleting')
            raise exception.NotSupported(feature=msg)

        # Just update cluster definition for test.
        cluster.size = size
        cluster.profile_type = profile

        # TODO: need to do real cluster update job here,
        # e.g. invoke cluster.update with updated parameters
        #event = eventlet.event.Event()
        #th = self.thread_group_mgr.start_with_lock(cnxt, current_cluster,
        #                                           self.engine_id,
        #                                           current_cluster.update)
        #th.link(self.thread_group_mgr.remove_event, current_cluster.id, event)
        #self.thread_group_mgr.add_event(current_cluster.id, event)

        return cluster['uuid']

    @request_context
    def delete_cluster(self, cnxt, cluster_identity):
        """
        The delete_cluster method deletes a given cluster.

        :param cnxt: RPC context.
        :param cluster_identity: Name or ID of the cluster you want to delete.
        """

        cluster = self._get_cluster(cnxt, cluster_identity)
        LOG.info(_LI('Deleting cluster %s'), st.name)

        lock = cluster_lock.ClusterLock(cnxt, cluster, self.engine_id)
        with lock.try_thread_lock(cluster.id) as acquire_result:

            # Successfully acquired lock
            if acquire_result is None:
                self.thread_group_mgr.stop_timers(cluster.id)
                self.thread_group_mgr.start_with_acquired_lock(cluster, lock,
                                                               cluster.destroy)
                return

        # Current engine has the lock
        if acquire_result == self.engine_id:
            # give threads which are almost complete an opportunity to
            # finish naturally before force stopping them
            eventlet.sleep(0.2)
            self.thread_group_mgr.stop(cluster.id)

        # Another active engine has the lock
        # Doesn't suppot yet!
        # TODO: add multi-engine support

        # There may be additional resources that we don't know about
        # if an update was in-progress when the cluster was stopped, so
        # reload the cluster from the database.
        cluster = self._get_cluster(cnxt, cluster_identity)

        self.thread_group_mgr.start_with_lock(cnxt, cluster, self.engine_id,
                                              cluster.destroy)
        return None

    @request_context
    def cluster_suspend(self, cnxt, cluster_identity):
        '''
        Handle request to perform suspend action on a cluster
        '''
        def _cluster_suspend(cluster):
            LOG.debug("suspending cluster %s" % cluster.name)
            cluster.suspend()

        s = self._get_cluster(cnxt, cluster_identity)

        cluster = parser.Cluster.load(cnxt, cluster=s)
        self.thread_group_mgr.start_with_lock(cnxt, cluster, self.engine_id,
                                              _cluster_suspend, cluster)

    @request_context
    def cluster_resume(self, cnxt, cluster_identity):
        '''
        Handle request to perform a resume action on a cluster
        '''
        def _cluster_resume(cluster):
            LOG.debug("resuming cluster %s" % cluster.name)
            cluster.resume()

        s = self._get_cluster(cnxt, cluster_identity)

        cluster = parser.Cluster.load(cnxt, cluster=s)
        self.thread_group_mgr.start_with_lock(cnxt, cluster, self.engine_id,
                                              _cluster_resume, cluster)
