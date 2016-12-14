#!/usr/bin/env python3

import os
import sys
import subprocess
import uuid
import socket
import time
import re
import base64
import pickle
import yaml
import libvirt
import requests
import yarl
import xml.etree.ElementTree as ET
from tempfile import mkdtemp
from collections import Counter
from multiprocessing import Pool, Queue, Process
from tqdm import tqdm

LIBVIRT_URI = 'qemu:///system'

BASE_IMAGE_DIR = '/var/lib/spinup/images'

xml_template = '''
<domain type='kvm'>
  <name>{cluster_id}-{name}</name>
  <uuid>{uuid}</uuid>
  <title>{name}</title>
  <description>{description}</description>
  <metadata>
    <spinup:instance xmlns:spinup='http://spinup.io/instance'>
      <spinup:path>{path}</spinup:path>
      <spinup:pickled-machine>{pickled_machine}</spinup:pickled-machine>
    </spinup:instance>
  </metadata>
  <os>
    <type>hvm</type>
  </os>
  <memory unit='MiB'>{memory}</memory>
  <vcpu>{cpus}</vcpu>

  <features>
    <acpi/>
    <apic/>
    <pae/>
  </features>

  <devices>
    <os>
      <type>hvm</type>
    </os>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{disk_image}'/>
      <backingStore/>
      <target dev='vda' bus='virtio'/>
      <alias name='virtio-disk0'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x03' function='0x0'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{config_drive}'/>
      <backingStore/>
      <target dev='hda' bus='ide'/>
      <readonly/>
      <alias name='ide0-0-0'/>
      <address type='drive' controller='0' bus='0' target='0' unit='0'/>
    </disk>
    <serial type='pty'>
      <source path='/dev/pts/9'/>
      <target port='0'/>
      <alias name='serial0'/>
    </serial>
    <console type='pty' tty='/dev/pts/9'>
      <source path='/dev/pts/9'/>
      <target type='serial' port='0'/>
      <alias name='serial0'/>
    </console>
    <interface type='network'>
      <source network='default'/>
    </interface>
  </devices>

</domain>
'''

def run_cmd(cmd):
    if isinstance(cmd, str):
        cmd = cmd.split(' ')
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=False)
    out, err = proc.communicate()
    return proc.returncode, out, err

def get_default_username(machine):
    return {
        'ubuntu': 'ubuntu',
        'centos': 'centos',
        'coreos': 'core',
    }[machine['os_variant']]

def create_cloud_config_drive(machine):
    print('{}: Creating config drive...'.format(machine['name']))
    with open(os.path.expanduser('~/.ssh/id_rsa.pub')) as f:
        public_key = f.read()

    public_key = public_key.strip()
    public_key = public_key.strip()[public_key.index(' ') + 1:]
    public_key = public_key.strip()[:public_key.index(' ')]

    meta_data = {
        'instance-id': machine['instance_id'],
        'local-hostname': machine['hostname']
    }

    meta_data = yaml.dump(meta_data)

    user_data = {
        'hostname': machine['hostname'],

        'ssh_authorized_keys': [
            'ssh-rsa {public_key} {username}@{hostname}'.format(
                username=get_default_username(machine),
                public_key=public_key,
                hostname=machine['hostname'])
        ],

        'manage_etc_hosts': 'localhost',
    }

    user_data = '#cloud-config\n\n' + yaml.dump(user_data)

    config_iso = os.path.join(BASE_IMAGE_DIR, machine['instance_id'] + '-config.iso')
    tmpdir = mkdtemp()
    meta_data_file = os.path.join(tmpdir, 'meta-data')
    user_data_file = os.path.join(tmpdir, 'user-data')

    with open(meta_data_file, 'w') as f:
        f.write(meta_data)
        meta_data_file = f.name

    with open(user_data_file, 'w') as f:
        f.write(user_data)
        user_data_file = f.name

    try:
        os.unlink(config_iso)
    except FileNotFoundError:
        pass

    if machine['os_variant'] == 'coreos':
        cmd = 'genisoimage -o {} -V config-2 -r -graft-points ' \
              '-J openstack/latest/user_data={} {}'.format(
                  config_iso, user_data_file, meta_data_file)
    else:
        cmd = 'genisoimage -o {} -V cidata -r -J {} {}'.format(
            config_iso, user_data_file, meta_data_file)
    code, out, err = run_cmd(cmd)
    if code != 0:
        raise RuntimeError('Error creating ISO image: ' + \
                           err.decode() if err else out.decode())

    os.unlink(meta_data_file)
    os.unlink(user_data_file)
    os.rmdir(tmpdir)

    return os.path.abspath(config_iso)

