# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.

from cloudify import ctx
from cloudify import exceptions as cfy_exc
from cloudify.decorators import operation
from vcloud_plugin_common import with_vca_client, wait_for_task, get_vcloud_config
import collections
from network_plugin import check_ip, save_gateway_configuration


VCLOUD_NETWORK_NAME = 'vcloud_network_name'
ADD_POOL = 1
DELETE_POOL = 2


@operation
@with_vca_client
def create(vca_client, **kwargs):
    vdc_name = get_vcloud_config()['vdc']
    if ctx.node.properties['use_external_resource']:
        network_name = ctx.node.properties['resource_id']
        if not _is_network_exists(vca_client, vdc_name, network_name):
            cfy_exc.NonRecoverableError("Can't find external resource: {0}".format(network_name))
        ctx.instance.runtime_properties[VCLOUD_NETWORK_NAME] = network_name
        ctx.logger.info("External resource has been used")
        return
    net_prop = ctx.node.properties["network"]
    network_name = net_prop["name"]\
        if "name" in net_prop\
           else ctx.node.properties['resource_id']
    if network_name in _get_network_list(vca_client, get_vcloud_config()['vdc']):
        ctx.logger.info("Network {0} already exists".format(network_name))
        return
    ip = _split_adresses(net_prop['static_range'])
    gateway_name = net_prop['edge_gateway']
    start_address = check_ip(ip.start)
    end_address = check_ip(ip.end)
    gateway_ip = check_ip(net_prop["gateway_ip"])
    netmask = check_ip(net_prop["netmask"])
    dns1 = check_ip(net_prop["dns"]) if net_prop.get('dns') else ""
    dns2 = ""
    dns_suffix = net_prop.get("dns_suffix")
    success, result = vca_client.create_vdc_network(vdc_name, network_name, gateway_name, start_address,
                                                    end_address, gateway_ip, netmask,
                                                    dns1, dns2, dns_suffix)
    if success:
        ctx.logger.info("Network {0} has been successfully created."
                        .format(network_name))
    else:
        raise cfy_exc.NonRecoverableError(
            "Could not create network{0}: {1}".format(network_name, result))
    wait_for_task(vca_client, result)
    ctx.instance.runtime_properties[VCLOUD_NETWORK_NAME] = network_name
    _dhcp_operation(vca_client, network_name, ADD_POOL)


@operation
@with_vca_client
def delete(vca_client, **kwargs):
    if ctx.node.properties['use_external_resource'] is True:
        del ctx.instance.runtime_properties[VCLOUD_NETWORK_NAME]
        ctx.logger.info("Network was not deleted - external resource has"
                        " been used")
        return
    network_name = _get_network_name(ctx.node.properties)
    _dhcp_operation(vca_client, network_name, DELETE_POOL)
    success, task = vca_client.delete_vdc_network(get_vcloud_config()['vdc'], network_name)
    if success:
        ctx.logger.info("Network {0} has been successful deleted.".format(network_name))
    else:
        raise cfy_exc.NonRecoverableError(
            "Could not delete network {0}".format(network_name))
    wait_for_task(vca_client, task)


@operation
@with_vca_client
def creation_validation(vca_client, **kwargs):
    net_list = _get_network_list(vca_client, get_vcloud_config()['vdc'])
    network_name = _get_network_name(ctx.node.properties)
    if network_name in net_list:
        ctx.logger.info('Network {0} is available.'.format(network_name))
    else:
        ctx.logger.info('Network {0} is not available.'.format(network_name))


def _dhcp_operation(vca_client, network_name, operation):
    dhcp_settings = ctx.node.properties['network'].get('dhcp')
    if dhcp_settings is None:
        return
    gateway_name = ctx.node.properties["network"]['edge_gateway']
    gateway = vca_client.get_gateway(get_vcloud_config()['vdc'], gateway_name)
    if not gateway:
        raise cfy_exc.NonRecoverableError("Gateway {0} not found!".format(gateway_name))

    if operation == ADD_POOL:
        ip = _split_adresses(dhcp_settings['dhcp_range'])
        low_ip_address = check_ip(ip.start)
        hight_ip_address = check_ip(ip.end)
        default_lease = dhcp_settings.get('default_lease')
        max_lease = dhcp_settings.get('max_lease')
        gateway.add_dhcp_pool(network_name, low_ip_address, hight_ip_address,
                              default_lease, max_lease)
        ctx.logger.info("DHCP rule successful created for network {0}".format(network_name))
        error_message = "Could not add DHCP pool"

    if operation == DELETE_POOL:
        gateway.delete_dhcp_pool(network_name)
        ctx.logger.info("DHCP rule successful deleted for network {0}".format(network_name))
        error_message = "Could not delete DHCP pool"

    save_gateway_configuration(gateway, vca_client, error_message)


def _split_adresses(address_range):
    adresses = [ip.strip() for ip in address_range.split('-')]
    IPRange = collections.namedtuple('IPRange', 'start end')
    try:
        start = check_ip(adresses[0])
        end = check_ip(adresses[1])
        #NOTE(achirko) string comparison, doesn't work for ips
        #if start > end:
        #    raise cfy_exc.NonRecoverableError(
        #        "Start address {0} is greater than end address: {1}".format(start, end))
        return IPRange(start=start, end=end)
    except IndexError:
        raise cfy_exc.NonRecoverableError("Can't parse IP range:{0}".
                                          format(address_range))
    except ValueError:
        raise cfy_exc.NonRecoverableError(
            "Incorrect Ip addresses: {0}".format(address_range))


def _get_network_list(vca_client, vdc_name):
    vdc = vca_client.get_vdc(vdc_name)
    return [net.name for net in vdc.AvailableNetworks.Network]


def _get_network_name(properties):
    return properties["network"]["name"]\
        if "name" in properties["network"]\
           else properties['resource_id']


def _is_network_exists(vca_client, vdc_name, network_name):
    networks = vca_client.get_networks(vdc_name)
    return any([network_name == net.get_name() for net in networks])
