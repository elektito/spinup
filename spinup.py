#!/usr/bin/env python3

import os
import sys
import subprocess
import uuid
import yaml
import libvirt
import xml.etree.ElementTree as ET
from tempfile import mkdtemp

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
    </spinup:instance>
  </metadata>
  <os>
    <type>hvm</type>
  </os>
  <memory unit='MiB'>1024</memory>

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
    <interface type='bridge'>
      <source bridge='virbr0'/>
      <model type='virtio'/>
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
    if os_type == 'linux' and os_variant == 'ubuntu':
        return os.path.join(BASE_IMAGE_DIR, 'xenial-server-cloudimg-amd64-disk1.img')

def create_disk_image(base_image, machine):
    print('Creating disk image...')
    image_filename = os.path.join(BASE_IMAGE_DIR, machine['id'] + '-disk.img')
    run_cmd('qemu-img create -f qcow2 -b {base_image} {image_filename}'.format(
        base_image=base_image,
        image_filename=image_filename))
    return image_filename

def create_vm(conn, path, machine):
    machine['id'] = path.replace('/', '-') + '-' + str(uuid.uuid4())
    machine['id'] = machine['id'][1:]
    machine['config_drive'] = create_cloud_config_drive(machine)
    base_image = get_image(machine['os_type'], machine['os_variant'])
    machine['disk_image'] = create_disk_image(base_image, machine)

    xml = xml_template.format(
        path=path,
        config_drive=machine['config_drive'],
        disk_image=machine['disk_image'])

    print('Defining VM...')
    domain = conn.defineXML(xml)

    print('Launching VM...')
    domain.create()

def main():
    uri = 'qemu:///system'

    cwd = os.path.abspath(os.curdir)

    print('Connecting to libvirt at {}...'.format(uri))
    conn = libvirt.open(uri)

    machine = {
        'instance_id': 'foo',
        'hostname': 'foo',
        'os_type': 'linux',
        'os_variant': 'ubuntu'
    }

    for domain_id in conn.listDomainsID():
        domain = conn.lookupByID(domain_id)
        metadata = domain.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                                   'http://spinup.io/instance')
        if metadata:
            tree = ET.fromstring(metadata)
            path = tree.find('./path').text
            if path == cwd:
                print('VM already exists.')
                return

    create_vm(conn, cwd, machine)

if __name__ == '__main__':
    main()
