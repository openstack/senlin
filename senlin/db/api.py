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

'''
Interface for database access.

SQLAlchemy is currently the only supported backend.
'''

from oslo.config import cfg
from oslo.db import api

CONF = cfg.CONF


_BACKEND_MAPPING = {'sqlalchemy': 'senlin.db.sqlalchemy.api'}

IMPL = api.DBAPI.from_config(CONF, backend_mapping=_BACKEND_MAPPING)


def get_engine():
    return IMPL.get_engine()


def get_session():
    return IMPL.get_session()


# Clusters
def cluster_create(context, values):
    return IMPL.cluster_create(context, values)


def cluster_get(context, cluster_id, show_deleted=False, tenant_safe=True):
    return IMPL.cluster_get(context, cluster_id, show_deleted=show_deleted,
                            tenant_safe=tenant_safe)


def cluster_get_by_name(context, cluster_name):
    return IMPL.cluster_get_by_name(context, cluster_name)


def cluster_get_by_name_and_parent(context, cluster_name, parent):
    return IMPL.cluster_get_by_name_and_parent(context, cluster_name, parent)


def cluster_get_all(context, limit=None, sort_keys=None, marker=None,
                    sort_dir=None, filters=None, tenant_safe=True,
                    show_deleted=False, show_nested=False):
    return IMPL.cluster_get_all(context, limit, sort_keys,
                                marker, sort_dir, filters, tenant_safe,
                                show_deleted, show_nested)


def cluster_get_all_by_parent(context, parent):
    return IMPL.cluster_get_all_by_parent(context, parent)


def cluster_count_all(context, filters=None, tenant_safe=True,
                      show_deleted=False, show_nested=False):
    return IMPL.cluster_count_all(context, filters=filters,
                                  tenant_safe=tenant_safe,
                                  show_deleted=show_deleted,
                                  show_nested=show_nested)


def cluster_update(context, cluster_id, values):
    return IMPL.cluster_update(context, cluster_id, values)


def cluster_delete(context, cluster_id):
    return IMPL.cluster_delete(context, cluster_id)


# Nodes
def node_create(context, values):
    return IMPL.node_create(context, values)


def node_get(context, node_id):
    return IMPL.node_get(context, node_id)


def node_get_all(context):
    return IMPL.node_get_all(context)


def node_get_all_by_cluster(context, cluster_id):
    return IMPL.node_get_all_by_cluster(context, cluster_id)


def node_get_by_name_and_cluster(context, node_name, cluster_id):
    return IMPL.node_get_by_name_and_cluster(context,
                                             node_name, cluster_id)


def node_get_by_physical_id(context, physical_id):
    return IMPL.node_get_by_physical_id(context, physical_id)


def node_set_status(context, node_id, status):
    # TODO(Qiming): Update nodes in cluster table; set status to
    #               ACTIVE if all created.
    return IMPL.node_set_status(context, node_id, status)


# Locks
def cluster_lock_create(cluster_id, worker_id):
    return IMPL.cluster_lock_create(cluster_id, worker_id)


def cluster_lock_steal(cluster_id, old_worker_id, new_worker_id):
    return IMPL.cluster_lock_steal(cluster_id, old_worker_id, new_worker_id)


def cluster_lock_release(cluster_id, worker_id):
    return IMPL.cluster_lock_release(cluster_id, worker_id)


def node_lock_create(node_id, worker_id):
    return IMPL.node_lock_create(node_id, worker_id)


def node_lock_steal(node_id, old_worker_id, new_worker_id):
    return IMPL.node_lock_steal(node_id, old_worker_id, new_worker_id)


def node_lock_release(node_id, worker_id):
    return IMPL.node_lock_release(node_id, worker_id)


# Policies
def policy_create(context, values):
    return IMPL.policy_create(context, values)


def policy_get(context, policy_id):
    return IMPL.policy_get(context, policy_id)


def policy_get_all(context):
    return IMPL.policy_get_all(context)


def policy_update(context, policy_id, values):
    return IMPL.policy_update(context, policy_id, values)


# Cluster-Policy Associations
def cluster_attach_policy(context, values):
    return IMPL.cluster_attach_policy(context, values)


def cluster_get_policies(context, cluster_id):
    return IMPL.cluster_get_policies(context, cluster_id)


def cluster_detach_policy(context, cluster_id, policy_id):
    return IMPL.cluster_detach_policy(context, cluster_id, policy_id)


def cluster_enable_policy(context, cluster_id, policy_id):
    return IMPL.cluster_get_policies(context, cluster_id, policy_id)


def cluster_disable_policy(context, cluster_id, policy_id):
    return IMPL.cluster_disable_policy(context, cluster_id, policy_id)


# Profiles
def profile_create(context, values):
    return IMPL.profile_create(context, values)


def profile_get(context, profile_id):
    return IMPL.profile_get(context, profile_id)


def profile_get_all(context):
    return IMPL.profile_get_all(context)


def profile_update(context, profile_id, values):
    return IMPL.profile_update(context, profile_id, values)


# Events
def event_create(context, values):
    return IMPL.event_create(context, values)


def event_get(context, event_id):
    return IMPL.event_get(context, event_id)


def event_get_all(context):
    return IMPL.event_get_all(context)


def event_get_all_by_cluster(context, cluster_id, limit=None, marker=None,
                             sort_keys=None, sort_dir=None, filters=None):
    return IMPL.event_get_all_by_cluster(context, cluster_id,
                                         limit=limit, marker=marker,
                                         sort_keys=sort_keys,
                                         sort_dir=sort_dir,
                                         filters=filters)

# Actions
def action_create(context, values):
    return IMPL.action_create(context, values)


def action_get(context, action_id):
    return IMPL.action_get(context, action_id)


def action_get_1st_ready(context):
    return IMPL.action_get_1st_ready(context)


def action_get_all_ready(context):
    return IMPL.action_get_all_ready(context)


def action_get_all_by_owner(context, owner):
    return IMPL.action_get_all_by_owner(context, owner)


def action_get_all(context):
    return IMPL.action_get_all(context)


def action_mark_complete(context, action_id):
    return IMPL.action_mark_complete(context, action_id)


def action_start_work_on(context, action_id, owner):
    return IMPL.action_start_work_on(context, action_id, owner)


def action_update(context, action_id, values):
    return IMPL.action_update(context, action_id, values)


def db_sync(engine, version=None):
    """Migrate the database to `version` or the most recent version."""
    return IMPL.db_sync(engine, version=version)


def db_version(engine):
    """Display the current database version."""
    return IMPL.db_version(engine)
