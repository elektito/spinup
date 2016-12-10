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
from tqdm import tqdm

BASE_IMAGE_DIR = '/var/lib/spinup/images'

xml_template = '''
<domain type='kvm'>
  <name>foo</name>
  <uuid>a1e08189-8d43-495a-85de-079b14781239</uuid>
  <title>some title</title>
  <description>some description</description>
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

def create_cloud_config_drive(machine):
    print('Creating config drive...')
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
        'ssh_authorized_keys': [
            'ssh-rsa {public_key} ubuntu@{hostname}'.format(
                public_key=public_key,
                hostname=machine['hostname'])
        ]
    }

    user_data = '#cloud-config\n\n' + yaml.dump(user_data)

    config_iso = os.path.join(BASE_IMAGE_DIR, machine['id'] + '-config.iso')
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
    run_cmd('genisoimage -o {} -V cidata -r -J {} {}'.format(
        config_iso, user_data_file, meta_data_file))

    os.unlink(meta_data_file)
    os.unlink(user_data_file)
    os.rmdir(tmpdir)

    return os.path.abspath(config_iso)

def get_image(os_type, os_variant):
    if os_type == 'linux':
        if os_variant == 'ubuntu':
            filename = 'ubuntu-16.04-server-cloudimg-amd64-disk1.img'
            url = 'https://cloud-images.ubuntu.com/releases/releases/16.04/release/ubuntu-16.04-server-cloudimg-amd64-disk1.img'
        elif os_variant == 'centos':
            filename = 'CentOS-7-x86_64-GenericCloud-1503.qcow2'
            url = 'http://cloud.centos.org/centos/7/images/CentOS-7-x86_64-GenericCloud-1503.qcow2.xz'

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
            print('Error decompressing image:', err.decode() if err else out)
            exit(1)

    return path

def create_disk_image(base_image, machine):
    print('Creating disk image...')
    image_filename = os.path.join(BASE_IMAGE_DIR, machine['id'] + '-disk.img')
    code, out, err = run_cmd('qemu-img create -f qcow2 -b {base_image} {image_filename}'.format(
        base_image=base_image,
        image_filename=image_filename))
    if code != 0:
        print('Error creating image:', err.decode() if err else out.decode())
        exit(1)
    return image_filename

def find_dhcp_lease(conn, mac):
    for lease in conn.networkLookupByName('default').DHCPLeases():
        if lease['mac'] == mac:
            return lease

def create_vm(conn, domain, path, machine, args):
    if domain:
        print('VM already exists.')
        return

    process_create_args(args, machine)

    machine['id'] = path.replace('/', '-') + '-' + str(uuid.uuid4())
    machine['id'] = machine['id'][1:]
    base_image = get_image(machine['os_type'], machine['os_variant'])
    machine['disk_image'] = create_disk_image(base_image, machine)
    machine['config_drive'] = create_cloud_config_drive(machine)

    pickled_machine = base64.b64encode(pickle.dumps(machine)).decode()

    xml = xml_template.format(
        path=path,
        pickled_machine=pickled_machine,
        **machine)

    print('Defining VM...')
    domain = conn.defineXML(xml)

    print('Launching VM...')
    domain.create()

    print('Waiting for a DHCP lease to appear...')
    xml = domain.XMLDesc()
    tree = ET.fromstring(xml)
    mac = tree.find('./devices/interface[@type="network"]/mac').attrib['address']

    lease = find_dhcp_lease(conn, mac)
    while not lease:
        lease = find_dhcp_lease(conn, mac)
    ip = lease['ipaddr']
    print('Machine IP address:', ip)

    print('Waiting for SSH port to open...')
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

    print('VM created successfully.')

def ssh_vm(conn, domain, directory, machine, args):
    xml = domain.XMLDesc()
    tree = ET.fromstring(xml)
    mac = tree.find('./devices/interface[@type="network"]/mac').attrib['address']
    lease = find_dhcp_lease(conn, mac)
    ip = lease['ipaddr']

    cmd = 'ssh -o StrictHostKeyChecking=no '
    cmd += '-o UserKnownHostsFile=/dev/null '
    cmd += '-o LogLevel=QUIET '
    cmd += 'ubuntu@{}'.format(ip)

    subprocess.call(cmd.split(' '))

def destroy_vm(conn, domain, directory, machine, args):
    xml = domain.XMLDesc()
    tree = ET.fromstring(xml)
    disk_file = tree.find('./devices/disk[@device="disk"]/source').attrib['file']
    config_drive_file = tree.find('./devices/disk[@device="cdrom"]/source').attrib['file']

    print('Destroying VM...')
    domain.destroy()

    print('Undefining VM...')
    domain.undefine()

    print('Removing disk images...')
    os.unlink(disk_file)
    os.unlink(config_drive_file)

    print('VM destroyed.')

cmd_to_func = {
    'create': create_vm,
    'ssh': ssh_vm,
    'destroy': destroy_vm,
}

def process_mem_arg(arg, match, machine):
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
        print('Too little memory:', arg)
        exit(1)

    machine['memory'] = int(value / 2**20)

def process_cpu_arg(arg, match, machine):
    value = int(match.group('value'))

    if value == 0:
        print('Can\'t have zero CPUs.')
        exit(1)

    machine['cpus'] = value

def process_os_arg(arg, match, machine):
    machine['os_variant'] = match.group('variant')

create_arg_processors = [
    ('(?P<value>\\d+)(?P<unit>[KMGT])', process_mem_arg),
    ('(?P<value>\\d+)cpus', process_cpu_arg),
    ('(?P<variant>ubuntu|centos)', process_os_arg),
]

def process_create_args(args, machine):
    global create_arg_processors

    arg_processors = [(re.compile(regex), processor)
                      for regex, processor in create_arg_processors]

    for arg in args:
        for regex, update_func in arg_processors:
            match = regex.fullmatch(arg)
            if match:
                update_func(arg, match, machine)
                break
        else:
            print('Invalid argument:', arg)
            exit(1)

def process_args():
    args = sys.argv
    if len(args) > 1 and args[1] in cmd_to_func:
        cmd = args[1]
        args = args[2:]
    else:
        cmd = 'create'
        args = args[1:]

    return cmd, args

def main():
    uri = 'qemu:///system'

    cwd = os.path.abspath(os.curdir)

    print('Connecting to libvirt at {}...'.format(uri))
    conn = libvirt.open(uri)

    machine = {
        'instance_id': 'foo',
        'hostname': 'foo',
        'os_type': 'linux',
        'os_variant': 'ubuntu',
        'memory': 1024,
        'cpus': 1,
    }

    for domain_id in conn.listDomainsID():
        domain = conn.lookupByID(domain_id)
        metadata = domain.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                                   'http://spinup.io/instance')
        if metadata:
            tree = ET.fromstring(metadata)
            path = tree.find('./path').text
            if path == cwd:
                pickled_machine = tree.find('./pickled-machine').text
                machine = pickle.loads(base64.b64decode(pickled_machine))
                break
    else:
        domain = None

    cmd, args = process_args()
    cmd_to_func[cmd](conn, domain, cwd, machine, args)

if __name__ == '__main__':
    main()
