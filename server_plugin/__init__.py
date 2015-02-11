import requests
from pyvcloud.schema.vcd.v1_5.schemas.vcloud import taskType, vcloudType, vAppType
from xml.etree import ElementTree as ET
from pyvcloud.helper import CommonUtils

from vcloud_plugin_common import get_vcloud_config


class VAppOperations(object):
    STATUS_MAP = {
        -1 : "Could not be created",
        0 : "Unresolved",
        1 : "Resolved",
        2 : "Deployed",
        3 : "Suspended",
        4 : "Powered on",
        5 : "Waiting for user input",
        6 : "Unknown state",
        7 : "Unrecognized state",
        8 : "Powered off",
        9 : "Inconsistent state",
        10 : "Children do not all have the same status",
        11 : "Upload initiated, OVF descriptor pending",
        12 : "Upload initiated, copying contents",
        13 : "Upload initiated , disk contents pending",
        14 : "Upload has been quarantined",
        15 : "Upload quarantine period has expired"
        }
    def __init__(self, vcd, vapp):
        self.vcd = vcd
        self.vapp = vapp
        self.vcloud_config = get_vcloud_config()

    def get_status(self):
        return self.STATUS_MAP[self.vapp.me.get_status()]

    def details_of_vms(self):
        result = []
        children = self.vapp.me.get_Children()
        if children:
            vms = children.get_Vm()
            for vm in vms:
                name = vm.get_name()
                status = self.STATUS_MAP[vm.get_status()]
                owner = self.vapp.me.get_Owner().get_User().get_name()
                sections = vm.get_Section()
                virtualHardwareSection = filter(lambda section: section.__class__.__name__== "VirtualHardwareSection_Type", sections)[0]
                items = virtualHardwareSection.get_Item()
                cpu = filter(lambda item: item.get_Description().get_valueOf_() == "Number of Virtual CPUs", items)[0]
                cpu_capacity = cpu.get_ElementName().get_valueOf_().split(" virtual CPU(s)")[0]
                memory = filter(lambda item: item.get_Description().get_valueOf_() == "Memory Size", items)[0]
                memory_capacity = int(memory.get_ElementName().get_valueOf_().split(" MB of memory")[0]) / 1024
                operatingSystemSection = filter(lambda section: section.__class__.__name__== "OperatingSystemSection_Type", sections)[0]
                os = operatingSystemSection.get_Description().get_valueOf_()
                result.append(
                    {'name': name,
                     'status': status,
                     'cpus': cpu_capacity,
                     'memory': memory_capacity,
                     'os': os,
                     'owner': owner}
                )
        return result

    def _get_vms(self):
        children = self.vapp.me.get_Children()
        if children:
            return children.get_Vm()
        else:
            return []

    def get_vms_network_info(self):
        result = []
        vms = self._get_vms()
        for vm in vms:
            nw_connections = []
            sections = vm.get_Section()
            networkConnectionSection = filter(lambda section: section.__class__.__name__ == "NetworkConnectionSectionType", sections)[0]
            connections = networkConnectionSection.get_NetworkConnection()
            for connection in connections:
                nw_connections.append(
                    {'network_name': connection.get_network(),
                     'ip': connection.get_IpAddress(),
                     'mac': connection.get_MACAddress(),
                     'is_connected': connection.get_IsConnected()
                     })
            result.append(nw_connections)
        return result

    def _modify_networkConnectionSection(self, section, new_connection,
                                         primary_index=None):

        for networkConnection in section.get_NetworkConnection():
            if (networkConnection.get_network().lower() ==
                new_connection.get_network().lower()):
                return (False,
                        "VApp {0} is already connected to org vdc network {1}"
                        .format(self.vapp.name, networkConnection.get_network()))

        section.add_NetworkConnection(new_connection)
        if section.get_Info() is None:
            info = vcloudType.Msg_Type()
            info.set_valueOf_("Network connection")
            section.set_Info(info)
        if primary_index is not None:
            section.set_PrimaryNetworkConnectionIndex(primary_index)

    def _create_networkConnection(self, network_name, index, ip_allocation_mode,
                                  mac_address=None, ip_address=None):
        networkConnection = vcloudType.NetworkConnectionType()
        networkConnection.set_network(network_name)
        networkConnection.set_NetworkConnectionIndex(index)
        networkConnection.set_IpAddressAllocationMode(ip_allocation_mode)
        networkConnection.set_IsConnected(True)
        if ip_address and ip_allocation_mode == 'MANUAL':
            networkConnection.set_IpAddress(ip_address)
        if mac_address:
            networkConnection.set_MACAddress(mac_address)
        return networkConnection

    def connect_network(self, network_name, connection_index,
                        connections_primary_index=None,
                        ip_allocation_mode='DHCP', mac_address=None,
                        ip_address=None):
        section, link = self._get_network_connection_data()
        new_connection = self._create_networkConnection(
            network_name, connection_index, ip_allocation_mode, mac_address,
            ip_address)
        self._modify_networkConnectionSection(section,
                                              new_connection,
                                              connections_primary_index)

        body = self._create_request_body(
            section,
            'NetworkConnectionSection',
            'xmlns="http://www.vmware.com/vcloud/v1.5" '
            'xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"')

        response = requests.put(link.get_href(),
                                data=body,
                                headers=self.vapp.headers)
        if response.status_code == requests.codes.accepted:
            task = taskType.parseString(response.content, True)
            return True, task
        else:
            return False, response.content

    def disconnect_network(self, network_name):
        section, link = self._get_network_connection_data()

        found = None
        for index, nwConnection in enumerate(section.get_NetworkConnection()):
            if nwConnection.get_network() == network_name:
                found = index

        if found is None:
            return False, "Network {0} could not be found".format(network_name)
        else:
            section.NetworkConnection.pop(found)

            body = self._create_request_body(
                section,
                'NetworkConnectionSection',
                'xmlns="http://www.vmware.com/vcloud/v1.5" '
                'xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"')

            response = requests.put(link.get_href(),
                                    data=body,
                                    headers=self.vapp.headers)
            if response.status_code == requests.codes.accepted:
                task = taskType.parseString(response.content, True)
                return True, task
            else:
                return False, response.content

    def update_guest_customization(self, enabled=False,
                                   admin_password=None,
                                   computer_name=None,
                                   customization_script=None):
        if not (admin_password or computer_name or customization_script):
            enabled = False
        vm = self._get_vms()[0]
        customization_section = [section for section in vm.get_Section()
                                 if (section.__class__.__name__ ==
                                     "GuestCustomizationSectionType")
                                 ][0]
        customization_section.set_Enabled(enabled)
        customization_section.set_AdminAutoLogonEnabled(False)
        customization_section.set_ResetPasswordRequired(False)
        if admin_password:
            customization_section.set_AdminPasswordEnabled(True)
            customization_section.set_AdminPassword(admin_password)
        if computer_name:
            customization_section.set_ComputerName(computer_name)
        if customization_script:
            customization_section.set_CustomizationScript(customization_script)
        body = self._create_request_body(
            customization_section,
            'GuestCustomizationSectionType',
            'xmlns="http://www.vmware.com/vcloud/v1.5" '
            'xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"')
        response = requests.put(customization_section.get_href(),
                                data=body,
                                headers=self.vapp.headers)
        if response.status_code == requests.codes.accepted:
            task = taskType.parseString(response.content, True)
            return True, task
        else:
            return False, response.content

    def customize_on_next_poweron(self):
        vm = self._get_vms()[0]
        link = filter(lambda link: link.get_rel() == "customizeAtNextPowerOn", vm.get_Link())
        if link:
            response = requests.post(link[0].get_href(), data=None,
                                     headers=self.vapp.headers)
            if response.status_code == requests.codes.no_content:
                return True
        return False

    def rename_vm(self, newname):
        vm = self._get_vms()[0]
        vm.set_name(newname)
        newvm = vAppType.VmType(name=newname)
        body = self._create_request_body(
            newvm, 'Vm',
            'xmlns="http://www.vmware.com/vcloud/v1.5" '
            'xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"')
        link = filter(lambda link: link.get_rel() == "reconfigureVm", vm.get_Link())
        response = requests.post(link[0].get_href(),
                                 data=body,
                                 headers=self.vapp.headers)
        if response.status_code == requests.codes.accepted:
            task = taskType.parseString(response.content, True)
            return True, task
        else:
            return False, response.content

    def add_network(self, network_name, fence_mode):
        vApp_NetworkConfigSection = [section for section in self.vapp.me.get_Section()
                                     if (section.__class__.__name__ ==
                                         "NetworkConfigSectionType")
                                     ][0]
        link = [link for link in vApp_NetworkConfigSection.get_Link()
                if (link.get_type() ==
                    "application/vnd.vmware.vcloud.networkConfigSection+xml")
                ][0]

        network_href = self._get_network_href(network_name)

        networkConfigSection = self.vapp.create_networkConfigSection(
            network_name, network_href, fence_mode)

        for networkConfig in vApp_NetworkConfigSection.get_NetworkConfig():
            if networkConfig.get_networkName().lower() == network_name.lower():
                return (False,
                        "VApp {0} is already connected to org vdc network {1}"
                        .format(self.vapp.name, network_name))
            networkConfigSection.add_NetworkConfig(networkConfig)

        body = CommonUtils.convertPythonObjToStr(
            networkConfigSection,
            name='NetworkConfigSection',
            namespacedef='xmlns="http://www.vmware.com/vcloud/v1.5"'
                         ' xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"'
        )
        body = body.replace('Info msgid=""', "ovf:Info")
        body = body.replace("/Info", "/ovf:Info")
        body = body.replace("vmw:", "")

        response = requests.put(link.get_href(), data=body, headers=self.vapp.headers)
        if response.status_code == requests.codes.accepted:
            task = taskType.parseString(response.content, True)
            return True, task
        else:
            return False, response.content

    def remove_network(self, network_name):
        networkConfigSection = [section for section in self.vapp.me.get_Section()
                                if (section.__class__.__name__ ==
                                    "NetworkConfigSectionType")][0]
        link = [link for link in networkConfigSection.get_Link()
                if (link.get_type() ==
                    "application/vnd.vmware.vcloud.networkConfigSection+xml")
                ][0]

        found = None
        for index, networkConfig in enumerate(networkConfigSection.get_NetworkConfig()):
            if networkConfig.get_networkName().lower() == network_name.lower():
                found = index
        if found is None:
            networkConfigSection.NetworkConfig.pop(found)

            body = CommonUtils.convertPythonObjToStr(
                networkConfigSection,
                name='NetworkConfigSection',
                namespacedef='xmlns="http://www.vmware.com/vcloud/v1.5" '
                'xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"')
            body = body.replace('Info xmlns:vmw="http://www.vmware.com/vcloud/v1.5" msgid=""', "ovf:Info")
            body = body.replace("/Info", "/ovf:Info")
            body = body.replace("vmw:", "")
            response = requests.put(link.get_href(), data=body, headers=self.vapp.headers)
            if response.status_code == requests.codes.accepted:
                task = taskType.parseString(response.content, True)
                return True, task
            else:
                return False, response.content
        else:
            return False, "Network {0} could not be found".format(network_name)

    def _create_request_body(self, obj, name, namespacedef):
        body = CommonUtils.convertPythonObjToStr(obj,
                                         name=name,
                                         namespacedef=namespacedef)
        body = body.replace("vmw:", "")
        body = body.replace('Info msgid=""', "ovf:Info")
        body = body.replace('Info xmlns:vmw="http://www.vmware.com/vcloud/'
                            'v1.5" msgid=""',
                            "ovf:Info")
        body = body.replace("/Info", "/ovf:Info")
        return body

    def _get_network_connection_data(self):
        vm = self._get_vms()[0]
        vm_nw_conn_section = [section for section in vm.get_Section()
                              if (section.__class__.__name__ ==
                                  "NetworkConnectionSectionType")][0]
        link = [link for link in vm_nw_conn_section.get_Link()
                if (link.get_type() ==
                    "application/vnd.vmware.vcloud.networkConnectionSection+xml")
                ][0]
        return vm_nw_conn_section, link

    def _get_network_href(self, network_name):
        network = self.vcd.get_network(self.vcloud_config['vdc'], network_name)
        if network:
            return network.get_href()


