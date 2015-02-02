# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import random

from senlin.common import exception
from senlin.common.i18n import _
from senlin.common.i18n import _LE
from senlin.db import api as db_api
from senlin.engine.actions import base
from senlin.engine import cluster as clusterm
from senlin.engine import dispatcher
from senlin.engine import node as nodes
from senlin.engine import scheduler
from senlin.openstack.common import log as logging
from senlin.policies import base as policies

LOG = logging.getLogger(__name__)


class ClusterAction(base.Action):
    '''An action performed on a cluster.'''

    ACTIONS = (
        CLUSTER_CREATE, CLUSTER_DELETE, CLUSTER_UPDATE,
        CLUSTER_ADD_NODES, CLUSTER_DEL_NODES,
        CLUSTER_SCALE_UP, CLUSTER_SCALE_DOWN,
        CLUSTER_ATTACH_POLICY, CLUSTER_DETACH_POLICY,
    ) = (
        'CLUSTER_CREATE', 'CLUSTER_DELETE', 'CLUSTER_UPDATE',
        'CLUSTER_ADD_NODES', 'CLUSTER_DEL_NODES',
        'CLUSTER_SCALE_UP', 'CLUSTER_SCALE_DOWN',
        'CLUSTER_ATTACH_POLICY', 'CLUSTER_DETACH_POLICY',
    )

    def __init__(self, context, action, **kwargs):
        super(ClusterAction, self).__init__(context, action, **kwargs)

    def _query_cluster_lock(self, cluster, enforce=False):
        """ Try to lock the cluster

            :param enforce: whether to cancel current actoin on the cluster
                            and get the lock
        """
        # Try lock the cluster
        action_id = db_api.cluster_lock_create(cluster.id, self.id)
        if action_id and action_id != self.id:
            # Cluster has been locked by other action
            if enforce:
                # try to cancel the action that owns the lock
                scheduler.cancel_action(self.context, action_id)

                # Sleep until this action get the lock or timeout
                action_id = db_api.cluster_lock_create(cluster.id, self.id)
                while action_id and action_id != self.id:
                    if scheduler.action_timeout(self):
                        # Action timeout, set cluster status to ERROR
                        LOG.debug('Cluster deletion %s timeout' % self.id)
                        cluster.set_status(self.context, cluster.ERROR,
                                           'Cluster deletion timeout')
                        return self.RES_TIMEOUT

                    scheduler.reschedule(self)
                    action_id = db_api.cluster_lock_create(cluster.id, self.id)
            else:
                # Return
                msg = _('Cluster is already locked by action %(old)s, action '
                        '%(new)s failed grabbing the lock') % {
                            'old': action_id, 'new': self.id}
                LOG.warn(msg)
                # TODO(Qiming): Decide how to handle this situation
                return False

        return True

    def _release_cluster_lock(self, cluster):
        db_api.cluster_lock_release(cluster.id, self.id)

    def _wait_for_action(self):
        while self.get_status() != self.READY:
            if scheduler.action_cancelled(self):
                # During this period, if cancel request come, cancel this
                # cluster operation immediately, then release the cluster
                # lock and return.
                LOG.debug(_('%(action)s %(id)s cancelled') % {
                    'action': self.action, 'id': self.id})
                return self.RES_CANCEL
            elif scheduler.action_timeout(self):
                # Action timeout, return
                LOG.debug(_('%(action)s %(id)s timeout') % {
                    'action': self.action, 'id': self.id})
                return self.RES_TIMEOUT

            # Continue waiting (with reschedule)
            scheduler.reschedule(self)

        return self.RES_OK

    def do_create(self, cluster):
        res = cluster.do_create(self.context)
        if not res:
            cluster.set_status(cluster.ERROR, 'Cluster creation failed')
            return self.RES_ERROR

        for m in range(cluster.size):
            name = 'node-%003d' % m
            node = nodes.Node(name, cluster.profile_id, cluster.id)
            node.store(self.context)
            kwargs = {
                'name': 'node_create_%s' % node.id[:8],
                'context': self.context,
                'target': node.id,
                'cause': 'Cluster creation',
            }

            action = base.Action(self.context, 'NODE_CREATE', **kwargs)
            action.store(self.context)

            # Build dependency and make the new action ready
            db_api.action_add_dependency(action.id, self.id)
            action.set_status(self.READY)

            dispatcher.notify(self.context, dispatcher.Dispatcher.NEW_ACTION,
                              None, action_id=action.id)

        # Wait for cluster creating complete
        result = self.RES_OK
        if cluster.size > 0:
            result = self._wait_for_action()

        if result == self.RES_OK:
            cluster.set_status(self.context, cluster.ACTIVE,
                               'Cluster creation completed')
        elif result == self.RES_CANCEL:
            cluster.set_status(self.context, cluster.ERROR,
                               'Cluster creation cancelled')
        elif result == self.RES_TIMEOUT:
            cluster.set_status(self.context, cluster.ERROR,
                               'Cluster creation timeout')
        else:
            # RETRY
            pass

        return result

    def do_update(self, cluster, new_profile_id):
        res = cluster.do_update(self.context, profile_id=new_profile_id)
        if not res:
            cluster.set_status(cluster.ACTIVE,
                               'Cluster updating was not executed')
            return self.RES_ERROR

        # Create NodeActions for all nodes
        node_list = cluster.get_nodes()
        for node_id in node_list:
            kwargs = {
                'name': 'node_update_%s' % node_id[:8],
                'context': self.context,
                'target': node_id,
                'cause': 'Cluster update',
                'inputs': {
                    'new_profile_id': new_profile_id,
                }
            }
            action = base.Action(self.context, 'NODE_UPDATE', **kwargs)
            action.store(self.context)

            db_api.action_add_dependency(action, self)
            action.set_status(self.READY)
            dispatcher.notify(self.context, dispatcher.Dispatcher.NEW_ACTION,
                              None, action_id=action.id)

        # Wait for cluster updating complete
        result = self.RES_OK
        if cluster.size > 0:
            result = self._wait_for_action()

        if result == self.RES_OK:
            cluster.set_status(self.context, cluster.ACTIVE,
                               'Cluster update completed')

        return self.RES_OK

    def do_delete(self, cluster):
        cluster.set_status(self.context, cluster.DELETING)
        node_list = cluster.get_nodes()
        for node_id in node_list:
            kwargs = {
                'name': 'node_delete_%s' % node_id[:8],
                'context': self.context,
                'target': node_id,
                'cause': 'Cluster deletion',
            }
            action = base.Action(self.context, 'NODE_DELETE', **kwargs)
            action.store(self.context)

            # Build dependency and make the new action ready
            db_api.action_add_dependency(action.id, self)
            action.set_status(self.READY)

            dispatcher.notify(self.context, dispatcher.Dispatcher.NEW_ACTION,
                              None, action_id=action.id)

        result = self.RES_OK
        if cluster.size > 0:
            result = self._wait_for_action()

        if result == self.RES_OK:
            result = cluster.do_delete(self.context)

        if result == self.RES_OK:
            cluster.set_status(self.context, cluster.DELETED,
                               'Cluster deletion completed')
        elif result == self.RES_CANCEL:
            cluster.set_status(self.context, cluster.ERROR,
                               'Cluster deletion cancelled')
        elif result == self.RES_TIMEOUT:
            cluster.set_status(self.context, cluster.ERROR,
                               'Cluster deletion timeout')
        else:
            # RETRY
            pass

        return self.RES_OK

    def do_add_nodes(self, cluster):
        return self.RES_OK

    def do_del_nodes(self, cluster):
        return self.RES_OK

    def do_scale_up(self, cluster):
        return self.RES_OK

    def do_scale_down(self, cluster, count, candidates):
        # Go through all policies before scaling down.
        if len(candidates) == 0:
            # No candidates for scaling down op which means no DeletionPolicy
            # is attached to cluster, we just choose random nodes to
            # delete based on scaling policy result.
            nodes = db_api.node_get_all_by_cluster(self.context,
                                                   self.cluster_id)
            # TODO(anyone): add some warning here
            if count > len(nodes):
                count = len(nodes)

            i = count
            while i > 0:
                rand = random.randrange(i)
                candidates.append(nodes[rand].id)
                nodes.remove(nodes[rand])
                i = i - 1

        action_list = []
        for node_id in candidates:
            kwargs = {
                'name': 'node-delete-%s' % node_id,
                'context': self.context,
                'target': node_id,
                'cause': 'Cluster scale down',
            }
            action = base.Action(self.context, 'NODE_DELETE', **kwargs)
            action.store(self.context)

            action_list.append(action.id)
            db_api.action_add_dependency(action, self)
            action.set_status(self.READY)

        # Notify dispatcher
        for action_id in action_list:
            dispatcher.notify(self.context,
                              dispatcher.Dispatcher.NEW_ACTION,
                              None,
                              action_id=action_id)

        # Wait for cluster creating complete. If timeout,
        # set cluster status to error.
        # Note: we don't allow to cancel scaling operations.
        while self.get_status() != self.READY:
            if scheduler.action_timeout(self):
                # Action timeout, set cluster status to ERROR and return
                LOG.debug('Cluster scale_down action %s timeout' % self.id)
                cluster.set_status(cluster.ERROR,
                                   'Cluster scaling down timeout')
                return self.RES_TIMEOUT

            # Continue waiting (with sleep)
            scheduler.reschedule(self)

        cluster.delete_nodes(candidates)

        # set cluster status to OK
        return self.RES_OK

    def do_attach_policy(self, cluster):
        policy_id = self.inputs.get('policy_id', None)
        if not policy_id:
            raise exception.PolicyNotSpecified()

        policy = policies.Policy.load(self.context, policy_id)
        # Check if policy has already been attached
        all = db_api.cluster_get_policies(self.context, cluster.id)
        for existing in all:
            # Policy already attached
            if existing.policy_id == policy_id:
                return self.RES_OK

            # Detect policy type conflicts
            curr = policies.Policy.load(self.context, existing.policy_id)
            if curr.type == policy.type:
                raise exception.PolicyExists(policy_type=policy.type)

        values = {
            'cooldown': self.inputs.get('cooldown', policy.cooldown),
            'level': self.inputs.get('level', policy.level),
            'enabled': self.inputs.get('enabled', True),
        }

        db_api.cluster_attach_policy(self.context, cluster.id, policy_id,
                                     values)

        cluster.rt.policies.append(policy)
        return self.RES_OK

    def do_detach_policy(self, cluster):
        return self.RES_OK

    def _execute(self, cluster):
        # do pre-action policy checking
        check_result = self.policy_check(cluster.id, 'BEFORE')
        if not check_result:
            # Don't emit message here since policy_check should have done it
            return self.RES_ERROR

        res = self.OK
        if self.action == self.CLUSTER_CREATE:
            res = self.do_create(cluster)
        elif self.action == self.CLUSTER_UPDATE:
            new_profile_id = self.inputs.get('new_profile_id')
            # TODO(anyone): check null?
            res = self.do_update(cluster, new_profile_id)
        elif self.action == self.CLUSTER_DELETE:
            res = self.do_delete(cluster)
        elif self.action == self.CLUSTER_ADD_NODES:
            res = self.do_add_nodes(cluster)
        elif self.action == self.CLUSTER_DEL_NODES:
            res = self.do_del_nodes(cluster)
        elif self.action == self.CLUSTER_SCALE_UP:
            res = self.do_scale_up(cluster)
        elif self.action == self.CLUSTER_SCALE_DOWN:
            count = check_result.get('count', 0)
            if count == 0:
                # We assume the policy has emit error/warnings
                return self.RES_ERROR
            candidates = check_result.get('candidates', [])
            res = self.do_scale_down(cluster, count, candidates)
        elif self.action == self.CLUSTER_ATTACH_POLICY:
            res = self.do_attach_policy(cluster)
        elif self.action == self.CLUSTER_DETACH_POLICY:
            res = self.do_detach_policy(cluster)

        # do post-action policy checking
        if res == self.RES_OK:
            check_result = self.policy_check(cluster.id, 'AFTER')
            res = self.RES_OK if check_result else self.ERROR

        return res

    def execute(self, **kwargs):
        try:
            cluster = clusterm.Cluster.load(self.context, self.target)
        except exception.NotFound:
            LOG.error(_LE('Cluster %(name)s [%(id)s] not found') % {
                'name': cluster.name, 'id': cluster.id})
            return self.RES_ERROR

        # Check if this is an enforced action to do
        if 'enforce' in kwargs:
            lock_enforce = kwargs.get('enforce')
        elif self.action in [self.CLUSTER_DELETE]:
            # TODO: add more actions which default
            # need to enforce
            lock_enforce = True
        else:
            lock_enforce = False

        # Check if this is an action should retry
        if 'retry' in kwargs:
            retry = kwargs.get('retry')
        elif self.action in [self.CLUSTER_UPDATE,
                             self.CLUSTER_SCALE_DOWN]:
            # TODO: add more actions which default
            # support retry
            retry = True
        else:
            retry = False

        # Try to lock cluster before do real action
        res = self._query_cluster_lock(cluster, lock_enforce)
        if not res:
            if retry:
                return self.RES_RETRY
            else:
                return self.ERROR
        elif res == self.TIMEOUT:
            return self.TIMEOUT
        else:
            res = self.RES_OK

        # Do real operation here
        res = self._execute(cluster)

        # We've done, release cluster lock
        self._release_cluster_lock(cluster)
        return res

    def cancel(self):
        return self.RES_OK
