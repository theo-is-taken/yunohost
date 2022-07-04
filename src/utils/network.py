# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2017 YUNOHOST.ORG

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program; if not, see http://www.gnu.org/licenses

"""
import os
import re
import logging
import time

from moulinette.utils.filesystem import read_file, write_to_file
from moulinette.utils.network import download_text
from moulinette.utils.process import check_output

from yunohost.settings import settings_get

logger = logging.getLogger("yunohost.utils.network")
    
def is_ip_local(ip):
    """Returns True if the provided Ip is local"""
    filters = ["192.168", "172.16.", "10."]
    for filter in filters:
        if ip.startswith(filter):
            return True


def get_public_ip(protocol=4):

    assert protocol in [4, 6], (
        f"Invalid protocol version for get_public_ip: {protocol}, expected 4 or 6"
    )

    cache_file = f"/var/cache/yunohost/ipv{protocol}"
    cache_duration = 120  # 2 min
    if (
        os.path.exists(cache_file)
        and abs(os.path.getctime(cache_file) - time.time()) < cache_duration
    ):
        ip = read_file(cache_file).strip()
        ip = ip if ip else None  # Empty file (empty string) means there's no IP
        logger.debug(f"Reusing IPv{protocol} from cache: {ip}")
    else:
        ip = get_public_ip_from_remote_server(protocol)
        logger.debug(f"IP fetched: {protocol}")
        write_to_file(cache_file, ip or "")
    return ip


def get_public_ip_from_remote_server(protocol=4):
    """Retrieve the public IP address from ip.yunohost.org or another Server"""

    # We can know that ipv6 is not available directly if this file does not exists
    if protocol == 6 and not os.path.exists("/proc/net/if_inet6"):
        logger.debug(
            "IPv6 appears not at all available on the system, so assuming there's no IP address for that version"
        )
        return None

    # If we are indeed connected in ipv4 or ipv6, we should find a default route
    routes = check_output(f"ip -{protocol} route show table all").split("\n")

    def is_default_route(r):
        # Typically the default route starts with "default"
        # But of course IPv6 is more complex ... e.g. on internet cube there's
        # no default route but a /3 which acts as a default-like route...
        # e.g. 2000:/3 dev tun0 ...
        return r.startswith("default") or (
            ":" in r and re.match(r".*/[0-3]$", r.split()[0])
        )

    if not any(is_default_route(r) for r in routes):
        logger.debug(
            f"No default route for IPv{protocol}, so assuming there's no IP address for that version"
        )
        return None

    # Check URLS
    ip_list = get_public_ips(protocol)
    if len(ip_list)>0:
        return ip_list[0]

    return None

def get_public_ips(protocol=4):
    """Retrieve a list (sorted by frequency) of public IP addresses from the IPmirrors. 
    We request the IP on several IPmirrors to avoid resilience issues and some attacks.
    In a classic way, those IPs are the same on the same protocol. However, in some cases 
    those public IPs could be different (attacks, several IPs on the server).
    
    Note: this function doesn't guarantee to return all public IPs in use by the server.
    """

    ip_url_yunohost_tab = settings_get("security.ipmirrors.v"+str(protocol)).split(",")
    ip_count = {} # Count the number of times an IP has appeared

    # Check URLS
    for url in ip_url_yunohost_tab[:3]:
        logger.debug(f"Fetching IP from {url}")
        try:
            ip = download_text(url, timeout=15).strip()
            if ip in ip_count.keys():
                ip_count[ip]+=1
            else:
                ip_count[ip]=1
        except Exception as e:
            logger.debug(
                f"Could not get public IPv{protocol} from {url} : {e}"
            )

    ip_list_with_count = [ (ip,ip_count[ip]) for ip in ip_count ]
    ip_list_with_count.sort(key=lambda x: x[1]) # Sort by frequency
    return [ x[0] for x in ip_list_with_count ]


def get_network_interfaces():

    # Get network devices and their addresses (raw infos from 'ip addr')
    devices_raw = {}
    output = check_output("ip addr show")
    for d in re.split(r"^(?:[0-9]+: )", output, flags=re.MULTILINE):
        # Extract device name (1) and its addresses (2)
        m = re.match(r"([^\s@]+)(?:@[\S]+)?: (.*)", d, flags=re.DOTALL)
        if m:
            devices_raw[m.group(1)] = m.group(2)

    # Parse relevant informations for each of them
    devices = {
        name: _extract_inet(addrs)
        for name, addrs in devices_raw.items()
        if name != "lo"
    }

    return devices


def get_gateway():

    output = check_output("ip route show")
    m = re.search(r"default via (.*) dev ([a-z]+[0-9]?)", output)
    if not m:
        return None

    addr = _extract_inet(m.group(1), True)
    return addr.popitem()[1] if len(addr) == 1 else None


def _extract_inet(string, skip_netmask=False, skip_loopback=True):
    """
    Extract IP addresses (v4 and/or v6) from a string limited to one
    address by protocol

    Keyword argument:
        string -- String to search in
        skip_netmask -- True to skip subnet mask extraction
        skip_loopback -- False to include addresses reserved for the
            loopback interface

    Returns:
        A dict of {protocol: address} with protocol one of 'ipv4' or 'ipv6'

    """
    ip4_pattern = (
        r"((25[0-5]|2[0-4]\d|[0-1]?\d?\d)(\.(25[0-5]|2[0-4]\d|[0-1]?\d?\d)){3}"
    )
    ip6_pattern = r"(((?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?)::((?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?)"
    ip4_pattern += r"/[0-9]{1,2})" if not skip_netmask else ")"
    ip6_pattern += r"/[0-9]{1,3})" if not skip_netmask else ")"
    result = {}

    for m in re.finditer(ip4_pattern, string):
        addr = m.group(1)
        if skip_loopback and addr.startswith("127."):
            continue

        # Limit to only one result
        result["ipv4"] = addr
        break

    for m in re.finditer(ip6_pattern, string):
        addr = m.group(1)
        if skip_loopback and addr == "::1":
            continue

        # Limit to only one result
        result["ipv6"] = addr
        break

    return result
