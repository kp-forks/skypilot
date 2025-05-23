"""Lambda Cloud helper functions."""

import json
import os
import time
import typing
from typing import Any, Dict, List, Optional, Tuple

from sky.adaptors import common as adaptors_common
from sky.utils import common_utils

if typing.TYPE_CHECKING:
    import requests
else:
    requests = adaptors_common.LazyImport('requests')

CREDENTIALS_PATH = '~/.lambda_cloud/lambda_keys'
API_ENDPOINT = 'https://cloud.lambdalabs.com/api/v1'
INITIAL_BACKOFF_SECONDS = 10
MAX_BACKOFF_FACTOR = 10
MAX_ATTEMPTS = 6


class LambdaCloudError(Exception):
    pass


class Metadata:
    """Per-cluster metadata file."""

    def __init__(self, path_prefix: str, cluster_name: str) -> None:
        # TODO(ewzeng): Metadata file is not thread safe. This is fine for
        # now since SkyPilot uses a per-cluster lock for ray-related
        # operations. In the future, add a filelock around __getitem__,
        # __setitem__ and refresh.
        self.path = os.path.expanduser(f'{path_prefix}-{cluster_name}')
        # In case parent directory does not exist
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def get(self, instance_id: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return None
        with open(self.path, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        return metadata.get(instance_id)

    def set(self, instance_id: str, value: Optional[Dict[str, Any]]) -> None:
        # Read from metadata file
        if os.path.exists(self.path):
            with open(self.path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
        else:
            metadata = {}
        # Update metadata
        if value is None:
            if instance_id in metadata:
                metadata.pop(instance_id)  # del entry
            if not metadata:
                if os.path.exists(self.path):
                    os.remove(self.path)
                return
        else:
            metadata[instance_id] = value
        # Write to metadata file
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f)

    def refresh(self, instance_ids: List[str]) -> None:
        """Remove all tags for instances not in instance_ids."""
        if not os.path.exists(self.path):
            return
        with open(self.path, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        for instance_id in list(metadata.keys()):
            if instance_id not in instance_ids:
                del metadata[instance_id]
        if not metadata:
            os.remove(self.path)
            return
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f)


def raise_lambda_error(response: 'requests.Response') -> None:
    """Raise LambdaCloudError if appropriate."""
    status_code = response.status_code
    if status_code == 200:
        return
    if status_code == 429:
        # https://docs.lambdalabs.com/public-cloud/cloud-api/
        raise LambdaCloudError('Your API requests are being rate limited.')
    try:
        resp_json = response.json()
        code = resp_json.get('error', {}).get('code')
        message = resp_json.get('error', {}).get('message')
    except json.decoder.JSONDecodeError as e:
        raise LambdaCloudError(
            'Response cannot be parsed into JSON. Status '
            f'code: {status_code}; reason: {response.reason}; '
            f'content: {response.text}') from e
    raise LambdaCloudError(f'{code}: {message}')


def _try_request_with_backoff(method: str,
                              url: str,
                              headers: Dict[str, str],
                              data: Optional[str] = None):
    backoff = common_utils.Backoff(initial_backoff=INITIAL_BACKOFF_SECONDS,
                                   max_backoff_factor=MAX_BACKOFF_FACTOR)
    for i in range(MAX_ATTEMPTS):
        if method == 'get':
            response = requests.get(url, headers=headers)
        elif method == 'post':
            response = requests.post(url, headers=headers, data=data)
        elif method == 'put':
            response = requests.put(url, headers=headers, data=data)
        else:
            raise ValueError(f'Unsupported requests method: {method}')
        # If rate limited, wait and try again
        if response.status_code == 429 and i != MAX_ATTEMPTS - 1:
            time.sleep(backoff.current_backoff())
            continue
        if response.status_code == 200:
            return response
        raise_lambda_error(response)


class LambdaCloudClient:
    """Wrapper functions for Lambda Cloud API."""

    def __init__(self) -> None:
        self.credentials = os.path.expanduser(CREDENTIALS_PATH)
        assert os.path.exists(self.credentials), 'Credentials not found'
        with open(self.credentials, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f.readlines() if ' = ' in line]
            self._credentials = {
                line.split(' = ')[0]: line.split(' = ')[1] for line in lines
            }
        self.api_key = self._credentials['api_key']
        self.headers = {'Authorization': f'Bearer {self.api_key}'}

    def create_instances(
        self,
        instance_type: str = 'gpu_1x_a100_sxm4',
        region: str = 'us-east-1',
        quantity: int = 1,
        name: str = '',
        ssh_key_name: str = '',
    ) -> List[str]:
        """Launch new instances."""
        # Optimization:
        # Most API requests are rate limited at ~1 request every second but
        # launch requests are rate limited at ~1 request every 10 seconds.
        # So don't use launch requests to check availability.
        # See https://docs.lambdalabs.com/public-cloud/cloud-api/ for more.
        available_regions = (self.list_catalog()[instance_type]
                             ['regions_with_capacity_available'])
        available_regions = [reg['name'] for reg in available_regions]
        if region not in available_regions:
            if available_regions:
                aval_reg = ' '.join(available_regions)
            else:
                aval_reg = 'None'
            raise LambdaCloudError(('instance-operations/launch/'
                                    'insufficient-capacity: Not enough '
                                    'capacity to fulfill launch request. '
                                    'Regions with capacity available: '
                                    f'{aval_reg}'))

        # Try to launch instance
        data = json.dumps({
            'region_name': region,
            'instance_type_name': instance_type,
            'ssh_key_names': [ssh_key_name],
            'quantity': quantity,
            'name': name,
        })
        response = _try_request_with_backoff(
            'post',
            f'{API_ENDPOINT}/instance-operations/launch',
            data=data,
            headers=self.headers,
        )
        return response.json().get('data', []).get('instance_ids', [])

    def remove_instances(self, instance_ids: List[str]) -> Dict[str, Any]:
        """Terminate instances."""
        data = json.dumps({'instance_ids': instance_ids})
        response = _try_request_with_backoff(
            'post',
            f'{API_ENDPOINT}/instance-operations/terminate',
            data=data,
            headers=self.headers,
        )
        return response.json().get('data', []).get('terminated_instances', [])

    def list_instances(self) -> List[Dict[str, Any]]:
        """List existing instances."""
        response = _try_request_with_backoff('get',
                                             f'{API_ENDPOINT}/instances',
                                             headers=self.headers)
        return response.json().get('data', [])

    def list_ssh_keys(self) -> List[Dict[str, str]]:
        """List ssh keys."""
        response = _try_request_with_backoff('get',
                                             f'{API_ENDPOINT}/ssh-keys',
                                             headers=self.headers)
        return response.json().get('data', [])

    def get_unique_ssh_key_name(self, prefix: str,
                                pub_key: str) -> Tuple[str, bool]:
        """Returns a ssh key name with the given prefix.

        If no names have given prefix, return prefix. If pub_key exists and
        has name with given prefix, return that name. Otherwise create a
        UNIQUE name with given prefix.

        The second return value is True iff the returned name already exists.
        """
        candidate_keys = [
            k for k in self.list_ssh_keys()
            if k.get('name', '').startswith(prefix)
        ]

        # Prefix not found
        if not candidate_keys:
            return prefix, False

        suffix_digits = [0]
        for key_info in candidate_keys:
            name = key_info.get('name', '')
            if key_info.get('public_key', '').strip() == pub_key.strip():
                # Pub key already exists. Use strip to avoid whitespace diffs.
                return name, True
            if (len(name) > len(prefix) + 1 and name[len(prefix)] == '-' and
                    name[len(prefix) + 1:].isdigit()):
                suffix_digits.append(int(name[len(prefix) + 1:]))
        return f'{prefix}-{max(suffix_digits) + 1}', False

    def register_ssh_key(self, name: str, pub_key: str) -> None:
        """Register ssh key with Lambda."""
        data = json.dumps({'name': name, 'public_key': pub_key})
        _try_request_with_backoff('post',
                                  f'{API_ENDPOINT}/ssh-keys',
                                  data=data,
                                  headers=self.headers)

    def list_catalog(self) -> Dict[str, Any]:
        """List offered instances and their availability."""
        response = _try_request_with_backoff('get',
                                             f'{API_ENDPOINT}/instance-types',
                                             headers=self.headers)
        return response.json().get('data', {})

    def list_firewall_rules(self) -> List[Dict[str, Any]]:
        """List firewall rules."""
        response = _try_request_with_backoff('get',
                                             f'{API_ENDPOINT}/firewall-rules',
                                             headers=self.headers)
        return response.json().get('data', [])

    def create_firewall_rule(self,
                             port_range: List[int],
                             protocol: str = 'tcp',
                             description: str = '') -> Dict[str, Any]:
        """Create a firewall rule.

        Args:
            port_range: Port range as [min_port, max_port]. For a single port,
              use [port, port].
            protocol: Protocol ('tcp', 'udp', 'icmp', or 'all').
            description: Description for the rule.

        Returns:
            The created firewall rule.
        """
        # First, get all existing rules
        existing_rules = self.list_firewall_rules()

        # Convert existing rules to the format expected by the API
        rule_list = []
        for rule in existing_rules:
            if rule.get('protocol') and rule.get('source_network'):
                api_rule = {
                    'protocol': rule.get('protocol'),
                    'source_network': rule.get('source_network'),
                    'description': rule.get('description', '')
                }

                # Add port_range for non-icmp protocols
                if rule.get('protocol') != 'icmp' and rule.get('port_range'):
                    api_rule['port_range'] = rule.get('port_range')

                rule_list.append(api_rule)

        # Add our new rule
        new_rule: Dict[str, Any] = {
            'protocol': protocol,
            'source_network': '0.0.0.0/0',  # Allow from any IP address
            'description': description or
                           ('SkyPilot auto-generated rule for port '
                            f'{port_range[0]}-{port_range[1]}/{protocol}')
        }

        # Add port_range for non-icmp protocols
        if protocol != 'icmp':
            new_rule['port_range'] = port_range

        # Check if this rule already exists to avoid duplicates
        rule_exists = False
        for rule in rule_list:
            if (rule.get('protocol') == protocol and
                    rule.get('source_network') == '0.0.0.0/0'):
                if protocol != 'icmp':
                    if rule.get('port_range') == port_range:
                        rule_exists = True
                        break
                else:
                    rule_exists = True
                    break

        # Only add the rule if it doesn't already exist
        if not rule_exists:
            rule_list.append(new_rule)

        # Create a data structure that matches the API schema
        data = json.dumps({'data': rule_list})

        response = _try_request_with_backoff(
            'put',  # Using PUT instead of POST as per API documentation
            f'{API_ENDPOINT}/firewall-rules',
            data=data,
            headers=self.headers,
        )
        return response.json().get('data', {})
