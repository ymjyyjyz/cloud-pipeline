# Copyright 2017-2019 EPAM Systems, Inc. (https://www.epam.com/)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import functools
import os
import uuid
from random import randint

from azure.common.client_factory import get_client_from_auth_file
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from cloudprovider import AbstractInstanceProvider

import utils


def azure_resource_type_cmp(r1, r2):
    if str(r1.type).split('/')[-1] == "virtualMachines":
        return -1
    elif str(r1.type).split('/')[-1] == "networkInterfaces" and str(r2.type).split('/')[-1] != "virtualMachines":
        return -1
    return 0


class AzureInstanceProvider(AbstractInstanceProvider):

    def __init__(self, zone):
        self.zone = zone
        self.resource_client = get_client_from_auth_file(ResourceManagementClient)
        self.network_client = get_client_from_auth_file(NetworkManagementClient)
        self.compute_client = get_client_from_auth_file(ComputeManagementClient)
        self.resource_group_name = os.environ["AZURE_RESOURCE_GROUP"]

    def run_instance(self, is_spot, bid_price, ins_type, ins_hdd, ins_img, ins_key, run_id, kms_encyr_key_id,
                     num_rep, time_rep, kube_ip, kubeadm_token):
        try:
            ins_key = utils.read_ssh_key(ins_key)
            user_data_script = utils.get_user_data_script(self.zone, ins_type, ins_img, kube_ip, kubeadm_token)
            instance_name = "az-" + uuid.uuid4().hex[0:16]
            self.__create_public_ip_address(instance_name, run_id)
            self.__create_nic(instance_name, run_id)
            return self.__create_vm(instance_name, run_id, ins_type, ins_img, ins_hdd, user_data_script,
                                  ins_key, "pipeline", kms_encyr_key_id)
        except Exception as e:
            self.__delete_all_by_run_id(run_id)
            raise RuntimeError(e)

    def verify_run_id(self, run_id):
        utils.pipe_log('Checking if instance already exists for RunID {}'.format(run_id))
        vm_name = None
        private_ip = None
        for resource in self.resource_client.resources.list(filter="tagName eq 'Name' and tagValue eq '" + run_id + "'"):
            if str(resource.type).split('/')[-1] == "virtualMachines":
                vm_name = resource.name

        if vm_name is not None:
            private_ip = self.network_client.network_interfaces \
                .get(self.resource_group_name, vm_name + '-nic').ip_configurations[0].private_ip_address

        return vm_name, private_ip

    def get_instance_names(self, ins_id):
        public_ip = self.network_client.public_ip_addresses.get(
            self.resource_group_name,
            ins_id + '-ip'
        )
        nodename_full = public_ip.dns_settings.fqdn
        nodename = nodename_full.split('.', 1)[0]
        return nodename_full, nodename

    def find_instance(self, run_id):
        for resource in self.resource_client.resources.list(filter="tagName eq 'Name' and tagValue eq '" + run_id + "'"):
            if str(resource.type).split('/')[-1] == "virtualMachines":
                return resource.name

        return None

    def check_instance(self, ins_id, run_id, num_rep, time_rep):
        pass

    def terminate_instance(self, ins_id):
        instance = self.compute_client.virtual_machines.get(self.resource_group_name, ins_id)
        if 'Name' in instance.tags:
            self.__delete_all_by_run_id(instance.tags['Name'])
        else:
            self.compute_client.virtual_machines.delete(self.resource_group_name, ins_id).wait()

    def terminate_instance_by_ip(self, node_internal_ip):
        nics = self.network_client.network_interfaces.list(resource_group_name=self.resource_group_name)
        for nic in nics:
            if nic.ip_configurations[0].private_ip_address == node_internal_ip:
                if nic.tags and 'Name' in nic.tags:
                    self.__delete_all_by_run_id(nic.tags['Name'])
                else:
                    raise RuntimeError("Cannot find instance with internal ip: {}".format(node_internal_ip))

    def find_and_tag_instance(self, old_id, new_id):
        ins_id = None
        for resource in self.resource_client.resources.list(
                filter="tagName eq 'Name' and tagValue eq '" + old_id + "'"):
            resource_group = resource.id.split('/')[4]
            resource_type = str(resource.type).split('/')[-1]
            if resource_type == "virtualMachines":
                ins_id = resource.name
                resource = self.compute_client.virtual_machines.get(resource_group, resource.name)
                resource.tags["Name"] = new_id
                self.compute_client.virtual_machines.create_or_update(resource_group, resource.name, resource)
            elif resource_type == "networkInterfaces":
                resource = self.network_client.network_interfaces.get(resource_group, resource.name)
                resource.tags["Name"] = new_id
                self.network_client.network_interfaces.create_or_update(resource_group, resource.name, resource)
            elif resource_type == "publicIPAddresses":
                resource = self.network_client.public_ip_addresses.get(resource_group, resource.name)
                resource.tags["Name"] = new_id
                self.network_client.public_ip_addresses.create_or_update(resource_group, resource.name, resource)
            elif resource_type == "disks":
                resource = self.compute_client.disks.get(resource_group, resource.name)
                resource.tags["Name"] = new_id
                self.compute_client.disks.create_or_update(resource_group, resource.name, resource)

        if ins_id is not None:
            return ins_id
        else:
            raise RuntimeError("Failed to find instance {}".format(old_id))

    def __create_public_ip_address(self, instance_name, run_id):
        public_ip_addess_params = {
            'location': self.zone,
            'public_ip_allocation_method': 'Dynamic',
            'dns_settings': {
                'domain_name_label': instance_name
            },
            'tags': AzureInstanceProvider.get_tags(run_id)
        }
        creation_result = self.network_client.public_ip_addresses.create_or_update(
            self.resource_group_name,
            instance_name + '-ip',
            public_ip_addess_params
        )

        return creation_result.result()

    def __create_nic(self, instance_name, run_id):
        allowed_networks = utils.get_networks_config(self.zone)
        security_groups = utils.get_security_groups(self.zone)
        if allowed_networks and len(allowed_networks) > 0:
            az_num = randint(0, len(allowed_networks) - 1)
            az_name = allowed_networks.items()[az_num][0]
            subnet = AzureInstanceProvider.get_subnet_name_from_id(allowed_networks.items()[az_num][1])
            resource_group, network = AzureInstanceProvider.get_res_grp_and_res_name_from_string(az_name, 'virtualNetworks')
            utils.pipe_log('- Networks list found, subnet {} in AZ {} will be used'.format(subnet, az_name))
        else:
            utils.pipe_log('- Networks list NOT found, trying to find network from region in the same resource group...')
            resource_group, network, subnet = self.get_any_network_from_location(self.zone)
            utils.pipe_log('- Network found, subnet {} in VNET {} will be used'.format(subnet, network))

        if not resource_group or not network or not subnet:
            raise RuntimeError("No networks with subnet found for location: {} in resourceGroup: {}".format(self.zone, self.resource_group_name))

        subnet_info = self.network_client.subnets.get(resource_group, network, subnet)

        public_ip_address = self.network_client.public_ip_addresses.get(
            self.resource_group_name,
            instance_name + '-ip'
        )

        if len(security_groups) != 1:
            raise AssertionError("Please specify only one security group as: <resource_group>/<security_group_name>")

        resource_group, secur_grp = AzureInstanceProvider.get_res_grp_and_res_name_from_string(security_groups[0], 'networkSecurityGroups')
        security_group_info = self.network_client.network_security_groups.get(resource_group, secur_grp)

        nic_params = {
            'location': self.zone,
            'ipConfigurations': [{
                'name': 'IPConfig',
                'publicIpAddress': public_ip_address,
                'subnet': {
                    'id': subnet_info.id
                }
            }],
            "networkSecurityGroup": {
                'id': security_group_info.id
            },
            'tags': AzureInstanceProvider.get_tags(run_id)
        }
        creation_result = self.network_client.network_interfaces.create_or_update(
            self.resource_group_name,
            instance_name + '-nic',
            nic_params
        )

        return creation_result.result()

    def __get_disk_type(self, instance_type):
        disk_type = None
        for sku in self.compute_client.resource_skus.list():
            if sku.locations[0].lower() == self.zone.lower() and sku.resource_type.lower() == "virtualmachines" \
                    and sku.name.lower() == instance_type.lower():
                for capability in sku.capabilities:
                    if capability.name.lower() == "premiumio":
                        disk_type = "Premium_LRS" if capability.value.lower() == "true" else "StandardSSD_LRS"
                        break
        return disk_type

    def __create_vm(self, instance_name, run_id, instance_type, instance_image, disk, user_data_script,
                  ssh_pub_key, user, kms_encyr_key_id):
        nic = self.network_client.network_interfaces.get(
            self.resource_group_name,
            instance_name + '-nic'
        )
        resource_group, image_name = AzureInstanceProvider.get_res_grp_and_res_name_from_string(instance_image, 'images')

        image = self.compute_client.images.get(
            resource_group,
            image_name
        )

        vm_parameters = {
            'location': self.zone,
            'os_profile': {
                'computer_name': instance_name,
                'admin_username': user,
                "linuxConfiguration": {
                    "ssh": {
                        "publicKeys": [
                            {
                                "path": "/home/" + user + "/.ssh/authorized_keys",
                                "key_data": "{key} {user}".format(key=ssh_pub_key, user=user)
                            }
                        ]
                    },
                    "disablePasswordAuthentication": True,
                },
                "custom_data": base64.b64encode(user_data_script)
            },
            'hardware_profile': {
                'vm_size': instance_type
            },
            'storage_profile': {
                'image_reference': {
                    'id': image.id
                },
                "osDisk": {
                    "caching": "ReadWrite",
                    "managedDisk": {
                        "storageAccountType": self.__get_disk_type(instance_type)
                    },
                    "name": instance_name + "-os_disk",
                    "createOption": "FromImage"
                },
                "dataDisks": [
                    {
                        "name": instance_name + "-data",
                        "diskSizeGB": disk,
                        "lun": 63,
                        "createOption": "Empty",
                        "managedDisk": {
                            "storageAccountType": self.__get_disk_type(instance_type)
                        }
                    }
                ]
            },
            'network_profile': {
                'network_interfaces': [{
                    'id': nic.id
                }]
            },
            'tags': AzureInstanceProvider.get_tags(run_id)
        }

        if kms_encyr_key_id:
            vault_id_key_url_secret = kms_encyr_key_id.split(";")
            vm_parameters["storage_profile"]["dataDisks"][0]["encryptionSettings"] = {
                "diskEncryptionKey": {
                    "sourceVault": {
                        "id": vault_id_key_url_secret[0]
                    },
                    "secretUrl": vault_id_key_url_secret[2]
                },
                "enabled": True,
                "keyEncryptionKey": {
                    "sourceVault": {
                        "id": vault_id_key_url_secret[0]
                    },
                    "keyUrl": vault_id_key_url_secret[1]
                }
            }

        creation_result = self.compute_client.virtual_machines.create_or_update(
            self.resource_group_name,
            instance_name,
            vm_parameters
        )
        creation_result.result()

        start_result = self.compute_client.virtual_machines.start(self.resource_group_name, instance_name)
        start_result.wait()

        private_ip = self.network_client.network_interfaces \
            .get(self.resource_group_name, instance_name + '-nic').ip_configurations[0].private_ip_address

        return instance_name, private_ip

    def __resolve_azure_api(self, resource):
        """ This method retrieves the latest non-preview api version for
        the given resource (unless the preview version is the only available
        api version) """
        provider = self.resource_client.providers.get(resource.id.split('/')[6])
        rt = next((t for t in provider.resource_types
                   if t.resource_type == '/'.join(resource.type.split('/')[1:])), None)
        if rt and 'api_versions' in rt.__dict__:
            api_version = [v for v in rt.__dict__['api_versions'] if 'preview' not in v.lower()]
            return api_version[0] if api_version else rt.__dict__['api_versions'][0]

    def __delete_all_by_run_id(self, run_id):
        resources = []
        resources.extend(self.resource_client.resources.list(filter="tagName eq 'Name' and tagValue eq '" + run_id + "'"))
        # we need to sort resources to be sure that vm and nic will be deleted first,
        # because it has attached resorces(disks and ip)
        resources.sort(key=functools.cmp_to_key(azure_resource_type_cmp))
        vm_name = resources[0].name if str(resources[0].type).split('/')[-1] == 'virtualMachines' else resources[0].name[0:19]
        self.__detach_disks_and_nic(vm_name)
        for resource in resources:
            self.resource_client.resources.delete(
                resource_group_name=resource.id.split('/')[4],
                resource_provider_namespace=resource.id.split('/')[6],
                parent_resource_path='',
                resource_type=str(resource.type).split('/')[-1],
                resource_name=resource.name,
                api_version=self.__resolve_azure_api(resource),
                parameters=resource
            ).wait()

    def __detach_disks_and_nic(self, vm_name):
        self.compute_client.virtual_machines.delete(self.resource_group_name, vm_name).wait()
        try:
            nic = self.network_client.network_interfaces.get(self.resource_group_name, vm_name + '-nic')
            nic.ip_configurations[0].public_ip_address = None
            self.network_client.network_interfaces.create_or_update(self.resource_group_name, vm_name + '-nic', nic).wait()
        except Exception as e:
            print e

    def get_any_network_from_location(self, location):
        resource_group, network, subnet = None, None, None

        for vnet in self.resource_client.resources.list(filter="resourceType eq 'Microsoft.Network/virtualNetworks' "
                                                          "and location eq '{}' "
                                                          "and resourceGroup eq '{}'".format(location, self.resource_group_name)):
            resource_group, network = AzureInstanceProvider.get_res_grp_and_res_name_from_string(vnet.id, 'virtualNetworks')
            break

        if not resource_group or not network:
            return resource_group, network, subnet

        for subnet_res in self.network_client.subnets.list(resource_group, network):
            subnet = AzureInstanceProvider.get_subnet_name_from_id(subnet_res.id)
            break
        return resource_group, network, subnet

    @staticmethod
    def get_subnet_name_from_id(subnet_id):
        if "/" not in subnet_id:
            return subnet_id
        subnet_params = subnet_id.split("/")
        # according to /subscriptions/<sub>/resourceGroups/<res_grp>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<subnet>
        if len(subnet_params) == 11 and subnet_params[3] == "resourceGroups" and subnet_params[9] == "subnets":
            return subnet_params[10]
        else:
            raise RuntimeError("Subnet dont match form of the Azure ID "
                               "/subscriptions/<sub>/resourceGroups/<res_grp>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<subnet>: {}".format(subnet_id))

    @staticmethod
    def get_res_grp_and_res_name_from_string(resource_id, resource_type):
        resource_params = resource_id.split("/")
        if len(resource_params) == 2:
            resource_group, resource = resource_params[0], resource_params[1]
        # according to full ID form: /subscriptions/<sub-id>/resourceGroups/<res-grp>/providers/Microsoft.Compute/images/<image>
        elif len(resource_params) == 9 and resource_params[3] == 'resourceGroups' and resource_params[7] == resource_type:
            resource_group, resource = resource_params[4], resource_params[8]
        else:
            raise RuntimeError(
                "Resource parameter doesn't match to Azure resource name convention: <resource_group>/<resource_name>"
                " or full resource id: /subscriptions/<sub-id>/resourceGroups/<res-grp>/providers/Microsoft.Compute/<type>/<name>. "
                "Node Up process will be stopped.")
        return resource_group, resource

    @staticmethod
    def resource_tags():
        tags = {}
        _, config_tags = utils.load_cloud_config()
        if config_tags is None:
            return tags
        for key, value in config_tags.iteritems():
            tags.update({key: value})
        return tags

    @staticmethod
    def run_id_tag(run_id):
        return {
            'Name': run_id,
        }

    @staticmethod
    def get_tags(run_id):
        tags = AzureInstanceProvider.run_id_tag(run_id)
        res_tags = AzureInstanceProvider.resource_tags()
        if res_tags:
            tags.update(res_tags)
        return tags