image_fetch_request_queue = Queue()
image_fetch_result_queue = Queue()
def fetch_image():
    while True:
        os_type, os_variant = image_fetch_request_queue.get()
        if os_type == None:
            break

        if os_type == 'linux':
            if os_variant == 'ubuntu':
                filename = 'ubuntu-16.04-server-cloudimg-amd64-disk1.img'
                url = 'https://cloud-images.ubuntu.com/releases/releases/16.04/release/ubuntu-16.04-server-cloudimg-amd64-disk1.img'
            elif os_variant == 'centos':
                filename = 'CentOS-7-x86_64-GenericCloud-1503.qcow2'
                url = 'http://cloud.centos.org/centos/7/images/CentOS-7-x86_64-GenericCloud-1503.qcow2.xz'
            elif os_variant == 'coreos':
                filename = 'coreos_production_qemu_image.img'
                url = 'https://alpha.release.core-os.net/amd64-usr/current/coreos_production_qemu_image.img.bz2'

        path = os.path.join(BASE_IMAGE_DIR, filename)
        if not os.path.exists(path):
            _, target_filename = os.path.split(yarl.URL(url).path)
            target = os.path.join(BASE_IMAGE_DIR, target_filename)
            response = requests.get(url, stream=True)
            total = int(response.headers.get('Content-Length'))
            with open(target, 'wb') as f:
                chunk_size = 2**20
                for data in tqdm(response.iter_content(chunk_size),
                                 total=total/chunk_size, unit='MiB',
                                 desc='Downloading disk image'):
                    f.write(data)

            code = 0
            if target_filename.endswith('.xz'):
                print('Decompressing image...')
                code, out, err = run_cmd('unxz {}'.format(target))
            elif target_filename.endswith('.gz'):
                print('Decompressing image...')
                code, out, err = run_cmd('gunzip {}'.format(target))
            elif target_filename.endswith('.bz2'):
                print('Decompressing image...')
                code, out, err = run_cmd('bunzip2 {}'.format(target))

            if code != 0:
                raise RuntimeError('Error decompressing image:' + \
                                   err.decode() if err else out)

        image_fetch_result_queue.put(path)

def get_image(os_type, os_variant):
    image_fetch_request_queue.put((os_type, os_variant))
    return image_fetch_result_queue.get()

def create_disk_image(base_image, machine):
    print('{}: Creating disk image...'.format(machine['name']))
    image_filename = os.path.join(BASE_IMAGE_DIR, machine['instance_id'] + '-disk.img')
    code, out, err = run_cmd('qemu-img create -f qcow2 -b {base_image} {image_filename}'.format(
        base_image=base_image,
        image_filename=image_filename))
    if code != 0:
        raise RuntimeError('Error creating image: ' + \
                           err.decode() if err else out.decode())
    return image_filename

def find_dhcp_lease(conn, mac):
    for lease in conn.networkLookupByName('default').DHCPLeases():
        if lease['mac'] == mac:
            return lease

def process_mem_descriptor(desc, match, machine):
    value = int(match.group('value'))
    unit = match.group('unit')

    if unit:
        mult = {
            'K': 2**10,
            'M': 2**20,
            'G': 2**30,
            'T': 2**40,
        }[unit]
        value *= mult

    if value < 2**20:
        raise RuntimeError('Too little memory: ' + desc)

    machine['memory'] = int(value / 2**20)

def process_cpu_descriptor(desc, match, machine):
    value = int(match.group('value'))

    if value == 0:
        raise RuntimeError('Can\'t have zero CPUs.')

    machine['cpus'] = value

def process_os_descriptor(desc, match, machine):
    machine['os_variant'] = match.group('variant')

def process_name_descriptor(desc, match, machine):
    machine['name'] = match.group('name')

descriptor_processors = [
    ('(?P<value>\\d+)(?P<unit>[KMGT])', process_mem_descriptor),
    ('(?P<value>\\d+)cpus?', process_cpu_descriptor),
    ('(?P<variant>ubuntu|centos|coreos)', process_os_descriptor),
    (':(?P<name>\\w+)', process_name_descriptor),
]