class MockCustomization:
    def __init__(self, headers, cpu=None, memory=None):
        self.headers = headers
        self.cpu = cpu
        self.memory = memory

    def __call__(self, name, template_href, deploy="true", power="true"):
        # template params that can be used as body of http request
        templateParams = vcloudType.InstantiateVAppTemplateParamsType()
        templateParams.set_name(name)
        templateParams.set_deploy(deploy)
        templateParams.set_powerOn(power)
        response = requests.get(template_href, headers=self.headers)
        if response.status_code == requests.codes.ok:
            vAppTemplate = ET.fromstring(response.content)
            for vm in vAppTemplate.iter('{http://www.vmware.com/vcloud/v1.5}Vm'):
                vm_href = vm.get('href')

        source = vcloudType.ReferenceType(href=vm_href)
        hardware = vcloudType.InstantiateVmHardwareCustomizationParamsType(NumberOfCpus=self.cpu,
                                                                           CoresPerSocket=self.cpu,
                                                                           MemorySize=self.memory)
        instantiation_params = vcloudType.SourcedVmInstantiationParamsType(Source=source,
                                                                           HardwareCustomization=hardware)
        templateParams.set_SourcedVmInstantiationParams([instantiation_params])
        # set source of the templateParams using href of the template
        source = vcloudType.ReferenceType(href=template_href)
        templateParams.set_Source(source)
        templateParams.set_AllEULAsAccepted("true")
        return templateParams