def get_machine(index, path, descriptors):
    global descriptor_processors

    # start with a default machine
    uuid4 = str(uuid.uuid4())
    path = path[:-1] if path.endswith('/') else path
    directory_name = os.path.split(path)[1]
    cluster_id = directory_name + '-' + uuid4
    name = 'machine-' + str(index)
    machine = {
        'uuid': uuid4,
        'name': name,
        'cluster_id': cluster_id,
        'instance_id': cluster_id + '-' + str(index),
        'description': '',
        'os_type': 'linux',
        'os_variant': 'ubuntu',
        'memory': 1024,
        'cpus': 1,
    }

    # compile regular expressions
    processors = [(re.compile(regex), processor)
                  for regex, processor in descriptor_processors]

    for desc in descriptors:
        for regex, update_func in processors:
            match = regex.fullmatch(desc)
            if match:
                update_func(desc, match, machine)
                break
        else:
            raise RuntimeError('Invalid descriptor: ' + desc)

    return machine

def create_single_vm(arg):
    path, machine = arg
    conn = libvirt.open(LIBVIRT_URI)
    base_image = get_image(machine['os_type'], machine['os_variant'])
    machine['disk_image'] = create_disk_image(base_image, machine)
    machine['config_drive'] = create_cloud_config_drive(machine)

    pickled_machine = base64.b64encode(pickle.dumps(machine)).decode()

    xml = xml_template.format(
        path=path,
        pickled_machine=pickled_machine,
        **machine)

    print('{}: Defining VM...'.format(machine['name']))
    domain = conn.defineXML(xml)

    print('{}: Launching VM...'.format(machine['name']))
    domain.create()

    print('{}: Waiting for a DHCP lease to appear...'.format(machine['name']))
    xml = domain.XMLDesc()
    tree = ET.fromstring(xml)
    mac = tree.find('./devices/interface[@type="network"]/mac').attrib['address']

    lease = find_dhcp_lease(conn, mac)
    while not lease:
        lease = find_dhcp_lease(conn, mac)
        time.sleep(0.1)
    ip = lease['ipaddr']
    print('{}: Machine IP address: {}'.format(machine['name'], ip))

    print('{}: Waiting for SSH port to open...'.format(machine['name']))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    while True:
        try:
            sock.connect((ip, 22))
        except ConnectionRefusedError:
            pass
        else:
            sock.close()
            break
        time.sleep(0.1)

    print('{}: VM created successfully.'.format(machine['name']))

def split_list(list, sep):
    parts = []
    part = []
    for e in list:
        if e == sep:
            parts.append(part)
            part = []
        else:
            part.append(e)
    parts.append(part)
    return parts

def create_vm(conn, path, args):
    descriptions = split_list(args, '--')
    machines = [get_machine(i + 1, path, desc)
                for i, desc in enumerate(descriptions)]
    duplicates = [item for item, count
                  in Counter([m['name'] for m in machines]).items()
                  if count > 1]
    if duplicates:
        raise RuntimeError('Duplicate names: ' + ', '.join(duplicates))

    # set machine hostnames
    for machine in machines:
        machine['hostname'] = machine['name']

    cluster = get_current_cluster(conn, path)
    if cluster:
        raise RuntimeError('A cluster is already running in this directory.')

    pool = Pool(len(machines) + 1)
    pool.apply_async(fetch_image, ())
    pool.map(create_single_vm, [(path, m) for m in machines])

    image_fetch_request_queue.put((None, None))

def ssh_vm(conn, path, args):
    cluster = get_current_cluster(conn, path)
    if not cluster:
        raise RuntimeError('No cluster found in this directory.')

    if len(cluster) == 1:
        domain, machine = cluster[0]
    else:
        if len(args) == 0:
            raise RuntimeError('More than one machine in the cluster. '
                               'Specify a machine name.')
        elif len(args) > 1:
            raise RuntimeError('You can only specify one machine name.')

        name = args[0]
        for domain, machine in cluster:
            if machine['name'] == name:
                break
        else:
            raise RuntimeError('No such machine in the cluster.')

    xml = domain.XMLDesc()
    tree = ET.fromstring(xml)
    mac = tree.find('./devices/interface[@type="network"]/mac').attrib['address']
    lease = find_dhcp_lease(conn, mac)
    ip = lease['ipaddr']

    username = get_default_username(machine)

    cmd = 'ssh -o StrictHostKeyChecking=no '
    cmd += '-o UserKnownHostsFile=/dev/null '
    cmd += '-o LogLevel=QUIET '
    cmd += '{}@{}'.format(username, ip)

    subprocess.call(cmd.split(' '))

def destroy_single_vm(arg):
    domain_name, machine = arg

    conn = libvirt.open(LIBVIRT_URI)
    domain = conn.lookupByName(domain_name)

    xml = domain.XMLDesc()
    tree = ET.fromstring(xml)
    disk_file = tree.find('./devices/disk[@device="disk"]/source').attrib['file']
    config_drive_file = tree.find('./devices/disk[@device="cdrom"]/source').attrib['file']

    print('{}: Destroying VM...'.format(machine['name']))
    try:
        domain.destroy()
    except libvirt.libvirtError:
        # this happens if the domain is shut-off.
        pass

    print('{}: Undefining VM...'.format(machine['name']))
    domain.undefine()

    print('{}: Removing disk images...'.format(machine['name']))
    os.unlink(disk_file)
    os.unlink(config_drive_file)

    print('{}: VM destroyed.'.format(machine['name']))

def destroy_vm(conn, path, args):
    cluster = get_current_cluster(conn, path)
    if not cluster:
        raise RuntimeError('No cluster found in this directory.')

    pool = Pool(len(cluster))
    pool.map(destroy_single_vm, [(d.name(), m) for d, m in cluster])

def shutdown_vm(conn, path, args):
    cluster = get_current_cluster(conn, path)
    if not cluster:
        raise RuntimeError('No cluster found in this directory.')

    if len(args) > 0:
        name = args[0]
        cluster = [(d, m) for d, m in cluster if m['name'] == name]
        if len(cluster) == 0:
            raise RuntimeError('No machine named "{}" found.'.format(name))

    for domain, machine in cluster:
        print('{}: Shutting VM down...'.format(machine['name']))
        try:
            domain.shutdown()
        except libvirt.libvirtError:
            # the domain is already shut off.
            pass

    while all(d.state()[0] != libvirt.VIR_DOMAIN_SHUTOFF for d, _ in cluster):
        time.sleep(0.1)

def start_vm(conn, path, args):
    cluster = get_current_cluster(conn, path)
    if not cluster:
        raise RuntimeError('No cluster found in this directory.')

    if len(args) > 0:
        name = args[0]
        cluster = [(d, m) for d, m in cluster if m['name'] == name]
        if len(cluster) == 0:
            raise RuntimeError('No machine named "{}" found.'.format(name))

    for domain, machine in cluster:
        print('{}: Starting VM...'.format(machine['name']))
        domain.create()

    while all(d.state()[0] != libvirt.VIR_DOMAIN_RUNNING for d, _ in cluster):
        time.sleep(0.1)

def status_vm(conn, path, args):
    cluster = get_current_cluster(conn, path)
    if not cluster:
        raise RuntimeError('No cluster found in this directory.')

    if len(args) > 0:
        name = args[0]
        cluster = [(d, m) for d, m in cluster if m['name'] == name]
        if len(cluster) == 0:
            raise RuntimeError('No machine named "{}" found.'.format(name))

    # this corresponds to enum virDomainState
    state_names = [
        'so state',
        'running',
        'blocked',
        'paused',
        'shutdown',
        'shutoff',
        'crashed',
        'pm-suspended',
    ]

    for domain, machine in cluster:
        print('{}: {}'.format(machine['name'], state_names[domain.state()[0]]))

cmd_to_func = {
    'create': create_vm,
    'ssh': ssh_vm,
    'destroy': destroy_vm,
    'shutdown': shutdown_vm,
    'start': start_vm,
    'status': status_vm,
}

def process_args():
    args = sys.argv
    if len(args) > 1 and args[1] in cmd_to_func:
        cmd = args[1]
        args = args[2:]
    else:
        cmd = 'create'
        args = args[1:]

    return cmd, args

def get_current_cluster(conn, source_dir):
    results = []
    for domain in conn.listAllDomains():
        machine = None
        metadata = domain.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                                   'http://spinup.io/instance')
        if metadata:
            tree = ET.fromstring(metadata)
            path = tree.find('./path').text
            if path == source_dir:
                pickled_machine = tree.find('./pickled-machine').text
                machine = pickle.loads(base64.b64decode(pickled_machine))

        results.append((domain, machine))

    return results

def main():
    cwd = os.path.abspath(os.curdir)

    print('Connecting to libvirt at {}...'.format(LIBVIRT_URI))
    conn = libvirt.open(LIBVIRT_URI)

    cmd, args = process_args()

    try:
        cmd_to_func[cmd](conn, cwd, args)
    except RuntimeError as e:
        print(e)
        exit(1)

    exit(0)

if __name__ == '__main__':
    main()
